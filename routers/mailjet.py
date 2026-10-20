"""
Route /mailjet — Gestion des opt-outs notifications UPlanet.
Auth NIP-42 (Schnorr BIP-340 pur Python) + détection roaming MULTIPASS.
Écrit ~/.zen/game/nostr/$email/.mailjet (JSON) comme marqueur.
Vérifié par Astroport.ONE/tools/mailjet.sh avant tout envoi.
"""
import asyncio
import base64
import hashlib
import html
import hmac
import io
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from core.config import settings
from services import degafam, facecloud_funnel
from services.memory_status import (
    get_memory_status, reset_memory, RESET_SCOPES, regenerate_lifeos_from_mastodon,
    get_identity_content, save_identity_file, IDENTITY_FILENAMES, IDENTITY_LABELS,
    IDENTITY_PLACEHOLDERS,
)
from services.roaming import resolve_home_http_url
from utils.crypto import verify_nostr_event as _verify_nostr_event
from utils.crypto import _pt_mul, _SECP256K1_G as _G  # ECDH auto-chiffrement NIP-04 ci-dessous
from utils.security import safe_json_body
# Code PASS (récupération de compte via /g1nostr) — mêmes helpers que l'admin,
# jamais dupliqués (cf. routers/identity.py).
from routers.identity import _resolve_pass_file, _pass_disabled_flag, _is_pass_restore_disabled

templates = Jinja2Templates(directory="templates")

router = APIRouter()
logger = logging.getLogger(__name__)

# ─── BRO Omni-Channel : scrapers cookie + surveillance (bro_watch_core.py) ───
_IA_PATH = Path.home() / ".zen" / "Astroport.ONE" / "IA"
sys.path.insert(0, str(_IA_PATH))
import bro_watch_core  # noqa: E402

# ─── Challenges NIP-42 (mémoire locale, TTL 5 min) ────────────────────────────
_challenges: dict[str, float] = {}  # challenge_hex → expiry_timestamp

# La vérification Schnorr BIP-340 (_verify_nostr_event) vit désormais dans
# utils/crypto.py — réutilisée aussi par routers/identity.py::/atom4love/*.


# ─── NIP-04 self-encryption (mailjet prefs → NOSTR) ──────────────────────────

_NOSTR_SEND = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "nostr_send_note.py"
_ASTRO_VENV = settings.ZEN_PATH / "Astroport.ONE" / ".venv" / "bin" / "python3"


def _parse_secret_nostr(user_dir: Path) -> Optional[bytes]:
    """Retourne la clé privée (32 bytes) depuis .secret.nostr, ou None."""
    f = user_dir / ".secret.nostr"
    if not f.exists():
        return None
    try:
        content = f.read_text().strip()
        m = re.search(r"NSEC=([^;]+)", content)
        if not m:
            return None
        import bech32
        hrp, data = bech32.bech32_decode(m.group(1).strip())
        if hrp != "nsec" or data is None:
            return None
        return bytes(bech32.convertbits(data, 5, 8, False))
    except Exception:
        return None


def _nip04_encrypt_to_self(plaintext: str, privkey_bytes: bytes) -> Optional[str]:
    """NIP-04 auto-chiffrement : chiffre pour soi-même (ECDH privkey×pubkey, AES-256-CBC)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        from cryptography.hazmat.backends import default_backend

        priv_int = int.from_bytes(privkey_bytes, "big")
        pub_point = _pt_mul(_G, priv_int)
        if pub_point is None:
            return None
        # ECDH to self: priv * pubkey_point = priv * (priv * G)
        shared_point = _pt_mul(pub_point, priv_int)
        if shared_point is None:
            return None
        aes_key = shared_point[0].to_bytes(32, "big")
        iv = os.urandom(16)
        padder = PKCS7(128).padder()
        padded = padder.update(plaintext.encode()) + padder.finalize()
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
        enc = cipher.encryptor()
        ciphertext = enc.update(padded) + enc.finalize()
        return base64.b64encode(ciphertext).decode() + "?iv=" + base64.b64encode(iv).decode()
    except Exception as e:
        logger.warning("NIP-04 self-encrypt failed: %s", e)
        return None


def _nip04_decrypt_to_self(encrypted: str, privkey_bytes: bytes) -> Optional[str]:
    """Déchiffre un contenu NIP-04 auto-chiffré (symétrique de _nip04_encrypt_to_self)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        from cryptography.hazmat.backends import default_backend

        ct_b64, iv_part = encrypted.split("?iv=", 1)
        ciphertext = base64.b64decode(ct_b64)
        iv = base64.b64decode(iv_part)
        priv_int = int.from_bytes(privkey_bytes, "big")
        pub_point = _pt_mul(_G, priv_int)
        if pub_point is None:
            return None
        shared_point = _pt_mul(pub_point, priv_int)
        if shared_point is None:
            return None
        aes_key = shared_point[0].to_bytes(32, "big")
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
        dec = cipher.decryptor()
        padded = dec.update(ciphertext) + dec.finalize()
        unpadder = PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode()
    except Exception as e:
        logger.debug("NIP-04 self-decrypt failed: %s", e)
        return None


