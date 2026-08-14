"""Auth admin Capitaine partagée entre routers/*.

Extrait de routers/nostr.py (endpoints /api/nostr/admin/*) pour être réutilisé
tel quel par d'autres domaines admin (ex. routers/finance.py::/api/oc_admin/*)
sans dupliquer la logique de vérification NIP-98 / UPLANETNAME.
"""
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request


def get_node_and_captain_hex() -> tuple:
    """Résout (node_hex, captain_hex) — même logique que admin_captain_info."""
    node_hex = ""
    captain_hex = ""
    secret_file = Path.home() / ".zen" / "game" / "secret.nostr"
    if secret_file.exists():
        try:
            content = secret_file.read_text()
            for part in content.replace(";", "\n").splitlines():
                part = part.strip()
                if part.startswith("HEX="):
                    node_hex = part[4:].strip()
                    break
        except Exception:
            pass
    if node_hex:
        for json_file in (Path.home() / ".zen" / "tmp").glob("*/12345.json"):
            try:
                data = json.loads(json_file.read_text())
                if data.get("NODEHEX") == node_hex:
                    captain_hex = data.get("captainHEX", "") or node_hex
                    break
            except Exception:
                pass
    return node_hex, (captain_hex or node_hex)


def get_uplanetname() -> str:
    """Lit UPLANETNAME depuis ~/.ipfs/swarm.key (dernière ligne)."""
    swarm_key_path = os.path.expanduser("~/.ipfs/swarm.key")
    try:
        if os.path.exists(swarm_key_path):
            with open(swarm_key_path, 'r') as f:
                lines = f.readlines()
                if lines:
                    return lines[-1].strip()
    except Exception:
        pass
    return "0000000000000000000000000000000000000000000000000000000000000000"


def validate_uplanetname(submitted: str) -> bool:
    """Valide le UPLANETNAME soumis contre la swarm.key locale."""
    if not submitted or len(submitted) != 64:
        return False
    try:
        int(submitted, 16)
    except ValueError:
        return False
    return submitted.lower() == get_uplanetname().lower()


async def is_captain_signed_request(request: Request) -> bool:
    """True si la requête porte un header Authorization NIP-98 valide
    (signature Schnorr vérifiée par verify_nip98_auth) ET signé par le
    pubkey du Capitaine de cette station. Permet au Capitaine d'agir sur les
    endpoints admin/* sans connaître le secret UPLANETNAME (~/.ipfs/swarm.key)
    — seule la possession de sa clé privée NOSTR fait foi."""
    auth_header = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("nostr "):
        return False
    try:
        from services.nostr import verify_nip98_auth
        pubkey = await verify_nip98_auth(request)
    except HTTPException:
        return False
    _, captain_hex = get_node_and_captain_hex()
    return bool(captain_hex) and pubkey.lower() == captain_hex.lower()


async def check_admin_auth(request: Request, uplanetname: Optional[str]) -> None:
    """Autorise un endpoint admin/* si UPLANETNAME est valide OU si la requête
    est signée NIP-98 par le Capitaine reconnu de la station. Lève 403 sinon."""
    if uplanetname and validate_uplanetname(uplanetname):
        return
    if await is_captain_signed_request(request):
        return
    raise HTTPException(
        status_code=403,
        detail="UPLANETNAME invalide et aucune signature NIP-98 du Capitaine reconnue",
    )


async def require_captain_signature(request: Request) -> str:
    """NIP-98 capitaine EXCLUSIVEMENT — aucun repli UPLANETNAME. Retourne le
    pubkey signataire (nécessaire à la piste d'audit). Réservé aux actions où
    le secret coopératif partagé (~/.ipfs/swarm.key, connu de toutes les
    stations de l'essaim) n'est pas une preuve d'autorité suffisante :
    écriture d'une clé sensible (NODE), déclenchement d'un run ARBOR à
    distance. Lève 403 si le header NIP-98 est absent, invalide, ou signé par
    quelqu'un d'autre que le Capitaine reconnu de cette station."""
    auth_header = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("nostr "):
        raise HTTPException(
            status_code=403,
            detail="Signature NIP-98 du Capitaine requise (UPLANETNAME seul non accepté ici).",
        )
    try:
        from services.nostr import verify_nip98_auth
        pubkey = await verify_nip98_auth(request)
    except HTTPException:
        raise HTTPException(status_code=403, detail="Signature NIP-98 invalide.")
    _, captain_hex = get_node_and_captain_hex()
    if not captain_hex or pubkey.lower() != captain_hex.lower():
        raise HTTPException(status_code=403, detail="Signature NIP-98 valide mais pubkey ≠ Capitaine.")
    return pubkey
