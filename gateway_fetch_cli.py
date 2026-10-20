#!/usr/bin/env python3
"""
gateway_fetch_cli.py — Rapatrier un CID par la PASSERELLE HTTP de l'ancienne station (changement de constellation).

  gateway_fetch_cli.py dag  <passerelle> <CID>            DAG complet (CAR → `ipfs dag import`), CID préservé, épinglé
  gateway_fetch_cli.py file <passerelle> <CID> <sortie>   contenu d'un bloc (archive chiffrée) écrit dans <sortie>
  <passerelle> : https://hote ou un lien https://hote/ipfs/<CID> (un CID seul n'est pas accepté ici)

Appelé par tools/nostr_RESTORE_TW.sh --gateway. Code 0 : ok ; 1 : échec (ligne ERROR:gateway:… sur stderr).
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

import sys

from services import gateway_fetch as gf  # noqa: E402


def main() -> int:
    a = sys.argv[1:]
    try:
        if len(a) == 3 and a[0] == "dag":
            print(gf.fetch_dag(a[2], a[1]))
        elif len(a) == 4 and a[0] == "file":
            data = gf.fetch_bytes(a[2], a[1])
            with open(a[3], "wb") as fh:
                fh.write(data)
            print(len(data))
        else:
            print(__doc__, file=sys.stderr)
            return 1
    except gf.GatewayError as exc:
        print(f"ERROR:gateway:{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
