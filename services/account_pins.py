"""
services/account_pins.py — Épingler (donc RAPATRIER) tous les documents IPFS d'un compte sur CETTE station.

Après un déménagement, le contenu du compte n'existe encore que chez l'ancienne station : un `ipfs pin add`
télécharge les blocs ET les garde. Sources recensées (collect) :
  * la racine publique du NOSTRNS (uDRIVE et fichiers publics du compte, récursif) ;
  * les fichiers chiffrés du ucloud (index SQLite : photos, contacts, vidéos FUN…) ;
  * le `final_cid` du manifeste uDRIVE ;
  * les CID cités par le manifeste de cookies (.cookie_manifest.json) ;
  * les CID cités par les événements NOSTR écrits par le compte (fichiers 1063, vidéos 21/22, articles,
    savoirs 30500-30505, assets du Studio…).

Garanties :
  * reprenable — `pin add` est idempotent, un CID déjà épinglé est sauté (`pin ls`, instantané) ;
  * l'espace disque est contrôlé AVANT de commencer (tailles connues de l'index) : insuffisant = erreur visible,
    rien n'est tenté ;
  * un CID qui ne vient pas dans le délai est compté en ÉCHEC (jamais un « ok » menteur) ;
  * l'ancienne station ne nettoie RIEN tant que `failed == 0` n'est pas confirmé (message migrate_pinned).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from services import cloud_storage

logger = logging.getLogger(__name__)

CID_RE = re.compile(r"\b(Qm[1-9A-HJ-NP-Za-km-z]{44}|bafy[a-z2-7]{50,})\b")
STRFRY_DIR = Path.home() / ".zen" / "strfry"
IPFS_REPO = Path.home() / ".ipfs"
PIN_TIMEOUT = 900          # secondes par CID ; au-delà : échec consigné, on passe au suivant
WORKERS = 4
RESERVE_BYTES = 2 * 1024 ** 3     # marge laissée libre en plus des tailles connues
SAFETY_FACTOR = 1.1


class PinError(Exception):
    """Échec explicite (espace insuffisant…), jamais un repli silencieux."""


def _cids_in(text: str) -> List[str]:
    return sorted(set(CID_RE.findall(text or "")))


def collect(email: str, roots: Iterable[str] = ()) -> Dict[str, Dict[str, Any]]:
    """{cid: {"source": …, "size": octets ou None}}. `roots` : racines /ipfs/<cid> à épingler en premier."""
    found: Dict[str, Dict[str, Any]] = {}

    def add(cid: str, source: str, size: Optional[int] = None) -> None:
        cur = found.get(cid)
        if cur is None:
            found[cid] = {"source": source, "size": size}
        elif cur.get("size") is None and size is not None:
            cur["size"] = size

    for r in roots:
        cid = r.rsplit("/", 1)[-1]
        if CID_RE.fullmatch(cid):
            add(cid, "nostrns")

    udir = cloud_storage.GAME_NOSTR_PATH / email
    try:                                                   # fichiers chiffrés du ucloud
        idx = cloud_storage.load_index(email)
        for entry in idx.get("entries", {}).values():
            if isinstance(entry, dict) and entry.get("cid"):
                add(entry["cid"], "ucloud", int(entry.get("size") or 0) or None)
    except Exception as exc:   # un index illisible n'est pas « zéro fichier » : on le dit
        logger.warning("account_pins: index ucloud illisible pour %s (%s)", email, exc)
        raise PinError(f"index ucloud illisible : {exc}")

    try:                                                   # uDRIVE
        man = json.loads((udir / "APP" / "uDRIVE" / "manifest.json").read_text())
        if man.get("final_cid"):
            add(man["final_cid"], "udrive", int(man.get("total_size") or 0) or None)
    except (OSError, ValueError):
        pass

    try:                                                   # cookies
        for cid in _cids_in((udir / ".cookie_manifest.json").read_text()):
            add(cid, "cookies")
    except OSError:
        pass

    hex_pub = cloud_storage.hex_for_email(email)           # événements du compte
    strfry = STRFRY_DIR / "strfry"
    if hex_pub and strfry.exists():
        p = subprocess.run([str(strfry), "scan", json.dumps({"authors": [hex_pub]})], cwd=str(STRFRY_DIR),
                           capture_output=True, timeout=600)
        if p.returncode != 0:
            raise PinError(f"strfry scan a échoué : {p.stderr.decode()[-200:]}")
        for cid in _cids_in(p.stdout.decode("utf-8", "replace")):
            add(cid, "events")
    return found


def check_space(found: Dict[str, Dict[str, Any]], repo: Path = IPFS_REPO) -> Dict[str, int]:
    """Lève PinError si les tailles CONNUES (+ marge) ne tiennent pas dans l'espace libre du dépôt IPFS.
    Les CID sans taille connue (événements, cookies) ne sont pas comptés : la marge les couvre."""
    known = sum(int(v["size"] or 0) for v in found.values())
    need = int(known * SAFETY_FACTOR) + RESERVE_BYTES
    free = shutil.disk_usage(repo if repo.exists() else Path.home()).free
    if free < need:
        raise PinError(f"espace disque insuffisant : {free // 1024 ** 2} Mo libres, {need // 1024 ** 2} Mo nécessaires "
                       f"({known // 1024 ** 2} Mo connus + marge) — rien n'a été épinglé")
    return {"known_bytes": known, "free_bytes": free}


def _pin_one(cid: str, timeout: int) -> str:
    """'already' | 'pinned' | 'failed'"""
    try:
        if subprocess.run(["ipfs", "pin", "ls", "--type=recursive", cid], capture_output=True, timeout=60).returncode == 0:
            return "already"
        r = subprocess.run(["ipfs", "pin", "add", "--progress=false", cid], capture_output=True, timeout=timeout)
        return "pinned" if r.returncode == 0 else "failed"
    except Exception as exc:
        logger.warning("account_pins: %s : %s", cid[:16], exc)
        return "failed"


def pin_all(found: Dict[str, Dict[str, Any]], progress: Optional[Callable[[Dict[str, Any]], None]] = None,
            timeout: int = PIN_TIMEOUT, workers: int = WORKERS) -> Dict[str, Any]:
    """Épingle tout (racines d'abord). Retourne {total, pinned, already, failed, failed_cids[:50], ok}."""
    order = sorted(found, key=lambda c: (found[c]["source"] != "nostrns", found[c]["source"] != "udrive", c))
    res: Dict[str, Any] = {"total": len(order), "pinned": 0, "already": 0, "failed": 0, "failed_cids": []}
    lock = threading.Lock()

    def work(cid: str) -> None:
        outcome = _pin_one(cid, timeout)
        with lock:
            if outcome == "failed":
                res["failed"] += 1
                if len(res["failed_cids"]) < 50:
                    res["failed_cids"].append(cid)
            else:
                res[outcome] += 1
            done = res["pinned"] + res["already"] + res["failed"]
            if progress and (done % 25 == 0 or done == res["total"]):
                progress({k: res[k] for k in ("total", "pinned", "already", "failed")})

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(work, order))
    res["ok"] = res["failed"] == 0
    return res