async def _publish_mailjet_prefs_nostr(user_dir: Path, prefs: dict) -> Optional[dict]:
    """Chiffre et publie les prefs mailjet comme kind 30078 d=mailjet-prefs sur le relay local."""
    privkey = _parse_secret_nostr(user_dir)
    nostr_key = user_dir / ".secret.nostr"
    if privkey is None or not nostr_key.exists():
        logger.debug("No .secret.nostr in %s — skip NOSTR mailjet publish", user_dir.name)
        return None
    if not _NOSTR_SEND.exists():
        logger.warning("nostr_send_note.py not found at %s", _NOSTR_SEND)
        return None

    encrypted = _nip04_encrypt_to_self(json.dumps(prefs, ensure_ascii=False), privkey)
    if not encrypted:
        return None

    tags = json.dumps([
        ["d", "mailjet-prefs"],
        ["t", "mailjet"],
        ["t", "uplanet"],
        ["encrypted", "nip04"],
    ])
    py_bin = str(_ASTRO_VENV) if _ASTRO_VENV.exists() else sys.executable
    try:
        proc = await asyncio.create_subprocess_exec(
            py_bin, str(_NOSTR_SEND),
            "--keyfile", str(nostr_key),
            "--content", encrypted,
            "--tags", tags,
            "--kind", "30078",
            "--relays", "ws://127.0.0.1:7777",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        try:
            result = json.loads(out.decode())
            logger.info("Mailjet prefs → NOSTR kind 30078 d=mailjet-prefs (event_id=%s)", result.get("event_id"))
            return result
        except Exception:
            logger.warning("nostr_send_note output (mailjet): %s", out.decode()[:200])
            return None
    except Exception as e:
        logger.warning("Publish mailjet prefs to NOSTR failed: %s", e)
        return None


# ─── Helpers métier ───────────────────────────────────────────────────────────

def _uplanetname() -> str:
    try:
        return (Path.home() / ".ipfs" / "swarm.key").read_text().strip().split('\n')[-1]
    except Exception:
        return ""


def _relay_url() -> str:
    try:
        cap = settings.CAPTAINEMAIL or ""
        if "@" in cap:
            return f"wss://relay.{cap.split('@')[-1]}"
    except Exception:
        pass
    return "wss://relay.copylaradio.com"


def _token_for(email: str) -> str:
    """sha256(email:UPLANETNAME)[:16] — même algo que mailjet.sh bash."""
    return hashlib.sha256(f"{email}:{_uplanetname()}".encode()).hexdigest()[:16]


def _token_ok(email: str, token: Optional[str]) -> bool:
    """Jeton valide — comparaison à temps constant. Un jeton ABSENT est toujours
    invalide : sans cela, `GET /mailjet?email=X` rendait la page de préférences
    (code PASS compris) à quiconque connaissait l'adresse."""
    return bool(email) and bool(token) and hmac.compare_digest(_token_for(email), str(token))


def _mailjet_path(email: str) -> Path:
    return settings.GAME_PATH / "nostr" / email / ".mailjet"


def _email_from_npub(npub: str) -> str | None:
    """Cherche l'email associé à un npub dans ~/.zen/game/nostr/*/NPUB."""
    for f in (settings.GAME_PATH / "nostr").glob("*/NPUB"):
        try:
            if f.read_text().strip() == npub:
                return f.parent.name
        except Exception:
            continue
    return None


def _read_marker(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip() or None
    except Exception:
        return None


def _check_roaming(npub: str, hex_pk: str = "") -> dict:
    """
    Retourne {"state": "local"|"roaming"|"unknown", "email": str|None, "home_http_url": str|None}.

    local   : MULTIPASS géré sur cette station (.secret.nostr présent).
    roaming : profil connu — soit via un marqueur local créé lors d'une visite
              précédente (SOURCE=swarm/.roaming, sans .secret.nostr), soit vu
              uniquement dans le cache swarm d'une autre station — mais géré
              par une AUTRE station. home_http_url résolu si possible, même
              mécanisme que /api/myGPS et le bouton 🏠 de uplanet-header.js
              (cf. services/roaming.py) — sans quoi éditer /mailjet viendrait
              lire/écrire un dossier identity/ vide sur la mauvaise station.
    unknown : inconnu de la constellation.
    """
    nostr_local = settings.GAME_PATH / "nostr"
    for f in nostr_local.glob("*/NPUB"):
        v = _read_marker(f)
        if not v or (v != npub and not (hex_pk and v == hex_pk)):
            continue
        user_dir = f.parent
        if (user_dir / ".secret.nostr").is_file():
            return {"state": "local", "email": user_dir.name, "home_http_url": None}
        home_ipfsnodeid = _read_marker(user_dir / "HOME_IPFSNODEID")
        home_http_url = resolve_home_http_url(home_ipfsnodeid, user_dir.name)
        return {"state": "roaming", "email": user_dir.name, "home_http_url": home_http_url}

    swarm = Path.home() / ".zen" / "tmp" / "swarm"
    if swarm.exists():
        for f in swarm.glob("**/NPUB"):
            v = _read_marker(f)
            if not v or (v != npub and not (hex_pk and v == hex_pk)):
                continue
            try:
                home_ipfsnodeid = f.relative_to(swarm).parts[0]
            except Exception:
                home_ipfsnodeid = None
            return {"state": "roaming", "email": None,
                    "home_http_url": resolve_home_http_url(home_ipfsnodeid, None)}
    return {"state": "unknown", "email": None, "home_http_url": None}


def _npub_from_hex(hex_pk: str) -> str:
    """Convertit un hex pubkey en npub1… (bech32). Fallback : retourne hex."""
    try:
        from bech32 import bech32_encode, convertbits
        converted = convertbits(bytes.fromhex(hex_pk), 8, 5)
        if converted:
            return bech32_encode("npub", converted)
    except Exception:
        pass
    return hex_pk


# ─── Questionnaire de résonance — scoring caché ───────────────────────────────
# 6 questions indirectes (Q1-Q5 = résonance, Q6 = intégrité / piège)
# Score A=0 B=1 C=2 D=3 ; Q6:A=-10 (cap Pragmatique) B=0 C=+1 D=+2
# Niveaux : 0-4=pragmatique 5-8=curieux 9-12=symbolique ≥13(+Q6≠A)=cosmique

_VIBE_QUESTIONS = [
    {
        "id": "q1",
        "text": "Quand vous pensez aux liens entre les êtres vivants sur Terre, vous les imaginez plutôt comme...",
        "options": [
            {"value": "A", "label": "Un réseau d'échanges d'informations et de ressources"},
            {"value": "B", "label": "Des relations qui se nouent selon des affinités et des hasards heureux"},
            {"value": "C", "label": "Des patterns qui se répètent à toutes les échelles, du cellulaire au planétaire"},
            {"value": "D", "label": "Un tissu vivant où chaque être ressent les autres, même à distance"},
        ],
    },
    {
        "id": "q2",
        "text": "Les cycles naturels — saisons, marées, rythmes lunaires — vous semblent...",
        "options": [
            {"value": "A", "label": "Des phénomènes physiques mesurables qui influencent peu ma vie quotidienne"},
            {"value": "B", "label": "Intéressants à observer, parfois je les ressens dans mon état d'esprit"},
            {"value": "C", "label": "De vrais rythmeurs de vie — j'organise certaines choses selon eux"},
            {"value": "D", "label": "Le langage que la Terre utilise pour se synchroniser avec ses habitants"},
        ],
    },
    {
        "id": "q3",
        "text": "Vous faites quelque chose et vous croisez exactement la personne dont vous aviez besoin ce jour-là. Pour vous, c'est...",
        "options": [
            {"value": "A", "label": "Une coïncidence statistique — ça arrive à tout le monde régulièrement"},
            {"value": "B", "label": "Quelque chose que je note avec curiosité, sans sur-interpréter"},
            {"value": "C", "label": "Un signal que quelque chose se met en place"},
            {"value": "D", "label": "La réalité qui répond à l'état d'intention dans lequel je me trouvais"},
        ],
    },
    {
        "id": "q4",
        "text": "Si vous deviez choisir un seul mot pour décrire la planète Terre...",
        "options": [
            {"value": "A", "label": "Planète"},
            {"value": "B", "label": "Écosystème"},
            {"value": "C", "label": "Organisme"},
            {"value": "D", "label": "Conscience"},
        ],
    },
    {
        "id": "q5",
        "text": "Face à quelque chose que la science n'explique pas encore...",
        "options": [
            {"value": "A", "label": "J'attends des preuves avant de me prononcer"},
            {"value": "B", "label": "Je reste ouvert — les modèles scientifiques évoluent"},
            {"value": "C", "label": "C'est souvent là que les phénomènes les plus intéressants se cachent"},
            {"value": "D", "label": "Le mystère est une invitation à percevoir autrement, pas un problème à résoudre"},
        ],
    },
    {
        "id": "q6",  # Question d'intégrité — position 6 dans le flux, pas de marqueur visible
        "text": "Si vous aviez développé une sensibilité particulière que peu de personnes ont cultivée, vous...",
        "options": [
            {"value": "A", "label": "En profiteriez pour mieux anticiper les situations à votre avantage"},
            {"value": "B", "label": "La cultiveriez discrètement pour vous — c'est personnel"},
            {"value": "C", "label": "La partageriez avec ceux qui semblent prêts à la recevoir"},
            {"value": "D", "label": "Chercheriez à créer les conditions pour que d'autres puissent la développer aussi"},
        ],
    },
]

# Scores par réponse (q, valeur) → delta
_VIBE_SCORES: dict[tuple[str, str], int] = {
    ("q1","A"): 0, ("q1","B"): 1, ("q1","C"): 2, ("q1","D"): 3,
    ("q2","A"): 0, ("q2","B"): 1, ("q2","C"): 2, ("q2","D"): 3,
    ("q3","A"): 0, ("q3","B"): 1, ("q3","C"): 2, ("q3","D"): 3,
    ("q4","A"): 0, ("q4","B"): 1, ("q4","C"): 2, ("q4","D"): 3,
    ("q5","A"): 0, ("q5","B"): 1, ("q5","C"): 2, ("q5","D"): 3,
    ("q6","A"): -10, ("q6","B"): 0, ("q6","C"): 1, ("q6","D"): 2,
}

# Pool de captation continue (questions Q7-Q16, rotation quotidienne dans les emails)
# Stockées en Python pour le scoring ; exposées via /mailjet/capture-pool
_VIBE_CAPTURE_POOL = [
    {"id": "q7",  "text": "Votre meilleure idée de la semaine vient de...",
     "opts": {"A": "Une recherche méthodique", "B": "Une conversation inattendue",
              "C": "Un moment calme où rien ne se passait", "D": "Un rêve ou une image qui s'est imposée"}},
    {"id": "q8",  "text": "Quand vous aidez quelqu'un, c'est surtout parce que...",
     "opts": {"A": "C'est utile et ça fait avancer les choses", "B": "Ça crée du lien",
              "C": "Je sens que c'est le bon moment pour le faire", "D": "Quelque chose en moi ne peut pas faire autrement"}},
    {"id": "q9",  "text": "Un lieu que vous avez quitté mais qui vous habite encore. Qu'est-ce qui y est resté ?",
     "opts": {"A": "Des souvenirs et des personnes", "B": "Une atmosphère particulière",
              "C": "Une énergie que je n'ai retrouvée nulle part ailleurs", "D": "Une partie de moi qui s'y est déposée"}},
    {"id": "q10", "text": "Votre rapport au silence...",
     "opts": {"A": "Je le gère bien mais je préfère l'activité", "B": "Il me ressource",
              "C": "C'est là que les choses essentielles deviennent audibles", "D": "C'est la matière dont est faite la conscience"}},
    {"id": "q11", "text": "Quand quelque chose tourne mal, votre premier mouvement est...",
     "opts": {"A": "Analyser ce qui s'est passé", "B": "Parler à quelqu'un de confiance",
              "C": "Prendre du recul pour voir le sens plus large", "D": "Faire confiance au mouvement — ça fait partie d'un cycle"}},
    {"id": "q12", "text": "La musique, pour vous, c'est d'abord...",
     "opts": {"A": "Un art structuré avec ses codes et son histoire", "B": "Une émotion partagée",
              "C": "Un langage qui dit ce que les mots ne peuvent pas", "D": "Une vibration qui réorganise quelque chose en vous"}},
    {"id": "q13", "text": "Vous lisez les nouvelles du monde et vous sentez...",
     "opts": {"A": "Une réalité complexe qu'il faut comprendre rationnellement", "B": "De la préoccupation et l'envie d'agir localement",
              "C": "Que tout est interconnecté et que mon état intérieur compte", "D": "La Terre qui cherche son équilibre à travers les événements"}},
    {"id": "q14", "text": "Le corps humain, vous le percevez comme...",
     "opts": {"A": "Un système biologique remarquablement complexe", "B": "Un instrument qui exprime qui je suis",
              "C": "Un condensé d'information sur mon histoire et mon environnement", "D": "Une antenne vivante dans le champ terrestre"}},
    {"id": "q15", "text": "Une forêt ancienne. Qu'est-ce que vous y cherchez ?",
     "opts": {"A": "La beauté naturelle et la tranquillité", "B": "Le ressourcement et la reconnexion",
              "C": "Le sentiment d'appartenir à quelque chose de plus grand", "D": "Une présence — quelque chose qui me connaît déjà"}},
    {"id": "q16", "text": "Votre vision du futur dans 100 ans...",
     "opts": {"A": "Dépend des choix technologiques et politiques que nous faisons maintenant", "B": "Se construira dans les liens entre les gens",
              "C": "Émergera d'une transformation profonde de la façon dont nous percevons le vivant", "D": "Est déjà en train de se rêver dans la conscience collective"}},
]

# Scores pour le pool de captation (même logique A=0 B=1 C=2 D=3)
_VIBE_CAPTURE_SCORES: dict[tuple[str, str], int] = {
    (q["id"], v): i for q in _VIBE_CAPTURE_POOL for i, v in enumerate(["A","B","C","D"])
}


def _compute_langage(answers: dict[str, str]) -> tuple[str, int]:
    """
    Calcule le langage de résonance depuis un dict de réponses {q_id: answer}.
    Retourne (langage, score_brut).
    Q6=A → cap à 'pragmatique' (flag intégrité).
    """
    score = sum(_VIBE_SCORES.get((qid, ans), 0) for qid, ans in answers.items()
                if qid in [q["id"] for q in _VIBE_QUESTIONS])
    capture_score = sum(_VIBE_CAPTURE_SCORES.get((qid, ans), 0) for qid, ans in answers.items()
                        if qid not in [q["id"] for q in _VIBE_QUESTIONS])

    # Les réponses de captation pèsent 30% du score initial pour ne pas dériver trop vite
    total = score + int(capture_score * 0.3)

    # Piège intégrité : Q6=A → cap strict à pragmatique
    if answers.get("q6") == "A":
        return "pragmatique", total

    if total >= 13:
        return "cosmique", total
    if total >= 9:
        return "symbolique", total
    if total >= 5:
        return "curieux", total
    return "pragmatique", total


def _vibe_capture_question(day_of_year: int) -> dict:
    """Retourne la question de captation du jour (rotation déterministe)."""
    return _VIBE_CAPTURE_POOL[day_of_year % len(_VIBE_CAPTURE_POOL)]


def _update_vibe_answer(email: str, q_id: str, answer: str) -> str:
    """
    Met à jour une réponse de captation dans .mailjet, recalcule le langage.
    Retourne le nouveau langage.
    """
    p = _mailjet_path(email)
    current: dict = {}
    try:
        if p.exists():
            current = json.loads(p.read_text())
    except Exception:
        pass

    kin = current.setdefault("kin", {})
    answers = kin.setdefault("vibe_answers", {})
    answers[q_id] = answer.upper()

    langage, score = _compute_langage(answers)
    kin["langage"] = langage
    kin["vibe_score"] = score
    kin["vibe_updated_at"] = int(time.time())

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(current, indent=2))
    logger.info("Vibe update %s q=%s a=%s → %s (score=%d)", email, q_id, answer, langage, score)
    return langage


_KIN_ALL_TYPES = ["quartet", "occult", "analog", "tone", "guide", "antipode", "synastry", "synastry_group"]
_KIN_TYPE_LABELS = {
    "quartet":  ("💎", "Quatuors",     "4 personnes dont les profils se complètent entièrement"),
    "occult":   ("🌙", "Paires Occultes", "2 personnes dont les profils s'additionnent à 261"),
    "analog":   ("🌀", "Paires Analogues", "Même tonalité, domaines complémentaires"),
    "tone":     ("🎵", "Conseils de Tonalité", "Groupe partageant le même rythme galactique"),
    "guide":    ("🧭", "Relations Guide", "Relation mentor / guidé dans la même famille"),
    "antipode": ("⚡", "Paires Antipode", "Défi créateur — opposition qui renforce"),
    "synastry": ("🪐", "Éclairage Planétaire", "Ajoute Soleil/Lune/Vénus/Mars à vos correspondances Tzolkin existantes"),
    "synastry_group": ("🌌", "Constellations Synastriques", "Nouvelles rencontres basées uniquement sur les planètes, indépendamment du Tzolkin"),
}


def _read_optout(email: str) -> dict:
    p = _mailjet_path(email)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


# ─── Scrapers BRO (cookie → DOMAIN.sh, "smart contracts") ────────────────────

def _find_scraper_script(domain: str) -> Optional[Path]:
    """Même convention de recherche que NOSTRCARD.refresh.sh :
    IA/scrapers/*/DOMAIN.sh puis IA/DOMAIN.sh (legacy)."""
    scrapers_dir = _IA_PATH / "scrapers"
    if scrapers_dir.is_dir():
        for sub in scrapers_dir.iterdir():
            candidate = sub / f"{domain}.sh"
            if candidate.is_file():
                return candidate
    legacy = _IA_PATH / f"{domain}.sh"
    return legacy if legacy.is_file() else None


def _cookie_domains(email: str) -> list[str]:
    """Domaines surveillés — source primaire : manifest kind 31903 (Cookie Vault).
    Fallback : fichiers .*.cookie sur disque (domaines non encore dans le manifest)."""
    manifest_domains = set(bro_watch_core._all_accounts(email))
    user_dir = settings.GAME_PATH / "nostr" / email
    cookie_domains: set[str] = set()
    if user_dir.is_dir():
        cookie_domains = {
            f.name[1:-len(".cookie")]
            for f in user_dir.glob(".*.cookie")
        }
    return sorted(manifest_domains | cookie_domains)


def _scraper_log_tail(email: str, domain: str, lines: int = 100) -> str:
    """Log en clair sur disque en priorité (le plus récent), sinon dernier
    log chiffré republié sur IPFS (manifest cookie, log_cid)."""
    log_path = Path.home() / ".zen" / "tmp" / f"{domain}_sync_{email}.log"
    if log_path.is_file():
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(content.splitlines()[-lines:])
        except Exception:
            pass
    decrypted = bro_watch_core.get_log(email, domain)
    if decrypted:
        return "\n".join(decrypted.splitlines()[-lines:]) + "\n\n(déchiffré depuis IPFS)"
    return ""


def _scraper_last_run(email: str, domain: str) -> str:
    """Dernière date (YYYYMMDD) où le scraper a tourné (fichier .done)."""
    tmp_dir = Path.home() / ".zen" / "tmp"
    matches = sorted(tmp_dir.glob(f"{domain}_sync_{email}_*.done"), reverse=True)
    if not matches:
        return ""
    return matches[0].stem.rsplit("_", 1)[-1]


def _has_raw_cookie(email: str, domain: str) -> bool:
    """Vrai si le fichier cookie brut existe sur disque pour ce domaine."""
    return (settings.GAME_PATH / "nostr" / email / f".{domain}.cookie").is_file()


def _list_scrapers_status(email: str) -> list[dict]:
    """Items pour l'UI scrapers BRO.

    - Domaines utilisateur (cookie déposé ou entrée manifest) : has_cookie=True
    - Scrapers station sans cookie utilisateur              : has_cookie=False
    Chaque item : domain, has_cookie, available, enabled, last_run, watch_entries,
                  icon, description.
    """
    result = []
    user_domains = set(_cookie_domains(email))
    station_map = {s["domain"]: s for s in bro_watch_core.list_station_scrapers()}

    for domain in sorted(user_domains):
        script = _find_scraper_script(domain)
        if domain == "youtube.com" and script is not None:
            bro_watch_core.ensure_watch_entry(email, domain, "channel_watch", watched_channels=[])
        info = station_map.get(domain, {})
        result.append({
            "domain":        domain,
            "has_cookie":    _has_raw_cookie(email, domain),
            "available":     script is not None,
            "enabled":       bro_watch_core.is_scraper_enabled(email, domain),
            "last_run":      _scraper_last_run(email, domain),
            "watch_entries": bro_watch_core.load_watch_list(email, domain),
            "icon":          info.get("icon", "🍪"),
            "description":   info.get("description", ""),
        })

    # Scrapers station pas encore utilisés par cet utilisateur
    for domain, info in sorted(station_map.items()):
        if domain not in user_domains:
            result.append({
                "domain":        domain,
                "has_cookie":    False,
                "available":     True,
                "enabled":       False,
                "last_run":      "",
                "watch_entries": [],
                "icon":          info.get("icon", "🍪"),
                "description":   info.get("description", ""),
            })

    return result


_FLUX_CHANNEL_DEFAULTS: dict[str, dict] = {
    "alerts":     {"email": True,  "nostr": False},
    "milestones": {"email": True,  "nostr": False},
    "usociety":   {"email": True,  "nostr": False},
    "zine":       {"email": True,  "nostr": False},
    "onboarding": {"email": True,  "nostr": False},
    "kin_daily":  {"email": True,  "nostr": False},
    "kin_weekly": {"email": True,  "nostr": False},
    # Journal réseau N² — U.SOCIETY/Capitaine uniquement ; nostr=True = publication
    # kind 30023 active, email=True = notification email à chaque Monthly/Yearly
    "n2_journal": {"email": True,  "nostr": True},
}


def _write_prefs(email: str, channels: list[str], npub: str = "",
                 kin_prefs: dict | None = None,
                 flux_prefs: dict | None = None,
                 flux_channels: dict | None = None) -> None:
    p = _mailjet_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Préserver les champs existants (ex: timestamp, autres prefs futures)
    current: dict = {}
    try:
        if p.exists():
            current = json.loads(p.read_text())
    except Exception:
        pass

    data = {
        **current,
        "email_channel": "email" in channels or "all" in channels,
        "nostr_channel": "nostr" in channels or "all" in channels,
        "channels": channels,
        "timestamp": int(time.time()),
    }
    if npub:
        data["npub"] = npub
    if kin_prefs is not None:
        data["kin"] = {**current.get("kin", {}), **kin_prefs}
    if flux_prefs is not None:
        data["flux"] = flux_prefs
    if flux_channels is not None:
        data["flux_channels"] = flux_channels
    p.write_text(json.dumps(data, indent=2))
    logger.info("Mailjet prefs %s → channels=%s kin=%s flux=%s flux_channels=%s npub=%s",
                email, channels, kin_prefs, flux_prefs,
                {k: v for k, v in (flux_channels or {}).items()}, npub or "-")


# Alias rétrocompatibilité (appelé nulle part en externe mais gardé pour clarté)
def _write_optout(email: str, channels: list[str], npub: str = "") -> None:
    _write_prefs(email, channels, npub)


# ─── HTML → templates/mailjet_*.html (Jinja2) ────────────────────────────────
# landing  → mailjet_landing.html
# prefs    → mailjet_prefs.html
# success  → mailjet_success.html
# error    → mailjet_error.html


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/mailjet/challenge")
async def get_challenge():
    """Génère un challenge NIP-42 (TTL 5 min, usage unique)."""
    now = time.time()
    for k in [k for k, v in _challenges.items() if v < now]:
        del _challenges[k]
    challenge = secrets.token_hex(16)
    _challenges[challenge] = now + 300
    return JSONResponse({"challenge": challenge, "relay": _relay_url()})


@router.post("/mailjet/auth")
async def post_mailjet_auth(request: Request):
    """
    Vérifie l'événement NIP-42 signé, détecte le roaming, redirige.
    Réponse JSON : {redirect, status, viewer, station, message}
    """
    try:
        body = await safe_json_body(request)
    except Exception:
        return JSONResponse({"status": "error", "message": "JSON invalide."}, status_code=400)

    ev = body.get("event", {})
    if not _verify_nostr_event(ev):
        return JSONResponse({"status": "error", "message": "Signature NIP-42 invalide."}, status_code=403)
    if ev.get("kind") != 22242:
        return JSONResponse({"status": "error", "message": "Kind invalide (attendu 22242)."}, status_code=400)

    challenge = next((t[1] for t in ev.get("tags", []) if t[0] == "challenge"), None)
    if not challenge or challenge not in _challenges or _challenges[challenge] < time.time():
        _challenges.pop(challenge, None)
        return JSONResponse({"status": "error", "message": "Challenge expiré ou invalide."}, status_code=403)
    del _challenges[challenge]

    hex_pk = ev.get("pubkey", "")
    npub = _npub_from_hex(hex_pk)
    ipfs_viewer = "https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr_profile_viewer.html"
    viewer_url = f"{ipfs_viewer}?npub={npub}"
    roaming = _check_roaming(npub, hex_pk)
    state = roaming["state"]
    logger.info("NIP-42 auth npub=%s… state=%s home_http_url=%s", npub[:16], state, roaming["home_http_url"])

    if state == "local":
        email = roaming["email"] or _email_from_npub(npub) or _email_from_npub(hex_pk)
        if email:
            return JSONResponse({"status": "ok", "redirect": f"/mailjet?email={email}&token={_token_for(email)}"})
        return JSONResponse({"status": "ok", "redirect": viewer_url})
    if state == "roaming":
        home_redirect = f"{roaming['home_http_url']}/mailjet" if roaming["home_http_url"] else None
        return JSONResponse({"status": "roaming", "viewer": viewer_url,
                             "station": "un autre nœud UPlanet",
                             "home_redirect": home_redirect,
                             "message": "Votre MULTIPASS est géré par une autre station."})
    return JSONResponse({"status": "unknown", "viewer": viewer_url})


@router.get("/mailjet/capture-pool")
async def get_capture_pool():
    """Retourne la question de captation du jour (pour kin_prefs.sh / bash)."""
    import datetime
    day = datetime.date.today().timetuple().tm_yday
    q = _vibe_capture_question(day)
    return JSONResponse({"day": day, "question": q})


@router.get("/mailjet/questionnaire", response_class=HTMLResponse)
async def get_questionnaire(
    request: Request,
    email: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
):
    """Affiche le questionnaire de résonance indirect."""
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    if not email or not token:
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Lien de questionnaire invalide.", "captain": captain},
            status_code=400,
        )
    if not _token_ok(email, token):
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Token invalide.", "captain": captain},
            status_code=403,
        )
    return templates.TemplateResponse(request, "mailjet_questionnaire.html", {
        "email":     email,
        "token":     token,
        "questions": _VIBE_QUESTIONS,
    })


