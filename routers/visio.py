"""
routers/visio.py — Salles de visio privées (vdo.ninja auto-hébergé).

Avant : le nom de la salle d'un live (minelife.html) était `uplanet_<10 premiers
caractères du npub>`, sans mot de passe — l'npub étant public, n'importe qui pouvait
rejoindre le studio d'un membre. Désormais le serveur tire au sort une salle ET un mot
de passe, que seuls l'organisateur et ses invités (message privé chiffré) connaissent.

    POST /api/visio/room   NIP-98 → {room, password, join_url, base, expires_at}
    GET  /api/visio/config  public → {base}   (l'instance vdo.ninja de cette station)

La station ne garde RIEN : vdo.ninja ouvre la salle dès que le premier participant y
entre ; `expires_at` est indicatif (l'invitation porte son échéance, les clients ignorent
une invitation périmée). Le mot de passe est dans le FRAGMENT du lien (`#p=`, jamais envoyé au serveur : hors des journaux du proxy ; lettres et
chiffres seulement, consigne de &password). Anti-abus : plafond de salles par clé et par heure.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.nostr import require_nip98_only
from utils.helpers import get_env_from_mysh

logger = logging.getLogger(__name__)
router = APIRouter(tags=["visio"])

DEFAULT_BASE = "https://vdo.copylaradio.com"
DEFAULT_TTL = 4 * 3600
MAX_TTL = 24 * 3600
PER_KEY_PER_HOUR = 30
GLOBAL_PER_HOUR = 600

_hits: Dict[str, List[float]] = {}
_lock = threading.Lock()


class RoomRequest(BaseModel):
    ttl: int = Field(DEFAULT_TTL, ge=300, le=MAX_TTL, description="Durée de validité indicative (secondes)")


async def _base() -> str:
    base = (await get_env_from_mysh("VDONINJA", DEFAULT_BASE)).strip().rstrip("/")
    return base if base.startswith("https://") else DEFAULT_BASE


def _allow(key: str, now: float) -> bool:
    """Fenêtre glissante d'une heure, par clé et pour toute la station."""
    with _lock:
        for k in list(_hits):
            _hits[k] = [t for t in _hits[k] if now - t < 3600]
            if not _hits[k]:
                del _hits[k]
        if len(_hits.get(key, [])) >= PER_KEY_PER_HOUR or sum(len(v) for v in _hits.values()) >= GLOBAL_PER_HOUR:
            return False
        _hits.setdefault(key, []).append(now)
        return True


def new_room(base: str, ttl: int, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    room, password = secrets.token_hex(10), secrets.token_hex(8)
    return {
        "room": room, "password": password, "base": base,
        "join_url": f"{base}/?room={quote(room)}#p={quote(password)}",
        "expires_at": int(now) + ttl,
    }


@router.get("/api/visio/config", summary="Instance vdo.ninja de cette station")
async def visio_config():
    return {"base": await _base()}


@router.post("/api/visio/room", summary="Créer une salle de visio privée (NIP-98)")
async def create_room(request: Request, body: RoomRequest = RoomRequest()):
    npub = await require_nip98_only(request)
    if not _allow(npub, time.time()):
        logger.warning("visio: plafond atteint pour %s…", npub[:14])
        raise HTTPException(status_code=429, detail="Trop de salles créées : réessayez dans quelques minutes.")
    return new_room(await _base(), body.ttl)
