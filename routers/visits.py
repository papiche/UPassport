from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from core.middleware import get_client_ip
from services import visits
from utils.security import safe_json_body

router = APIRouter()


@router.post("/visit", summary="Compteur de visites de l'essaim",
             description="Compte une visite (ou un clic event=contribute) unique par IP, par jour et par site. Accepte n'importe quel site embarquant assets/visits.js.")
async def visit(request: Request):
    data = await safe_json_body(request, max_bytes=1024, max_depth=2)
    site = visits.normalize_site(data.get("site") if isinstance(data, dict) else None)
    if not site:
        raise HTTPException(status_code=400, detail="Site invalide.")
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname != site:  # un navigateur ne peut pas falsifier Origin
        raise HTTPException(status_code=403, detail="Origin différent du site déclaré.")
    event, label = data.get("event"), data.get("label", "")
    if event is not None and (event not in visits.EVENTS or not isinstance(label, str) or not visits.LABEL.match(label)):
        raise HTTPException(status_code=400, detail="Événement invalide.")
    counted = await run_in_threadpool(visits.count_visit, site, get_client_ip(request), event, label if event else "")
    if counted is None:
        raise HTTPException(status_code=429, detail="Trop de sites ou d'emplacements suivis sur cette station.")
    return {"ok": True, "counted": counted}
