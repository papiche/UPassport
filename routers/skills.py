"""
skills.py — Endpoints pour le système de compétences WoTx2

GET  /api/skill/session       — Dernière session install (install_session.json)
GET  /api/skill/media/{skill} — Médias Kind 30504 partagés pour un skill (relay NOSTR)
GET  /api/skill/oracles       — Pubkeys NOSTR hex des oracles WoTx2 (nœud + constellation)

« Qui d'autre maîtrise ça dans le coin ? » (NIP-98, MULTIPASS de cette station ; cf. services/skill_map.py) :
GET    /api/skills/mine       — mes savoir-faire (certificats 30503) + leur visibilité (none|region|sector|umap) + ma position
POST   /api/skills            — déclarer / mettre à jour : skill, level, description, icon, visibility, lat?, lon?
DELETE /api/skills?skill=     — retirer (certificat + annuaire des cases)
GET    /api/skills/nearby     — skill, scope (umap|sector|region), min_level, lat?, lon? → les autres membres, vérifiés
GET    /api/skills/of?hex=    — savoir-faire déclarés par un membre (publics : ses certificats)
"""
import json
import glob
import os
import re
import asyncio
import websockets
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import JSONResponse

from services import relay_io, skill_map
from services.nostr import fetch_nostr_profiles, require_nostr_auth

logger = logging.getLogger(__name__)
router = APIRouter()

RELAY_WS = os.environ.get("NOSTR_RELAY_WS", "ws://127.0.0.1:7777")
_IPFS_CONFIG = os.path.expanduser("~/.ipfs/config")


def _get_ipfsnodeid() -> str:
    try:
        with open(_IPFS_CONFIG) as f:
            return json.load(f).get("Identity", {}).get("PeerID", "")
    except Exception:
        return ""


@router.get("/api/skill/session", summary="Dernière session install pour craft WoTx2")
async def get_skill_session():
    """
    Retourne le JSON de la dernière session install publiée dans
    ~/.zen/tmp/$IPFSNODEID/install_session.json
    Utilisé par install_craft.html pour afficher les skills détectés.
    """
    ipfsnodeid = _get_ipfsnodeid()
    if ipfsnodeid:
        session_file = os.path.expanduser(f"~/.zen/tmp/{ipfsnodeid}/install_session.json")
        if os.path.exists(session_file):
            try:
                with open(session_file) as f:
                    data = json.load(f)
                data["ipfsnodeid"] = ipfsnodeid
                return JSONResponse(content=data)
            except Exception as e:
                logger.warning(f"Lecture install_session.json: {e}")

    # Fallback : dernier fichier log de session dans ~/.zen/log/
    log_dir = os.path.expanduser("~/.zen/log")
    sessions = sorted(glob.glob(f"{log_dir}/install_session_*.log"), reverse=True)
    if sessions:
        return JSONResponse(content={
            "type": "install_session",
            "ipfsnodeid": ipfsnodeid,
            "log_file": sessions[0],
            "timestamp": "",
            "captain": "",
            "profile": "standard",
            "mode": "install",
            "score": 0,
            "tier": "unknown",
            "log_cid": ""
        })

    raise HTTPException(status_code=404, detail="Aucune session install trouvée")


@router.get("/api/skill/media/{skill}", summary="Médias partagés pour un skill (Kind 30504)")
async def get_skill_media(skill: str, limit: int = 20):
    """
    Interroge le relay NOSTR local pour les events Kind 30504 liés à ce skill.
    Retourne les CIDs IPFS des médias existants pour que le capitaine puisse
    réutiliser une preuve déjà partagée dans la constellation.
    """
    skill_norm = skill.lower().strip()
    results = []

    try:
        filt = json.dumps({
            "kinds": [30504],
            "#t": [skill_norm],
            "limit": limit
        })
        msg = json.dumps(["REQ", "skill_media_" + skill_norm[:16], json.loads(filt)])

        async def _fetch():
            async with websockets.connect(RELAY_WS, open_timeout=5, close_timeout=3) as ws:
                await ws.send(msg)
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        data = json.loads(raw)
                        if data[0] == "EOSE":
                            break
                        if data[0] == "EVENT" and len(data) > 2:
                            ev = data[2]
                            # Extraire CID depuis les tags r ou url
                            for tag in ev.get("tags", []):
                                if tag[0] in ("r", "url") and len(tag) > 1:
                                    val = tag[1]
                                    if val.startswith("ipfs://"):
                                        results.append({
                                            "cid": val[7:],
                                            "url": val,
                                            "event_id": ev.get("id", ""),
                                            "pubkey": ev.get("pubkey", ""),
                                            "created_at": ev.get("created_at", 0),
                                            "content": _safe_content(ev.get("content", ""))
                                        })
                    except asyncio.TimeoutError:
                        break
        await _fetch()
    except Exception as e:
        logger.debug(f"get_skill_media relay error: {e}")

    # Dédoublonner par CID
    seen = set()
    deduped = []
    for item in results:
        if item["cid"] not in seen:
            seen.add(item["cid"])
            deduped.append(item)

    return JSONResponse(content={"skill": skill_norm, "media": deduped, "count": len(deduped)})


