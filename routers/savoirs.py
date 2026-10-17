"""
routers/savoirs.py — Révisions chaînées des savoirs partagés (logique « git » sur NOSTR + IPFS).

CONVENTION (cf. UPlanet/earth/CLAUDE.md § Révisions chaînées) : un événement WoTx²
(kinds 30500-30505) qui REMPLACE une version précédente porte
    ["prev", "<CID IPFS de l'événement précédent>"]   — le parent
    ["rev",  "<N>"]                                   — numéro de révision (parent + 1)
    ["merge","<CID>"]                                 — facultatif : second parent (fusion)
Le CID est celui du JSON CANONIQUE de l'événement parent (clés triées, sans espaces),
donc recalculable par n'importe qui. Un événement remplaçable NOSTR écrase le précédent
sur le relay ; la chaîne, elle, survit sur IPFS — c'est l'historique. Un FORK est une
révision publiée par un AUTRE auteur dont `prev` pointe vers l'événement d'origine.

- POST /api/savoirs/pin       épingle un événement signé (NIP-98 requis) → {cid}
- GET  /api/savoirs/history   remonte la chaîne `prev` depuis un CID (public, lecture seule)
"""
import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from services.nostr import require_nostr_auth
from utils.crypto import verify_nostr_event
from utils.security import safe_json_body

router = APIRouter()
logger = logging.getLogger(__name__)

KNOWLEDGE_KINDS = {30500, 30501, 30502, 30503, 30504, 30505}
MAX_EVENT_BYTES = 64 * 1024
MAX_CHAIN = 60
_CID_RE = re.compile(r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{50,100})$")


def _env():
    env = dict(os.environ)
    env["PATH"] = f"{Path.home()}/.local/bin:/usr/local/bin:" + env.get("PATH", "")
    return env


def canonical(event: dict) -> bytes:
    """JSON canonique : clés triées, séparateurs compacts, UTF-8 — ce qui est haché pour le CID."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _tag(event: dict, name: str) -> str:
    for t in event.get("tags", []):
        if isinstance(t, list) and t and t[0] == name and len(t) > 1:
            return str(t[1])
    return ""


def validate_event(event) -> dict:
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="event invalide.")
    if event.get("kind") not in KNOWLEDGE_KINDS:
        raise HTTPException(status_code=400, detail=f"kind non géré (attendu : {sorted(KNOWLEDGE_KINDS)}).")
    if len(canonical(event)) > MAX_EVENT_BYTES:
        raise HTTPException(status_code=413, detail="événement trop volumineux.")
    try:
        ok = verify_nostr_event(event)
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(status_code=400, detail="signature ou identifiant d'événement invalide.")
    return event


def _pin(event: dict) -> str:
    r = subprocess.run(["ipfs", "add", "-Q", "--pin=true"], input=canonical(event), capture_output=True, timeout=30, env=_env())
    cid = r.stdout.decode().strip()
    if r.returncode != 0 or not _CID_RE.match(cid):
        raise HTTPException(status_code=503, detail=("IPFS indisponible : " + r.stderr.decode()[:200]).strip())
    return cid


def _cat(cid: str):
    r = subprocess.run(["ipfs", "cat", "-l", str(MAX_EVENT_BYTES + 1), cid], capture_output=True, timeout=20, env=_env())
    if r.returncode != 0 or not r.stdout or len(r.stdout) > MAX_EVENT_BYTES:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except Exception:
        return None


def _history(cid: str, limit: int) -> dict:
    chain, seen, cur, broken = [], set(), cid, ""
    while cur and len(chain) < limit:
        if cur in seen:
            broken = "cycle détecté"
            break
        seen.add(cur)
        ev = _cat(cur)
        if ev is None:
            broken = f"révision introuvable sur IPFS ({cur[:12]}…)"
            break
        try:
            valid = ev.get("kind") in KNOWLEDGE_KINDS and verify_nostr_event(ev)
        except Exception:
            valid = False
        if not valid:
            broken = f"révision non valide ({cur[:12]}…)"
            break
        rev = _tag(ev, "rev")
        chain.append({"cid": cur, "event": ev, "rev": int(rev) if rev.isdigit() else None, "merge": _tag(ev, "merge") or None})
        nxt = _tag(ev, "prev")
        cur = nxt if _CID_RE.match(nxt or "") else ""
    return {"chain": chain, "complete": not broken and not cur, "problem": broken or None}


@router.post("/api/savoirs/pin")
async def savoirs_pin(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {event}. L'événement doit être un événement de savoir (30500-30505) correctement signé."""
    body = await safe_json_body(request)
    event = validate_event(body.get("event"))
    cid = await asyncio.to_thread(_pin, event)
    return JSONResponse({"cid": cid, "id": event.get("id"), "rev": _tag(event, "rev") or None})


@router.get("/api/savoirs/history")
async def savoirs_history(cid: str, limit: int = 30):
    """Chaîne des révisions en remontant `prev` depuis `cid` (la plus récente d'abord)."""
    if not _CID_RE.match(cid or ""):
        raise HTTPException(status_code=400, detail="CID invalide.")
    return JSONResponse(await asyncio.to_thread(_history, cid, max(1, min(int(limit), MAX_CHAIN))))
