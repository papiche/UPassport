"""
Route /mailjet — Gestion des opt-outs notifications UPlanet.
Auth NIP-42 (Schnorr BIP-340 pur Python) + détection roaming MULTIPASS.
Écrit ~/.zen/game/nostr/$email/.mailjet (JSON) comme marqueur.
Vérifié par Astroport.ONE/tools/mailjet.sh avant tout envoi.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from core.config import settings
from services.memory_status import get_memory_status, reset_memory, RESET_SCOPES, regenerate_lifeos_from_mastodon
from utils.crypto import verify_nostr_event as _verify_nostr_event
from utils.crypto import _pt_mul, _SECP256K1_G as _G  # ECDH auto-chiffrement NIP-04 ci-dessous

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


def _check_roaming(npub: str, hex_pk: str = "") -> str:
    """
    Retourne 'local' | 'roaming' | 'unknown'.
    local   : MULTIPASS géré sur cette station.
    roaming : MULTIPASS présent dans le swarm mais pas localement.
    unknown : inconnu dans la constellation.
    """
    nostr_local = settings.GAME_PATH / "nostr"
    for f in nostr_local.glob("*/NPUB"):
        try:
            v = f.read_text().strip()
            if v == npub or (hex_pk and v == hex_pk):
                return "local"
        except Exception:
            continue
    swarm = Path.home() / ".zen" / "tmp" / "swarm"
    if swarm.exists():
        for f in swarm.glob("**/NPUB"):
            try:
                v = f.read_text().strip()
                if v == npub or (hex_pk and v == hex_pk):
                    return "roaming"
            except Exception:
                continue
    return "unknown"


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


_KIN_ALL_TYPES = ["quartet", "occult", "analog", "tone", "guide", "antipode"]
_KIN_TYPE_LABELS = {
    "quartet":  ("💎", "Quatuors",     "4 personnes dont les profils se complètent entièrement"),
    "occult":   ("🌙", "Paires Occultes", "2 personnes dont les profils s'additionnent à 261"),
    "analog":   ("🌀", "Paires Analogues", "Même tonalité, domaines complémentaires"),
    "tone":     ("🎵", "Conseils de Tonalité", "Groupe partageant le même rythme galactique"),
    "guide":    ("🧭", "Relations Guide", "Relation mentor / guidé dans la même famille"),
    "antipode": ("⚡", "Paires Antipode", "Défi créateur — opposition qui renforce"),
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
        body = await request.json()
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
    state = _check_roaming(npub, hex_pk)
    logger.info("NIP-42 auth npub=%s… state=%s", npub[:16], state)

    if state == "local":
        email = _email_from_npub(npub) or _email_from_npub(hex_pk)
        if email:
            return JSONResponse({"status": "ok", "redirect": f"/mailjet?email={email}&token={_token_for(email)}"})
        return JSONResponse({"status": "ok", "redirect": viewer_url})
    if state == "roaming":
        return JSONResponse({"status": "roaming", "viewer": viewer_url,
                             "station": "un autre nœud UPlanet",
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
            "mailjet_error.html",
            {"request": request, "message": "Lien de questionnaire invalide.", "captain": captain},
            status_code=400,
        )
    if _token_for(email) != token:
        return templates.TemplateResponse(
            "mailjet_error.html",
            {"request": request, "message": "Token invalide.", "captain": captain},
            status_code=403,
        )
    return templates.TemplateResponse("mailjet_questionnaire.html", {
        "request":   request,
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
    if not email or not token or _token_for(email) != token:
        return templates.TemplateResponse(
            "mailjet_error.html",
            {"request": request, "message": "Token invalide.", "captain": captain},
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
):
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"

    if not email:
        return templates.TemplateResponse("mailjet_landing.html", {"request": request})

    expected = _token_for(email)
    if token and token != expected:
        return templates.TemplateResponse(
            "mailjet_error.html",
            {"request": request, "message": "Lien invalide ou expiré.", "captain": captain},
            status_code=403,
        )

    # ── Captation continue : mise à jour silencieuse d'une réponse de vibe ───
    if vibeq and vibea and token == expected:
        _update_vibe_answer(email, vibeq, vibea)
        return templates.TemplateResponse("mailjet_success.html", {
            "request": request, "email": email, "token": expected,
            "npub": "", "channels": [], "kin_prefs": None,
            "vibe_capture": True,
        })

    current = _read_optout(email)
    kin = current.get("kin", {})

    # ── Onboarding : rediriger vers le questionnaire si aucune réponse encore ─
    if not kin.get("vibe_answers") and not onboarded:
        return RedirectResponse(f"/mailjet/questionnaire?email={email}&token={expected}", status_code=303)

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

    return templates.TemplateResponse("mailjet_prefs.html", {
        "request":              request,
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
            "mailjet_error.html",
            {"request": request, "message": "Paramètres manquants.", "captain": captain},
            status_code=400,
        )
    if _token_for(email) != token:
        return templates.TemplateResponse(
            "mailjet_error.html",
            {"request": request, "message": "Token invalide.", "captain": captain},
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

    return templates.TemplateResponse("mailjet_success.html", {
        "request":   request,
        "email":     email,
        "token":     token,
        "npub":      npub or "",
        "channels":  channels,
        "kin_prefs": kin_prefs,
    })


# ─── Scrapers BRO — activer/désactiver, paramètres, logs ─────────────────────

def _require_token(request: Request, email: str, token: str, captain: str):
    """Retourne une TemplateResponse d'erreur si le token est invalide, None sinon."""
    if _token_for(email) != token:
        return templates.TemplateResponse(
            "mailjet_error.html",
            {"request": request, "message": "Token invalide.", "captain": captain},
            status_code=403,
        )
    return None


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
            "mailjet_error.html",
            {"request": request, "message": "Périmètre de réinitialisation invalide.", "captain": captain},
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
            "mailjet_error.html",
            {"request": request, "message": result.get("error", "Régénération échouée."), "captain": captain},
            status_code=502,
        )

    found = result.get("own_posts_found", 0)
    return templates.TemplateResponse("mailjet_success.html", {
        "request": request, "email": email, "token": token,
        "npub": "", "channels": [], "kin_prefs": None,
        "vibe_capture": False,
        "custom_message": f"🐘 Profil LifeOS régénéré depuis {found} post(s) Mastodon récent(s).",
    })


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
    if _token_for(email) != token:
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
            {"authors": [hex_pubkey], "kinds": [31903]},
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


@router.get("/mailjet/scraper-log", response_class=HTMLResponse)
async def get_scraper_log(
    request: Request,
    email: str = Query(...),
    token: str = Query(...),
    domain: str = Query(...),
):
    if _token_for(email) != token:
        return HTMLResponse("Token invalide.", status_code=403)

    log_text = _scraper_log_tail(email, domain) or "(aucun log pour l'instant — le scraper n'a pas encore tourné)"
    safe_text = (log_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return HTMLResponse(
        f"<pre style='background:#0a0f14;color:#9fdfae;padding:16px;white-space:pre-wrap;"
        f"word-break:break-word;font-size:0.78rem;font-family:monospace;border-radius:6px;'>"
        f"{safe_text}</pre>"
    )