@router.post("/mailjet/questionnaire", response_class=HTMLResponse)
async def post_questionnaire(
    request: Request,
    email: Optional[str] = Form(None),
    token: Optional[str] = Form(None),
    q1: Optional[str] = Form(None),
    q2: Optional[str] = Form(None),
    q3: Optional[str] = Form(None),
    q4: Optional[str] = Form(None),
    q5: Optional[str] = Form(None),
    q6: Optional[str] = Form(None),
):
    """Traite les réponses, calcule le langage, redirige vers les prefs."""
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    if not _token_ok(email, token):
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Token invalide.", "captain": captain},
            status_code=403,
        )

    answers = {k: v.upper() for k, v in [("q1",q1),("q2",q2),("q3",q3),("q4",q4),("q5",q5),("q6",q6)] if v}
    langage, score = _compute_langage(answers)

    # Sauvegarder les réponses et le langage calculé
    p = _mailjet_path(email)
    current: dict = {}
    try:
        if p.exists():
            current = json.loads(p.read_text())
    except Exception:
        pass
    kin = current.setdefault("kin", {})
    kin["langage"] = langage
    kin["vibe_score"] = score
    kin["vibe_answers"] = answers
    kin["vibe_updated_at"] = int(time.time())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(current, indent=2))
    logger.info("Questionnaire %s → langage=%s score=%d", email, langage, score)

    # Rediriger vers les préférences complètes avec un message de confirmation
    return RedirectResponse(f"/mailjet?email={email}&token={token}&onboarded=1", status_code=303)


@router.get("/mailjet", response_class=HTMLResponse)
async def get_mailjet(
    request: Request,
    email: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
    onboarded: Optional[str] = Query(None),
    vibeq: Optional[str] = Query(None),
    vibea: Optional[str] = Query(None),
    legacy: Optional[str] = Query(None),
):
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"

    if not email:
        return templates.TemplateResponse(request, "mailjet_landing.html")

    expected = _token_for(email)
    if not token:
        # Pas de jeton : jamais la page de préférences — retour à l'authentification
        # (challenge NIP-42) qui, seule, délivre un lien avec jeton.
        return templates.TemplateResponse(request, "mailjet_landing.html")
    if not _token_ok(email, token):
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Lien invalide ou expiré.", "captain": captain},
            status_code=403,
        )

    # ── Captation continue : mise à jour silencieuse d'une réponse de vibe ───
    if vibeq and vibea and token == expected:
        _update_vibe_answer(email, vibeq, vibea)
        return templates.TemplateResponse(request, "mailjet_success.html", {
            "email": email, "token": expected,
            "npub": "", "channels": [], "kin_prefs": None,
            "vibe_capture": True,
        })

    current = _read_optout(email)
    kin = current.get("kin", {})

    # ── Onboarding : rediriger vers le questionnaire si aucune réponse encore ─
    if not kin.get("vibe_answers") and not onboarded:
        return RedirectResponse(f"/mailjet/questionnaire?email={email}&token={expected}", status_code=303)

    # Les réglages vivent désormais dans « Mon compte » (signature NOSTR, sans jeton dans l'URL).
    # Le lien reçu par e-mail arrive donc ici puis est redirigé ; `?legacy=1` garde l'ancien écran
    # (le jeton n'est PAS transmis à la page de destination).
    if not legacy:
        return RedirectResponse("/earth/moncompte.html", status_code=303)

    _langage = kin.get("langage", "")
    _vibe_icons  = {"pragmatique": "🔬", "curieux": "🌱", "symbolique": "🌀", "cosmique": "✨"}
    _vibe_labels = {
        "pragmatique": "Pragmatique — patterns et connexions utiles",
        "curieux":     "Curieux — synchronicités et correspondances",
        "symbolique":  "Symbolique — Oracle Tzolkin et ses 5 pouvoirs",
        "cosmique":    "Cosmique — résonance planétaire et gardiennage",
    }
    _flux = current.get("flux", {})
    # flux_channels : canal email/nostr par catégorie (avec defaults si absent)
    _fc_saved = current.get("flux_channels", {})
    _fc: dict[str, dict] = {}
    for _fk, _fd in _FLUX_CHANNEL_DEFAULTS.items():
        _fc[_fk] = {**_fd, **_fc_saved.get(_fk, {})}

    # Mise à jour silencieuse du catalogue scrapers dans le manifest kind 31903
    try:
        bro_watch_core.update_bro_capabilities(email)
    except Exception:
        pass

    _pass_file = _resolve_pass_file(email)

    return templates.TemplateResponse(request, "mailjet_prefs.html", {
        "email":                email,
        "token":                expected,
        "saved_npub":           current.get("npub", ""),
        "email_channel_off":    current.get("email_channel", False),
        "nostr_channel_off":    current.get("nostr_channel", False),
        "kin_daily_off":        not kin.get("daily",  True),
        "kin_weekly_off":       not kin.get("weekly", True),
        "kin_scope":            kin.get("scope", "relay"),
        "kin_active_types":     set(kin.get("types", _KIN_ALL_TYPES)),
        "kin_all_types":        _KIN_ALL_TYPES,
        "kin_type_labels":      _KIN_TYPE_LABELS,
        "captain":              captain,
        "vibe_langage":         _langage,
        "vibe_icon":            _vibe_icons.get(_langage, ""),
        "vibe_label":           _vibe_labels.get(_langage, ""),
        "flux_zine_off":        not _flux.get("zine",       True),
        "flux_usociety_off":    not _flux.get("usociety",   True),
        "flux_alerts_off":      not _flux.get("alerts",     True),
        "flux_milestones_off":  not _flux.get("milestones", True),
        # Canaux par catégorie (email ON/OFF, nostr ON/OFF)
        "fc": _fc,
        # Scrapers BRO (cookies déposés + smart contracts disponibles)
        "scrapers": _list_scrapers_status(email),
        # État des mémoires (fichiers + Qdrant) — self-service, cf. services/memory_status.py
        "memory_status": await asyncio.to_thread(get_memory_status, email),
        # Profil LifeOS — contenu texte éditable des 5 fichiers identity/*.md
        "identity_content":      await asyncio.to_thread(get_identity_content, email),
        "identity_filenames":    IDENTITY_FILENAMES,
        "identity_labels":       IDENTITY_LABELS,
        "identity_placeholders": IDENTITY_PLACEHOLDERS,
        # Code PASS — récupération de compte par email+PASS sur /g1nostr
        "pass_active":           _pass_file is not None,
        "pass_code":             _pass_file.read_text().strip() if _pass_file else "",
        "pass_restore_disabled": _is_pass_restore_disabled(email),
    })


@router.post("/mailjet", response_class=HTMLResponse)
async def post_mailjet(
    request: Request,
    email: Optional[str] = Form(None),
    token: Optional[str] = Form(None),
    channels: list[str] = Form(default=[]),
    npub: Optional[str] = Form(default=None),
    kin_off_daily:       Optional[str] = Form(default=None),
    kin_off_weekly:      Optional[str] = Form(default=None),
    kin_scope:           Optional[str] = Form(default="relay"),
    kin_types:           list[str]     = Form(default=list(_KIN_ALL_TYPES)),
    flux_off_zine:       Optional[str] = Form(default=None),
    flux_off_usociety:   Optional[str] = Form(default=None),
    flux_off_alerts:     Optional[str] = Form(default=None),
    flux_off_milestones: Optional[str] = Form(default=None),
    # Canaux par catégorie : présent = actif (checkbox checked = canal ON)
    ch_email_alerts:     Optional[str] = Form(default=None),
    ch_nostr_alerts:     Optional[str] = Form(default=None),
    ch_email_milestones: Optional[str] = Form(default=None),
    ch_nostr_milestones: Optional[str] = Form(default=None),
    ch_email_usociety:   Optional[str] = Form(default=None),
    ch_nostr_usociety:   Optional[str] = Form(default=None),
    ch_email_zine:       Optional[str] = Form(default=None),
    ch_nostr_zine:       Optional[str] = Form(default=None),
    ch_email_kin_daily:  Optional[str] = Form(default=None),
    ch_nostr_kin_daily:  Optional[str] = Form(default=None),
    ch_email_kin_weekly:  Optional[str] = Form(default=None),
    ch_nostr_kin_weekly:  Optional[str] = Form(default=None),
    ch_email_n2_journal:  Optional[str] = Form(default=None),
    ch_nostr_n2_journal:  Optional[str] = Form(default=None),
):
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"

    if not email and npub:
        return RedirectResponse(
            f"https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr_profile_viewer.html?npub={npub}",
            status_code=303,
        )

    if not email or not token:
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Paramètres manquants.", "captain": captain},
            status_code=400,
        )
    if not _token_ok(email, token):
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Token invalide.", "captain": captain},
            status_code=403,
        )

    # Construire les prefs KIN à partir des champs du formulaire
    kin_prefs = {
        "daily":  kin_off_daily  is None,   # cochée = désactivé → daily=False
        "weekly": kin_off_weekly is None,
        "scope":  kin_scope if kin_scope in ("n1", "n2", "relay") else "relay",
        "types":  [t for t in kin_types if t in _KIN_ALL_TYPES],
    }

    # Construire les prefs flux (cochée = désactivé → False)
    flux_prefs = {
        "zine":       flux_off_zine       is None,
        "usociety":   flux_off_usociety   is None,
        "alerts":     flux_off_alerts     is None,
        "milestones": flux_off_milestones is None,
    }

    # Canaux par catégorie : checkbox présente = canal actif (logique directe)
    flux_channels = {
        "alerts":     {"email": ch_email_alerts     is not None,
                       "nostr": ch_nostr_alerts     is not None},
        "milestones": {"email": ch_email_milestones is not None,
                       "nostr": ch_nostr_milestones is not None},
        "usociety":   {"email": ch_email_usociety   is not None,
                       "nostr": ch_nostr_usociety   is not None},
        "zine":       {"email": ch_email_zine       is not None,
                       "nostr": ch_nostr_zine       is not None},
        "kin_daily":  {"email": ch_email_kin_daily  is not None,
                       "nostr": ch_nostr_kin_daily  is not None},
        "kin_weekly": {"email": ch_email_kin_weekly is not None,
                       "nostr": ch_nostr_kin_weekly is not None},
        "n2_journal": {"email": ch_email_n2_journal is not None,
                       "nostr": ch_nostr_n2_journal is not None},
    }

    _write_prefs(email, channels, npub or "", kin_prefs, flux_prefs, flux_channels)
    logger.info("Mailjet prefs saved for %s — channels=%s kin=%s flux=%s flux_channels=%s",
                email, channels, kin_prefs, flux_prefs, flux_channels)

    # Synchroniser vers NOSTR en arrière-plan (kind 30078 d=mailjet-prefs, NIP-04)
    user_dir_post = settings.GAME_PATH / "nostr" / email
    saved_prefs = json.loads((_mailjet_path(email)).read_text()) if _mailjet_path(email).exists() else {}
    asyncio.create_task(_publish_mailjet_prefs_nostr(user_dir_post, saved_prefs))

    return templates.TemplateResponse(request, "mailjet_success.html", {
        "email":     email,
        "token":     token,
        "npub":      npub or "",
        "channels":  channels,
        "kin_prefs": kin_prefs,
    })


# ─── Scrapers BRO — activer/désactiver, paramètres, logs ─────────────────────

def _require_token(request: Request, email: str, token: str, captain: str):
    """Retourne une TemplateResponse d'erreur si le token est invalide, None sinon."""
    if not _token_ok(email, token):
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Token invalide.", "captain": captain},
            status_code=403,
        )
    return None


# ─── Code PASS — désactivation self-service de la récupération par email+PASS ─

@router.post("/mailjet/pass-toggle")
async def post_pass_toggle(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    disable: str = Form(...),  # "1" = bloquer la restauration, "0" = réactiver
):
    """Bascule le flag .pass.disabled (cf. routers/identity.py::_pass_disabled_flag) :
    bloque/débloque la récupération de compte par email+PASS sur /g1nostr et
    /atom4love/activate, sans détruire le code PASS lui-même (réversible par
    l'utilisateur, contrairement au verrouillage anti-bruteforce à 3 échecs)."""
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    err = _require_token(request, email, token, captain)
    if err:
        return err

    flag = _pass_disabled_flag(email)
    if disable == "1":
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        flag.chmod(0o600)
    else:
        flag.unlink(missing_ok=True)
    logger.info("PASS restore %s pour %s", "désactivé" if disable == "1" else "réactivé", email)

    return RedirectResponse(f"/mailjet?email={email}&token={token}#securite", status_code=303)


@router.post("/mailjet/scraper-toggle")
async def post_scraper_toggle(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    domain: str = Form(...),
    enabled: str = Form(...),
):
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    err = _require_token(request, email, token, captain)
    if err:
        return err

    bro_watch_core.set_scraper_enabled(email, domain, enabled == "1")
    logger.info("Scraper %s %s pour %s", domain, "activé" if enabled == "1" else "désactivé", email)

    return RedirectResponse(f"/mailjet?email={email}&token={token}", status_code=303)


@router.post("/mailjet/scraper-config")
async def post_scraper_config(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    domain: str = Form(...),
    channel: str = Form(...),
    keywords: str = Form(default=""),
    learn_from: str = Form(default=""),
):
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    err = _require_token(request, email, token, captain)
    if err:
        return err

    fields = {
        "keywords": [k.strip() for k in keywords.split(",") if k.strip()],
        "learn_from": learn_from.strip().lstrip("@"),
    }
    bro_watch_core.update_watch_entry(email, domain, channel, **fields)
    logger.info("Config bro_watch %s — %s/%s : %s", email, domain, channel, fields)

    return RedirectResponse(f"/mailjet?email={email}&token={token}", status_code=303)


