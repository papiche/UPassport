#!/usr/bin/env python3
"""
ucloud_describe.py — Décrit les photos d'un cloud FaceCloud avec le modèle vision d'Ollama.

    python3 ucloud_describe.py scan  <email> [--paths P…] [--mode auto|direct|relay] [--limit N] [--force] [--workers 1]
    python3 ucloud_describe.py --apply            (JSON d'un résultat de Brain sur stdin : mode relais)

Sans `--paths` : toutes les photos SANS description. Une description écrite ou corrigée à la main
(`desc.src == "user"`) n'est JAMAIS remplacée, même avec --force. Où ça tourne (choix du capitaine,
services/analysis.py) :
  direct  la photo est déchiffrée en mémoire et passée à `Astroport.ONE/IA/vision_describe.py --stdin`
          (Ollama local, tunnel SSH ou P2P IPFS) — une à la fois, le GPU est le goulot ;
  relay   un message NIP-44 `vision_analysis_job` (task=describe, photo = CID + clé) part vers un Brain ; sa
          réponse revient sur `vision_analysis_result` et `bro_dm_daemon.sh` appelle `--apply`. La file a une
          durée de vie de 30 min : on n'envoie que RELAY_BATCH photos par passage, à renouveler.
  auto    direct si Ollama répond, sinon relais.

Progression : `.ucloud/describe_scan.json` (running, total, done, failed, submitted, mode). Une seule instance
par compte (flock `.describe.lock`).
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
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from services import analysis, cloud_storage, ucloud_db  # noqa: E402

VISION = Path.home() / ".zen" / "Astroport.ONE" / "IA" / "vision_describe.py"
RELAY_BATCH = 60              # photos envoyées au Brain par passage (file à TTL 30 min)
RELAY_RESEND_AFTER = 35 * 60  # un job plus récent que ça est encore dans la file : on ne le renvoie pas


def progress_path(email: str) -> Path:
    return cloud_storage.ucloud_dir(email) / "describe_scan.json"


def _write_progress(email: str, data: dict) -> None:
    data["updated_at"] = int(time.time())
    p = progress_path(email)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)


def set_description(email: str, path: str, text: str, src: str = "ai", model: str = "", force: bool = False) -> str:
    return cloud_storage.set_photo_description(email, path, text, src, model)


def describe_direct(email: str, path: str, runner: Optional[Callable[[bytes], dict]] = None) -> dict:
    """Déchiffre en mémoire et décrit par l'accès direct. `runner` (tests) remplace vision_describe.py."""
    plain, _mime = cloud_storage.read_plaintext(email, path)
    if runner:
        return runner(plain)
    if not VISION.exists():
        return {"error": f"{VISION} introuvable"}
    proc = subprocess.run([sys.executable, str(VISION), "--stdin"], input=plain, capture_output=True, timeout=420)
    for line in reversed(proc.stdout.decode("utf-8", "replace").strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"error": proc.stderr.decode("utf-8", "replace")[-300:] or "pas de réponse"}


def submit_relay(email: str, path: str) -> bool:
    """Envoie la tâche au Brain (message NIP-44). La clé AES voyage dans le DM déjà chiffré (même contrat
    que le FaceID : le Brain déchiffre en mémoire et ne garde rien)."""
    entry = cloud_storage.db_entry(email, path) or {}
    key_hex = cloud_storage.get_key_hex(email, entry.get("cid", "")) if entry.get("cid") else None
    if not key_hex:
        return False
    hex_pub = cloud_storage.hex_for_email(email)
    trigger = cloud_storage.settings.TOOLS_PATH / "trigger_bro_vision_analysis.sh"
    if not hex_pub or not trigger.exists():
        return False
    env = dict(os.environ, UCLOUD_DECRYPTION_KEY=key_hex, UCLOUD_TASK="describe")
    try:
        proc = subprocess.run(["bash", str(trigger), email, hex_pub, path, entry["cid"], ""], env=env,
                              capture_output=True, timeout=120)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    ucloud_db.patch_entry(cloud_storage.ucloud_dir(email), path, lambda e: e.update(desc_job=int(time.time())) or True)
    return True


