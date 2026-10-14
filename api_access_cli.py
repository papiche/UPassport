#!/usr/bin/env python3
"""
api_access_cli.py — CLI synchrone pour lire/écrire le vault API Access
(kind 31904, services/api_access_store.py) depuis des scripts bash
(Astroport.ONE), sans dépendre du serveur FastAPI en cours d'exécution.

Même frontière de confiance que IA/get_cookie.sh : un script qui tourne SUR
la station elle-même, pour un email résolu LOCALEMENT
(~/.zen/game/nostr/{email}/) — pas d'auth NIP-98 (ça, c'est le web only,
cf. routers/api_access.py). Celui qui peut exécuter ce script sur cette
machine a déjà un accès équivalent (ou supérieur) aux fichiers du compte.

Usage :
    api_access_cli.py list   <email>                          # {provider: bool configuré}, JSON
    api_access_cli.py get    <email> <provider>                # JSON déchiffré sur stdout ({} si absent)
    api_access_cli.py set    <email> <provider> <json_values>  # stocke, exit 0/1
    api_access_cli.py delete <email> <provider>                # CID dépinglé sur stdout (vide si rien)
"""
import sys
import os
import json
import asyncio
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from services.api_access_store import (  # noqa: E402
    API_PROVIDERS, load_manifest, get_provider_config,
    set_provider_config, delete_provider_config,
)

ZEN_NOSTR_DIR = Path(os.path.expanduser("~/.zen/game/nostr"))

## Garde-fou dur : set_provider_config()/delete_provider_config() programment une
## publication NOSTR en tâche de fond (asyncio.create_task, fire-and-forget) — un
## design pensé pour FastAPI, où la boucle asyncio reste vivante bien après le
## retour de la fonction. Dans un CLI de courte durée (asyncio.run() par
## invocation), cette tâche résiduelle bloque la fermeture de la boucle (hang
## constaté systématiquement sur set/delete, > 20s, alors que natools.py/ipfs
## add/nostr_send_note.py pris isolément répondent tous en < 1s). Plutôt que de
## changer le comportement partagé avec le router FastAPI, on borne ICI la
## commande entière : jamais plus de CLI_TIMEOUT secondes, quoi qu'il arrive.
CLI_TIMEOUT = 30


def _user_dir(email: str) -> Path:
    d = ZEN_NOSTR_DIR / email
    if not d.is_dir():
        print(f"ERROR:api_access_cli:unknown_email:{email}", file=sys.stderr)
        sys.exit(2)
    return d


async def _run(cmd: str, user_dir: Path, provider: str = "", values: dict | None = None):
    if cmd == "list":
        result = {"list": {p: (p in load_manifest(user_dir)) for p in API_PROVIDERS}}
    elif cmd == "get":
        result = {"get": await get_provider_config(user_dir, provider) or {}}
    elif cmd == "set":
        result = {"set": await set_provider_config(user_dir, provider, values)}
    elif cmd == "delete":
        result = {"delete": await delete_provider_config(user_dir, provider)}
    else:
        raise ValueError(f"unknown command: {cmd}")

    ## set_provider_config()/delete_provider_config() programment la publication
    ## NOSTR via asyncio.create_task() (fire-and-forget, pensé pour FastAPI où la
    ## boucle reste vivante). Dans ce CLI de courte durée, une tâche encore
    ## pendante au moment où asyncio.run() se termine bloque indéfiniment son
    ## nettoyage interne (_cancel_all_tasks) — constaté : hang systématique,
    ## alors que chaque sous-processus pris isolément répond en < 2s. On
    ## l'attend ICI explicitement, pendant que la boucle tourne normalement,
    ## pour qu'il ne reste plus rien en suspens au retour de cette coroutine.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=15)

    return result


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    cmd, email = sys.argv[1], sys.argv[2]
    user_dir = _user_dir(email)

    provider = ""
    values = None
    if cmd != "list":
        if len(sys.argv) < 4:
            print(__doc__, file=sys.stderr)
            sys.exit(2)
        provider = sys.argv[3]
        if provider not in API_PROVIDERS:
            print(f"ERROR:api_access_cli:unknown_provider:{provider}", file=sys.stderr)
            sys.exit(2)
    if cmd == "set":
        if len(sys.argv) < 5:
            print(__doc__, file=sys.stderr)
            sys.exit(2)
        try:
            values = json.loads(sys.argv[4])
        except json.JSONDecodeError as e:
            print(f"ERROR:api_access_cli:invalid_json:{e}", file=sys.stderr)
            sys.exit(2)
    if cmd not in ("list", "get", "set", "delete"):
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    try:
        result = asyncio.run(asyncio.wait_for(_run(cmd, user_dir, provider, values), timeout=CLI_TIMEOUT))
    except asyncio.TimeoutError:
        print(f"ERROR:api_access_cli:timeout_after_{CLI_TIMEOUT}s", file=sys.stderr)
        sys.exit(1)

    if cmd == "list":
        print(json.dumps(result["list"]))
    elif cmd == "get":
        print(json.dumps(result["get"], ensure_ascii=False))
    elif cmd == "set":
        sys.exit(0 if result["set"] else 1)
    elif cmd == "delete":
        print(result["delete"] or "")


if __name__ == "__main__":
    main()
