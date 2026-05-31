"""
skills.py — Endpoints pour le système de compétences WoTx2

GET  /api/skill/session       — Dernière session install (install_session.json)
GET  /api/skill/media/{skill} — Médias Kind 30504 partagés pour un skill (relay NOSTR)
GET  /api/skill/oracles       — Pubkeys NOSTR hex des oracles WoTx2 (nœud + constellation)
"""
import json
import glob
import os
import re
import asyncio
import websockets
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

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