@router.post("/mailjet/scraper-channels")
async def post_scraper_channels(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    channels: str = Form(default=""),
):
    """Chaînes YouTube (ou équivalent) à surveiller — liste stockée dans le
    sous-canal 'channel_watch' du manifest cookie, donc sauvegardée
    automatiquement en NOSTR comme le reste du manifest (voir
    bro_watch_core._publish_manifest_to_nostr)."""
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    err = _require_token(request, email, token, captain)
    if err:
        return err

    urls = [u.strip() for u in channels.splitlines() if u.strip() and not u.strip().startswith("#")]
    bro_watch_core.update_watch_entry(email, "youtube.com", "channel_watch", watched_channels=urls)
    logger.info("Chaînes YouTube suivies mises à jour pour %s : %d chaîne(s)", email, len(urls))

    return RedirectResponse(f"/mailjet?email={email}&token={token}", status_code=303)


# ─── Mémoire BRO/MUSE — état + réinitialisation self-service ────────────────

@router.post("/mailjet/memory-reset")
async def post_memory_reset(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    scope: str = Form(...),
):
    """Réinitialisation self-service d'un périmètre de mémoire (conversations,
    mémoire BRO, persona, LOVE/MUSE, ou préférences apprises) — chacun contrôle
    ses propres états mémoires, cf. services/memory_status.py::reset_memory."""
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    err = _require_token(request, email, token, captain)
    if err:
        return err
    if scope not in RESET_SCOPES:
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Périmètre de réinitialisation invalide.", "captain": captain},
            status_code=400,
        )

    report = await asyncio.to_thread(reset_memory, email, scope)
    logger.info("Mémoire %s réinitialisée pour %s (%d élément(s))", scope, email, len(report["deleted"]))

    return RedirectResponse(f"/mailjet?email={email}&token={token}#memoire", status_code=303)


@router.post("/mailjet/memory-regenerate")
async def post_memory_regenerate(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
):
    """Régénère le profil LifeOS depuis les propres posts Mastodon — proposé
    uniquement si un cookie mastodon.social est déjà déposé (voir bandeau
    'Régénérer depuis Mastodon' de mailjet_prefs.html). Appel bloquant côté
    Playwright (~15-30s) — page de résultat dédiée plutôt qu'un redirect
    silencieux, pour ne pas laisser l'utilisateur sans nouvelle."""
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    err = _require_token(request, email, token, captain)
    if err:
        return err

    result = await asyncio.to_thread(regenerate_lifeos_from_mastodon, email)
    logger.info("Régénération LifeOS depuis Mastodon pour %s → %s", email, result)

    if not result.get("ok"):
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": result.get("error", "Régénération échouée."), "captain": captain},
            status_code=502,
        )

    found = result.get("own_posts_found", 0)
    return templates.TemplateResponse(request, "mailjet_success.html", {
        "email": email, "token": token,
        "npub": "", "channels": [], "kin_prefs": None,
        "vibe_capture": False,
        "custom_message": f"🐘 Profil LifeOS régénéré depuis {found} post(s) Mastodon récent(s).",
    })


@router.post("/mailjet/identity-save")
async def post_identity_save(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    filename: str = Form(...),
    content: str = Form(default=""),
):
    """Édition self-service directe d'un fichier identity/*.md (profil LifeOS)
    — même fonction Python que l'admin capitaine (services.memory_status.
    save_identity_file → bro.identity.write_identity_file), jamais dupliquée."""
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    err = _require_token(request, email, token, captain)
    if err:
        return err
    if filename not in IDENTITY_FILENAMES:
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": "Fichier identity invalide.", "captain": captain},
            status_code=400,
        )

    ok, msg = await asyncio.to_thread(save_identity_file, email, filename, content)
    logger.info("Identity %s sauvegardé pour %s (ok=%s)", filename, email, ok)
    if not ok:
        return templates.TemplateResponse(
            request,
            "mailjet_error.html",
            {"message": f"Échec de la sauvegarde : {msg}", "captain": captain},
            status_code=500,
        )

    return RedirectResponse(f"/mailjet?email={email}&token={token}#lifeos", status_code=303)


@router.get("/mailjet/nostr-events")
async def get_mailjet_nostr_events(
    email: str = Query(...),
    token: str = Query(...),
):
    """Retourne les événements NOSTR liés à la config mailjet de l'utilisateur.

    Sources :
    - .mailjet  : config locale (préférences, canaux, kin, vibe)
    - kind 31903 d=cookies : manifest BRO/cookie vault (strfry local)
    - kind 30078 : données app-specific (strfry local)
    """
    if not _token_ok(email, token):
        return JSONResponse({"error": "Token invalide."}, status_code=403)

    user_dir = settings.GAME_PATH / "nostr" / email

    # ── Config locale .mailjet ──────────────────────────────────────────────
    mailjet_local: dict = {}
    mailjet_path = user_dir / ".mailjet"
    if mailjet_path.exists():
        try:
            mailjet_local = json.loads(mailjet_path.read_text())
        except Exception:
            pass

    # ── Pubkey HEX de l'utilisateur ────────────────────────────────────────
    hex_pubkey = ""
    hex_file = user_dir / "HEX"
    if hex_file.exists():
        hex_pubkey = hex_file.read_text().strip()
    if not hex_pubkey:
        npub = mailjet_local.get("npub", "")
        if npub:
            try:
                from utils.crypto import npub_to_hex
                hex_pubkey = npub_to_hex(npub)
            except Exception:
                pass

    # ── Événements strfry local (kind 31903 + 30078) ───────────────────────
    nostr_events: list = []
    strfry_dir = settings.ZEN_PATH / "strfry"
    strfry_bin = strfry_dir / "strfry"
    if hex_pubkey and strfry_bin.exists():
        for filt in [
            {"authors": [hex_pubkey], "kinds": [31903], "#d": ["cookies"]},  # 31903 porte aussi d=memory_sources
            {"authors": [hex_pubkey], "kinds": [30078]},
        ]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(strfry_bin), "scan", json.dumps(filt),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=str(strfry_dir),
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
                for line in out.decode().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        try:
                            ev["_content_json"] = json.loads(ev.get("content", ""))
                        except Exception:
                            pass
                        nostr_events.append(ev)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("strfry scan kind %s: %s", filt["kinds"], e)

    nostr_events.sort(key=lambda e: e.get("created_at", 0), reverse=True)

    # ── Décrypter les events NIP-04 auto-chiffrés ─────────────────────────────
    privkey = _parse_secret_nostr(user_dir)
    for ev in nostr_events:
        content = ev.get("content", "")
        if "?iv=" in content and privkey:
            decrypted = _nip04_decrypt_to_self(content, privkey)
            if decrypted:
                try:
                    ev["_content_json"] = json.loads(decrypted)
                except Exception:
                    ev["_content_decrypted"] = decrypted

    # ── Publier si aucun kind 30078 d=mailjet-prefs n'existe encore ──────────
    published_new = None
    has_mailjet_prefs = any(
        any(t[:2] == ["d", "mailjet-prefs"] for t in ev.get("tags", []))
        for ev in nostr_events
        if ev.get("kind") == 30078
    )
    if not has_mailjet_prefs and mailjet_local:
        published_new = await _publish_mailjet_prefs_nostr(user_dir, mailjet_local)

    return JSONResponse({
        "email":              email,
        "hex_pubkey":         hex_pubkey,
        "mailjet_local":      mailjet_local,
        "nostr_events":       nostr_events,
        "event_count":        len(nostr_events),
        "published_new_event": published_new,
    })


# ─── FaceID — catalogue de visages (Qdrant `faces_{hex}`) ───────────────────
#
# Collection alimentée par Astroport.ONE/IA/bro/satellite_face_matcher.py : un
# point par visage vu dans le uDRIVE, payload {name, pubkey, timestamp}. Tant
# qu'un visage n'a pas de `pubkey`, il est nommé `Inconnu_xxxxxxxx` et aucune
# photo n'est partagée. C'est ICI que l'utilisateur lui donne un nom et le relie
# à une clé NOSTR — après quoi le partage automatique n'a lieu que si les deux
# comptes se suivent mutuellement (réciprocité N1 vérifiée côté matcher).
#
# Qdrant est interrogé via son API REST (httpx, déjà une dépendance UPassport)
# plutôt que via qdrant-client, qui n'est pas installé dans cet environnement.

_QDRANT_URL = "http://localhost:6333"

# Même seuil que Astroport.ONE/IA/bro/satellite_face_matcher.py::MATCH_THRESHOLD
# (correspondance automatique) — un point sous ce score reste "à nommer" même
# s'il s'agit en réalité de la même personne (visage vieilli, angle différent,
# éclairage...). _MAYBE_SAME_MIN borne en dessous : sur des archives de
# plusieurs décennies, une même personne à des âges très différents peut
# tomber nettement sous 0.82 sans que ce soit pour autant quelqu'un d'autre —
# la suggestion reste une PROPOSITION que l'utilisateur confirme (miniature à
# l'appui), jamais un ré-étiquetage automatique.
_AUTO_MATCH_THRESHOLD = 0.82
_MAYBE_SAME_MIN = 0.55

# Garde-fous de volume : le catalogue d'une archive importée (WebDAV) peut
# compter plusieurs milliers de visages. Tout le calcul de similarité
# (regroupement, suggestions) est fait en numpy par blocs — plus de plafond
# à 400 visages comme du temps du Python pur O(n²).
_FACES_MAX = 20000
_SCROLL_PAGE = 1000
_PCA_MAX_FACES = 20000
_SIM_BLOCK = 512


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _is_named(name: str, pubkey: str) -> bool:
    """Un visage appartient à une PERSONNE dès qu'il porte une clé NOSTR OU
    un vrai nom — « Inconnu_xxx » (bootstrap du matcher) et « Inconnu »
    (visage retiré d'une personne dans ucloud.html) restent à identifier."""
    return bool(pubkey) or bool(name and not name.startswith("Inconnu"))


def _unit_rows(mat):
    """Normalise les lignes d'une matrice déjà en ndarray (lignes nulles inchangées)."""
    import numpy as np
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _unit_matrix(vectors: list):
    import numpy as np
    mat = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


_GROUP_KNN = 8          # voisins examinés par visage
_GROUP_MAX = 120        # au-delà, un groupe est redécoupé avec un seuil plus strict


def _mutual_knn_groups(xu, idx: list, threshold: float) -> list:
    """Groupes de visages probablement identiques parmi les lignes `idx` de
    `xu` (vecteurs unitaires). Un lien i–j n'existe que si chacun est parmi
    les _GROUP_KNN plus proches de l'autre ET cosinus ≥ `threshold` : un
    simple seuil transitif (version d'origine) enchaînait A–B–C–D… jusqu'à
    des groupes géants mélangeant plusieurs personnes (constaté sur 1215
    visages d'une archive, 2026-10-12). Un groupe encore > _GROUP_MAX est
    redécoupé récursivement avec un seuil relevé de 0,05."""
    import numpy as np
    n = len(idx)
    if n < 2:
        return [idx]
    sub = xu[idx]
    k = min(_GROUP_KNN, n - 1)
    neigh: list = []
    for start in range(0, n, _SIM_BLOCK):
        sims = sub[start:start + _SIM_BLOCK] @ sub.T
        rows = np.arange(sims.shape[0])
        sims[rows, start + rows] = -1.0
        top = np.argpartition(sims, n - k, axis=1)[:, n - k:]      # les k plus grands, sans négation ni copie
        ok = np.take_along_axis(sims, top, axis=1) >= threshold
        for r in range(sims.shape[0]):
            neigh.append(set(top[r][ok[r]].tolist()))
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in neigh[i]:
            if j > i and i in neigh[j]:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    comps: dict = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    out = []
    for members in comps.values():
        real = [idx[i] for i in members]
        if len(real) > _GROUP_MAX and threshold < 0.8:
            out.extend(_mutual_knn_groups(xu, real, threshold + 0.05))
        else:
            out.append(real)
    return out


def _annotate_faces(faces: list, refs: Optional[list] = None) -> None:
    """Calculs de similarité pour GET /mailjet/faces, en place :

    * `maybe` — pour un visage NON identifié, la personne identifiée la plus
      proche quand le score tombe dans [_MAYBE_SAME_MIN, _AUTO_MATCH_THRESHOLD[
      (même personne à un autre âge, autre angle…) : une PROPOSITION que
      l'utilisateur confirme, jamais un ré-étiquetage automatique ;
    * `group_id`/`group_size` — visages non identifiés proches ENTRE EUX
      (k plus proches voisins MUTUELS au même seuil, cf. _mutual_knn_groups) :
      même personne vue sur plusieurs photos, à nommer d'un seul geste ;
    * `x`/`y` — projection PCA 2D normalisée dans [-1, 1].

    Les vecteurs 512D ne quittent jamais ce process ; rien n'est persisté."""
    import numpy as np
    with_vec = [f for f in faces if isinstance(f.get("_vector"), list)]
    if not with_vec:
        return
    unnamed = [f for f in with_vec if not f["_named"]]
    # Candidats de ressemblance : les personnes déjà rangées ET les photos des
    # fiches de contact (`refs`, jamais affichées ni rangées elles-mêmes).
    named = [f for f in with_vec if f["_named"]] + \
        [r for r in (refs or []) if isinstance(r.get("_vector"), list)]

    # UNE conversion liste → ndarray pour tout le catalogue (c'était 3 fois :
    # non nommés, nommés, PCA — la plus coûteuse du calcul sur 20 000 visages).
    raw = np.asarray([f["_vector"] for f in with_vec] + [r["_vector"] for r in (refs or [])
                                                         if isinstance(r.get("_vector"), list)],
                     dtype=np.float32)
    xall = _unit_rows(raw)
    pos = {id(f): i for i, f in enumerate(with_vec)}
    u_idx = np.asarray([pos[id(f)] for f in unnamed], dtype=np.int64)
    n_idx = np.asarray([pos[id(f)] for f in with_vec if f["_named"]]
                       + list(range(len(with_vec), len(xall))), dtype=np.int64)

    if len(u_idx) and len(n_idx):
        xu = xall[u_idx]
        xn = xall[n_idx]
        for start in range(0, len(unnamed), _SIM_BLOCK):
            sims = xu[start:start + _SIM_BLOCK] @ xn.T
            best = sims.argmax(axis=1)
            scores = sims[np.arange(sims.shape[0]), best]
            hit = np.nonzero((scores >= _MAYBE_SAME_MIN) & (scores < _AUTO_MATCH_THRESHOLD))[0]
            for r in hit.tolist():
                ref = named[int(best[r])]
                unnamed[start + r]["maybe"] = {
                    "name": ref["name"], "pubkey": ref["pubkey"], "score": round(float(scores[r]), 3),
                    "contact": ref.get("contact") or "", "email": ref.get("email") or ""}

    if len(u_idx) >= 2:
        xu = xall[u_idx]
        for members in _mutual_knn_groups(xu, list(range(len(unnamed))), _MAYBE_SAME_MIN):
            if len(members) < 2:
                continue
            ids = sorted(str(unnamed[i]["id"]) for i in members)
            group_id = hashlib.sha1("|".join(ids).encode()).hexdigest()[:12]
            for i in members:
                unnamed[i]["group_id"] = group_id
                unnamed[i]["group_size"] = len(members)

    coords = _pca_2d(raw[:len(with_vec)])
    for f, (x, y) in zip(with_vec, coords):
        f["x"], f["y"] = round(x, 4), round(y, 4)


