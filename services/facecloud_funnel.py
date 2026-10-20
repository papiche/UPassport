"""
services/facecloud_funnel.py — Entonnoir viral de FaceCloud : mesurer avant de croire à un « K > 1 ».

Une ligne JSON par étape franchie, dans ~/.zen/log/facecloud_funnel.jsonl (journal permanent, pas
~/.zen/tmp/ purgé chaque nuit). On ne garde JAMAIS le jeton d'invitation (secret au porteur) ni un
email en clair : seulement des empreintes courtes.

Étapes d'une invitation :
  created    l'expéditeur crée le lien / l'email                       (gen = génération de l'EXPÉDITEUR)
  preview    un robot (WhatsApp, Telegram…) lit l'aperçu du lien
  visit      un humain ouvre le lien (/i/<jeton>)
  opened     la page d'accueil a chargé l'invitation (API)
  started    l'invité clique « créer mon MULTIPASS »
  claimed    MULTIPASS créé grâce à l'invitation                      (gen = génération du NOUVEAU compte)
  delivered  l'invité avait déjà un compte : les photos lui sont arrivées, aucun nouveau compte

Génération : 0 = inscrit hors invitation ; un compte créé par l'invitation d'un compte de génération g est de
génération g+1 (`.facecloud_origin.json` dans le dossier du MULTIPASS).

Le « K » honnête = part des invités devenus expéditeurs × invitations par expéditeur × taux de réclamation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from core.config import settings

logger = logging.getLogger(__name__)

STEPS = ("created", "preview", "visit", "opened", "started", "claimed", "delivered")
LOG_PATH = Path.home() / ".zen" / "log" / "facecloud_funnel.jsonl"
MAX_BYTES = 4_000_000            # au-delà, on ne garde que la moitié la plus récente
_lock = threading.Lock()
_BOT_MARKERS = ("bot", "crawler", "spider", "whatsapp", "facebookexternalhit", "slurp", "preview",
                "embed", "skypeuripreview", "applebot", "linkedin", "pinterest", "vkshare", "okhttp")


def _short(value: str, salt: str) -> str:
    return hashlib.sha256((salt + "\0" + (value or "")).encode()).hexdigest()[:12]


def invite_ref(token: str) -> str:
    return _short(token, "funnel-invite")


def account_ref(email: str) -> str:
    return _short((email or "").strip().lower(), "funnel-account")


def is_bot(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    return not ua or any(m in ua for m in _BOT_MARKERS)


# ── génération d'un compte ────────────────────────────────────────────────────
def _origin_path(email: str) -> Path:
    return settings.GAME_PATH / "nostr" / email / ".facecloud_origin.json"


def generation_of(email: str) -> int:
    """0 si ce MULTIPASS n'est pas né d'une invitation (ou n'est pas hébergé ici)."""
    try:
        return int(json.loads(_origin_path(email).read_text(encoding="utf-8")).get("gen", 0))
    except (OSError, ValueError, TypeError):
        return 0


def remember_origin(email: str, parent_gen: int, from_name: str) -> int:
    """Note qu'un MULTIPASS vient d'être créé par invitation ; rend sa génération."""
    gen = int(parent_gen or 0) + 1
    try:
        p = _origin_path(email)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"gen": gen, "from": from_name, "at": int(time.time())}), encoding="utf-8")
        p.chmod(0o600)
    except OSError as exc:
        logger.warning("facecloud_funnel: origine de %s non écrite (%s)", account_ref(email), exc)
    return gen


# ── journal ───────────────────────────────────────────────────────────────────
def record(step: str, token: str, gen: int = 0, mode: str = "", sender: str = "", account: str = "") -> None:
    """Ajoute une étape. Échoue toujours en silence : mesurer ne doit jamais casser un parcours."""
    if step not in STEPS or not token:
        return
    line = {"t": int(time.time()), "step": step, "h": invite_ref(token), "gen": int(gen or 0)}
    if mode:
        line["mode"] = mode
    if sender:
        line["s"] = account_ref(sender)
    if account:
        line["a"] = account_ref(account)
    try:
        with _lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
            if LOG_PATH.stat().st_size > MAX_BYTES:
                rows = LOG_PATH.read_text(encoding="utf-8").splitlines()
                LOG_PATH.write_text("\n".join(rows[len(rows) // 2:]) + "\n", encoding="utf-8")
    except Exception as exc:                       # noqa: BLE001
        logger.warning("facecloud_funnel: %s non journalisée (%s)", step, exc)


def _read(since: float) -> List[Dict[str, Any]]:
    try:
        rows = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for r in rows:
        try:
            d = json.loads(r)
        except ValueError:
            continue
        if d.get("t", 0) >= since:
            out.append(d)
    return out


def stats(days: int = 30) -> Dict[str, Any]:
    """Comptes par étape (invitations DISTINCTES), par génération, et le K mesuré."""
    days = max(1, min(int(days), 365))
    rows = _read(time.time() - days * 86400)
    seen: Dict[str, set] = {s: set() for s in STEPS}
    by_mode: Dict[str, int] = {}
    created_by_sender: Dict[str, set] = {}
    claimed_accounts: Dict[str, int] = {}           # compte → génération
    claimed_by_gen: Dict[int, int] = {}
    for d in rows:
        step, h = d.get("step"), d.get("h")
        if step not in seen or not h:
            continue
        if h in seen[step]:
            continue
        seen[step].add(h)
        if step == "created":
            by_mode[d.get("mode") or "?"] = by_mode.get(d.get("mode") or "?", 0) + 1
            if d.get("s"):
                created_by_sender.setdefault(d["s"], set()).add(h)
        elif step == "claimed":
            g = int(d.get("gen", 1))
            claimed_by_gen[g] = claimed_by_gen.get(g, 0) + 1
            if d.get("a"):
                claimed_accounts[d["a"]] = g
    counts = {s: len(seen[s]) for s in STEPS}
    created, claimed = counts["created"], counts["claimed"]
    recruiters = [a for a in claimed_accounts if a in created_by_sender]
    invites_by_recruiters = sum(len(created_by_sender[a]) for a in recruiters)

    def ratio(a: float, b: float) -> Any:
        return round(a / b, 3) if b else None

    share_rate = ratio(len(recruiters), len(claimed_accounts))
    per_recruiter = ratio(invites_by_recruiters, len(recruiters))
    claim_rate = ratio(claimed, created)
    k = round(share_rate * per_recruiter * claim_rate, 3) if None not in (share_rate, per_recruiter, claim_rate) else None
    return {
        "days": days, "counts": counts, "created_by_mode": by_mode,
        "rates": {"visit_per_created": ratio(counts["visit"], created),
                  "opened_per_visit": ratio(counts["opened"], counts["visit"]),
                  "started_per_opened": ratio(counts["started"], counts["opened"]),
                  "claimed_per_started": ratio(claimed, counts["started"]),
                  "claimed_per_created": claim_rate},
        "claimed_by_generation": {str(g): n for g, n in sorted(claimed_by_gen.items())},
        "viral": {"new_accounts": len(claimed_accounts), "became_senders": len(recruiters),
                  "share_rate": share_rate, "invites_per_sender": per_recruiter, "k": k},
    }
