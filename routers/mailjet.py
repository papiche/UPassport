"""
Route /mailjet — Gestion des opt-outs notifications UPlanet.
Auth NIP-42 (Schnorr BIP-340 pur Python) + détection roaming MULTIPASS.
Écrit ~/.zen/game/nostr/$email/.mailjet (JSON) comme marqueur.
Vérifié par Astroport.ONE/tools/mailjet.sh avant tout envoi.
"""
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from core.config import settings

templates = Jinja2Templates(directory="templates")

router = APIRouter()
logger = logging.getLogger(__name__)

# ─── Challenges NIP-42 (mémoire locale, TTL 5 min) ────────────────────────────
_challenges: dict[str, float] = {}  # challenge_hex → expiry_timestamp

# ─── secp256k1 / BIP-340 Schnorr (pur Python) ────────────────────────────────

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _pt_add(P, Q):
    if P is None: return Q
    if Q is None: return P
    x1, y1 = P; x2, y2 = Q
    if x1 == x2:
        if y1 != y2: return None
        lam = 3 * x1 * x1 * pow(2 * y1, _P - 2, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    return x3, (lam * (x1 - x3) - y1) % _P


def _pt_mul(P, n):
    R = None
    for i in range(256):
        if (n >> i) & 1: R = _pt_add(R, P)
        P = _pt_add(P, P)
    return R


def _lift_x(x: int):
    if x >= _P: return None
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if pow(y, 2, _P) != y_sq: return None
    return x, (y if y % 2 == 0 else _P - y)


def _tagged_hash(tag: str, data: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + data).digest()


def _schnorr_verify(msg: bytes, pk32: bytes, sig64: bytes) -> bool:
    """Vérifie une signature Schnorr BIP-340."""
    if len(pk32) != 32 or len(sig64) != 64: return False
    P = _lift_x(int.from_bytes(pk32, "big"))
    if P is None: return False
    r = int.from_bytes(sig64[:32], "big")
    s = int.from_bytes(sig64[32:], "big")
    if r >= _P or s >= _N: return False
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", sig64[:32] + pk32 + msg), "big"
    ) % _N
    R = _pt_add(_pt_mul(_G, s), _pt_mul(P, _N - e))
    return R is not None and R[1] % 2 == 0 and R[0] == r


def _verify_nostr_event(ev: dict) -> bool:
    """Vérifie l'ID (SHA-256) et la signature Schnorr d'un événement NOSTR."""
    try:
        serial = json.dumps(
            [0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
            separators=(",", ":"), ensure_ascii=False,
        )
        if ev.get("id") != hashlib.sha256(serial.encode()).hexdigest():
            return False
        return _schnorr_verify(
            bytes.fromhex(ev["id"]),
            bytes.fromhex(ev["pubkey"]),
            bytes.fromhex(ev["sig"]),
        )
    except Exception:
        return False


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


def _write_prefs(email: str, channels: list[str], npub: str = "",
                 kin_prefs: dict | None = None,
                 flux_prefs: dict | None = None) -> None:
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
    p.write_text(json.dumps(data, indent=2))
    logger.info("Mailjet prefs %s → channels=%s kin=%s flux=%s npub=%s",
                email, channels, kin_prefs, flux_prefs, npub or "-")


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

    _write_prefs(email, channels, npub or "", kin_prefs, flux_prefs)
    logger.info("Mailjet prefs saved for %s — channels=%s kin=%s flux=%s",
                email, channels, kin_prefs, flux_prefs)

    return templates.TemplateResponse("mailjet_success.html", {
        "request":   request,
        "email":     email,
        "token":     token,
        "npub":      npub or "",
        "channels":  channels,
        "kin_prefs": kin_prefs,
    })
