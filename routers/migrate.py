"""
routers/migrate.py — Onglet « Déménager » de /scan : déménager son MULTIPASS vers CETTE station avec le QR P1 du
billet MULTIPASS + le PASS, sans être connecté (ce qu'on a quand on arrive sur une station inconnue).

- POST /api/migration/start   {qr, pass, from_node?} → {email, ticket, status}   (QR P1 « M-… » + PASS)
- GET  /api/migration/status  ?email=&ticket=       → état et progression du rapatriement des documents
- POST /api/migration/abort   {email, ticket}       → annule une demande non aboutie
- POST /api/migration/pin     {email, ticket}       → relance le rapatriement des documents (état pin_error)

Aucune signature ici : la preuve est faite par `multipass_migrate.py` (process court, seul à lire le NSEC). Le PASS est
vérifié par l'ANCIENNE station (compteur anti-bruteforce, 3 échecs = verrou + Capitaine prévenu). Le ticket renvoyé au
navigateur (aléatoire, seul son hachage est gardé) est le seul moyen de suivre la demande : pas d'oracle sur les comptes.
Les corps de requête (QR, PASS) ne sont jamais journalisés. Version authentifiée NIP-98 : /api/account/migration.
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services import migration_api
from utils.security import safe_json_body

router = APIRouter()

_CODES = {"bad_qr": 422, "bad_pass": 422, "hosted_here": 409, "hydrate_failed": 404,
          "request_failed": 409, "throttled": 429}


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@router.post("/api/migration/start", summary="Déménager mon MULTIPASS vers cette station (QR P1 + PASS)")
async def migration_start(request: Request):
    body = await safe_json_body(request)
    res = await asyncio.to_thread(migration_api.start_from_qr, str(body.get("qr") or ""), str(body.get("pass") or ""),
                                  body.get("from_node") or None, _ip(request))
    if not res["ok"]:
        return JSONResponse(status_code=_CODES.get(res.get("code"), 400),
                            content={k: res[k] for k in ("code", "error") if k in res})
    return JSONResponse(status_code=202, content={k: res[k] for k in ("email", "ticket", "status")})


@router.get("/api/migration/status", summary="Suivi d'un déménagement (ticket)")
async def migration_status(email: str = "", ticket: str = ""):
    st = await asyncio.to_thread(migration_api.status_for_ticket, email, ticket)
    if st is None:
        raise HTTPException(status_code=404, detail="Aucun déménagement pour ce ticket.")
    return JSONResponse(st)


@router.post("/api/migration/pin", summary="Relancer le rapatriement des documents (ticket)")
async def migration_pin(request: Request):
    body = await safe_json_body(request)
    res = await asyncio.to_thread(migration_api.resume_pin_for_ticket, str(body.get("email") or ""), str(body.get("ticket") or ""))
    if res is None:
        raise HTTPException(status_code=404, detail="Aucun déménagement pour ce ticket.")
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "relance impossible"))
    return JSONResponse(res, status_code=202)


@router.post("/api/migration/abort", summary="Annuler un déménagement non abouti (ticket)")
async def migration_abort(request: Request):
    body = await safe_json_body(request)
    res = await asyncio.to_thread(migration_api.abort_for_ticket, str(body.get("email") or ""), str(body.get("ticket") or ""))
    if res is None:
        raise HTTPException(status_code=404, detail="Aucun déménagement pour ce ticket.")
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "annulation impossible"))
    return JSONResponse(res)
