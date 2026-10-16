"""
services/visits.py — Compteur de visites de l'essaim UPlanet.

Tout site peut embarquer le compteur (assets/visits.js) : la visite arrive en POST /visit sur la station la plus proche.
  1. Chaque station compte elle-même (une IP = une visite par jour et par site) et publie ses totaux, signés par sa clé NODE
     (`~/.zen/game/secret.nostr`), en un événement kind 10600 (#t visit_counter). Kind 10600 est remplaçable : le dernier gagne.
  2. La station bootstrap (première de A_boostrap_nodes.txt, comme pour ORACLE.refresh.sh) additionne les totaux des stations
     de l'essaim et publie « la parole de l'essaim » : kind 10600 (#t swarm_visits) signé par la clé ORACLE
     (`~/.zen/game/uplanet.G1.nostr`). Si VISITS_SWARM_PERMIT est défini (ex. PERMIT_DRAGON), seules comptent les stations dont
     le Capitaine détient une accréditation ORACLE valide (kind 30503, d=<permis>, p=<capitaine>).

Seul un hash (sel secret + jour + site + IP) est conservé, jamais l'IP.
CLI (cron quotidien) : `python3 -m services.visits station` puis, sur la station bootstrap, `python3 -m services.visits swarm`.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from core.config import settings

logger = logging.getLogger(__name__)

BASE = settings.ZEN_PATH / "tmp" / "visits"
SALT_FILE = settings.GAME_PATH / ".visit_salt"
NODE_KEYFILE = settings.GAME_PATH / "secret.nostr"
ORACLE_KEYFILE = settings.GAME_PATH / "uplanet.G1.nostr"
SENDER = settings.TOOLS_PATH / "nostr_send_note.py"
MAX_SITES = int(os.environ.get("VISITS_MAX_SITES", "500"))   # sites suivis par station (anti-pollution)
TOP_SITES = 100                                             # sites publiés par événement (taille < 128 Ko)
KEEP_DAYS = 14
SWARM_PERMIT = os.environ.get("VISITS_SWARM_PERMIT", "")

_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalize_site(raw) -> Optional[str]:
    """Nom d'hôte en minuscules, ou None s'il n'est pas un domaine valide (jamais de chemin ni de port)."""
    site = raw.strip().lower().rstrip(".") if isinstance(raw, str) else ""
    return site if _HOST.match(site) else None


