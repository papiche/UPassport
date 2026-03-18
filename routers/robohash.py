import io
import logging
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse
from robohash import Robohash

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/robohash/{pubkey}", summary="Avatar Robohash local", description="Génère un avatar Robohash localement sans contacter robohash.org")
async def get_robohash(
    pubkey: str,
    size: int = Query(default=200, ge=10, le=1024, description="Taille de l'image en pixels"),
    set: int = Query(default=1, ge=1, le=4, description="Jeu de sprites (1=robots, 2=monsters, 3=heads, 4=cats)"),
):
    """
    Génère un avatar Robohash localement à partir d'une pubkey Nostr.
    Remplace l'appel externe à https://robohash.org/ pour préserver la vie privée.
    """
    try:
        rh = Robohash(pubkey)
        rh.assemble(roboset=f"set{set}", color=None, format="png", bgset=None, sizex=size, sizey=size)

        img_io = io.BytesIO()
        rh.img.save(img_io, format="PNG")
        img_io.seek(0)

        return StreamingResponse(
            img_io,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=86400",
                "X-Pubkey": pubkey[:16] + "...",
            },
        )
    except Exception as e:
        logger.error(f"Erreur génération robohash pour {pubkey[:16]}...: {e}")
        raise HTTPException(status_code=500, detail="Erreur lors de la génération de l'avatar")
