#!/usr/bin/env python3
"""
multipass_migrate.py — Déménagement d'un MULTIPASS entre stations, à clé identique (CLI).

Process COURTS, hors serveur UPassport (la preuve est signée avec le NSEC du compte).
Protocole et garanties : services/migration.py.

  NOUVELLE station (après « QR P1 + PASS » : roaming, clés re-dérivées) :
      multipass_migrate.py --request <email> [--from-node NODEHEX] [--pass-stdin]
      multipass_migrate.py --status  <email>

  Appelés par bro_dm_daemon.sh (JSON sur stdin, --sender = NODEHEX authentifié swarm) :
      multipass_migrate.py --serve-request  --sender HEX    ancienne station : migrate_request
      multipass_migrate.py --receive-bundle --sender HEX    nouvelle station : migrate_bundle
      multipass_migrate.py --serve-ack      --sender HEX    ancienne station : migrate_ack
      multipass_migrate.py --receive-commit --sender HEX    nouvelle station : migrate_commit

      multipass_migrate.py --serve-pinned   --sender HEX    ancienne station : migrate_pinned (nettoyage seulement
                                                            si l'épinglage est confirmé sans échec)

  Lancé détaché par --receive-commit :
      multipass_migrate.py --finish <email>                 publie l'IPNS (séquence supérieure), mises à jour
                                                            publiques, puis épingle TOUS les documents du compte

  Opérateur :
      multipass_migrate.py --pin <email>                    reprend l'épinglage (états claimed / pin_error)
      multipass_migrate.py --abort <email>                  annule une demande non aboutie (requested / error)
      multipass_migrate.py --cleanup <email>                ancienne station : relance le nettoyage (après avoir
                                                            traité une ZenCard, par exemple)

Sortie : une ligne JSON ; code 0 ok, 1 refus/échec (ligne ERROR:migration:… sur stderr).
"""

import sys as _sys
import os as _os

_venv_python = _os.path.expanduser("~/.astro/bin/python3")
if _os.path.exists(_venv_python) and _sys.executable != _venv_python:
    _os.execv(_venv_python, [_venv_python] + _sys.argv)

_UPASSPORT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _UPASSPORT_DIR not in _sys.path:
    _sys.path.insert(0, _UPASSPORT_DIR)
del _sys, _os

import argparse
import json
import re
import sys

from services import migration  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Déménagement d'un MULTIPASS entre stations (clé identique)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--request", metavar="EMAIL")
    g.add_argument("--status", metavar="EMAIL")
    g.add_argument("--finish", metavar="EMAIL")
    g.add_argument("--pin", metavar="EMAIL")
    g.add_argument("--abort", metavar="EMAIL")
    g.add_argument("--cleanup", metavar="EMAIL")
    g.add_argument("--serve-request", action="store_true")
    g.add_argument("--receive-bundle", action="store_true")
    g.add_argument("--serve-ack", action="store_true")
    g.add_argument("--receive-commit", action="store_true")
    g.add_argument("--serve-pinned", action="store_true")
    ap.add_argument("--from-node", metavar="NODEHEX")
    ap.add_argument("--pass-stdin", action="store_true",
                    help="--request : lit le PASS (une ligne) sur l'entrée standard — jamais en argument (visible dans ps)")
    ap.add_argument("--sender", metavar="NODEHEX")
    a = ap.parse_args()
    try:
        if a.request:
            pass_code = sys.stdin.readline().strip() if a.pass_stdin else ""
            res = migration.request(a.request, (a.from_node or "").lower() or None, pass_code)
        elif a.status:
            st = migration.load_state(a.status)
            res = {k: v for k, v in st.items() if k != "key"}      # jamais la clé du paquet
        elif a.finish:
            res = migration.finish(a.finish)
        elif a.pin:
            res = migration.pin(a.pin)
        elif a.abort:
            res = migration.abort(a.abort)
        elif a.cleanup:
            res = migration.cleanup_source(a.cleanup)
        else:
            sender = (a.sender or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sender):
                ap.error("--sender (NODEHEX 64 hex) requis")
            message = json.load(sys.stdin)
            fn = (migration.serve_request if a.serve_request else migration.receive_bundle if a.receive_bundle
                  else migration.serve_ack if a.serve_ack else migration.serve_pinned if a.serve_pinned
                  else migration.receive_commit)
            res = fn(message, sender)
    except migration.MigrationError as exc:
        print(f"ERROR:migration:{exc}", file=sys.stderr)
        return 1
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
