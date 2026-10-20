#!/usr/bin/env python3
"""
ucloud_onboarding.py — « Se dégafamiser » pas à pas : le message du jour pour UN MULTIPASS (voir services/degafam.py).

    python3 ucloud_onboarding.py <email>                   # décide d'après l'état réel, envoie au plus UN message
    python3 ucloud_onboarding.py <email> --dry-run         # dit ce qui serait envoyé, sans rien envoyer ni écrire
    python3 ucloud_onboarding.py <email> --preview DIR     # écrit les 6 messages en HTML (relecture), sans envoi
    python3 ucloud_onboarding.py <email> --force photos    # envoie CETTE étape maintenant (test, ignore le calendrier)

Lancé chaque jour par RUNTIME/NOSTRCARD.refresh.sh (processus autonome, `setsid nohup`) pour les comptes récents.
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
import sys
import time
from pathlib import Path

from services import cloud_storage, degafam  # noqa: E402


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] ucloud_onboarding: {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("email")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preview", metavar="DIR", help="écrit les messages en HTML dans DIR, sans rien envoyer")
    ap.add_argument("--force", choices=degafam.ALL_STEPS, help="envoie cette étape tout de suite")
    args = ap.parse_args()
    if not cloud_storage.hex_for_email(args.email):
        print(f"ERROR:ucloud_onboarding:aucun MULTIPASS '{args.email}' sur cette station", file=sys.stderr)
        return 1
    if args.preview:
        out = Path(args.preview)
        out.mkdir(parents=True, exist_ok=True)
        st = degafam.state(args.email)
        for step in degafam.ALL_STEPS:
            subject, body = degafam.compose(step, args.email, st)
            (out / f"{step}.html").write_text(body, encoding="utf-8")
            say(f"{step}.html — {subject}")
        return 0
    res = degafam.run(args.email, dry_run=args.dry_run, force_step=args.force or "")
    say(f"{args.email} : {res['status']}" + (f" — étape {res['step']}" if res.get("step") else "") + f" — {res.get('why', '')}")
    return 1 if res["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
