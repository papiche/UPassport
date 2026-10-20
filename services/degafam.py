"""
services/degafam.py — « Se dégafamiser » pas à pas : l'état réel du compte et la série d'emails qui en découle.

Le goulot du parcours viral n'est pas l'invitation, c'est ce qui suit : l'invité a son MULTIPASS et ses premières
photos, mais ses propres photos, contacts et agenda dorment encore chez Google ou Apple. Quatre étapes, dans l'ordre :

  photos    brancher le téléphone sur son cloud (WebDAV) pour y copier ses photos
  contacts  raccorder son carnet d'adresses (CardDAV)
  agenda    raccorder son agenda (CalDAV)
  amis      rendre à ses proches leurs photos (la boucle virale)

Principe : on ne relance JAMAIS ce qui est déjà fait. Chaque étape est « cochée d'après l'état réel » (comme le
bandeau « Bien démarrer » de ucloud.html) — la MÊME fonction `state()` alimente l'assistant web (degafam.html) et
l'email du jour. Un email au plus tous les `MIN_GAP_DAYS` jours, un seul par étape, `MAX_MAILS` au total, jamais
avant `FIRST_AFTER_HOURS` (le code PASS est déjà arrivé), jamais pour un compte de plus de `MAX_AGE_DAYS` jours
(on n'inonde pas les anciens comptes), jamais si le membre a coupé le flux « onboarding » (Mon compte).

Après les quatre étapes viennent deux découvertes, une fois chacune : Coracle (le réseau) et Zelkova (le portefeuille
Ẑen). Les autres applications ne sont pas prêtes : aucun message à leur sujet.

Cette série REMPLACE le welcome d'origine et les zines J1→J6 (NOSTRCARD.refresh.sh). `ucloud_onboarding.py <email>
--preview DIR` écrit les messages en HTML pour relecture sans rien envoyer.

Aucun email ne contient d'identifiant ni de jeton : le guide web (connecté par la clé du membre) les affiche.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import settings
from services import cloud_storage

logger = logging.getLogger(__name__)

CHANNEL = "onboarding"                       # flux de notification (Mon compte) et canal de mailjet.sh
STEP_ORDER = ("photos", "contacts", "agenda", "amis")
DISCOVERY = ("zelkova", "coracle")           # présentées une fois chacune, sans état à cocher
ALL_STEPS = STEP_ORDER + DISCOVERY
PHOTOS_DONE = 20                              # photos PROPRES (hors reçues) pour cocher « photos »
MIN_GAP_DAYS = 3
MAX_MAILS = 6
FIRST_AFTER_HOURS = 12
MAX_AGE_DAYS = 60
DAY = 86400


def _nostr(email: str) -> Path:
    return settings.GAME_PATH / "nostr" / email


def _udir(email: str) -> Path:
    return cloud_storage.ucloud_dir(email)


# ── état réel ─────────────────────────────────────────────────────────────────
def _count_contacts(email: str) -> int:
    try:
        from services import contacts_store
        return len((contacts_store.load(email) or {}).get("cards") or {})
    except Exception as exc:                  # noqa: BLE001 — l'état ne doit jamais casser un envoi ni la page
        logger.warning("degafam: contacts de %s illisibles (%s)", email, exc)
        return 0


def _count_events(email: str) -> int:
    try:
        from services import calendar_store
        return len((calendar_store.load(email) or {}).get("items") or {})
    except Exception as exc:                  # noqa: BLE001
        logger.warning("degafam: agenda de %s illisible (%s)", email, exc)
        return 0


def _shared_with_friends(email: str) -> bool:
    inv = _nostr(email) / ".face_invites"
    try:
        if inv.is_dir() and any(inv.glob("*.json")):
            return True
        given = json.loads((_udir(email) / "face_ref_given.json").read_text(encoding="utf-8"))
        return bool(given)
    except (OSError, ValueError):
        return False


def state(email: str) -> Dict[str, Any]:
    """{étape: {done, count, started}} + `dav_enrolled`. `started` = le chemin est ouvert mais pas terminé."""
    udir = _udir(email)
    own = 0
    try:
        own = int(cloud_storage.ucloud_db.analysis_counts(udir)["images"])
    except Exception as exc:                  # noqa: BLE001
        logger.warning("degafam: photos de %s illisibles (%s)", email, exc)
    enrolled = bool(cloud_storage.load_dav_token(email))
    contacts, events = _count_contacts(email), _count_events(email)
    sources = udir / "webdav_sources.json"
    return {
        "dav_enrolled": enrolled,
        "photos": {"done": own >= PHOTOS_DONE, "count": own,
                   "started": own > 0 or enrolled or (sources.is_file() and sources.stat().st_size > 2)},
        "contacts": {"done": contacts >= 1, "count": contacts, "started": enrolled},
        "agenda": {"done": events >= 1, "count": events, "started": enrolled},
        "amis": {"done": _shared_with_friends(email), "count": 0, "started": own > 0},
    }


# ── avancement de la série ────────────────────────────────────────────────────
def _progress_path(email: str) -> Path:
    return _udir(email) / "degafam.json"


def load_progress(email: str) -> Dict[str, Any]:
    try:
        return json.loads(_progress_path(email).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sent": {}, "last": 0, "count": 0}


def _save_progress(email: str, data: Dict[str, Any]) -> None:
    p = _progress_path(email)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(p)


def _optout(email: str) -> Optional[str]:
    """Raison pour laquelle on n'écrit PAS à ce membre, ou None. Même lecture que tools/mailjet.sh (qui, lui,
    sort en succès sans rien envoyer : un envoi « annulé » ne doit pas compter comme envoyé)."""
    try:
        prefs = json.loads((_nostr(email) / ".mailjet").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if any(c in ("email", "all") for c in (prefs.get("channels") or [])):
        return "désabonné de tous les emails"
    if prefs.get("flux", {}).get(CHANNEL) is False:
        return "flux « guide pas à pas » coupé dans Mon compte"
    if (prefs.get("flux_channels") or {}).get(CHANNEL, {}).get("email") is False:
        return "canal email coupé pour le guide pas à pas"
    return None


def _account_age(email: str) -> Optional[float]:
    try:
        return time.time() - (_nostr(email) / "HEX").stat().st_mtime
    except OSError:
        return None


def decide(email: str, now: Optional[float] = None, st: Optional[dict] = None) -> Tuple[Optional[str], str]:
    """(étape à envoyer | None, raison). Pure lecture : n'écrit rien, n'envoie rien."""
    now = now or time.time()
    prog = load_progress(email)
    if prog.get("finished"):
        return None, "série terminée"
    if (why := _optout(email)):
        return None, why
    age = _account_age(email)
    if age is None:
        return None, "compte introuvable"
    if age < FIRST_AFTER_HOURS * 3600:
        return None, f"compte trop récent ({age / 3600:.0f} h) : le code PASS vient d'arriver"
    if age > MAX_AGE_DAYS * DAY:
        return None, f"compte de plus de {MAX_AGE_DAYS} jours : pas de série pour les anciens comptes"
    if int(prog.get("count", 0)) >= MAX_MAILS:
        return None, f"{MAX_MAILS} messages déjà envoyés"
    if prog.get("last") and now - float(prog["last"]) < MIN_GAP_DAYS * DAY:
        return None, f"dernier message il y a moins de {MIN_GAP_DAYS} jours"
    st = st or state(email)
    for key in STEP_ORDER:
        if not st[key]["done"] and key not in (prog.get("sent") or {}):
            return key, f"étape « {key} » pas faite et pas encore proposée"
    for key in DISCOVERY:
        if key not in (prog.get("sent") or {}):
            return key, f"découverte « {key} » pas encore présentée"
    return None, "tous les messages utiles ont été envoyés"


# ── les messages ──────────────────────────────────────────────────────────────
def base_url() -> str:
    return str(settings.uSPOT).rstrip("/")


def coracle_url() -> str:
    return (os.environ.get("myCORACLE") or "https://coracle.copylaradio.com").rstrip("/")


def _first_name(email: str) -> str:
    local = re.split(r"[.+_-]", email.split("@", 1)[0])[0]
    return local.capitalize() if re.fullmatch(r"[A-Za-zÀ-ÿ]{2,20}", local) else ""


def layout(heading: str, intro: str, steps: List[str], cta_label: str, cta_url: str, ps: str, preheader: str = "",
           unsubscribe: bool = True) -> str:
    """Mise en page commune à tous les messages FaceCloud : un titre, une phrase, au plus 3 gestes, UN bouton."""
    e = html.escape
    lis = "".join(
        f'<tr><td style="width:34px;vertical-align:top"><div style="width:26px;height:26px;border-radius:50%;'
        f'background:#dcc196;color:#5c4219;font-weight:800;text-align:center;line-height:26px">{i}</div></td>'
        f'<td style="padding:2px 0 12px">{s}</td></tr>' for i, s in enumerate(steps, 1))
    unsub = (f'<p style="color:#8a949a;font-size:12px;margin:18px 0 0">Vous recevez ce guide parce que vous avez ouvert '
             f'un MULTIPASS. <a href="{e(base_url())}/earth/moncompte.html#notifications" style="color:#8a949a">'
             f'Ne plus le recevoir</a>.</p>') if unsubscribe else ""
    return (
        '<div style="background:#f6f3ec;padding:20px 12px"><div style="max-width:520px;margin:0 auto;background:#fff;'
        'border-radius:14px;padding:24px;font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#1d2a32">'
        f'<span style="display:none;max-height:0;overflow:hidden">{e(preheader)}</span>'
        f'<h2 style="font-size:22px;line-height:1.25;margin:0 0 12px">{e(heading)}</h2>'
        f'<p style="margin:0 0 16px">{intro}</p>'
        f'<table role="presentation" style="width:100%;border-collapse:collapse;margin:0 0 8px">{lis}</table>'
        f'<p style="margin:12px 0 18px"><a href="{e(cta_url)}" style="display:block;background:#1f5fd1;color:#fff;'
        f'padding:14px 18px;border-radius:10px;text-decoration:none;font-weight:700;text-align:center">{e(cta_label)}</a></p>'
        f'<p style="color:#5a6a73;font-size:14px;margin:0">{ps}</p>{unsub}</div></div>')


def compose(step: str, email: str, st: Optional[dict] = None) -> Tuple[str, str]:
    """(sujet, HTML) du message de `step` pour `email`, avec ses vrais chiffres."""
    st = st or state(email)
    hi = _first_name(email)
    hello = f"Bonjour {html.escape(hi)}, " if hi else ""
    guide = f"{base_url()}/earth/degafam.html"
    n_photos, n_contacts = st["photos"]["count"], st["contacts"]["count"]
    if step == "photos":
        return ("📷 Étape 1/3 — Vos photos, chez vous, en 3 minutes", layout(
            "Et si vos photos dormaient chez vous ?",
            f"{hello}vos photos vivent aujourd'hui chez Google ou Apple. Ce soir, elles peuvent aussi dormir "
            "dans votre cloud chiffré — <b>une copie</b> : rien n'est supprimé chez eux tant que vous n'êtes pas rassuré·e.",
            ["<b>Ouvrez le guide</b> (bouton ci-dessous) : il affiche vos identifiants, prêts à copier.",
             "<b>Choisissez Android ou iPhone</b> : une application envoie vos photos toute seule, la nuit, en Wi-Fi.",
             "<b>Regardez la première photo arriver</b> : le guide se met à jour en direct."],
            "Brancher mon téléphone", f"{guide}#photos",
            "Ensuite FaceCloud reconnaît vos proches et peut leur rendre <b>leurs</b> photos, en un geste.",
            preheader="Une copie chiffrée de vos photos, en 3 minutes. Rien n'est supprimé chez Google."))
    if step == "contacts":
        got = f"Vos photos arrivent déjà ({n_photos}). " if n_photos else ""
        return ("📇 Étape 2/3 — Vos contacts, enfin chez vous", layout(
            "Votre carnet d'adresses vous appartient",
            f"{hello}{got}Reste le carnet d'adresses : celui que Google connaît mieux que vous. "
            "On le raccorde à votre cloud, et vos contacts restent synchronisés sur votre téléphone.",
            ["<b>Ouvrez le guide</b> : même identifiant que pour vos photos, rien à retaper.",
             "<b>Android</b> : l'application DAVx⁵. <b>iPhone</b> : Réglages → Contacts → Comptes → CardDAV.",
             "<b>Déjà un carnet ?</b> Exportez-le en .vcf depuis Google ou Apple et importez-le d'un clic."],
            "Raccorder mes contacts", f"{guide}#contacts",
            "Bonus : FaceCloud relie vos contacts à leurs visages — « envoyer à Maman ses photos » devient un clic.",
            preheader="Même identifiant que vos photos. 3 minutes."))
    if step == "agenda":
        got = f"Photos ({n_photos}) et contacts ({n_contacts}) sont chez vous. " if n_photos and n_contacts else ""
        return ("📅 Étape 3/3 — Votre agenda quitte Google", layout(
            "Dernier morceau : votre agenda",
            f"{hello}{got}Votre agenda est la dernière chose qui vous retient chez eux. "
            "Une fois raccordé en CalDAV, vos rendez-vous s'affichent sur votre téléphone comme avant, mais ils sont chiffrés chez vous.",
            ["<b>Ouvrez le guide</b> : vos identifiants y sont déjà.",
             "<b>Android</b> : DAVx⁵ (le même compte que les contacts). <b>iPhone</b> : Réglages → Calendrier → Comptes → CalDAV.",
             "<b>Gardez votre ancien agenda</b> en lecture quelques semaines, le temps de vous habituer."],
            "Raccorder mon agenda", f"{guide}#agenda",
            "Vos événements sont aussi disponibles dans « Mes calendriers », chiffrés quand ils viennent du téléphone.",
            preheader="Vos rendez-vous, chiffrés chez vous."))
    if step == "amis":
        return ("💌 Pour finir — rendez leurs photos à vos proches", layout(
            "Maintenant, le plus agréable",
            f"{hello}vous avez {n_photos or 'des'} photos chez vous. FaceCloud sait reconnaître les personnes dessus : "
            "nommez un visage, et envoyez à chacun toutes les photos où il apparaît — en un clic, par WhatsApp, SMS ou mail.",
            ["<b>Ouvrez FaceCloud</b> : les visages sont rangés par personne.",
             "<b>Nommez une personne</b> que vous aimez.",
             "<b>Envoyez-lui ses photos</b> : elle reçoit un lien, ses photos l'attendent, et son cloud est offert."],
            "Rendre ses photos à un proche", f"{base_url()}/earth/ucloud.html",
            "C'est la meilleure façon de leur faire quitter les GAFAM : en leur offrant quelque chose.",
            preheader="Un clic, et quelqu'un que vous aimez retrouve ses photos."))
    if step == "coracle":
        return ("🌐 Coracle — votre porte d'entrée sur le réseau", layout(
            "Votre identité numérique est prête",
            f"{hello}votre MULTIPASS vous a généré une clef secrète : votre signature numérique souveraine — aucun serveur "
            "ne peut vous la retirer, aucune entreprise ne peut fermer votre compte. "
            "<b>Coracle</b> est le client du réseau : un espace de publication décentralisé, sans algorithme ni publicité.",
            ["<b>Ouvrez Coracle</b> (bouton ci-dessous).",
             "<b>Connectez-vous</b> avec votre MULTIPASS : « Login », puis « Clef privée ».",
             "<b>Votre profil apparaît</b> : vous êtes sur le réseau."],
            "Ouvrir Coracle", coracle_url(),
            "💡 Sur ce réseau, chaque like (♥) que vous recevez vaut 1 Ẑen crédité sur votre portefeuille : "
            "un vrai paiement, traité automatiquement.",
            preheader="Votre porte d'entrée sur le réseau, sans algorithme ni publicité."))
    if step == "zelkova":
        return ("💎 Zelkova — votre portefeuille Ẑen", layout(
            "Comprendre le Ẑen, et le garder sur vous",
            f"{hello}le Ẑen est l'unité de compte coopérative d'UPlanet : pas une crypto spéculative, un outil de "
            "coordination, adossé à la monnaie libre Ğ1. <b>Zelkova</b> est le portefeuille mobile pour le suivre.",
            ["<b>Ouvrez Zelkova</b> (bouton ci-dessous).",
             "<b>Récupérez votre MULTIPASS</b> avec votre email et votre code PASS.",
             "<b>Consultez votre solde</b> et votre historique, scannez un QR de paiement."],
            "Ouvrir Zelkova", "https://z.astroport.one",
            "1 Ẑen = 1 € sur UPlanet Ẑen (production), 0,1 Ğ1 sur UPlanet ORIGIN (phase alpha). "
            "Vous en gagnez en recevant des likes sur Coracle.",
            preheader="Le Ẑen, expliqué simplement — et votre portefeuille mobile."))
    raise ValueError(f"étape inconnue : {step}")


# ── envoi ─────────────────────────────────────────────────────────────────────
def _send(email: str, subject: str, body: str) -> bool:
    script = settings.TOOLS_PATH / "mailjet.sh"
    if not script.exists():
        logger.error("degafam: mailjet.sh introuvable (%s)", script)
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(body)
        path = f.name
    try:
        proc = subprocess.run([str(script), "--channel", CHANNEL, "--no-ipfs-link", email, path, subject],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            logger.error("degafam: mailjet.sh a échoué pour %s (code %s) : %s", email, proc.returncode, proc.stderr[-300:])
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("degafam: envoi à %s impossible (%s)", email, exc)
        return False
    finally:
        Path(path).unlink(missing_ok=True)


def run(email: str, dry_run: bool = False, force_step: str = "") -> Dict[str, Any]:
    """Un passage quotidien pour `email`. Rend {"status": sent|skip|dry-run|error, "step", "why"}."""
    st = state(email)
    step, why = (force_step, "forcé") if force_step else decide(email, st=st)
    prog = load_progress(email)
    if step is None:
        if not force_step and all(k in (prog.get("sent") or {}) or st[k]["done"] for k in STEP_ORDER) \
                and all(k in (prog.get("sent") or {}) for k in DISCOVERY) and not prog.get("finished"):
            prog["finished"] = int(time.time())
            if not dry_run:
                _save_progress(email, prog)
        return {"status": "skip", "why": why}
    subject, body = compose(step, email, st)
    if dry_run:
        return {"status": "dry-run", "step": step, "why": why, "subject": subject}
    if not _send(email, subject, body):
        return {"status": "error", "step": step, "why": "envoi échoué — repris demain"}
    prog.setdefault("sent", {})[step] = int(time.time())
    prog["last"] = int(time.time())
    prog["count"] = int(prog.get("count", 0)) + 1
    _save_progress(email, prog)
    logger.info("degafam: %s → étape %s envoyée (%d/%d)", email, step, prog["count"], MAX_MAILS)
    return {"status": "sent", "step": step, "why": why, "subject": subject}
