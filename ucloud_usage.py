#!/usr/bin/env python3
"""ucloud_usage.py <email> — mesure l'espace d'un MULTIPASS (.ucloud + uDRIVE), l'écrit dans
`~/.zen/game/nostr/<email>/.storage_usage.json` et l'affiche en JSON.

Appelé chaque jour par NOSTRCARD.refresh.sh ; ZEN.ECONOMY.sh additionne ces fichiers pour la capacité de la station.
Erreurs sur stderr, code retour non nul (jamais de silence)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import storage_quota  # noqa: E402

GAME_NOSTR = Path.home() / ".zen" / "game" / "nostr"


def main(argv):
    if len(argv) != 2 or "@" not in argv[1] or "/" in argv[1] or ".." in argv[1]:
        print("usage: ucloud_usage.py <email>", file=sys.stderr)
        return 2
    d = GAME_NOSTR / argv[1]
    if not d.is_dir():
        print(f"ERROR:ucloud_usage:MULTIPASS introuvable : {d}", file=sys.stderr)
        return 1
    print(json.dumps(storage_quota.record(d)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
