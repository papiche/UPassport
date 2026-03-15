import logging
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse

from utils.helpers import render_page

router = APIRouter()

@router.get("/nostr", summary="NOSTR Page", description="Route NOSTR avec support de différents types de templates.")
async def get_nostr(request: Request, type: str = "default"):
    """
    Route NOSTR avec support de différents types de templates
    """
    try:
        if type not in ["default", "uplanet"]:
            raise HTTPException(
                status_code=400, 
                detail=f"Type invalide: '{type}'. Types supportés: 'default', 'uplanet'"
            )
        
        if type == "default":
            template_name = "nostr.html"
        elif type == "uplanet":
            template_name = "nostr_uplanet.html"
        
        logging.info(f"Serving NOSTR template: {template_name} (type={type})")
        
        return render_page(request, template_name)
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors du chargement du template NOSTR: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Erreur interne lors du chargement du template: {str(e)}"
        )
