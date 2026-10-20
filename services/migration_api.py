"""
services/migration_api.py — Point d'entrée UTILISATEUR du déménagement d'un MULTIPASS (routers/account.py).

Parcours « QR + PASS » : la personne scanne son QR et saisit son PASS sur la NOUVELLE station (roaming,
check_ssss.sh : clés re-dérivées, compte marqué .roaming), puis demande ici le déménagement. Le serveur
UPassport ne signe JAMAIS rien : la preuve de la demande est faite par `multipass_migrate.py`, un process
court qui lit le NSEC du compte (même règle que calendar_nostr_bridge.py).

    start_from_qr(qr, pass)  onglet « Déménager » de /scan : QR P1 + PASS saisis sur la NOUVELLE station (sans compte
                             connecté) ; renvoie un ticket qui seul donne accès au suivi
    status(email)            état lisible (sans la clé du paquet), progression de l'épinglage
    start(email, from_node)  envoie la demande à l'ancienne station (--request) ; erreur lisible sinon
    abort(email)             annule une demande non aboutie
    resume_pin(email)        relance l'épinglage des documents (process détaché, états claimed/pin_error)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import settings
from services import migration

SCRIPT = settings.BASE_DIR / "multipass_migrate.py"
LOG_DIR = Path.home() / ".zen" / "log"

# Étapes lisibles pour l'interface, dans l'ordre du protocole.
STEPS = {
    "requested": "Demande envoyée à l'ancienne station",
    "imported": "Données importées — publication en cours",
    "claiming": "Publication de votre espace IPFS (IPNS)",
    "claimed": "Compte installé — rapatriement des documents",
    "done": "Terminé : votre compte est sur cette station",
    "pin_error": "Compte installé — documents à rapatrier à nouveau",
    "error": "Échec de l'import — rien n'a été modifié chez l'ancienne station",
}


def _cli(args: List[str], timeout: int = 150, stdin: Optional[str] = None) -> Tuple[int, str, str]:
    p = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=timeout,
                       input=stdin)
    return p.returncode, p.stdout, p.stderr


def _error(err: str) -> str:
    """Dernière ligne ERROR:migration:… de la CLI, sans le préfixe technique."""
    for line in reversed(err.strip().splitlines()):
        if line.startswith("ERROR:migration:"):
            return line[len("ERROR:migration:"):].strip()
    return (err.strip().splitlines() or ["échec inconnu"])[-1][:300]


# Étapes vues par la personne, dans l'ordre du protocole : (id, libellé).
STEP_DEFS = [
    ("recognized", "Compte reconnu avec votre QR"),
    ("accepted", "Ancienne station : PASS vérifié, compte gelé"),
    ("received", "Données reçues et vérifiées"),
    ("published", "Espace IPFS publié sous la même adresse"),
    ("documents", "Documents rapatriés sur cette station"),
    ("notified", "Vous et les Capitaines prévenus par e-mail"),
]
# état → (étapes terminées, index de l'étape en cours | None, index de l'étape en échec | None, % de base)
_PROGRESS = {
    "requested": (1, 1, None, 5),
    "imported": (3, 3, None, 40),
    "claiming": (3, 3, None, 50),
    "claimed": (4, 4, None, 50),
    "pin_error": (4, None, 4, 50),
    "done": (5, 5, None, 100),
    "error": (2, None, 2, 0),
}


def _steps(state: str, notified_ok: Optional[bool]) -> List[Dict[str, str]]:
    done, active, failed, _ = _PROGRESS.get(state, (0, None, None, 0))
    out = []
    for i, (sid, label) in enumerate(STEP_DEFS):
        if i == failed:
            st = "error"
        elif i < done:
            st = "done"
        elif i == active:
            st = "active"
        else:
            st = "pending"
        if sid == "notified" and state == "done":
            st = "done" if notified_ok else "warn"        # « warn » : déménagement réussi, e-mail non parti
        out.append({"id": sid, "label": label, "status": st})
    return out


def _message(state: str, pin: Optional[Dict[str, Any]], errors: List[str]) -> str:
    if state == "requested":
        return "En attente de l'ancienne station (vérification du PASS, export de votre compte)…"
    if state in ("imported", "claiming"):
        return "Vos données sont arrivées ; publication de votre espace IPFS en cours…"
    if state == "claimed":
        n = (pin or {}).get("total")
        return (f"Votre compte est installé ici. Rapatriement de {n} documents en cours — vous pouvez déjà vous servir "
                "de votre compte.") if n else "Votre compte est installé ici. Rapatriement des documents en cours…"
    if state == "pin_error":
        return "Votre compte est installé, mais certains documents n'ont pas pu être rapatriés : relancez le rapatriement."
    if state == "done":
        return "Terminé : votre compte et tous ses documents sont sur cette station. L'ancienne station est prévenue."
    if state == "error":
        return "Le déménagement a échoué avant tout changement chez l'ancienne station : " + (errors[0] if errors else "voir le journal")
    return ""


def status(email: str) -> Dict[str, Any]:
    st = migration.load_state(email)
    if not st or st.get("role") != "target":
        return {"state": "none", "role": None, "frozen": False}
    state = st.get("state", "")
    pin = st.get("pin") or None
    errors = [str(e)[:300] for e in (st.get("errors") or [])]
    notified = st.get("notified_arrival") or {}
    notified_ok = bool(notified) and all(v == "ok" for v in notified.values())
    base = _PROGRESS.get(state, (0, None, None, 0))[3]
    percent = base
    if state in ("claimed", "pin_error") and pin and pin.get("total"):
        percent = base + int(49 * min(1.0, (pin.get("pinned", 0) + pin.get("already", 0)) / pin["total"]))
    out: Dict[str, Any] = {
        "state": state, "role": "target", "label": STEPS.get(state, state),
        "frozen": state in ("requested", "imported", "claiming", "error"),
        "from_node": str(st.get("from_node", ""))[:12], "at": st.get("at"), "started": st.get("started"),
        "pin": pin,
        "steps": _steps(state, notified_ok), "percent": percent, "message": _message(state, pin, errors),
        "finished": state == "done", "failed": state in ("error", "pin_error"),
        "can_abort": state in ("requested", "error"), "can_retry_pin": state in ("claimed", "pin_error"),
    }
    if state == "done":
        out["notified"] = {k: ("ok" if v == "ok" else "échec") for k, v in notified.items()}
    for key in ("pin_error", "pin_failed_cids", "fixups_warnings"):
        if st.get(key):
            out[key] = st[key]
    if errors:
        out["errors"] = errors
    return out                          # jamais `key`, `cid` ni `value` du paquet


def start(email: str, from_node: Optional[str] = None, pass_code: str = "") -> Dict[str, Any]:
    """Envoie la demande. Le PASS (vérifié par l'ANCIENNE station) est transmis à la CLI par l'entrée standard."""
    cur = status(email)
    if cur["state"] not in ("none",):
        return {"ok": False, "error": f"un déménagement est déjà en cours ({cur['label']})", "status": cur}
    args = ["--request", email] + (["--pass-stdin"] if pass_code else [])
    if from_node:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", from_node):
            return {"ok": False, "error": "station d'origine invalide (64 caractères hexadécimaux attendus)"}
        args += ["--from-node", from_node.lower()]
    rc, out, err = _cli(args, stdin=(pass_code + "\n") if pass_code else None)
    if rc != 0:
        return {"ok": False, "error": _error(err)}
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "réponse illisible de multipass_migrate.py"}
    return {"ok": True, "result": res, "status": status(email)}


def abort(email: str) -> Dict[str, Any]:
    rc, out, err = _cli(["--abort", email], timeout=60)
    return {"ok": True, "status": status(email)} if rc == 0 else {"ok": False, "error": _error(err)}


def resume_pin(email: str) -> Dict[str, Any]:
    st = migration.load_state(email)
    if st.get("role") != "target" or st.get("state") not in ("claimed", "pin_error"):
        return {"ok": False, "error": f"épinglage impossible dans l'état « {st.get('state', 'aucun')} »"}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"migrate_pin_{email}.log", "ab") as fh:
        subprocess.Popen([sys.executable, str(SCRIPT), "--pin", email], stdout=fh, stderr=fh,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "started": True}


# ═════════════════════════════════════════════════════════════════════════════
# Parcours « QR P1 + PASS » (onglet « Déménager » de /scan) — aucun compte connecté, aucune signature NIP-98 :
# la personne prouve qu'elle est la propriétaire par la part P1 de son billet MULTIPASS (QR « M-… », secret) ET
# son PASS ; ce dernier est vérifié par l'ANCIENNE station avec le compteur anti-bruteforce (3 échecs = verrou).
# ═════════════════════════════════════════════════════════════════════════════
CHECK_SSSS = settings.BASE_DIR / "check_ssss.sh"
MBASE58 = settings.TOOLS_PATH / "Mbase58.py"
# « M-<base58 de la part P1>:<NOSTRNS> » (make_NOSTRCARD.sh : Mbase58.py encode '<part>:<NOSTRNS>' ; le suffixe
# après « : » reste en clair)
QR_RE = re.compile(r"^M-[1-9A-HJ-NP-Za-km-z]{60,300}:k51[a-z0-9]{40,70}$")
HEAD_RE = re.compile(r"^[1-3]-[a-f0-9]{100,}$")
NS_RE = re.compile(r"^k51[a-z0-9]{40,70}$")
PASS_RE = re.compile(r"^\d{4,8}$")
IP_LIMIT, IP_WINDOW = 5, 3600           # démarrages par IP et par heure
EMAIL_COOLDOWN = 60                     # secondes entre deux démarrages pour un même compte
_throttle_lock = threading.Lock()
_by_ip: Dict[str, List[float]] = {}
_by_email: Dict[str, float] = {}


class QrError(Exception):
    pass


def decode_qr_p1(text: str) -> Tuple[str, str]:
    """« M-… » (QR P1 du billet MULTIPASS) → (part SSSS joueur « 1-<hex> », NOSTRNS « k51… »). Entrée et sortie
    strictement validées : rien d'autre que ces deux formes n'atteint un script."""
    t = (text or "").strip()
    if not QR_RE.fullmatch(t):
        raise QrError("QR P1 invalide : le code lu n'est pas celui d'un billet MULTIPASS (« M-… »)")
    p = subprocess.run([sys.executable, str(MBASE58), "decode", t[2:]], capture_output=True, text=True, timeout=20)
    out = p.stdout.strip()
    head, _, ns = out.partition(":")
    if p.returncode != 0 or not HEAD_RE.fullmatch(head) or not NS_RE.fullmatch(ns):
        raise QrError("QR P1 illisible ou altéré")
    return head, ns


def hosted_here(nostrns: str) -> Optional[str]:
    """Email du compte DÉJÀ hébergé ici (même NOSTRNS, pas un visiteur .roaming), sinon None."""
    for f in cloud_storage_path().glob("*@*/NOSTRNS"):
        try:
            same = f.read_text().strip().rsplit("/", 1)[-1] == nostrns
        except OSError:
            continue
        if same and not (f.parent / ".roaming").exists():
            return f.parent.name
    return None


def cloud_storage_path() -> Path:
    from services import cloud_storage
    return cloud_storage.GAME_NOSTR_PATH


def _hydrate(head: str, nostrns: str) -> Dict[str, Any]:
    """Roaming (check_ssss.sh, mode json) : NOSTRNS → part publique → DISCO → clés vérifiées contre les références
    publiées. La part P1 passe par l'entrée standard."""
    p = subprocess.run(["bash", str(CHECK_SSSS), nostrns, "-", "", "json"], input=head + "\n", capture_output=True,
                       text=True, timeout=240, cwd=str(settings.BASE_DIR))
    for line in reversed(p.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"valid": False, "reason": "réponse illisible de check_ssss.sh"}


def _throttled(ip: str) -> bool:
    now = time.time()
    with _throttle_lock:
        hits = [t for t in _by_ip.get(ip, []) if now - t < IP_WINDOW]
        if len(hits) >= IP_LIMIT:
            _by_ip[ip] = hits
            return True
        hits.append(now)
        _by_ip[ip] = hits
    return False


def _cooldown(email: str) -> bool:
    now = time.time()
    with _throttle_lock:
        if now - _by_email.get(email, 0) < EMAIL_COOLDOWN:
            return True
        _by_email[email] = now
    return False


def _fail(code: str, error: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "code": code, "error": error, **extra}


def start_from_qr(qr: str, pass_code: str, from_node: Optional[str] = None, ip: str = "?") -> Dict[str, Any]:
    """QR P1 + PASS → compte hydraté (roaming) → demande de déménagement. Retourne {ok, email, ticket, status} ou
    {ok: False, code, error}. Le ticket (aléatoire, seul son hachage est conservé) donne accès au suivi."""
    if _throttled(ip):
        return _fail("throttled", "Trop de tentatives depuis cette adresse : réessayez dans une heure.")
    pass_code = (pass_code or "").strip()
    if not PASS_RE.fullmatch(pass_code):
        return _fail("bad_pass", "Code PASS invalide (4 chiffres).")
    try:
        head, nostrns = decode_qr_p1(qr)
    except QrError as exc:
        return _fail("bad_qr", str(exc))
    here = hosted_here(nostrns)
    if here:
        return _fail("hosted_here", "Ce MULTIPASS est déjà hébergé sur cette station : rien à déménager.")
    hyd = _hydrate(head, nostrns)
    if not hyd.get("valid"):
        reason = str(hyd.get("reason", "échec"))
        if "already hosted" in reason:
            return _fail("hosted_here", "Ce MULTIPASS est déjà hébergé sur cette station : rien à déménager.")
        return _fail("hydrate_failed", "Impossible de reconnaître ce MULTIPASS avec ce QR (station d'origine "
                     "injoignable, QR altéré, ou compte d'une autre UPlanet).", detail=reason)
    email = str(hyd["email"])
    if _cooldown(email):
        return _fail("throttled", "Une demande vient d'être faite pour ce compte : patientez une minute.")
    res = start(email, from_node, pass_code)
    if not res["ok"]:
        return _fail("request_failed", res["error"])
    ticket = secrets.token_urlsafe(16)
    _write_ticket(email, ticket)
    return {"ok": True, "email": email, "ticket": ticket, "status": status(email)}


def _ticket_file(email: str) -> Path:
    return cloud_storage_path() / email / ".migration.ticket"


def _write_ticket(email: str, ticket: str) -> None:
    """Seul le HACHAGE du ticket est conservé, dans un fichier À PART (.migration.json est réécrit en entier par
    d'autres processus : un champ ajouté ici pourrait être perdu). 0600, écrit par le serveur seul."""
    import os
    path = _ticket_file(email)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(hashlib.sha256(ticket.encode()).hexdigest())


def _ticket_ok(email: str, ticket: str) -> bool:
    try:
        want = _ticket_file(email).read_text().strip()
    except OSError:
        return False
    got = hashlib.sha256(str(ticket or "").encode()).hexdigest()
    return bool(want) and hmac.compare_digest(want, got)


def status_for_ticket(email: str, ticket: str) -> Optional[Dict[str, Any]]:
    """Suivi public : None si le ticket ne correspond pas (même réponse qu'un compte inconnu : pas d'oracle)."""
    return status(email) if _ticket_ok(email, ticket) else None


def resume_pin_for_ticket(email: str, ticket: str) -> Optional[Dict[str, Any]]:
    return resume_pin(email) if _ticket_ok(email, ticket) else None


def abort_for_ticket(email: str, ticket: str) -> Optional[Dict[str, Any]]:
    if not _ticket_ok(email, ticket):
        return None
    res = abort(email)
    if res.get("ok"):
        _ticket_file(email).unlink(missing_ok=True)
    return res
