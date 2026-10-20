"""routers/degafam.py — assistant « Se dégafamiser » (UPlanet/earth/degafam.html) : l'état réel des 4 étapes."""

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from routers.cloud import _email_for_authenticated_npub, _sync_urls
from services import cloud_storage, degafam
from services.nostr import require_nostr_auth

router = APIRouter()


@router.get("/api/degafam/status", summary="Les étapes pour quitter Google/Apple, cochées d'après l'état réel")
async def degafam_status(npub: str = Depends(require_nostr_auth)):
    """`steps` (photos, contacts, agenda, amis) : {done, count, started}. Même calcul que la série d'emails."""
    email = _email_for_authenticated_npub(npub)
    st = await asyncio.to_thread(degafam.state, email)
    dav_url = await cloud_storage.dav_public_url()
    return JSONResponse({
        "email": email, "enrolled": st["dav_enrolled"], "dav_url": dav_url, **_sync_urls(dav_url),
        "steps": {k: st[k] for k in degafam.STEP_ORDER},
        "next": next((k for k in degafam.STEP_ORDER if not st[k]["done"]), None),
    }, headers={"Cache-Control": "no-store"})
