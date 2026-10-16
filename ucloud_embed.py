#!/usr/bin/env python3
"""
ucloud_embed.py — Vectorise les descriptions des photos d'un cloud FaceCloud (recherche par le sens).

    python3 ucloud_embed.py scan <email> [--limit N] [--model M]

Traite les photos DÉCRITES dont le vecteur manque, ou a été calculé avec un autre modèle ou une autre description
(une description corrigée à la main est donc re-vectorisée). Par paquets de 32, ~100 photos/s sur GPU.
Progression : `.ucloud/embed_scan.json`. Une seule instance par compte (flock `.embed.lock`). Rien ne quitte la station.
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
from pathlib import Path

from services import cloud_storage, trace, ucloud_db, ucloud_embed  # noqa: E402


def progress_path(email: str) -> Path:
    return cloud_storage.ucloud_dir(email) / "embed_scan.json"


def _write(email: str, data: dict) -> None:
    data["updated_at"] = int(time.time())
    p = progress_path(email)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)


def scan(email: str, limit: int = 0, model: str = ucloud_embed.MODEL, verbose: int = 0, only_cids=None) -> dict:
    if verbose:
        trace.set_level(max(trace.level(), int(verbose)))
    udir = cloud_storage.ensure_ucloud_dir(email)
    state = None
    lock_fd = cloud_storage.try_lock_nowait(udir / ".embed.lock")
    if lock_fd is None:
        print("ERROR:embed: un passage est déjà en cours", file=sys.stderr)
        return {"status": "busy"}
    try:
        c = ucloud_embed.counts(udir, model)
        trace.log("embed", f"{email} : modèle={model}, Ollama={ucloud_embed.OLLAMA_URL}, base={udir / 'ucloud.db'}; "
                           f"photos décrites={c['described']}, déjà vectorisées={c['indexed']}, à faire={c['pending']}")
        with trace.Timer() as t_sel:
            todo = ucloud_embed.pending(udir, model)
        trace.log("embed", f"sélection des descriptions à vectoriser : {len(todo)} en {t_sel.s:.2f}s (limite : {limit or 'aucune'})", 2)
        if only_cids is not None:                       # appelé après des descriptions : seulement celles-là, pas les 21 000 autres
            todo = [t for t in todo if t[0] in only_cids]
            trace.log("embed", f"limité aux {len(only_cids)} photo(s) qui viennent d'être décrites → {len(todo)} à vectoriser")
        if limit:
            todo = todo[:limit]
        state = {"running": True, "pid": os.getpid(), "model": model, "total": len(todo), "done": 0, "failed": 0,
                 "started_at": int(time.time())}
        _write(email, state)
        last_log = 0.0
        for i in range(0, len(todo), ucloud_embed.BATCH):
            chunk = todo[i:i + ucloud_embed.BATCH]
            texts = [ucloud_embed.text_for(t) for _c, _n, t in chunk]
            first = i == 0
            nb = i // ucloud_embed.BATCH + 1
            if first or nb % 50 == 0:                       # -vv : le détail d'un paquet de temps en temps (pas 666 fois)
                trace.log("embed", f"paquet {nb}/{-(-len(todo) // ucloud_embed.BATCH)} : {len(chunk)} description(s), "
                                   f"{sum(map(len, texts)) // len(texts)} caractères en moyenne", 2)
                for c_, _n, _t in chunk[:2]:
                    trace.log("embed", f"  exemple cid={c_[:16]}… texte embarqué : {ucloud_embed.text_for(_t)[:200]!r}", 2)
            t_batch = time.time()
            try:
                vecs = ucloud_embed.embed_texts(texts, model)
            except ucloud_embed.EmbedError as exc:
                state.update(running=False, error=str(exc)[:300])
                print(f"ERROR:embed: {exc}", file=sys.stderr)
                _write(email, state)
                return state
            ucloud_embed.store(udir, [(c, n) for c, n, _t in chunk], vecs, model)
            state["done"] = min(len(todo), i + len(chunk))
            _write(email, state)
            dt = time.time() - state["started_at"]
            if first or state["done"] == len(todo) or time.time() - last_log >= 5:     # une ligne de progression toutes les 5 s
                last_log = time.time()
                trace.log("embed", f"{state['done']}/{len(todo)} vectorisées ({state['done'] / max(dt, 1e-9):.0f} descriptions/s, "
                                   f"{vecs.shape[1]} dimensions, fin estimée dans "
                                   f"{(len(todo) - state['done']) / max(state['done'] / max(dt, 1e-9), 1e-9):.0f}s) — Ctrl-C pour arrêter, rien n'est perdu")
        state["running"] = False
        _write(email, state)
        trace.log("embed", f"terminé : {state['done']} vectorisée(s), {state['failed']} échec(s) en {time.time() - state['started_at']:.1f}s")
        print(json.dumps(state))
        return state
    finally:
        if state and state.get("running"):              # Ctrl-C ou erreur : l'état ne doit pas rester « en cours »
            state.update(running=False, error=state.get("error") or "interrompu")
            _write(email, state)
            print(f"embed: interrompu après {state['done']}/{state['total']} — ce qui est vectorisé est conservé", file=sys.stderr, flush=True)
        os.close(lock_fd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["scan"])
    ap.add_argument("email")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=ucloud_embed.MODEL)
    ap.add_argument("--verbose", "-v", action="count", default=0, help="-v : chaque paquet ; -vv : requêtes Ollama, textes embarqués, durées")
    args = ap.parse_args()
    try:
        res = scan(args.email, args.limit, args.model, args.verbose)
    except KeyboardInterrupt:
        return 130
    if res.get("status") == "busy":
        return 75                                   # EX_TEMPFAIL : un autre passage tourne déjà, ce n'est pas une erreur
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
