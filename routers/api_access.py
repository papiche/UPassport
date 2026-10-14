"""API Access Vault — identifiants d'API officielle (Mastodon, Telegram...)
par MULTIPASS. Distinct de routers/cookie.py (cookies de session scraper) :
cf. services/api_access_store.py pour la justification du cloisonnement."""

import logging
logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Body
from pydantic import BaseModel

from services.api_access_store import (
    API_PROVIDERS,
    load_manifest,
    get_provider_config,
    set_provider_config,
    delete_provider_config,
)
from services.nostr import require_nostr_auth
from utils.crypto import npub_to_hex
from utils.security import find_user_directory_by_hex

router = APIRouter(prefix="/api_access", tags=["api_access"])


def _user_dir(npub: str) -> Path:
    return find_user_directory_by_hex(npub_to_hex(npub))


def _validate_provider(provider: str):
    if provider not in API_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Provider inconnu : {provider}")


class ProviderValues(BaseModel):
    values: dict


# ──────────────────────────────────────────────────────────────────────────────
# GET /api_access — schéma des providers + statut de configuration (jamais les secrets)
# ──────────────────────────────────────────────────────────────────────────────

@router.get("", summary="Schéma des providers + statut pour l'utilisateur authentifié")
async def list_api_access(
    request: Request,
    npub: Optional[str] = Query(None, description="User npub (requis sans en-tête NIP-98)"),
):
    auth_npub = await require_nostr_auth(request, npub, force_check=False)
    user_dir = _user_dir(auth_npub)
    manifest = load_manifest(user_dir)

    providers = []
    for key, spec in API_PROVIDERS.items():
        info = manifest.get(key)
        providers.append({
            "provider":    key,
            "label":       spec["label"],
            "icon":        spec["icon"],
            "fields":      spec["fields"],
            "howto":       spec["howto"],
            "configured":  bool(info),
            "updated_at":  info.get("updated_at") if info else None,
        })
    return {"providers": providers}


# ──────────────────────────────────────────────────────────────────────────────
# GET /api_access/{provider} — valeurs déchiffrées (les champs "secret" sont
# renvoyés tronqués, jamais en entier — l'utilisateur ressaisit pour changer)
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/{provider}", summary="Valeurs configurées pour ce provider (secrets masqués)")
async def get_api_access(
    provider: str,
    request: Request,
    npub: Optional[str] = Query(None),
):
    _validate_provider(provider)
    auth_npub = await require_nostr_auth(request, npub, force_check=False)
    user_dir = _user_dir(auth_npub)

    values = await get_provider_config(user_dir, provider)
    if values is None:
        return {"provider": provider, "configured": False, "values": {}}

    spec = API_PROVIDERS[provider]
    masked = {}
    for f in spec["fields"]:
        v = values.get(f["key"], "")
        if f.get("secret") and v:
            masked[f["key"]] = v[:4] + "…" + v[-4:] if len(v) > 10 else "••••"
        else:
            masked[f["key"]] = v
    return {"provider": provider, "configured": True, "values": masked}


# ──────────────────────────────────────────────────────────────────────────────
# POST /api_access/{provider} — stocke/remplace les valeurs
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/{provider}", summary="Enregistre les identifiants pour ce provider")
async def set_api_access(
    provider: str,
    request: Request,
    body: ProviderValues = Body(...),
    npub: Optional[str] = Query(None),
):
    _validate_provider(provider)
    auth_npub = await require_nostr_auth(request, npub, force_check=False)
    user_dir = _user_dir(auth_npub)

    spec = API_PROVIDERS[provider]
    allowed_keys = {f["key"] for f in spec["fields"]}
    values = {k: v for k, v in body.values.items() if k in allowed_keys}
    if not values:
        raise HTTPException(status_code=400, detail="Aucun champ valide fourni")

    ok = await set_provider_config(user_dir, provider, values)
    if not ok:
        raise HTTPException(
            status_code=500,
            detail="Échec du chiffrement/stockage (clé G1 absente ou IPFS indisponible)",
        )
    return {"success": True, "provider": provider}


# ──────────────────────────────────────────────────────────────────────────────
# DELETE /api_access/{provider}
# ──────────────────────────────────────────────────────────────────────────────

@router.delete("/{provider}", summary="Supprime les identifiants pour ce provider")
async def delete_api_access(
    provider: str,
    request: Request,
    npub: Optional[str] = Query(None),
):
    _validate_provider(provider)
    auth_npub = await require_nostr_auth(request, npub, force_check=False)
    user_dir = _user_dir(auth_npub)

    cid = await delete_provider_config(user_dir, provider)
    if cid:
        import asyncio
        try:
            proc = await asyncio.create_subprocess_exec(
                "ipfs", "pin", "rm", cid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            pass
    return {"success": True, "provider": provider, "cid_unpinned": cid}
