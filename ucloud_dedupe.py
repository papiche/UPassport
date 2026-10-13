#!/usr/bin/env python3
"""
ucloud_dedupe.py — Enrichit les photos ANTÉRIEURES au schéma v2 en UN passage de déchiffrement :
empreinte perceptuelle (dHash : quasi-doublons), date de prise de vue (EXIF : frise et carte) et
coordonnées GPS (carte) quand elles manquent.

    python3 ucloud_dedupe.py scan <email> [--workers 3] [--limit N]

Les doublons EXACTS (même SHA-256) n'ont pas besoin de ce passage : ils sont trouvés
instantanément par SQL. Les photos ajoutées depuis la version 2 du schéma reçoivent leur
empreinte à l'envoi ; ce script rattrape l'archive existante (décryptage de chaque photo :
quelques dizaines de ms à ~0,3 s par image — d'où l'exécution en arrière-plan).

Progression dans `.ucloud/dedupe_scan.json` (lue par GET /api/cloud/duplicates),
une seule instance par compte (flock `.dedupe.lock`). Reprend où il s'était arrêté :
seules les images SANS empreinte sont traitées.
"""
import sys as _sys
import os as _os

_venv_python = _os.path.expanduser("~/.astro/bin/python3")
if _os.path.exists(_venv_python) and _sys.executable != _venv_python:
    _os.execv(_venv_python, [_venv_python] + _sys.argv)
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
del _sys, _os

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services import cloud_storage, ucloud_db  # noqa: E402


def progress_path(email: str) -> Path:
    return cloud_storage.ucloud_dir(email) / "dedupe_scan.json"


def _write_progress(email: str, data: dict) -> None:
    data["updated_at"] = int(time.time())
    p = progress_path(email)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)


def scan(email: str, workers: int = 3, limit: int = 0) -> dict:
    udir = cloud_storage.ensure_ucloud_dir(email)
    lock_fd = cloud_storage.try_lock_nowait(udir / ".dedupe.lock")
    if lock_fd is None:
        print("ERROR:dedupe: un passage est déjà en cours", file=sys.stderr)
        return {"status": "busy"}
    try:
        todo = ucloud_db.phash_pending(udir)
        if limit:
            todo = todo[:limit]
        state = {"running": True, "pid": os.getpid(), "total": len(todo), "done": 0, "failed": 0, "started_at": int(time.time())}
        _write_progress(email, state)

        def one(path: str) -> bool:
            try:
                plain, _mime = cloud_storage.read_plaintext(email, path)
                ph = cloud_storage.compute_phash(plain)
                info = cloud_storage._inspect_image(plain)      # une seule lecture : date de prise de vue + GPS
            except Exception as exc:
                print(f"WARN:dedupe:{path}: {exc}", file=sys.stderr)
                return False
            # « 0 » = illisible : marqué pour ne pas le retraiter à chaque passage.
            value = ph or "0" * 16

            def patch(e: dict) -> bool:
                e["phash"] = value
                # Complète seulement ce qui manque (jamais d'écrasement) : les photos importées avant
                # l'extraction EXIF n'avaient ni date de prise de vue ni lieu — donc ni frise ni carte.
                if info.get("taken_at") and not e.get("taken_at"):
                    e["taken_at"] = info["taken_at"]
                if info.get("geo") and not e.get("geo"):
                    e["geo"] = info["geo"]
                return True
            with cloud_storage.index_lock(email):
                ucloud_db.patch_entry(udir, path, patch)
            return bool(ph)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for i, ok in enumerate(pool.map(one, todo), 1):
                state["done"] = i
                if not ok:
                    state["failed"] += 1
                if i % 25 == 0:
                    _write_progress(email, state)
        state["running"] = False
        _write_progress(email, state)
        print(json.dumps(state))
        return state
    finally:
        os.close(lock_fd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["scan"])
    ap.add_argument("email")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    return 0 if scan(args.email, args.workers, args.limit).get("status") != "busy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