@router.get("/api/skill/oracles", summary="Pubkeys NOSTR des oracles WoTx2 (Nœud + Constellation)")
async def get_oracle_pubkeys():
    """
    Retourne les pubkeys hex NOSTR (64 chars) des deux oracles WoTx2 :
    - node          : ~/.zen/game/secret.nostr           — clé NOSTR locale du nœud
    - constellation : ~/.zen/game/uplanet.G1.nostr       — clé du 1er bootstrap IPFS (primaire)

    Les événements Kind 30503 oracle sont identifiables par le tag ["l","PERMIT_SKILL_Xn","permit_type"].
    Ces pubkeys permettent aux clients de filtrer les vues oracle dans le widget SkillCloud.
    """
    hex_re = re.compile(r'^[0-9a-fA-F]{64}$')
    keyfiles = {
        "node":          "~/.zen/game/secret.nostr",
        "constellation": "~/.zen/game/uplanet.G1.nostr",
    }
    result = {}
    for name, path in keyfiles.items():
        try:
            content = open(os.path.expanduser(path)).read()
            for segment in re.split(r'[;\n]', content):
                segment = segment.strip()
                if segment.startswith('HEX='):
                    val = segment[4:].strip().rstrip(';').strip()
                    if hex_re.match(val):
                        result[name] = val
                    break
        except Exception:
            pass
    return JSONResponse(content=result)


def _safe_content(content: str) -> dict:
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return {k: v for k, v in obj.items() if k in ("title", "skill_tag", "cid", "uploaded_at")}
    except Exception:
        pass
    return {}


# ─── « Qui d'autre maîtrise ça dans le coin ? » ─────────────────────────────────────────────────────────
def _me(npub: str):
    """(email, hex) du MULTIPASS authentifié sur CETTE station."""
    from routers.cloud import _email_for_authenticated_npub, _hex_from_npub
    return _email_for_authenticated_npub(npub), _hex_from_npub(npub)


def _position(email: str, lat, lon):
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    return skill_map.member_gps(email)


async def _relay_guard(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except relay_io.RelayUnavailable:
        raise HTTPException(status_code=503, detail="Aucun relais NOSTR ne répond : réessayez dans un instant.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        logger.error("skills: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/api/skills/mine", summary="Mes savoir-faire et leur visibilité")
async def skills_mine(lat: float = Query(None), lon: float = Query(None), npub: str = Depends(require_nostr_auth)):
    email, hex_pub = _me(npub)
    pos = _position(email, lat, lon)
    mine = await _relay_guard(skill_map.skills_of, hex_pub)
    vis = await _relay_guard(skill_map.my_visibility, hex_pub, *pos) if pos else {}
    for sk in mine:
        sk["visibility"] = vis.get(sk["skill"], "none")
    return {"skills": mine, "position": {"lat": pos[0], "lon": pos[1]} if pos else None, "cells": skill_map.cell_names(*pos) if pos else {}}


@router.post("/api/skills", summary="Déclarer un savoir-faire")
async def skills_declare(skill: str = Form(...), level: int = Form(1), description: str = Form(""), icon: str = Form(""),
                         visibility: str = Form(skill_map.DEFAULT_VISIBILITY), lat: float = Form(None), lon: float = Form(None),
                         npub: str = Depends(require_nostr_auth)):
    email, _hex = _me(npub)
    return await _relay_guard(skill_map.declare, email, skill, level, description.strip(), icon.strip(), visibility, lat, lon)


@router.delete("/api/skills", summary="Retirer un savoir-faire")
async def skills_retract(skill: str = Query(..., min_length=2), npub: str = Depends(require_nostr_auth)):
    email, _hex = _me(npub)
    return await _relay_guard(skill_map.retract, email, skill)


@router.get("/api/skills/nearby", summary="Qui d'autre maîtrise ce savoir-faire dans le coin ?")
async def skills_nearby(skill: str = Query(..., min_length=2), scope: str = Query("sector"), min_level: int = Query(1, ge=1, le=9),
                        lat: float = Query(None), lon: float = Query(None), npub: str = Depends(require_nostr_auth)):
    email, hex_pub = _me(npub)
    pos = _position(email, lat, lon)
    if not pos:
        raise HTTPException(status_code=400, detail="position inconnue : indiquez lat et lon")
    found = await _relay_guard(skill_map.nearby, skill, pos[0], pos[1], scope, min_level, hex_pub)
    profiles = await fetch_nostr_profiles([f["hex"] for f in found][:50]) if found else {}
    for f in found:
        pr = profiles.get(f["hex"]) or {}
        f["name"] = pr.get("display_name") or pr.get("name") or ""
    return {"skill": skill_map.skill_norm(skill), "scope": scope, "cell": skill_map.cell_names(*pos)[scope], "members": found}


@router.get("/api/skills/of", summary="Savoir-faire déclarés par un membre")
async def skills_of(hex: str = Query(..., min_length=64, max_length=64), npub: str = Depends(require_nostr_auth)):
    _me(npub)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", hex):
        raise HTTPException(status_code=400, detail="clé publique invalide")
    return {"hex": hex.lower(), "skills": await _relay_guard(skill_map.skills_of, hex.lower())}
