#!/usr/bin/env python3
"""
ucloud_export.py — Exporte l'index et le keyring d'un cloud `.ucloud` (SQLite)
au format JSON historique, pour les scripts bash (nostr_DESTROY_TW.sh, jq…).

    python3 ucloud_export.py <email> <dossier> [--names backup|plain]

  backup (défaut) : ucloud_index.json + ucloud_keyring.json (noms attendus par
                    nostr_RESTORE_TW.sh)
  plain           : index.json + keyring.json

Stdlib seule (services/ucloud_db.py n'importe rien d'autre) : tourne sous
n'importe quel python3, sans le venv ni FastAPI. Affiche une ligne JSON
{"entries": N, "keys": M} ; code de sortie 1 si le cloud n'existe pas.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "services"))
import ucloud_db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("email")
    ap.add_argument("dest")
    ap.add_argument("--names", choices=("backup", "plain"), default="backup")
    args = ap.parse_args()
    udir = Path(os.environ.get("HOME", "~")).expanduser() / ".zen" / "game" / "nostr" / args.email / ".ucloud"
    if not (ucloud_db.exists(udir) or (udir / "index.json").exists()):
        print(f"ERROR:ucloud_export:aucun cloud pour {args.email}", file=sys.stderr)
        return 1
    names = ("ucloud_index.json", "ucloud_keyring.json") if args.names == "backup" else ("index.json", "keyring.json")
    print(json.dumps(ucloud_db.export_json(udir, Path(args.dest), *names)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