def _pca_2d(vectors) -> list:
    """Projection PCA 2D des embeddings (ndarray n×d ou liste) — positions pour
    la vue « nébuleuse » de ucloud.html. Les vecteurs 512D ne quittent jamais ce
    process : seules des coordonnées x,y normalisées dans [-1, 1] sont
    renvoyées au client. Les deux axes principaux viennent de la covariance
    d×d (eigh, ~20 ms) et non d'un SVD n×d : même résultat (au signe près),
    sans l'ordre de grandeur de temps en plus sur 20 000 visages."""
    n = len(vectors)
    if n < 2:
        return [(0.0, 0.0) for _ in range(n)]
    if n > _PCA_MAX_FACES:
        logger.info("FaceID : projection PCA ignorée (%d visages > %d)", n, _PCA_MAX_FACES)
        return [(0.0, 0.0) for _ in range(n)]
    import numpy as np
    mat = np.asarray(vectors, dtype=np.float32)
    centered = mat - mat.mean(axis=0)
    try:
        cov = (centered.T @ centered).astype(np.float64)
        _w, v = np.linalg.eigh(cov)           # valeurs propres croissantes
    except np.linalg.LinAlgError:
        return [(0.0, 0.0) for _ in range(n)]
    axes = v[:, ::-1][:, :2].astype(np.float32)
    coords = centered @ axes
    if coords.shape[1] < 2:
        coords = np.pad(coords, ((0, 0), (0, 2 - coords.shape[1])))
    max_abs = float(np.abs(coords).max()) or 1.0
    coords = coords / max_abs
    return list(zip(coords[:, 0].tolist(), coords[:, 1].tolist()))


def _qdrant_headers() -> dict:
    """Clé d'API Qdrant — même source que IA/bro/rag.py::_qdrant_client()."""
    try:
        for line in (Path.home() / ".zen" / "ai-company" / ".env").read_text().splitlines():
            if line.startswith("QDRANT_API_KEY="):
                return {"api-key": line.split("=", 1)[1].strip()}
    except Exception:
        pass
    return {}


def _faces_collection(email: str) -> Optional[str]:
    """`faces_{hex}` du MULTIPASS — la collection appartient à l'utilisateur,
    pas à la station (cf. satellite_face_matcher.py)."""
    try:
        hex_pk = (settings.GAME_PATH / "nostr" / email / "HEX").read_text().strip()
    except Exception:
        return None
    return f"faces_{hex_pk}" if len(hex_pk) == 64 else None


def _faces_auth(
    request: Request,
    email: Optional[str],
    token: Optional[str],
) -> tuple[Optional[str], Optional[JSONResponse]]:
    """Authentifie une requête FaceID et retourne (email_autorisé, erreur_JSON).

    Deux credentials acceptés, dans cet ordre :

    1. `Authorization: Nostr <event base64>` (NIP-98, kind 27235) — MÊME
       mécanisme que /api/cloud/enroll et /api/fileupload, vérifié par
       services.nostr._decode_and_verify_nip98_event (id NIP-01 + Schnorr
       BIP-340 + fraîcheur). L'EMAIL est résolu depuis le pubkey vérifié via
       services.cloud_storage.email_for_hex (même source de vérité que
       routers/cloud.py::_email_for_authenticated_npub). Les paramètres
       email/token de la requête sont alors ignorés : c'est la signature qui
       désigne le MULTIPASS.
    2. Repli historique `email` + `token` (sous-système mailjet), conservé pour
       ne pas casser mailjet_prefs.html.

    Variante JSON de _require_token (ces routes sont appelées en AJAX : une
    TemplateResponse HTML y serait illisible côté client).
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.strip().lower().startswith("nostr "):
        from services.nostr import _decode_and_verify_nip98_event
        from services import cloud_storage
        try:
            auth_event = _decode_and_verify_nip98_event(auth_header)
        except Exception as exc:
            logger.warning("FaceID : NIP-98 refusée (%s)", exc)
            return None, JSONResponse({"error": f"NIP-98 invalide : {exc}"}, status_code=403)
        resolved = cloud_storage.email_for_hex(auth_event.get("pubkey", ""))
        if not resolved:
            return None, JSONResponse(
                {"error": "Aucun MULTIPASS sur cette station pour cette clé NOSTR."},
                status_code=404,
            )
        return resolved, None

    if not email or not token:
        return None, JSONResponse(
            {"error": "Authentification requise (NIP-98, ou email+token)."},
            status_code=403,
        )
    if not _token_ok(email, token):
        return None, JSONResponse({"error": "Token invalide."}, status_code=403)
    return email, None


async def _scroll_all_points(collection: str, with_vector: bool) -> list:
    """Tous les points d'une collection (pagination Qdrant `next_page_offset`,
    plafonnée à _FACES_MAX). [] si la collection n'existe pas encore (404).
    Lève RuntimeError si Qdrant répond une erreur."""
    points: list = []
    offset = None
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            body = {"limit": _SCROLL_PAGE, "with_payload": True, "with_vector": with_vector}
            if offset is not None:
                body["offset"] = offset
            resp = await client.post(
                f"{_QDRANT_URL}/collections/{collection}/points/scroll",
                headers=_qdrant_headers(), json=body,
            )
            if resp.status_code == 404:
                return []
            if resp.status_code != 200:
                raise RuntimeError(f"Qdrant scroll HTTP {resp.status_code}")
            # un lot avec vecteurs pèse plusieurs Mo : le décoder dans la boucle uvicorn la figeait
            body_json = await asyncio.to_thread(resp.json) if with_vector else resp.json()
            result = body_json.get("result", {})
            points.extend(result.get("points", []))
            offset = result.get("next_page_offset")
            if offset is None or len(points) >= _FACES_MAX:
                return points


def _build_faces(points: list) -> tuple:
    """Points Qdrant → (visages affichables, références de contact/amis)."""
    faces = []
    refs = []       # visages de RÉFÉRENCE des fiches de contact (routers/contacts.py)
    for point in points:
        payload = point.get("payload") or {}
        name = payload.get("name") or ""
        pubkey = payload.get("pubkey") or ""
        if payload.get("ref") == "shared":
            # empreinte confiée par un ami (services/face_ref.py) : sert à la reconnaître, jamais affichée en visage
            refs.append({"id": point.get("id"), "name": name, "pubkey": pubkey, "_named": True,
                         "contact": "", "email": "", "_vector": point.get("vector")})
            continue
        if payload.get("ref") == "contact":
            refs.append({"id": point.get("id"), "name": name, "pubkey": "", "_named": True,
                         "contact": payload.get("contact") or "", "email": payload.get("email") or "",
                         "_vector": point.get("vector")})
            continue
        faces.append({
            "id":        point.get("id"),
            "name":      name,
            "pubkey":    pubkey,
            "email":     payload.get("email") or "",
            "contact":   payload.get("contact") or "",
            "timestamp": payload.get("timestamp") or "",
            # source_path présent seulement pour les points catalogués après
            # 2026-09-20 — sans lui, aucune miniature ni envoi possible.
            "has_photo": bool(payload.get("source_path")),
            "photo":     payload.get("source_path") or "",
            "sent":      payload.get("sent") or None,
            "_named":    _is_named(name, pubkey),
            "_vector":   point.get("vector"),
        })
    return faces, refs


_ANNOT_FIELDS = ("maybe", "group_id", "group_size", "x", "y")
_ANNOT_CACHE: dict = {}     # collection -> (empreinte, {id: {champ: valeur}})
_ANNOT_LOCKS: dict = {}     # collection -> asyncio.Lock (un seul calcul à la fois)


def _annot_fingerprint(faces: list, refs: list) -> str:
    """Empreinte de ce qui influence _annotate_faces : ids et statut
    nommé/nom/clé (un vecteur est immuable pour un id donné). Inchangée entre
    deux polls tant que le catalogue ne bouge pas."""
    h = hashlib.sha1()
    for f in faces:
        h.update(f"{f.get('id')}|{f.get('_named')}|{f.get('name')}|{f.get('pubkey')};".encode())
    h.update(b"#refs:")
    for f in refs:
        h.update(f"{f.get('id')}|{f.get('name')}|{f.get('pubkey')}|{f.get('contact')}|{f.get('email')};".encode())
    return h.hexdigest()


async def _annotate_faces_cached(collection: str, faces: list, refs: list, email: str,
                                 compute: bool = False) -> bool:
    """_annotate_faces() mémoïsé par catalogue + sérialisé par collection : la
    page interroge /mailjet/faces chaque minute, et le calcul (10–17 s de CPU,
    non annulable dans son thread) s'empilait à chaque poll.

    Sans `compute`, ne fait que RESSERVIR le cache (faces sans vecteurs) et
    rend False s'il n'est pas à jour ; avec `compute` (faces portant leurs
    vecteurs), calcule puis mémorise."""
    fp = _annot_fingerprint(faces, refs)
    lock = _ANNOT_LOCKS.setdefault(collection, asyncio.Lock())
    async with lock:
        cached = _ANNOT_CACHE.get(collection)
        if not (cached and cached[0] == fp):
            if not compute:
                return False
            try:
                await asyncio.to_thread(_annotate_faces, faces, refs)
            except Exception as exc:   # le catalogue reste utilisable sans suggestions
                logger.warning("FaceID : annotation des visages échouée pour %s : %s", email, exc)
                return True
            _ANNOT_CACHE[collection] = (fp, {
                f["id"]: {k: f[k] for k in _ANNOT_FIELDS if k in f} for f in faces})
            return True
    for f in faces:
        f.update(cached[1].get(f["id"], {}))
    return True


@router.get("/mailjet/faces")
async def get_mailjet_faces(
    request: Request,
    email: Optional[str] = Query(default=None),
    token: Optional[str] = Query(default=None),
):
    """Liste les visages catalogués :
    [{id, name, pubkey, email, timestamp, has_photo, photo, sent, maybe,
      group_id, group_size, x, y}].

    `photo` = chemin de la photo source dans le cloud (compte des photos
    d'une personne, cf. « Envoyer ses photos ») ; `sent` = dernier envoi à
    cette personne (POST /mailjet/person-send) ; `maybe`/`group_id`/`x`/`y` :
    cf. _annotate_faces(). Le tableau de tri de ucloud.html est construit
    entièrement sur cette réponse."""
    email, err = _faces_auth(request, email, token)
    if err:
        return err

    collection = _faces_collection(email)
    if not collection:
        return JSONResponse({"faces": [], "reason": "no_hex"})

    try:
        points = await _scroll_all_points(collection, with_vector=False)
    except Exception as exc:
        logger.warning("Qdrant injoignable pour %s : %s", collection, exc)
        return JSONResponse({"faces": [], "reason": "qdrant_unavailable"})

    faces, refs = _build_faces(points)
    truncated = len(points) >= _FACES_MAX
    # Les vecteurs d'un point ne changent jamais : tant que ids / noms / clés
    # sont identiques, ni leur téléchargement ni le calcul ne sont refaits.
    if not await _annotate_faces_cached(collection, faces, refs, email):
        try:
            vpoints = await _scroll_all_points(collection, with_vector=True)
        except Exception as exc:
            logger.warning("Qdrant injoignable pour %s : %s", collection, exc)
            vpoints = []
        if vpoints:
            faces, refs = _build_faces(vpoints)
            await _annotate_faces_cached(collection, faces, refs, email, compute=True)
            truncated = len(vpoints) >= _FACES_MAX


    for f in faces:
        f.pop("_vector", None)
        f.pop("_named", None)
    faces.sort(key=lambda f: (not f["pubkey"], f["name"].lower()))
    return JSONResponse({"faces": faces, "truncated": truncated})


async def _fetch_face_point(collection: str, point_id: str) -> Optional[dict]:
    """Retourne le payload Qdrant d'UN point (name/pubkey/source_path/bbox/…),
    ou None si absent/erreur. Partagé par toutes les routes qui résolvent un
    point_id (thumbnail, invite)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_QDRANT_URL}/collections/{collection}/points",
                headers=_qdrant_headers(),
                json={"ids": [point_id], "with_payload": True, "with_vector": False},
            )
        points = resp.json().get("result", []) if resp.status_code == 200 else []
    except Exception as exc:
        logger.warning("Qdrant get point %s/%s : %s", collection, point_id, exc)
        return None
    return points[0].get("payload") if points else None


async def _fetch_face_points(collection: str, ids: list) -> dict:
    """{point_id: payload} pour plusieurs points (par lots de 256)."""
    out: dict = {}
    async with httpx.AsyncClient(timeout=20) as client:
        for i in range(0, len(ids), 256):
            try:
                resp = await client.post(
                    f"{_QDRANT_URL}/collections/{collection}/points",
                    headers=_qdrant_headers(),
                    json={"ids": ids[i:i + 256], "with_payload": True, "with_vector": False},
                )
            except Exception as exc:
                logger.warning("Qdrant get points %s : %s", collection, exc)
                continue
            if resp.status_code == 200:
                for p in resp.json().get("result", []):
                    out[str(p.get("id"))] = p.get("payload") or {}
    return out


async def _set_points_payload(collection: str, ids: list, payload: dict) -> bool:
    """Fusionne `payload` dans plusieurs points (par lots de 256)."""
    ok = True
    async with httpx.AsyncClient(timeout=20) as client:
        for i in range(0, len(ids), 256):
            try:
                resp = await client.post(
                    f"{_QDRANT_URL}/collections/{collection}/points/payload?wait=true",
                    headers=_qdrant_headers(),
                    json={"payload": payload, "points": ids[i:i + 256]},
                )
                ok = ok and resp.status_code == 200
            except Exception as exc:
                logger.warning("Qdrant set_payload %s : %s", collection, exc)
                ok = False
    return ok


async def _decrypt_source_photo(email: str, source_path: str) -> tuple[bytes, str]:
    """Déchiffre la photo source COMPLÈTE (pas recadrée) : (plaintext, mime).
    Lève HTTPException si la photo/clé est introuvable ou le déchiffrement
    échoue. Partagé par la miniature de visage (recadrée ensuite) et la
    migration de photo lors d'une invitation FaceCloud acceptée (photo
    complète, jamais juste le visage recadré)."""
    from services import cloud_storage
    try:
        entry = await asyncio.to_thread(cloud_storage.db_entry, email, source_path)   # UNE ligne SQLite
    except ValueError:
        entry = None
    if not entry or not entry.get("cid"):
        raise HTTPException(status_code=404, detail="photo source introuvable (supprimée ?)")
    key_hex = cloud_storage.get_key_hex(email, entry["cid"])
    if not key_hex:
        raise HTTPException(status_code=404, detail="clé de déchiffrement introuvable")

    sys.path.insert(0, str(settings.TOOLS_PATH))
    import uenc_codec  # noqa: E402

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            ipfs_resp = await client.post(f"http://127.0.0.1:5001/api/v0/cat?arg={entry['cid']}")
            ipfs_resp.raise_for_status()
        plaintext = uenc_codec.decrypt_aes256gcm(ipfs_resp.content, key_hex)
    except Exception as exc:
        logger.error("FaceID : déchiffrement échoué pour %s (%s): %s", source_path, email, exc)
        raise HTTPException(status_code=502, detail=f"déchiffrement impossible: {type(exc).__name__}")
    return plaintext, (entry.get("mime") or "application/octet-stream")