def _salt() -> str:
    if not SALT_FILE.exists():
        SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(SALT_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:  # création simultanée par un autre worker
            pass
    return SALT_FILE.read_text().strip()


def count_visit(site: str, ip: str) -> Optional[bool]:
    """True : nouvelle visite du jour. False : déjà comptée. None : site refusé (trop de sites suivis)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    h = hashlib.sha256(f"{_salt()}:{day}:{site}:{ip}".encode()).hexdigest()[:20]
    BASE.mkdir(parents=True, exist_ok=True)
    with open(BASE / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = BASE / site
        if not d.exists():
            if sum(1 for p in BASE.iterdir() if p.is_dir()) >= MAX_SITES:
                return None
            d.mkdir()
        seen = d / f"{day}.seen"
        if seen.exists() and h in seen.read_text().split():
            return False
        with seen.open("a") as f:
            f.write(h + "\n")
        counts_file = d / "counts.json"
        counts = json.loads(counts_file.read_text()) if counts_file.exists() else {}
        counts[day] = counts.get(day, 0) + 1
        tmp = counts_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(counts))
        tmp.replace(counts_file)
    return True


def station_totals() -> Dict[str, dict]:
    """{site: {total, days}} pour les TOP_SITES sites les plus visités ; purge les hash d'IP de plus de 2 jours."""
    sites = {}
    for d in (p for p in BASE.glob("*") if p.is_dir()):
        counts_file = d / "counts.json"
        if not counts_file.exists():
            continue
        counts = json.loads(counts_file.read_text())
        sites[d.name] = {"total": sum(counts.values()), "days": dict(sorted(counts.items())[-KEEP_DAYS:])}
        for seen in d.glob("*.seen"):
            if seen.stat().st_mtime < time.time() - 2 * 86400:
                seen.unlink()
    return dict(sorted(sites.items(), key=lambda kv: -kv[1]["total"])[:TOP_SITES])


def _sign_and_send(keyfile: Path, kind_tags: list, content: dict) -> bool:
    r = subprocess.run([sys.executable, str(SENDER), "--keyfile", str(keyfile), "--kind", "10600",
                        "--content", json.dumps(content, separators=(",", ":")), "--tags", json.dumps(kind_tags)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        logger.warning("visits: publication refusée (%s) %s", keyfile.name, (r.stdout + r.stderr)[-300:])
    return r.returncode == 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def publish_station() -> bool:
    """Publie les totaux de cette station, signés par la clé NODE."""
    sites = station_totals()
    if not sites:
        return False
    return _sign_and_send(NODE_KEYFILE, [["t", "analytics"], ["t", "visit_counter"]],
                          {"type": "visit_counter", "station": settings.IPFSNODEID, "sites": sites, "updated": _now()})


# ── Parole de l'essaim ────────────────────────────────────────────────────────────────────────────────────────────────

def is_bootstrap_station() -> bool:
    """Même règle qu'ORACLE.refresh.sh : IPFSNODEID == première station de la liste bootstrap."""
    for f in (settings.GAME_PATH / "MY_boostrap_nodes.txt", settings.ZEN_PATH / "Astroport.ONE" / "A_boostrap_nodes.txt"):
        if f.is_file():
            straps = [ln.strip().rsplit("/", 1)[-1] for ln in f.read_text().splitlines()
                      if ln.strip() and not ln.strip().startswith("#")]
            return bool(straps) and bool(settings.IPFSNODEID) and straps[0] == settings.IPFSNODEID
    return False


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def swarm_members() -> Dict[str, str]:
    """{clé NODE: clé du Capitaine} des stations de l'essaim connues (dossier swarm + cette station)."""
    dirs = list((settings.ZEN_PATH / "tmp" / "swarm").glob("*")) + [settings.ZEN_PATH / "tmp" / settings.IPFSNODEID]
    members = {}
    for d in dirs:
        node = _read(d / "HEX")
        if re.fullmatch(r"[0-9a-f]{64}", node):
            members[node] = _read(d / "HEX_CAPTAIN")
    return members


def credentialed_captains(captains: List[str], permit: str) -> set:
    """Capitaines détenant une accréditation ORACLE valide (kind 30503, d=<permis>, p=<capitaine>, non expirée)."""
    from services import relay_io
    events = relay_io.query({"kinds": [30503], "#d": [permit], "#p": captains, "limit": 500}, strict=True)
    now, ok = time.time(), set()
    for ev in events:
        exp = next((int(t[1]) for t in ev["tags"] if t[0] == "expiration" and t[1].isdigit()), None)
        if exp is None or exp > now:
            ok.update(t[1] for t in ev["tags"] if t[0] == "p")
    return ok


def aggregate(events: List[dict], members: Dict[str, str]) -> Dict[str, dict]:
    """Additionne le dernier événement de chaque station membre. {site: {total, stations}}."""
    latest: Dict[str, dict] = {}
    for ev in events:
        pk = ev.get("pubkey")
        if pk in members and (pk not in latest or ev.get("created_at", 0) > latest[pk].get("created_at", 0)):
            latest[pk] = ev
    sites: Dict[str, dict] = {}
    for ev in latest.values():
        try:
            for site, v in json.loads(ev["content"])["sites"].items():
                total = v["total"]
                if normalize_site(site) and type(total) is int and total >= 0:
                    s = sites.setdefault(site, {"total": 0, "stations": 0})
                    s["total"] += total
                    s["stations"] += 1
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.info("visits: événement ignoré (contenu invalide) de %s", ev.get("pubkey", "")[:12])
    return dict(sorted(sites.items(), key=lambda kv: -kv[1]["total"])[:TOP_SITES])


def publish_swarm() -> bool:
    """Station bootstrap uniquement : publie la parole de l'essaim, signée par la clé ORACLE."""
    if not is_bootstrap_station():
        logger.info("visits: cette station n'est pas la station bootstrap")
        return False
    from services import relay_io
    members = swarm_members()
    if SWARM_PERMIT:
        allowed = credentialed_captains([c for c in members.values() if c], SWARM_PERMIT)
        members = {n: c for n, c in members.items() if c in allowed}
    events = relay_io.query({"kinds": [10600], "#t": ["visit_counter"], "authors": list(members), "limit": 500}, strict=True)
    sites = aggregate(events, members)
    if not sites:
        return False
    return _sign_and_send(ORACLE_KEYFILE, [["t", "analytics"], ["t", "swarm_visits"]],
                          {"type": "swarm_visits", "members": len(members), "permit": SWARM_PERMIT or None,
                           "sites": sites, "updated": _now()})


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ("station", "swarm"):
        sys.exit("usage : python3 -m services.visits station|swarm")
    done = publish_station() if action == "station" else publish_swarm()
    print("publié" if done else "rien publié")