def scan(email: str, paths: Optional[list] = None, mode: Optional[str] = None, limit: int = 0,
         force: bool = False, runner: Optional[Callable[[bytes], dict]] = None) -> dict:
    udir = cloud_storage.ensure_ucloud_dir(email)
    lock_fd = cloud_storage.try_lock_nowait(udir / ".describe.lock")
    if lock_fd is None:
        print("ERROR:describe: un passage est déjà en cours", file=sys.stderr)
        return {"status": "busy"}
    try:
        mode = mode or analysis.get_mode()
        eff = analysis.resolve("describe", mode)["effective"]
        if runner:
            eff = "direct"
        todo = paths if paths else ucloud_db.describe_pending(udir)
        if not force:
            keep = []
            for p in todo:
                e = cloud_storage.db_entry(email, p) or {}
                d = e.get("desc") if isinstance(e.get("desc"), dict) else {}
                if d.get("text") and (d.get("src") == "user" or not paths):
                    continue
                if eff == "relay" and e.get("desc_job") and time.time() - e["desc_job"] < RELAY_RESEND_AFTER:
                    continue
                keep.append(p)
            todo = keep
        if limit:
            todo = todo[:limit]
        if eff == "relay":
            todo = todo[:RELAY_BATCH]
        state = {"running": True, "pid": os.getpid(), "mode": eff, "mode_requested": mode, "total": len(todo),
                 "done": 0, "failed": 0, "submitted": 0, "started_at": int(time.time())}
        if eff == "none":
            state.update(running=False, error="no_path")
            _write_progress(email, state)
            print("ERROR:describe: ni accès direct à Ollama, ni relais NOSTR", file=sys.stderr)
            return state
        _write_progress(email, state)
        for i, path in enumerate(todo, 1):
            try:
                if eff == "relay":
                    ok = submit_relay(email, path)
                    state["submitted" if ok else "failed"] += 1
                else:
                    out = describe_direct(email, path, runner)
                    if out.get("description"):
                        set_description(email, path, out["description"], "ai", out.get("model", ""), force=force)
                        state["ok"] = state.get("ok", 0) + 1
                    else:
                        state["failed"] += 1
                        print(f"WARN:describe:{path}: {out.get('error', '?')}", file=sys.stderr)
                        if state["failed"] >= 5 and not state.get("ok"):
                            state["error"] = "model_unavailable"
                            print("ERROR:describe: 5 échecs d'affilée — arrêt (modèle ou tunnel indisponible)", file=sys.stderr)
                            break
            except Exception as exc:
                state["failed"] += 1
                print(f"WARN:describe:{path}: {exc}", file=sys.stderr)
            state["done"] = i
            if i % 5 == 0 or i == len(todo):
                _write_progress(email, state)
        state["running"] = False
        _write_progress(email, state)
        print(json.dumps(state))
        return state
    finally:
        os.close(lock_fd)


def apply_result(message: dict) -> int:
    """Résultat d'un Brain (mode relais) : {email, path, task:'describe', status, description, model}."""
    email, path = str(message.get("email") or ""), str(message.get("path") or "")
    if not cloud_storage.hex_for_email(email) or not path:
        print("ERROR:describe:destinataire inconnu", file=sys.stderr)
        return 1
    if message.get("status") != "ok" or not message.get("description"):
        print(f"WARN:describe:{path}: Brain en échec ({message.get('error', '?')})", file=sys.stderr)
        return 1
    r = set_description(email, path, message["description"], "ai", str(message.get("model") or ""))
    print(json.dumps({"path": path, "result": r}))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--apply"]:
        return apply_result(json.load(sys.stdin))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["scan"])
    ap.add_argument("email")
    ap.add_argument("--paths", nargs="*")
    ap.add_argument("--mode", choices=analysis.MODES, default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    res = scan(args.email, args.paths, args.mode, args.limit, args.force)
    return 1 if res.get("status") == "busy" or res.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