def _crop_face_jpeg(plaintext: bytes, bbox: Optional[dict],
                    max_side: int = 300, quality: int = 85) -> bytes:
    """Recadre une image quelconque sur `bbox` (marge 50%) et la ré-encode en
    JPEG ≤300px — jamais persisté, juste retourné en mémoire. Lève
    HTTPException si le recadrage échoue (image corrompue, etc.)."""
    try:
        from PIL import Image, ImageOps
        # exif_transpose AVANT crop : bbox a été calculé par ComfyUI (LoadImage
        # normalise l'orientation EXIF avant détection), donc sur une image déjà
        # stockée à plat (photos ingérées avant le correctif cloud_storage.py::
        # ingest_plaintext du 2026-10-03), recadrer sans retransposer pointait
        # au mauvais endroit — défensif, no-op sur une image déjà canonique.
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(plaintext))).convert("RGB")
        if bbox:
            w, h = img.size
            x1 = float(bbox.get("x1", 0)); y1 = float(bbox.get("y1", 0))
            x2 = float(bbox.get("x2", w)); y2 = float(bbox.get("y2", h))
            bw, bh = max(x2 - x1, 1.0), max(y2 - y1, 1.0)
            pad = 0.5  # marge autour du visage : un peu de contexte pour reconnaître
            cx1 = max(0, x1 - bw * pad); cy1 = max(0, y1 - bh * pad)
            cx2 = min(w, x2 + bw * pad); cy2 = min(h, y2 + bh * pad)
            if cx2 > cx1 and cy2 > cy1:
                img = img.crop((int(cx1), int(cy1), int(cx2), int(cy2)))
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"recadrage impossible: {type(exc).__name__}")


def _resize_full_jpeg(plaintext: bytes, max_side: int = 1024) -> bytes:
    """Ré-encode une image quelconque en JPEG ≤max_side px, SANS recadrage —
    contrairement à _crop_face_jpeg, pour l'aperçu photo entière au survol
    (vue nébuleuse, cf. ucloud.html). Jamais persisté, juste retourné."""
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(plaintext))).convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"redimensionnement impossible: {type(exc).__name__}")


def _open_reduced(plaintext: bytes, need_long_side: int):
    """Ouvre une image en ne décodant que la résolution nécessaire (`draft`
    JPEG : réduction 1/2, 1/4, 1/8 DANS le décodeur). Mesuré sur 12 Mpx :
    135 ms en décodage complet contre ~60 ms. Retourne (image RGB orientée,
    facteur décodé/original) — le facteur sert à recaler les bbox."""
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(plaintext))
    orig_long = max(img.size) or 1
    if need_long_side and need_long_side < orig_long:
        ratio = need_long_side / orig_long
        img.draft("RGB", (max(1, int(img.size[0] * ratio)), max(1, int(img.size[1] * ratio))))
    img = ImageOps.exif_transpose(img).convert("RGB")
    return img, max(img.size) / orig_long


def _crop_faces(plaintext: bytes, items: list, size: int) -> dict:
    """Vignettes recadrées de PLUSIEURS visages d'une même photo, décodée une
    seule fois, juste assez finement pour que le plus petit visage atteigne
    `size` px. items = [(point_id, bbox)] → {point_id: jpeg}."""
    from PIL import Image
    probe = Image.open(io.BytesIO(plaintext))
    orig_long = max(probe.size) or 1
    widths = [max(1.0, (float(b.get("x2", 0)) - float(b.get("x1", 0))) * 2) for _pid, b in items if b]
    need = orig_long if not widths else min(orig_long, int(orig_long * size / min(widths)) + 1)
    img, factor = _open_reduced(plaintext, need)
    w, h = img.size
    out = {}
    for pid, bbox in items:
        crop = img
        if bbox:
            x1 = float(bbox.get("x1", 0)) * factor; y1 = float(bbox.get("y1", 0)) * factor
            x2 = float(bbox.get("x2", w / factor)) * factor; y2 = float(bbox.get("y2", h / factor)) * factor
            bw, bh = max(x2 - x1, 1.0), max(y2 - y1, 1.0)
            cx1, cy1 = max(0, x1 - bw * .5), max(0, y1 - bh * .5)
            cx2, cy2 = min(w, x2 + bw * .5), min(h, y2 + bh * .5)
            if cx2 > cx1 and cy2 > cy1:
                crop = img.crop((int(cx1), int(cy1), int(cx2), int(cy2)))
        crop = crop.copy()
        crop.thumbnail((size, size))
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=80)
        out[pid] = buf.getvalue()
    return out


# Cache DISQUE des vignettes de visages, CHIFFRÉ (AES-256-GCM, même codec
# UENC que le cloud) avec une clé propre au compte, 0600 à côté du trousseau :
# jamais de clair sur disque, mais plus besoin de redécoder des centaines de
# photos à chaque redémarrage du service ou rechargement de page.
def _tcache_key(email: str) -> str:
    path = settings.GAME_PATH / "nostr" / email / ".ucloud" / "thumbcache.key"
    try:
        key = path.read_text().strip()
        if len(key) == 64:
            return key
    except OSError:
        pass
    key = os.urandom(32).hex()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


def _tcache_path(email: str, point_id: str, source_path: str, size: int) -> Path:
    name = hashlib.sha256(f"{point_id}|{source_path}|{size}".encode()).hexdigest()[:40]
    return settings.GAME_PATH / "nostr" / email / ".ucloud" / "thumbcache" / f"{name}.uenc"


def _tcache_get(email: str, key: str, path: Path) -> Optional[bytes]:
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    sys.path.insert(0, str(settings.TOOLS_PATH))
    import uenc_codec  # noqa: E402
    try:
        return uenc_codec.decrypt_aes256gcm(blob, key)
    except Exception:
        path.unlink(missing_ok=True)
        return None


def _tcache_put(key: str, path: Path, jpeg: bytes) -> None:
    sys.path.insert(0, str(settings.TOOLS_PATH))
    import uenc_codec  # noqa: E402
    payload, _iv = uenc_codec.encrypt_aes256gcm(jpeg, key)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, path)


# Photos entières redimensionnées (aperçu au survol / visionneuse) : cache
# MÉMOIRE seulement, borné — survoler plusieurs visages d'une même photo de
# groupe ne la redéchiffre qu'une fois.
_PHOTO_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_PHOTO_CACHE_MAX = 80


@router.get("/mailjet/faces/photo")
async def get_face_full_photo(
    request: Request,
    point_id: str = Query(...),
    max_side: int = Query(default=1024),
    mark: int = Query(default=0),
    email: Optional[str] = Query(default=None),
    token: Optional[str] = Query(default=None),
):
    """Photo ENTIÈRE où apparaît le visage `point_id`, déchiffrée à la volée,
    ≤ `max_side` px (1024 pour l'aperçu au survol, jusqu'à 2048 pour la
    visionneuse). `mark=1` entoure le visage d'une ellipse — pour retrouver
    QUI est désigné sur une photo de groupe. Jamais persistée en clair ;
    cache mémoire borné de la photo redimensionnée (non marquée)."""
    email, err = _faces_auth(request, email, token)
    if err:
        return err
    collection = _faces_collection(email)
    if not collection:
        raise HTTPException(status_code=404, detail="no_hex")
    payload = await _fetch_face_point(collection, point_id)
    if not payload:
        raise HTTPException(status_code=404, detail="point introuvable")
    source_path = payload.get("source_path")
    if not source_path:
        raise HTTPException(status_code=404, detail="pas de photo source enregistrée pour ce visage")
    max_side = max(256, min(int(max_side or 1024), 2048))

    key = f"{collection}:{source_path}:{max_side}"
    base = _PHOTO_CACHE.get(key)
    factor = None
    if base is None:
        plaintext, _mime = await _decrypt_source_photo(email, source_path)

        def _resize():
            img, f = _open_reduced(plaintext, max_side)
            img.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            from PIL import Image
            return buf.getvalue(), max(img.size) / (max(Image.open(io.BytesIO(plaintext)).size) or 1)
        base, factor = await asyncio.to_thread(_resize)
        _PHOTO_CACHE[key] = base
        _PHOTO_CACHE[key + ":f"] = str(factor).encode()
        while len(_PHOTO_CACHE) > _PHOTO_CACHE_MAX * 2:
            _PHOTO_CACHE.popitem(last=False)
    else:
        _PHOTO_CACHE.move_to_end(key)
        factor = float((_PHOTO_CACHE.get(key + ":f") or b"1").decode())

    jpeg = base
    bbox = payload.get("bbox") or {}
    if mark and bbox:
        def _mark():
            from PIL import Image, ImageDraw
            img = Image.open(io.BytesIO(base)).convert("RGB")
            x1, y1 = float(bbox.get("x1", 0)) * factor, float(bbox.get("y1", 0)) * factor
            x2, y2 = float(bbox.get("x2", 0)) * factor, float(bbox.get("y2", 0)) * factor
            pad = max(x2 - x1, y2 - y1) * 0.25
            width = max(3, int(max(img.size) / 250))
            ImageDraw.Draw(img).ellipse((x1 - pad, y1 - pad, x2 + pad, y2 + pad),
                                        outline=(91, 143, 240), width=width)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            return buf.getvalue()
        jpeg = await asyncio.to_thread(_mark)

    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=1800"})


@router.get("/mailjet/faces/thumbnail")
async def get_face_thumbnail(
    request: Request,
    point_id: str = Query(...),
    email: Optional[str] = Query(default=None),
    token: Optional[str] = Query(default=None),
):
    """Miniature RECADRÉE sur le visage `point_id`, déchiffrée à la volée.

    Seule façon de reconnaître "Inconnu_xxx" avant de le nommer (cf. FaceCloud,
    section "À nommer"). Jamais persistée en clair : déchiffrement en mémoire
    depuis le blob UENC de la photo source, recadrage, réponse JPEG directe —
    même discipline que le GET /dav/ (cloud_storage.py::get_content).
    """
    email, err = _faces_auth(request, email, token)
    if err:
        return err

    collection = _faces_collection(email)
    if not collection:
        raise HTTPException(status_code=404, detail="no_hex")

    payload = await _fetch_face_point(collection, point_id)
    if not payload:
        raise HTTPException(status_code=404, detail="point introuvable")

    source_path = payload.get("source_path")
    if not source_path:
        raise HTTPException(status_code=404, detail="pas de photo source enregistrée pour ce visage")

    plaintext, _mime = await _decrypt_source_photo(email, source_path)
    jpeg = _crop_face_jpeg(plaintext, payload.get("bbox") or {})

    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=1800"},
    )


_THUMB_WORKERS = 4      # photos sources traitées en parallèle par lot


@router.post("/mailjet/faces/thumbs")
async def post_face_thumbs(
    request: Request,
    point_ids: str = Form(...),
    size: int = Form(default=96),
    email: Optional[str] = Form(default=None),
    token: Optional[str] = Form(default=None),
):
    """Miniatures recadrées de PLUSIEURS visages en un seul appel :
    {thumbs: {point_id: "data:image/jpeg;base64,…"}} — une signature NIP-98
    pour 80 visages. `size` 48–320 (la table demande 96, puis 256 pour les
    visages affichés en grand quand on zoome).

    Coût dominé par le décodage JPEG (mesuré : ipfs cat ~90 ms, AES ~5 ms,
    décodage 12 Mpx ~135 ms) : chaque photo source n'est décodée qu'une fois
    pour tous ses visages du lot, à la résolution juste nécessaire
    (_crop_faces/draft), _THUMB_WORKERS photos en parallèle, et le résultat
    est gardé dans un cache disque CHIFFRÉ propre au compte
    (.ucloud/thumbcache/, clé .ucloud/thumbcache.key, 0600) — un rechargement
    ou un redémarrage du service ne recalcule rien."""
    email, err = _faces_auth(request, email, token)
    if err:
        return err
    collection = _faces_collection(email)
    if not collection:
        return JSONResponse({"thumbs": {}})
    size = max(48, min(int(size or 96), 320))
    ids = [pid.strip() for pid in point_ids.split(",") if pid.strip()][:80]
    payloads = await _fetch_face_points(collection, ids)
    ckey = _tcache_key(email)

    thumbs: dict = {}
    by_photo: dict = {}
    for pid, payload in payloads.items():
        sp = payload.get("source_path")
        if not sp:
            continue
        cpath = _tcache_path(email, pid, sp, size)
        cached = _tcache_get(email, ckey, cpath)
        if cached:
            thumbs[pid] = "data:image/jpeg;base64," + base64.b64encode(cached).decode()
        else:
            by_photo.setdefault(sp, []).append((pid, payload.get("bbox") or {}, cpath))

    sem = asyncio.Semaphore(_THUMB_WORKERS)

    async def one_photo(source_path: str, items: list) -> None:
        async with sem:
            try:
                plaintext, _mime = await _decrypt_source_photo(email, source_path)
                crops = await asyncio.to_thread(_crop_faces, plaintext, [(p, b) for p, b, _c in items], size)
            except Exception as exc:
                logger.warning("FaceID vignettes : %s ignorée (%s)", source_path, exc)
                return
            for pid, _b, cpath in items:
                jpeg = crops.get(pid)
                if not jpeg:
                    continue
                thumbs[pid] = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
                try:
                    await asyncio.to_thread(_tcache_put, ckey, cpath, jpeg)
                except Exception as exc:
                    logger.warning("FaceID vignettes : cache non écrit (%s)", exc)

    await asyncio.gather(*(one_photo(sp, items) for sp, items in by_photo.items()))
    return JSONResponse({"thumbs": thumbs})


def _identity_payload(name: str, pubkey: str, contact_email: str, contact: str = "") -> dict:
    """Payload Qdrant d'identité. `contact_email` / `contact` (nom de la fiche
    du carnet, routers/contacts.py) : vide = inchangé ; "-" = effacé (sinon on ne pourrait jamais corriger une erreur).
    (Le champ `via` des anciens « cercles » n'existe plus : les relations viennent du kind 3.)"""
    payload = {"name": name.strip(), "pubkey": pubkey or None}
    contact = (contact or "").strip()
    if contact == "-":
        payload["contact"] = None
    elif contact:
        payload["contact"] = contact
    contact_email = (contact_email or "").strip().lower()
    if contact_email == "-":
        payload["email"] = None
    elif contact_email and _EMAIL_RE.match(contact_email):
        payload["email"] = contact_email
    return payload


@router.post("/mailjet/faces-edit")
async def post_mailjet_faces_edit(
    request: Request,
    email: Optional[str] = Form(default=None),
    token: Optional[str] = Form(default=None),
    point_id: str = Form(...),
    name: str = Form(...),
    pubkey: str = Form(default=""),
    contact_email: str = Form(default=""),
    contact: str = Form(default=""),
):
    """Nomme un visage et l'associe à une clé NOSTR et/ou un email de contact
    (payload merge, le vecteur est inchangé)."""
    email, err = _faces_auth(request, email, token)
    if err:
        return err

    collection = _faces_collection(email)
    if not collection:
        return JSONResponse({"error": "MULTIPASS sans HEX sur cette station."}, status_code=404)

    pubkey = pubkey.strip().lower()
    if pubkey and (len(pubkey) != 64 or not re.fullmatch(r"[0-9a-f]{64}", pubkey)):
        return JSONResponse({"error": "La clé publique doit être 64 caractères hexadécimaux."},
                            status_code=400)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_QDRANT_URL}/collections/{collection}/points/payload?wait=true",
                headers=_qdrant_headers(),
                json={"payload": _identity_payload(name, pubkey, contact_email, contact),
                      "points": [point_id]},
            )
    except Exception as exc:
        logger.warning("Qdrant set_payload %s : %s", collection, exc)
        return JSONResponse({"error": "Qdrant injoignable."}, status_code=503)

    if resp.status_code != 200:
        return JSONResponse({"error": f"Qdrant HTTP {resp.status_code}"}, status_code=502)

    logger.info("FaceID %s : point %s → name=%r pubkey=%s", email, point_id, name, pubkey[:12])
    return JSONResponse({"ok": True})


@router.post("/mailjet/faces-edit-bulk")
async def post_mailjet_faces_edit_bulk(
    request: Request,
    email: Optional[str] = Form(default=None),
    token: Optional[str] = Form(default=None),
    point_ids: str = Form(...),
    name: str = Form(...),
    pubkey: str = Form(default=""),
    contact_email: str = Form(default=""),
    contact: str = Form(default=""),
):
    """Nomme EN UNE FOIS plusieurs visages 'Inconnu' rassemblés — via le
    `group_id` suggéré par GET /mailjet/faces, ou une sélection manuelle dans
    ucloud.html — plutôt qu'un aller-retour NIP-98 par photo. `point_ids` :
    identifiants séparés par des virgules. Même sémantique que
    /mailjet/faces-edit (payload merge Qdrant, vecteurs inchangés)."""
    email, err = _faces_auth(request, email, token)
    if err:
        return err

    collection = _faces_collection(email)
    if not collection:
        return JSONResponse({"error": "MULTIPASS sans HEX sur cette station."}, status_code=404)

    pubkey = pubkey.strip().lower()
    if pubkey and (len(pubkey) != 64 or not re.fullmatch(r"[0-9a-f]{64}", pubkey)):
        return JSONResponse({"error": "La clé publique doit être 64 caractères hexadécimaux."},
                            status_code=400)

    ids = [pid.strip() for pid in point_ids.split(",") if pid.strip()]
    if not ids:
        return JSONResponse({"error": "Aucun visage sélectionné."}, status_code=400)
    if len(ids) > 2000:
        return JSONResponse({"error": "Trop de visages sélectionnés (2000 max)."}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_QDRANT_URL}/collections/{collection}/points/payload?wait=true",
                headers=_qdrant_headers(),
                json={"payload": _identity_payload(name, pubkey, contact_email, contact),
                      "points": ids},
            )
    except Exception as exc:
        logger.warning("Qdrant set_payload bulk %s : %s", collection, exc)
        return JSONResponse({"error": "Qdrant injoignable."}, status_code=503)

    if resp.status_code != 200:
        return JSONResponse({"error": f"Qdrant HTTP {resp.status_code}"}, status_code=502)

    logger.info("FaceID %s : %d points → name=%r pubkey=%s", email, len(ids), name, pubkey[:12])
    return JSONResponse({"ok": True, "updated": len(ids)})


@router.post("/mailjet/faces-delete")
async def post_mailjet_faces_delete(
    request: Request,
    email: Optional[str] = Form(default=None),
    token: Optional[str] = Form(default=None),
    point_id: str = Form(default=""),
    point_ids: str = Form(default=""),
):
    """Oublie définitivement un ou plusieurs visages (`point_id`, ou
    `point_ids` séparés par des virgules, 2000 max — ex. « Oublier » sur une
    sélection du tableau de tri). Le vecteur est supprimé : il sera
    re-catalogué comme inconnu s'il réapparaît sur une future photo. Les
    photos elles-mêmes restent dans le cloud."""
    email, err = _faces_auth(request, email, token)
    if err:
        return err

    collection = _faces_collection(email)
    if not collection:
        return JSONResponse({"error": "MULTIPASS sans HEX sur cette station."}, status_code=404)

    ids = [pid.strip() for pid in (point_ids or point_id).split(",") if pid.strip()]
    if not ids or len(ids) > 2000:
        return JSONResponse({"error": "Sélection vide ou trop grande (2000 max)."}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_QDRANT_URL}/collections/{collection}/points/delete?wait=true",
                headers=_qdrant_headers(),
                json={"points": ids},
            )
    except Exception as exc:
        logger.warning("Qdrant delete %s : %s", collection, exc)
        return JSONResponse({"error": "Qdrant injoignable."}, status_code=503)

    if resp.status_code != 200:
        return JSONResponse({"error": f"Qdrant HTTP {resp.status_code}"}, status_code=502)

    logger.info("FaceID %s : %d point(s) supprimé(s)", email, len(ids))
    return JSONResponse({"ok": True, "deleted": len(ids)})


# ─── « Envoyer ses photos » — POST /mailjet/person-send (depuis 2026-10-12) ──
#
# Remplace /mailjet/faces-invite (4 photos max, seulement pour un inconnu).
# Le propriétaire désigne une PERSONNE (tous ses visages) et son email ou sa
# clé ; le serveur choisit le canal :
#   - MULTIPASS sur cette station → copie directe (cloud_storage.share_copy) ;
#   - MULTIPASS ailleurs (swarm, tools/search_for_this_email_in_nostr.sh) →
#     messages fichier NIP-17 (satellite_face_matcher.py --send) ;
#   - aucune identité nulle part → email d'invitation à créer un MULTIPASS,
#     avec un jeton de "claim" en attente (7 jours) portant TOUTES les photos.
#
# Si la personne crée ensuite son MULTIPASS sur CETTE station avec la même
# adresse email, process_pending_face_claims() (appelée en tâche de fond juste
# après chaque création réussie, cf. routers/identity.py::scan_qr) complète la
# migration. Dans tous les cas : photos COMPLÈTES (pas juste le visage
# recadré), rechiffrées avec une clé propre au destinataire — l'expéditeur
# garde sa copie intacte, ce n'est jamais un déplacement.
#
# ⚠️ Le jeton en attente est stocké sous ~/.zen/game/nostr/{owner}/.face_invites/
# (durable), PAS sous ~/.zen/tmp/ qui est purgé quotidiennement et détruirait
# silencieusement l'invitation avant l'échéance de 7 jours annoncée par email.

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FACE_INVITE_TTL = 7 * 24 * 3600  # 7 jours — aligné sur mailjet.sh --expire 7d


def _face_invites_dir(owner_email: str) -> Path:
    return settings.GAME_PATH / "nostr" / owner_email / ".face_invites"


def _face_invite_path(owner_email: str, token: str) -> Path:
    # `token` vient de secrets.token_urlsafe (déjà alphanumérique/URL-safe),
    # mais on borne quand même le nom de fichier par défense en profondeur.
    safe = re.sub(r"[^A-Za-z0-9_-]", "", token)[:64]
    return _face_invites_dir(owner_email) / f"{safe}.json"


async def _search_email_in_nostr_hex(email: str) -> Optional[str]:
    """Résout un email en HEX NOSTR en cherchant LOCAL, puis CACHE (TW de cette
    station), puis SWARM (tools/search_for_this_email_in_nostr.sh) — jamais
    limité à ~/.zen/game/nostr/ de cette seule station : un MULTIPASS peut
    avoir été créé ailleurs dans la constellation. Retourne None si aucune
    identité n'est trouvée nulle part."""
    hex_val = await _search_email_via_swarm_files(email)
    if hex_val:
        return hex_val
    # Les fichiers du swarm ne couvrent que ce que cette station a synchronisé : le relais commun
    # (profils kind 0 portant ["i","email:…"]) répond pour TOUTE la constellation.
    from services import face_invites
    try:
        acct = await asyncio.to_thread(face_invites.account_for_email, email)
    except face_invites.RelayUnavailable:
        return None
    return acct["hex"] if acct else None


async def _search_email_via_swarm_files(email: str) -> Optional[str]:
    script = settings.TOOLS_PATH / "search_for_this_email_in_nostr.sh"
    if not script.exists():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script), email,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except Exception as exc:
        logger.warning("search_for_this_email_in_nostr.sh(%s) : %s", email, exc)
        return None
    match = re.search(r"\bHEX=(\S+)", stdout.decode("utf-8", "ignore"))
    hex_val = match.group(1) if match else None
    return hex_val if hex_val and len(hex_val) == 64 else None


def _owner_display_name(email: str) -> str:
    """Nom de l'expéditeur tel qu'il apparaît chez le destinataire
    (/Reçues/<nom>/…) — même convention que satellite_face_matcher.py."""
    return email.split("@", 1)[0]


def _local_hex_for_email(email: str) -> Optional[str]:
    """HEX du MULTIPASS `email` s'il est hébergé sur CETTE station."""
    try:
        hex_val = (settings.GAME_PATH / "nostr" / email / "HEX").read_text().strip()
    except OSError:
        return None
    return hex_val if len(hex_val) == 64 else None


async def _copy_photos_to(owner_email: str, photo_refs: list,
                          recipient_email: str) -> dict:
    """Copie chaque photo dans le cloud de `recipient_email` (même station),
    via le canal UNIQUE cloud_storage.share_copy (rechiffrée, rangée sous
    /Reçues/<expéditeur>/, idempotente par contenu, sans analyse FaceID chez
    le destinataire). Best-effort par photo."""
    from services import cloud_storage
    owner_hex = _local_hex_for_email(owner_email) or ""
    counts = {"copied": 0, "duplicate": 0, "error": 0}
    for ref in photo_refs:
        try:
            res = await asyncio.to_thread(
                cloud_storage.share_copy, owner_email, ref["source_path"],
                recipient_email, owner_hex, _owner_display_name(owner_email))
            counts[res["status"]] = counts.get(res["status"], 0) + 1
        except Exception as exc:
            counts["error"] += 1
            logger.warning("FaceCloud : copie %s → %s échouée (%s)", ref.get("source_path"), recipient_email, exc)
    return counts


async def _mail(to_email: str, subject: str, body_html: str, attach: Optional[list] = None,
                expire: Optional[str] = None) -> bool:
    """Un email via tools/mailjet.sh (canal facecloud-invite)."""
    mailjet_sh = settings.TOOLS_PATH / "mailjet.sh"
    if not mailjet_sh.exists():
        return False
    html_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(body_html)
            html_path = f.name
        cmd = [str(mailjet_sh), "--channel", "facecloud-invite"]
        cmd += ["--expire", expire] if expire else ["--no-ipfs-link"]
        for af in attach or []:
            cmd += ["--attach", af]
        cmd += [to_email, html_path, subject]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0
    except Exception as exc:
        logger.warning("FaceCloud : email à %s échoué (%s)", to_email, exc)
        return False
    finally:
        if html_path:
            Path(html_path).unlink(missing_ok=True)


async def _send_local_and_record(owner_email: str, collection: str, ids: list,
                                 photo_refs: list, recipient_email: str) -> None:
    """Tâche de fond de « Envoyer ses photos » (destinataire sur cette station) :
    copie, mémorise l'envoi sur les points (`sent`, affiché dans ucloud.html),
    puis UN SEUL email récapitulatif au destinataire — jamais un message par
    photo."""
    counts = await _copy_photos_to(owner_email, photo_refs, recipient_email)
    await _set_points_payload(collection, ids, {"sent": {
        "at": int(time.time()), "mode": "local",
        "photos": counts["copied"] + counts["duplicate"], "errors": counts["error"]}})
    logger.info("FaceCloud : %s → %s : %s", owner_email, recipient_email, counts)
    if counts["copied"]:
        who = _owner_display_name(owner_email)
        n = counts["copied"]
        base = degafam.base_url()
        await _mail(recipient_email, f"📸 {who} vous a envoyé {n} photo{'s' if n > 1 else ''}", degafam.layout(
            f"{n} photo{'s' if n > 1 else ''} de vous, chez vous",
            f"<b>{html.escape(who)}</b> vous a reconnu·e sur des photos et vous en a envoyé une copie. "
            f"Elles vous attendent dans votre cloud chiffré, dossier <code>/Reçues/{html.escape(who)}/</code>.",
            ["<b>Ouvrez FaceCloud</b> (bouton ci-dessous).",
             f"<b>Retrouvez-les</b> dans l'onglet Photos → « Reçues ».",
             f"<b>Rendez la pareille</b> : FaceCloud peut retrouver {html.escape(who)} dans vos propres photos."],
            "Voir mes photos", f"{base}/earth/ucloud.html#photos",
            f"Et vos autres photos, contacts et agenda ? <a href=\"{base}/earth/degafam.html\">Le guide pas à pas</a> "
            "les met à l'abri en trois étapes. Chaque copie est chiffrée avec votre propre clé.",
            preheader=f"{who} vous a reconnu·e sur des photos.", unsubscribe=False))


async def _send_remote(owner_email: str, collection: str, ids: list, photo_refs: list,
                       recipient_hex: str, name: str) -> bool:
    """Destinataire sur une AUTRE station. Station connue de la constellation
    (services/identity_links.py) → canal inter-NODE : sa station range les
    photos dans son cloud (services/node_share.py), aucun message humain.
    Station inconnue → message fichier NIP-17 par photo
    (satellite_face_matcher.py --send), en dernier recours."""
    from services import identity_links, node_share
    owner_hex = _local_hex_for_email(owner_email)
    if not owner_hex:
        return False
    account = identity_links.email_for_hex(recipient_hex)
    paths = [r["source_path"] for r in photo_refs]
    if account and account.get("where") == "swarm" and node_share.node_hex_for(account.get("node", "")):
        async def _bg() -> None:
            res = await asyncio.to_thread(node_share.send, owner_email, _owner_display_name(owner_email),
                                          account, paths)
            await _set_points_payload(collection, ids, {"sent": {
                "at": int(time.time()), "mode": "node", "photos": res.get("photos", 0)}})
        task = asyncio.create_task(_bg())
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return True

    matcher = Path.home() / ".zen" / "Astroport.ONE" / "IA" / "bro" / "satellite_face_matcher.py"
    if not matcher.exists():
        return False
    log = open(Path.home() / ".zen" / "tmp" / "facecloud_send.log", "ab")
    await asyncio.create_subprocess_exec(sys.executable, str(matcher), "--send", owner_email, owner_hex,
                                         recipient_hex, name or "?", *paths, stdout=log, stderr=log,
                                         stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    log.close()
    await _set_points_payload(collection, ids, {"sent": {
        "at": int(time.time()), "mode": "remote", "photos": len(photo_refs)}})
    return True


async def _publish_portable_invite(owner_email: str, token: str) -> bool:
    """Publie l'invitation locale `token` sur le relais commun : n'importe quelle station de la
    constellation pourra l'honorer (et saura si l'email a déjà un MULTIPASS ailleurs). Si aucun
    relais ne l'accepte, l'invitation reste locale (elle ne marchera que sur CETTE station)."""
    from services import face_invites
    try:
        data = json.loads(_face_invite_path(owner_email, token).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    relays = await asyncio.to_thread(face_invites.publish, owner_email, token, data, _owner_display_name(owner_email))
    if not relays:
        logger.warning("FaceCloud : invitation %s… non publiée sur le relais — valable sur cette station seulement", token[:6])
    return bool(relays)


async def _send_invite(owner_email: str, collection: str, ids: list, photo_refs: list,
                       to_email: str) -> Optional[str]:
    """Aucun MULTIPASS nulle part pour `to_email` : jeton d'invitation durable
    portant TOUTES les photos (plus seulement 4), email avec un aperçu
    recadré des premières. process_pending_face_claims() fera la copie à
    l'inscription. Retourne None si OK, sinon un message d'erreur."""
    attach_files: list = []
    for ref in photo_refs[:4]:
        try:
            plaintext, _mime = await _decrypt_source_photo(owner_email, ref["source_path"])
            jpeg = _crop_face_jpeg(plaintext, ref.get("bbox") or {})
        except HTTPException:
            continue
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(jpeg)
        tmp.close()
        attach_files.append(tmp.name)

    invite_token = secrets.token_urlsafe(24)
    invites_dir = _face_invites_dir(owner_email)
    invites_dir.mkdir(parents=True, exist_ok=True)
    _face_invite_path(owner_email, invite_token).write_text(json.dumps({
        "owner_email": owner_email, "collection": collection, "to_email": to_email,
        "photos": photo_refs, "created_at": int(time.time()),
        "gen": facecloud_funnel.generation_of(owner_email),
        "expires_at": int(time.time()) + _FACE_INVITE_TTL,
    }), encoding="utf-8")
    await _publish_portable_invite(owner_email, invite_token)
    facecloud_funnel.record("created", invite_token, gen=facecloud_funnel.generation_of(owner_email),
                            mode="email", sender=owner_email)

    n = len(photo_refs)
    who = _owner_display_name(owner_email)
    signup_url = f"{str(settings.uSPOT).rstrip('/')}/i/{invite_token}"
    sent = await _mail(to_email, f"📸 {who} vous a reconnu·e sur {n} photo{'s' if n > 1 else ''}", degafam.layout(
        f"{who} vous a reconnu·e sur {n} photo{'s' if n > 1 else ''}",
        "… et vous les offre. Un aperçu est joint à ce message.",
        ["<b>Cliquez sur le bouton</b> ci-dessous.",
         "<b>Validez cette adresse email</b> — pas de mot de passe à inventer, une minute suffit.",
         "<b>Vos photos sont là</b>, chiffrées : vous seul·e y avez accès."],
        "Voir mes photos", signup_url,
        f"🎁 Gratuit, rien à installer, et vous gardez la main : tout est supprimable. Valable 7 jours, une seule fois. "
        f"Et si vous avez des photos de {html.escape(who)}, vous pourrez les lui offrir en retour 😉",
        preheader=f"{who} vous offre {n} photo{'s' if n > 1 else ''} où vous apparaissez.", unsubscribe=False),
        attach=attach_files, expire="7d")
    for af in attach_files:
        Path(af).unlink(missing_ok=True)
    if not sent:
        _face_invite_path(owner_email, invite_token).unlink(missing_ok=True)
        return "Échec de l'envoi de l'invitation"
    await _set_points_payload(collection, ids, {"sent": {
        "at": int(time.time()), "mode": "invited", "photos": n}})
    return None


async def _photo_refs_for(collection: str, ids: list) -> list:
    """Une entrée par PHOTO (dédoublonnée) portée par les visages `ids`."""
    payloads = await _fetch_face_points(collection, ids)
    refs, seen = [], set()
    for pid in ids:
        payload = payloads.get(pid) or {}
        sp = payload.get("source_path")
        if sp and sp not in seen:
            seen.add(sp)
            refs.append({"point_id": pid, "source_path": sp, "bbox": payload.get("bbox")})
    return refs


_BG_TASKS: set = set()


@router.post("/mailjet/person-invite-link")
async def post_mailjet_person_invite_link(
    request: Request,
    point_ids: str = Form(...),
    name: str = Form(default=""),
    email: Optional[str] = Form(default=None),
    token: Optional[str] = Form(default=None),
):
    """« Inviter par un lien » — pour WhatsApp, Telegram, SMS, Signal, AirDrop, QR…

    Même idée que l'invitation par email, SANS adresse : un jeton ouvert, à usage UNIQUE, valable
    7 jours, qui porte TOUTES les photos des visages `point_ids`. La personne ouvre le lien
    (`ucloud.html#demarrer&claim=<jeton>`), voit un aperçu de ses visages, saisit SON adresse email,
    et ses photos arrivent dans son MULTIPASS (`process_pending_face_claims(email, claim_token)`).
    Le jeton est un secret au porteur : à n'envoyer qu'à la personne concernée (l'interface le rappelle).
    Rend `{url, photos, expires_at}`."""
    owner_email, err = _faces_auth(request, email, token)
    if err:
        return err
    collection = _faces_collection(owner_email)
    if not collection:
        return JSONResponse({"error": "MULTIPASS sans HEX sur cette station."}, status_code=404)
    ids = [pid.strip() for pid in point_ids.split(",") if pid.strip()]
    if not ids or len(ids) > 2000:
        return JSONResponse({"error": "Sélection vide ou trop grande (2000 max)."}, status_code=400)
    photo_refs = await _photo_refs_for(collection, ids)
    if not photo_refs:
        return JSONResponse({"error": "Aucune photo exploitable pour cette personne."}, status_code=404)

    invite_token = secrets.token_urlsafe(24)
    _face_invites_dir(owner_email).mkdir(parents=True, exist_ok=True)
    expires = int(time.time()) + _FACE_INVITE_TTL
    _face_invite_path(owner_email, invite_token).write_text(json.dumps({
        "owner_email": owner_email, "collection": collection, "to_email": "", "open": True,
        "photos": photo_refs, "created_at": int(time.time()), "expires_at": expires,
        "gen": facecloud_funnel.generation_of(owner_email),
    }), encoding="utf-8")
    portable = await _publish_portable_invite(owner_email, invite_token)
    facecloud_funnel.record("created", invite_token, gen=facecloud_funnel.generation_of(owner_email),
                            mode="link", sender=owner_email)
    if name.strip():
        await _set_points_payload(collection, ids, {"name": name.strip()})
    await _set_points_payload(collection, ids, {"sent": {
        "at": int(time.time()), "mode": "link", "photos": len(photo_refs)}})
    url = f"{str(settings.uSPOT).rstrip('/')}/i/{invite_token}"
    logger.info("[person-invite-link] %s : %d photo(s), lien ouvert créé", owner_email, len(photo_refs))
    return JSONResponse({"ok": True, "url": url, "photos": len(photo_refs), "expires_at": expires,
                         "portable": portable})


@router.post("/mailjet/person-send")
async def post_mailjet_person_send(
    request: Request,
    point_ids: str = Form(...),
    name: str = Form(default=""),
    pubkey: str = Form(default=""),
    to_email: str = Form(default=""),
    email: Optional[str] = Form(default=None),
    token: Optional[str] = Form(default=None),
):
    """« Envoyer ses photos » — UN geste pour donner à une personne TOUTES les
    photos où apparaissent les visages `point_ids` (une personne du tableau
    de tri de ucloud.html), quel que soit son statut :

    * MULTIPASS sur CETTE station (clé ou email) → copie directe dans son
      cloud (`/Reçues/<expéditeur>/`), un seul email récapitulatif ;
    * MULTIPASS ailleurs dans la constellation → messages fichier NIP-17 ;
    * aucun MULTIPASS → invitation par email (jeton 7 jours, TOUTES les
      photos copiées à l'inscription).

    Action EXPLICITE du propriétaire : pas de condition de suivi mutuel
    (elle reste exigée pour le partage AUTOMATIQUE du matcher). L'identité
    (nom/clé/email) est mémorisée sur les visages au passage."""
    owner_email, err = _faces_auth(request, email, token)
    if err:
        return err
    collection = _faces_collection(owner_email)
    if not collection:
        return JSONResponse({"error": "MULTIPASS sans HEX sur cette station."}, status_code=404)
    owner_hex = collection[len("faces_"):]

    ids = [pid.strip() for pid in point_ids.split(",") if pid.strip()]
    if not ids or len(ids) > 2000:
        return JSONResponse({"error": "Sélection vide ou trop grande (2000 max)."}, status_code=400)
    pubkey = pubkey.strip().lower()
    if pubkey and not re.fullmatch(r"[0-9a-f]{64}", pubkey):
        return JSONResponse({"error": "La clé publique doit être 64 caractères hexadécimaux."}, status_code=400)
    to_email = to_email.strip().lower()
    if to_email and not _EMAIL_RE.match(to_email):
        return JSONResponse({"error": "Adresse email invalide"}, status_code=400)
    if not pubkey and not to_email:
        return JSONResponse({"error": "Indiquez l'email de la personne."}, status_code=400)

    photo_refs = await _photo_refs_for(collection, ids)
    if not photo_refs:
        return JSONResponse({"error": "Aucune photo exploitable pour cette personne."}, status_code=404)

    from services import cloud_storage
    recipient_email = recipient_hex = None
    if pubkey:
        recipient_hex = pubkey
        recipient_email = cloud_storage.email_for_hex(pubkey)
    elif to_email:
        local_hex = _local_hex_for_email(to_email)
        if local_hex:
            recipient_email, recipient_hex = to_email, local_hex
        else:
            # Annuaire de la constellation d'abord (instantané, aucune requête),
            # recherche réseau ensuite (tools/search_for_this_email_in_nostr.sh).
            from services import identity_links
            exact = [a for a in identity_links.accounts_for_emails([to_email]) if a["match"] == "exact"]
            recipient_hex = exact[0]["hex"] if exact else await _search_email_in_nostr_hex(to_email)
    if recipient_hex == owner_hex:
        return JSONResponse({"error": "C'est votre propre visage."}, status_code=400)

    identity = {}
    if name.strip():
        identity["name"] = name.strip()
    if recipient_hex:
        identity["pubkey"] = recipient_hex
    if to_email:
        identity["email"] = to_email
    if identity:
        await _set_points_payload(collection, ids, identity)

    n = len(photo_refs)
    if recipient_email:
        task = asyncio.create_task(_send_local_and_record(owner_email, collection, ids, photo_refs, recipient_email))
        _BG_TASKS.add(task)   # référence forte : sinon la tâche peut être ramassée en cours
        task.add_done_callback(_BG_TASKS.discard)
        mode = "local"
    elif recipient_hex:
        if not await _send_remote(owner_email, collection, ids, photo_refs, recipient_hex, name.strip()):
            return JSONResponse({"error": "Envoi vers une autre station indisponible."}, status_code=502)
        mode = "remote"
    else:
        problem = await _send_invite(owner_email, collection, ids, photo_refs, to_email)
        if problem:
            return JSONResponse({"error": problem}, status_code=502)
        mode = "invited"
    logger.info("[person-send] %s → %s/%s : %d photo(s), mode=%s",
                owner_email, to_email or "-", (recipient_hex or "-")[:12], n, mode)
    return JSONResponse({"ok": True, "mode": mode, "photos": n, "pubkey": recipient_hex or ""})


async def process_pending_face_claims(email: str, claim_token: str = "") -> None:
    """Hook additif appelé en tâche de fond (fire-and-forget, cf.
    routers/identity.py::scan_qr) juste après une création MULTIPASS réussie
    SUR CETTE STATION.

    Parcourt les invitations FaceCloud en attente de TOUS les comptes locaux
    (~/.zen/game/nostr/*/.face_invites/*.json — stockage durable, cf.
    post_mailjet_faces_invite) et, pour celles dont `to_email` correspond et
    qui ne sont pas expirées, copie les photos COMPLÈTES (déchiffrées puis
    rechiffrées avec une clé propre au nouveau MULTIPASS) dans son cloud
    chiffré, et marque le visage correspondant comme identifié chez
    l'expéditeur d'origine.

    Ne couvre que le cas où la personne invitée crée son MULTIPASS SUR CETTE
    STATION : si une identité existait déjà ailleurs (autre station, swarm),
    c'est post_mailjet_faces_invite qui l'a déjà traité immédiatement à
    l'envoi (cf. _search_email_in_nostr_hex) — ce hook ne fait qu'attraper le
    cas restant : email totalement inconnu au moment de l'invitation.

    Ne lève JAMAIS d'exception vers l'appelant et ne bloque jamais : une panne
    ici ne doit en aucun cas faire échouer une création de compte."""
    try:
        # `_scan_qr_impl` (routers/identity.py) résout le dossier MULTIPASS avec
        # l'email TEL QUE SOUMIS (pas de .lower()) — on doit utiliser exactement
        # la même casse ici pour retrouver le bon dossier/HEX, la comparaison
        # avec l'invitation restant elle insensible à la casse.
        raw_email = (email or "").strip()
        email_lc = raw_email.lower()
        nostr_root = settings.GAME_PATH / "nostr"
        if not raw_email or not nostr_root.exists():
            return

        hex_file = nostr_root / raw_email / "HEX"
        if not hex_file.exists():
            return
        claimant_hex = hex_file.read_text().strip()
        if len(claimant_hex) != 64:
            return

        now = int(time.time())
        from services import face_invites
        handled: set = set()

        for invite_file in list(nostr_root.glob("*/.face_invites/*.json")):
            try:
                data = json.loads(invite_file.read_text(encoding="utf-8"))
            except Exception:
                invite_file.unlink(missing_ok=True)
                continue

            # Invitation par email : l'adresse doit correspondre. Invitation par LIEN (« open », usage unique) :
            # seul le porteur du jeton exact la réclame, avec n'importe quelle adresse.
            by_link = bool(data.get("open")) and bool(claim_token) and invite_file.stem == claim_token
            if not by_link and (data.get("to_email") or "").strip().lower() != email_lc:
                continue
            if now > int(data.get("expires_at", 0)):
                invite_file.unlink(missing_ok=True)
                continue

            owner_email = data.get("owner_email")
            collection = data.get("collection")
            photos = data.get("photos") or []
            if not owner_email:
                invite_file.unlink(missing_ok=True)
                continue

            counts = await _copy_photos_to(owner_email, photos, raw_email)
            ids = [str(p["point_id"]) for p in photos if p.get("point_id")]
            if collection and ids:
                await _set_points_payload(collection, ids, {
                    "pubkey": claimant_hex,
                    "sent": {"at": int(time.time()), "mode": "local",
                             "photos": counts["copied"] + counts["duplicate"]}})
            invite_file.unlink(missing_ok=True)
            handled.add(invite_file.stem)
            await asyncio.to_thread(face_invites.mark_claimed, invite_file.stem, raw_email)
            logger.info("FaceID claim : %s → %s (%s, inscription locale)",
                        owner_email, raw_email, counts)

        # Invitation créée sur UNE AUTRE station : elle n'existe que sur le relais commun.
        if claim_token and claim_token not in handled:
            record = await asyncio.to_thread(face_invites.fetch, claim_token)
            if record and (record.get("open") or (record.get("to_email") or "").strip().lower() == email_lc):
                counts = await asyncio.to_thread(face_invites.adopt_items, raw_email, record)
                await asyncio.to_thread(face_invites.mark_claimed, claim_token, raw_email)
                logger.info("FaceID claim : %s → %s (%s, invitation du relais)",
                            record.get("owner_email"), raw_email, counts)
    except Exception as exc:
        logger.warning("process_pending_face_claims(%s) : erreur inattendue (%s)", email, exc)


@router.get("/mailjet/scraper-log", response_class=HTMLResponse)
async def get_scraper_log(
    request: Request,
    email: str = Query(...),
    token: str = Query(...),
    domain: str = Query(...),
):
    if not _token_ok(email, token):
        return HTMLResponse("Token invalide.", status_code=403)

    log_text = _scraper_log_tail(email, domain) or "(aucun log pour l'instant — le scraper n'a pas encore tourné)"
    safe_text = (log_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return HTMLResponse(
        f"<pre style='background:#0a0f14;color:#9fdfae;padding:16px;white-space:pre-wrap;"
        f"word-break:break-word;font-size:0.78rem;font-family:monospace;border-radius:6px;'>"
        f"{safe_text}</pre>"
    )
