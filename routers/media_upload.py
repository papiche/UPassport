import os
import json
import time
import uuid
import base64
import hashlib
import logging
logger = logging.getLogger(__name__)
import asyncio
import traceback
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

import aiofiles
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from utils.helpers import run_script, get_myipfs_gateway, as_form, safe_json_load
from core.middleware import get_client_ip
from core.config import settings, ASTRO_PYTHON
from services.ipfs import run_uDRIVE_generation_script
from utils.security import (
    get_authenticated_user_directory,
    get_max_file_size_for_user,
    validate_uploaded_file,
    sanitize_filename_python,
    detect_file_type,
    find_user_directory_by_hex,
    is_safe_email
)
from services.nostr import verify_nostr_auth, require_nostr_auth
from utils.crypto import npub_to_hex, hex_to_npub
from services.nostr import fetch_video_event_from_nostr, parse_video_metadata
from models.schemas import UploadResponse, UploadFromDriveResponse
from services.cookie_store import store_cookie_encrypted

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _safe_arg(val: str) -> str:
    """Prevent option injection: strip leading dashes from user-supplied subprocess args.
    Values starting with '--' could be misinterpreted as flags by bash arg parsers."""
    if val.startswith('-'):
        stripped = val.lstrip('-')
        return stripped if stripped else val
    return val


def _get_node_nsec() -> str:
    """Lit le NSEC du NODE local depuis secret.nostr."""
    secret_path = settings.GAME_PATH / "secret.nostr"
    if secret_path.exists():
        for line in secret_path.read_text().splitlines():
            if line.startswith("NSEC="):
                return line[5:].strip()
    return ""


def _get_node_relays() -> list:
    relays = [r for r in settings.NOSTR_RELAYS.split() if r.startswith("wss://")]
    return relays or ["wss://relay.copylaradio.com"]


async def _resolve_home_node_hex(user_dir: Path) -> str:
    """Résout le NODE HEX de la home station pour un utilisateur roaming.

    Ordre de priorité (du plus rapide au plus lent) :
      1. home.station local (cache d'une résolution précédente)
      2. Scan strfry kind 0 du joueur → champ home_station (local, rapide)
      3. Scan swarm TW : swarm/*/TW/{email}/ → 12345.json → NODEHEX
      4. IPFS via NOSTRNS (réseau, peut échouer)
      5. IPFS via HOME_IPFSNODEID (réseau, peut échouer)

    Cache le résultat dans user_dir/home.station.
    Retourne "" si introuvable.

    Note : HOME_IPFSNODEID et NOSTRNS sauvegardés par 22242.sh pour
    amisOfAmis_roaming contiennent la clé IPNS CIDv1 du joueur (k51…),
    pas le peer ID libp2p de la home station — d'où la priorité au kind 0.
    """
    user_email = user_dir.name
    home_station_file = user_dir / "home.station"

    # 1. Cache local home.station
    if home_station_file.exists():
        content = home_station_file.read_text().strip()
        if ":" in content:
            candidate = content.split(":", 1)[1].strip()
            if len(candidate) == 64:
                return candidate

    # 2. Scan strfry local : kind 0 du joueur → champ home_station
    user_hex_file = user_dir / "HEX"
    user_hex = user_hex_file.read_text().strip() if user_hex_file.exists() else ""
    if len(user_hex) == 64:
        strfry_dir = settings.ZEN_PATH / "strfry"
        strfry_bin = strfry_dir / "strfry"
        if strfry_bin.exists():
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(strfry_bin), "scan",
                    json.dumps({"authors": [user_hex], "kinds": [0]}),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=str(strfry_dir),
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                events = []
                for line in out.decode().splitlines():
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except Exception:
                            pass
                if events:
                    latest = max(events, key=lambda e: e.get("created_at", 0))
                    try:
                        profile = json.loads(latest.get("content", "{}"))
                        hs = profile.get("home_station", "")
                        if ":" in hs:
                            candidate = hs.split(":", 1)[1].strip()
                            if len(candidate) == 64:
                                home_station_file.write_text(hs.strip() + "\n")
                                logger.info(
                                    f"Roaming: home_station résolu via kind 0 relay pour {user_email}"
                                )
                                return candidate
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Roaming: strfry scan kind 0 échec: {e}")

    # 3. Scan swarm TW : trouver la station qui héberge cet email
    swarm_dir = settings.ZEN_PATH / "tmp" / "swarm"
    if swarm_dir.exists():
        for station_dir in swarm_dir.iterdir():
            if not station_dir.is_dir():
                continue
            if (station_dir / "TW" / user_email).exists():
                station_12345 = station_dir / "12345.json"
                if station_12345.exists():
                    try:
                        data = json.loads(station_12345.read_text())
                        candidate = data.get("NODEHEX", "")
                        if len(candidate) == 64:
                            hid = data.get("ipfsnodeid", station_dir.name)
                            home_station_file.write_text(f"{hid}:{candidate}\n")
                            logger.info(
                                f"Roaming: home_station résolu via swarm TW pour {user_email}"
                            )
                            return candidate
                    except Exception:
                        pass

    # 4. IPFS via NOSTRNS (lent, peut échouer si non pinné)
    nostrns_file = user_dir / "NOSTRNS"
    if nostrns_file.exists():
        nostrns = nostrns_file.read_text().strip()
        if nostrns:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ipfs", "--timeout", "10s", "cat",
                    f"{nostrns}/{user_email}/home.station",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
                content = out.decode().strip()
                if ":" in content:
                    candidate = content.split(":", 1)[1].strip()
                    if len(candidate) == 64:
                        home_station_file.write_text(content + "\n")
                        return candidate
            except Exception:
                pass

    # 5. IPFS via HOME_IPFSNODEID (dernier recours)
    home_ipfsnodeid_file = user_dir / "HOME_IPFSNODEID"
    if home_ipfsnodeid_file.exists():
        home_ipfsnodeid = home_ipfsnodeid_file.read_text().strip()
        if home_ipfsnodeid:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ipfs", "--timeout", "10s", "cat",
                    f"/ipns/{home_ipfsnodeid}/{user_email}/home.station",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
                content = out.decode().strip()
                if ":" in content:
                    candidate = content.split(":", 1)[1].strip()
                    if len(candidate) == 64:
                        home_station_file.write_text(content + "\n")
                        logger.info(
                            f"Roaming: home.station récupéré via HOME_IPFSNODEID IPNS pour {user_email}"
                        )
                        return candidate
            except Exception:
                pass

    logger.warning(f"Roaming: home_node_hex introuvable pour {user_email}")
    return ""


async def _maybe_send_roaming_dm(
    user_dir: Path, file_cid: str, sanitized_filename: str, file_type: str
) -> bool:
    """Si l'utilisateur est en roaming, envoie un DM NIP-04 (kind 4) à la home station.

    Retourne True si le DM a été envoyé avec succès.
    En cas d'échec, laisser le fichier en place — NOSTRCARD.refresh.sh le reprendra.
    """
    if not (user_dir / ".roaming").exists():
        return False

    user_email = user_dir.name
    home_node_hex = await _resolve_home_node_hex(user_dir)
    if not home_node_hex:
        return False

    node_nsec = _get_node_nsec()
    if not node_nsec:
        logger.warning(f"Roaming DM: secret.nostr absent pour {user_email}")
        return False

    ft_map = {"image": "image", "video": "video", "audio": "audio", "document": "document"}
    filetype_arg = ft_map.get(file_type, "file")

    intercom = settings.TOOLS_PATH / "nostr_node_intercom.py"
    cmd = [
        ASTRO_PYTHON, str(intercom), "send-udrive",
        "--nsec", node_nsec,
        "--to", home_node_hex,
        "--email", user_email,
        "--cid", file_cid,
        "--filename", sanitized_filename,
        "--filetype", filetype_arg,
        "--relays", *_get_node_relays(),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            logger.info(
                f"✈️ Roaming DM OK: {user_email} → {home_node_hex[:12]}… "
                f"file={sanitized_filename} cid={file_cid[:12]}…"
            )
            return True
        logger.warning(
            f"✈️ Roaming DM échec (code {proc.returncode}): {stderr.decode()[:200]}"
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        logger.warning(f"✈️ Roaming DM timeout pour {user_email}")
    except Exception as e:
        logger.warning(f"✈️ Roaming DM erreur: {e}")
    return False


async def _send_roaming_media_event_dm(
    user_dir: Path, channel: str, payload: dict
) -> bool:
    """En roaming, envoie un DM au home NODE pour publier un event NOSTR media.

    Channels : 'vocals' (kind 1222/1244), 'webcam' (kind 21/22).
    La home station (NOSTRCARD.refresh.sh) reçoit le DM et publie l'event.
    """
    home_node_hex = await _resolve_home_node_hex(user_dir)
    if not home_node_hex:
        return False

    node_nsec = _get_node_nsec()
    if not node_nsec:
        logger.warning(f"Roaming {channel} DM: secret.nostr absent")
        return False

    intercom = settings.TOOLS_PATH / "nostr_node_intercom.py"
    cmd = [
        ASTRO_PYTHON, str(intercom), "send",
        "--nsec", node_nsec,
        "--to", home_node_hex,
        "--channel", channel,
        "--payload", json.dumps(payload),
        "--relays", *_get_node_relays(),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            logger.info(
                f"✈️ Roaming {channel} DM OK: {user_dir.name} → {home_node_hex[:12]}…"
            )
            return True
        logger.warning(
            f"✈️ Roaming {channel} DM échec (code {proc.returncode}): {stderr.decode()[:200]}"
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        logger.warning(f"✈️ Roaming {channel} DM timeout")
    except Exception as e:
        logger.warning(f"✈️ Roaming {channel} DM erreur: {e}")
    return False

@as_form
class WebcamForm(BaseModel):
    player: str
    ipfs_cid: str
    title: str
    npub: str
    file_hash: str
    info_cid: str
    thumbnail_ipfs: str = ""
    gifanim_ipfs: str = ""
    mime_type: str = "video/webm"
    upload_chain: str = ""
    duration: str = "0"
    video_dimensions: str = "640x480"
    file_size: str = "0"
    description: str = ""
    publish_nostr: str = "false"
    latitude: str = ""
    longitude: str = ""
    youtube_url: str = ""
    genres: str = ""

@router.get("/webcam", response_class=HTMLResponse)
async def get_webcam_page(request: Request):
    """Render the webcam recording page"""
    return templates.TemplateResponse(request, "webcam.html", {
        "myIPFS": await get_myipfs_gateway()
    })

@router.post("/webcam", response_class=HTMLResponse)
async def process_webcam_video(
    request: Request,
    form_data: WebcamForm = Depends(WebcamForm.as_form)
):
    player = form_data.player
    ipfs_cid = form_data.ipfs_cid
    title = form_data.title
    npub = form_data.npub
    file_hash = form_data.file_hash
    info_cid = form_data.info_cid
    thumbnail_ipfs = form_data.thumbnail_ipfs
    gifanim_ipfs = form_data.gifanim_ipfs
    mime_type = form_data.mime_type
    upload_chain = form_data.upload_chain
    duration = form_data.duration
    video_dimensions = form_data.video_dimensions
    file_size = form_data.file_size
    description = form_data.description
    publish_nostr = form_data.publish_nostr
    latitude = form_data.latitude
    longitude = form_data.longitude
    youtube_url = form_data.youtube_url
    genres = form_data.genres
    """Process webcam video and publish to NOSTR as NIP-71 video event"""
    if not ipfs_cid or not ipfs_cid.strip():
        return templates.TemplateResponse(request, "webcam.html", {
            "error": "No IPFS CID provided. Video must be uploaded via /api/fileupload first.", 
            "recording": False,
            "myIPFS": await get_myipfs_gateway()
        })

    try:
        file_size_from_form = 0
        try:
            file_size_from_form = int(file_size) if file_size and file_size != "0" else 0
        except (ValueError, TypeError):
            file_size_from_form = 0
        
        file_size = file_size_from_form
        video_dimensions_param = video_dimensions if video_dimensions and video_dimensions != "640x480" else "640x480"
        try:
            duration_param = int(float(duration)) if duration else 0
        except (ValueError, TypeError):
            duration_param = 0
        
        video_dimensions = video_dimensions_param
        duration = duration_param
        thumbnail_ipfs_from_info = thumbnail_ipfs if thumbnail_ipfs else ""
        gifanim_ipfs_from_info = gifanim_ipfs if gifanim_ipfs else ""
        
        if info_cid:
            try:
                import httpx
                gateway = await get_myipfs_gateway()
                info_url = f"{gateway}/ipfs/{info_cid}"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    info_response = await client.get(info_url)
                    if info_response.status_code == 200:
                        info_data = info_response.json()
                        if info_data.get("file") and info_data["file"].get("hash"):
                            file_hash = info_data["file"]["hash"]
                        if info_data.get("provenance") and info_data["provenance"].get("upload_chain"):
                            upload_chain = info_data["provenance"]["upload_chain"]
                        
                        protocol_version = info_data.get("protocol", {}).get("version", "1.0.0")
                        is_v2 = protocol_version.startswith("2.")
                        
                        if info_data.get("media"):
                            media = info_data["media"]
                            if media.get("dimensions"):
                                video_dimensions = media["dimensions"]
                            if media.get("duration"):
                                duration = float(media["duration"])
                            if file_size == 0 and media.get("file_size"):
                                try: file_size = int(media["file_size"])
                                except (ValueError, TypeError): pass
                            
                            if not thumbnail_ipfs:
                                if is_v2 and media.get("thumbnails"):
                                    thumbnail_cid = media["thumbnails"].get("static") or media["thumbnails"].get("animated")
                                    if thumbnail_cid:
                                        thumbnail_ipfs_from_info = thumbnail_cid.replace("/ipfs/", "").replace("ipfs://", "")
                                elif not is_v2 and media.get("thumbnail_ipfs"):
                                    thumbnail_ipfs_from_info = media["thumbnail_ipfs"].replace("/ipfs/", "").replace("ipfs://", "")
                            
                            if not gifanim_ipfs:
                                if is_v2 and media.get("thumbnails"):
                                    gifanim_cid = media["thumbnails"].get("animated")
                                    if gifanim_cid:
                                        gifanim_ipfs_from_info = gifanim_cid.replace("/ipfs/", "").replace("ipfs://", "")
                                elif not is_v2 and media.get("gifanim_ipfs"):
                                    gifanim_ipfs_from_info = media["gifanim_ipfs"].replace("/ipfs/", "").replace("ipfs://", "")
                        
                        if file_size == 0 and info_data.get("file_size"):
                            try: file_size = int(info_data["file_size"])
                            except (ValueError, TypeError): pass
                        if file_size == 0 and info_data.get("fileSize"):
                            try: file_size = int(info_data["fileSize"])
                            except (ValueError, TypeError): pass
            except Exception as e:
                logger.warning(f"Could not load metadata from info.json: {e}")
        
        final_thumbnail_ipfs = thumbnail_ipfs if thumbnail_ipfs else thumbnail_ipfs_from_info
        final_gifanim_ipfs = gifanim_ipfs if gifanim_ipfs else gifanim_ipfs_from_info
        
        hex_pubkey = npub_to_hex(npub) if npub else None
        filename = None
        
        if hex_pubkey:
            try:
                user_dir = find_user_directory_by_hex(hex_pubkey)
                directory_email = user_dir.name if '@' in user_dir.name else None
                
                if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player):
                    if directory_email and is_safe_email(directory_email):
                        player = directory_email
                elif not is_safe_email(player):
                    if directory_email and is_safe_email(directory_email):
                        player = directory_email
                
                user_drive_path = user_dir / "APP" / "uDRIVE" / "Videos"
                if user_drive_path.exists():
                    video_files = sorted(user_drive_path.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if video_files:
                        filename = video_files[0].name
                        if file_size == 0:
                            file_size = video_files[0].stat().st_size
            except Exception:
                if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player) or not is_safe_email(player):
                    return templates.TemplateResponse(request, "webcam.html", {
                        "error": "Could not determine user email.", 
                        "recording": False,
                        "myIPFS": await get_myipfs_gateway()
                    })
        
        if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player) or not is_safe_email(player):
            return templates.TemplateResponse(request, "webcam.html", {
                "error": "No valid email address could be determined.", 
                "recording": False,
                "myIPFS": await get_myipfs_gateway()
            })
        
        if not filename:
            filename = f"video_{int(time.time())}.webm"
        
        if file_size == 0:
            return templates.TemplateResponse(request, "webcam.html", {
                "error": "File size is missing or invalid.", 
                "recording": False,
                "myIPFS": await get_myipfs_gateway()
            })
        
        ipfs_url = f"/ipfs/{ipfs_cid}/{filename}"
        if not title:
            title = f"Webcam recording {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        nostr_event_id = None
        if publish_nostr.lower() == "true" and npub:
            try:
                if not await verify_nostr_auth(npub):
                    return templates.TemplateResponse(request, "webcam.html", {
                        "error": "NOSTR authentication failed.", 
                        "recording": False
                    })
                
                user_dir = get_authenticated_user_directory(npub)
                secret_file = user_dir / ".secret.nostr"
                
                if not secret_file.exists():
                    # Roaming : déléguer la publication vidéo à la home station via DM
                    if isinstance(user_dir, Path) and (user_dir / ".roaming").exists():
                        try:
                            _lat = float(latitude) if latitude else 0.00
                            _lon = float(longitude) if longitude else 0.00
                        except (ValueError, TypeError):
                            _lat = 0.00
                            _lon = 0.00
                        _fname = filename or f"video_{int(time.time())}.webm"
                        payload = {
                            "email":          user_dir.name,
                            "cid":            ipfs_cid,
                            "filename":       _fname,
                            "filetype":       "video",
                            "mime_type":      mime_type or "video/webm",
                            "duration":       str(duration),
                            "title":          title,
                            "description":    description or "",
                            "dimensions":     str(video_dimensions),
                            "file_size":      str(file_size),
                            "thumbnail_ipfs": final_thumbnail_ipfs or "",
                            "gifanim_ipfs":   final_gifanim_ipfs or "",
                            "info_cid":       info_cid or "",
                            "file_hash":      file_hash or "",
                            "latitude":       str(_lat),
                            "longitude":      str(_lon),
                            "channel":        player,
                        }
                        dm_sent = await _send_roaming_media_event_dm(user_dir, "webcam", payload)
                        if dm_sent:
                            _ipfs_url = f"/ipfs/{ipfs_cid}/{_fname}"
                            return templates.TemplateResponse(request, "webcam.html", {
                                "message": f"Vidéo transmise à la home station (roaming). IPFS: {_ipfs_url}",
                                "recording": False,
                                "ipfs_url": _ipfs_url,
                            })
                    return templates.TemplateResponse(request, "webcam.html", {
                        "error": "NOSTR secret file not found.",
                        "recording": False
                    })
                
                try:
                    lat = float(latitude) if latitude else 0.00
                    lon = float(longitude) if longitude else 0.00
                except (ValueError, TypeError):
                    lat = 0.00
                    lon = 0.00
                
                publish_script = settings.TOOLS_PATH / "publish_nostr_video.sh"
                if not os.path.exists(publish_script):
                    return templates.TemplateResponse(request, "webcam.html", {
                        "error": "NOSTR publish script not found.", 
                        "recording": False
                    })
                
                publish_cmd = [
                    "bash", publish_script,
                    "--nsec", str(secret_file),
                    "--ipfs-cid", ipfs_cid,
                    "--filename", _safe_arg(filename),
                    "--title", _safe_arg(title),
                    "--json"
                ]

                if description: publish_cmd.extend(["--description", _safe_arg(description)])
                if final_thumbnail_ipfs: publish_cmd.extend(["--thumbnail-cid", final_thumbnail_ipfs])
                if final_gifanim_ipfs: publish_cmd.extend(["--gifanim-cid", final_gifanim_ipfs])
                if info_cid: publish_cmd.extend(["--info-cid", info_cid])
                if file_hash: publish_cmd.extend(["--file-hash", file_hash])
                if mime_type: publish_cmd.extend(["--mime-type", mime_type])
                if upload_chain:
                    if isinstance(upload_chain, (list, dict)):
                        upload_chain_str = json.dumps(upload_chain)
                    else:
                        upload_chain_str = str(upload_chain)
                    publish_cmd.extend(["--upload-chain", upload_chain_str])
                
                if isinstance(video_dimensions, dict):
                    width = video_dimensions.get('width', '')
                    height = video_dimensions.get('height', '')
                    if width and height:
                        dimensions_str = f"{width}x{height}"
                    else:
                        dimensions_str = json.dumps(video_dimensions)
                else:
                    dimensions_str = str(video_dimensions)
                
                publish_cmd.extend([
                    "--duration", str(duration),
                    "--dimensions", dimensions_str,
                    "--file-size", str(file_size),
                    "--latitude", str(lat),
                    "--longitude", str(lon),
                    "--channel", player
                ])
                
                if youtube_url:
                    publish_cmd.extend(["--source-type", "youtube"])
                else:
                    publish_cmd.extend(["--source-type", "webcam"])
                
                if genres and genres.strip():
                    try:
                        genres_json = json.loads(genres)
                        if isinstance(genres_json, list) and len(genres_json) > 0:
                            genres_compact = json.dumps(genres_json, ensure_ascii=False, separators=(',', ':'))
                            publish_cmd.extend(["--genres", genres_compact])
                    except (json.JSONDecodeError, ValueError):
                        pass
                
                process = await asyncio.create_subprocess_exec(
                    *publish_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    raise

                if process.returncode == 0:
                    try:
                        result_json = safe_json_load(stdout.decode().strip())
                        nostr_event_id = result_json.get('event_id', '')
                    except ValueError:
                        event_id_match = re.search(r'"event_id"\s*:\s*"([a-f0-9]{64})"', stdout.decode())
                        if event_id_match:
                            nostr_event_id = event_id_match.group(1)
                        else:
                            nostr_event_id = stdout.decode().strip().split('\n')[-1] if stdout.decode() else ""
                            if not re.match(r'^[a-f0-9]{64}$', nostr_event_id):
                                nostr_event_id = ""
            except Exception as e:
                logger.error(f"Error during NOSTR publishing: {e}")

        success_message = f"Video processed successfully! IPFS: {ipfs_url}"
        if nostr_event_id:
            success_message += f" | NOSTR Event: {nostr_event_id}"
        
        return templates.TemplateResponse(request, "webcam.html", {
            "message": success_message,
            "recording": False,
            "ipfs_url": ipfs_url,
            "nostr_event_id": nostr_event_id,
            "video_info": {
                "title": title,
                "duration": duration,
                "dimensions": video_dimensions,
                "file_size": file_size
            }
        })

    except Exception as e:
        logger.error(f"Error processing webcam video: {e}")
        return templates.TemplateResponse(request, "webcam.html", {
            "error": f"Error processing video: {str(e)}", 
            "recording": False
        })


@as_form
class VocalsForm(BaseModel):
    player: str
    ipfs_cid: str
    title: str
    npub: str
    file_hash: str
    info_cid: str = ""
    mime_type: str = "audio/mpeg"
    file_name: str = ""
    duration: str = "0"
    description: str = ""
    publish_nostr: str = "false"
    latitude: str = ""
    longitude: str = ""
    encrypted: str = "false"
    encryption_method: str = "nip44"
    recipients: str = ""
    waveform: str = ""
    kind: str = "1222"
    reply_to_event_id: str = ""
    reply_to_pubkey: str = ""
    expiration: str = ""

@router.post("/vocals", response_class=HTMLResponse)
async def process_vocals_message(
    request: Request,
    form_data: VocalsForm = Depends(VocalsForm.as_form)
):
    player = form_data.player
    ipfs_cid = form_data.ipfs_cid
    title = form_data.title
    npub = form_data.npub
    file_hash = form_data.file_hash
    info_cid = form_data.info_cid
    mime_type = form_data.mime_type
    file_name = form_data.file_name
    duration = form_data.duration
    description = form_data.description
    publish_nostr = form_data.publish_nostr
    latitude = form_data.latitude
    longitude = form_data.longitude
    encrypted = form_data.encrypted
    encryption_method = form_data.encryption_method
    recipients = form_data.recipients
    waveform = form_data.waveform
    kind = form_data.kind
    reply_to_event_id = form_data.reply_to_event_id
    reply_to_pubkey = form_data.reply_to_pubkey
    expiration = form_data.expiration
    """Process voice message and publish to NOSTR as NIP-A0 voice event"""
    if not ipfs_cid or not ipfs_cid.strip():
        return templates.TemplateResponse(request, "vocals.html", {
            "error": "No IPFS CID provided.",
            "myIPFS": await get_myipfs_gateway()
        })
    
    is_encrypted = encrypted.lower() == "true"
    if is_encrypted:
        if not recipients or not recipients.strip():
            return templates.TemplateResponse(request, "vocals.html", {
                "error": "Recipients required for encrypted voice messages.",
                "myIPFS": await get_myipfs_gateway()
            })
        try:
            recipients_list = json.loads(recipients)
            if not isinstance(recipients_list, list) or len(recipients_list) == 0:
                raise ValueError("Recipients must be a non-empty array")
        except Exception as e:
            return templates.TemplateResponse(request, "vocals.html", {
                "error": f"Invalid recipients format: {e}",
                "myIPFS": await get_myipfs_gateway()
            })
    
    try:
        try:
            user_dir = get_authenticated_user_directory(npub)
            secret_file = user_dir / ".secret.nostr"
        except Exception:
            user_dir = settings.GAME_PATH / "nostr" / player
            secret_file = os.path.join(user_dir, ".secret.dunikey")
            if not os.path.exists(secret_file):
                secret_file = os.path.join(user_dir, ".secret.nostr")
        
        if not os.path.exists(secret_file):
            # Roaming : déléguer la publication NOSTR à la home station via DM
            if isinstance(user_dir, Path) and (user_dir / ".roaming").exists():
                _fname = (file_name.strip() if file_name and file_name.strip()
                          else f"voice_{int(time.time())}.{mime_type.split('/')[-1] if '/' in mime_type else 'mp3'}")
                try:
                    _vkind = int(kind) if kind else 1222
                    if _vkind not in [1222, 1244]:
                        _vkind = 1222
                except (ValueError, TypeError):
                    _vkind = 1222
                payload = {
                    "email":             user_dir.name,
                    "cid":               ipfs_cid,
                    "filename":          _fname,
                    "filetype":          "audio",
                    "mime_type":         mime_type,
                    "duration":          str(duration),
                    "title":             title,
                    "description":       description or "",
                    "waveform":          waveform or "",
                    "kind":              str(_vkind),
                    "file_hash":         file_hash or "",
                    "info_cid":          info_cid or "",
                }
                if _vkind == 1244 and reply_to_event_id and reply_to_pubkey:
                    payload["reply_to_event_id"] = reply_to_event_id
                    payload["reply_to_pubkey"]   = reply_to_pubkey
                dm_sent = await _send_roaming_media_event_dm(user_dir, "vocals", payload)
                if dm_sent:
                    return templates.TemplateResponse(request, "vocals.html", {
                        "success": "Message vocal transmis à la home station (roaming).",
                        "event_id": "",
                        "myIPFS": await get_myipfs_gateway()
                    })
            return templates.TemplateResponse(request, "vocals.html", {
                "error": "NOSTR authentication required.",
                "myIPFS": await get_myipfs_gateway()
            })
        
        try:
            lat = float(latitude) if latitude else 0.00
            lon = float(longitude) if longitude else 0.00
        except (ValueError, TypeError):
            lat = 0.00
            lon = 0.00

        try:
            voice_kind = int(kind) if kind else 1222
            if voice_kind not in [1222, 1244]:
                voice_kind = 1222
        except (ValueError, TypeError):
            voice_kind = 1222
        
        if voice_kind == 1244:
            if not reply_to_event_id or not reply_to_event_id.strip() or not reply_to_pubkey or not reply_to_pubkey.strip():
                voice_kind = 1222
        
        publish_script = settings.TOOLS_PATH / "publish_nostr_vocal.sh"
        if not os.path.exists(publish_script):
            return templates.TemplateResponse(request, "vocals.html", {
                "error": "NOSTR publish script not found.",
                "myIPFS": await get_myipfs_gateway()
            })
        
        if file_name and file_name.strip():
            actual_filename = file_name.strip()
        else:
            extension = mime_type.split('/')[-1] if '/' in mime_type else 'mp3'
            actual_filename = f"voice_{int(time.time())}.{extension}"
        
        publish_cmd = [
            "bash", publish_script,
            "--nsec", str(secret_file),
            "--ipfs-cid", ipfs_cid,
            "--filename", _safe_arg(actual_filename),
            "--title", _safe_arg(title),
            "--json",
            "--kind", str(voice_kind)
        ]

        if description: publish_cmd.extend(["--description", _safe_arg(description)])
        if file_hash: publish_cmd.extend(["--file-hash", file_hash])
        if mime_type: publish_cmd.extend(["--mime-type", mime_type])
        if duration: publish_cmd.extend(["--duration", str(duration)])
        if lat != 0.00 or lon != 0.00: publish_cmd.extend(["--latitude", str(lat), "--longitude", str(lon)])
        if waveform: publish_cmd.extend(["--waveform", waveform])
        if info_cid: publish_cmd.extend(["--info-cid", info_cid])
        
        if voice_kind == 1244 and reply_to_event_id and reply_to_pubkey:
            publish_cmd.extend(["--reply-to-event-id", reply_to_event_id])
            publish_cmd.extend(["--reply-to-pubkey", reply_to_pubkey])
        
        if expiration and expiration.strip():
            try:
                exp_timestamp = int(expiration)
                if exp_timestamp > 0:
                    publish_cmd.extend(["--expiration", str(exp_timestamp)])
            except (ValueError, TypeError):
                pass
        
        if is_encrypted:
            publish_cmd.extend(["--encrypted", "true"])
            publish_cmd.extend(["--encryption-method", encryption_method])
            if recipients:
                publish_cmd.extend(["--recipients", recipients])
        
        publish_cmd.extend(["--channel", player])

        process = await asyncio.create_subprocess_exec(
            *publish_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            try:
                process.kill() # <-- TUE LE PROCESSUS BASH ENFANT
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="timeout")
        
        if process.returncode == 0:
            try:
                result_json = safe_json_load(stdout.decode().strip())
                nostr_event_id = result_json.get('event_id', '')
                return templates.TemplateResponse(request, "vocals.html", {
                    "success": f"Voice message published successfully! Event ID: {nostr_event_id[:16]}...",
                    "event_id": nostr_event_id,
                    "myIPFS": await get_myipfs_gateway()
                })
            except ValueError:
                return templates.TemplateResponse(request, "vocals.html", {
                    "error": "Voice message published but could not parse response.",
                    "myIPFS": await get_myipfs_gateway()
                })
        else:
            return templates.TemplateResponse(request, "vocals.html", {
                "error": f"Failed to publish voice message: {stderr.decode()}",
                "myIPFS": await get_myipfs_gateway()
            })
            
    except Exception as e:
        return templates.TemplateResponse(request, "vocals.html", {
            "error": f"Error processing voice message: {str(e)}",
            "myIPFS": await get_myipfs_gateway()
        })

def transform_youtube_metadata_to_structured(flat_metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Transform flat YouTube metadata to structured format"""
    channel_name = flat_metadata.get('channel') or flat_metadata.get('uploader', '')
    channel_info = {
        'display_name': channel_name,
        'name': channel_name,
        'channel_id': flat_metadata.get('channel_id', ''),
        'channel_url': flat_metadata.get('channel_url', ''),
        'channel_follower_count': flat_metadata.get('channel_follower_count')
    }
    
    content_info = {
        'description': flat_metadata.get('description', ''),
        'language': flat_metadata.get('language', ''),
        'license': flat_metadata.get('license', ''),
        'tags': flat_metadata.get('tags', []),
        'categories': flat_metadata.get('categories', [])
    }
    
    technical_info = {
        'abr': flat_metadata.get('abr', 0),
        'acodec': flat_metadata.get('acodec', ''),
        'format_note': flat_metadata.get('format_note', ''),
        'vcodec': flat_metadata.get('vcodec', ''),
        'vbr': flat_metadata.get('vbr', 0),
        'tbr': flat_metadata.get('tbr', 0),
        'resolution': flat_metadata.get('resolution', ''),
        'fps': flat_metadata.get('fps', 0)
    }
    
    statistics = {
        'view_count': flat_metadata.get('view_count', 0),
        'like_count': flat_metadata.get('like_count', 0),
        'comment_count': flat_metadata.get('comment_count', 0),
        'average_rating': flat_metadata.get('average_rating')
    }
    
    dates = {
        'upload_date': flat_metadata.get('upload_date', ''),
        'release_date': flat_metadata.get('release_date', ''),
        'timestamp': flat_metadata.get('timestamp'),
        'release_timestamp': flat_metadata.get('release_timestamp')
    }
    
    media_info = {
        'artist': flat_metadata.get('artist', ''),
        'album': flat_metadata.get('album', ''),
        'track': flat_metadata.get('track', ''),
        'creator': flat_metadata.get('creator', '')
    }
    
    thumbnails = {
        'thumbnail': flat_metadata.get('thumbnail', ''),
        'thumbnails': flat_metadata.get('thumbnails', [])
    }
    
    playlist_info = {}
    if flat_metadata.get('playlist') or flat_metadata.get('playlist_id'):
        playlist_info = {
            'playlist': flat_metadata.get('playlist', ''),
            'playlist_id': flat_metadata.get('playlist_id', ''),
            'playlist_title': flat_metadata.get('playlist_title', ''),
            'playlist_index': flat_metadata.get('playlist_index'),
            'n_entries': flat_metadata.get('n_entries')
        }
    
    subtitles_info = {
        'subtitles': flat_metadata.get('subtitles', {}),
        'automatic_captions': flat_metadata.get('automatic_captions', {})
    }
    
    live_info = {
        'live_status': flat_metadata.get('live_status', ''),
        'was_live': flat_metadata.get('was_live', False),
        'is_live': flat_metadata.get('is_live', False)
    }
    
    structured = {
        'title': flat_metadata.get('title', ''),
        'description': flat_metadata.get('description', ''),
        'duration': flat_metadata.get('duration', 0),
        'youtube_url': flat_metadata.get('youtube_url', ''),
        'youtube_short_url': flat_metadata.get('youtube_short_url', ''),
        'youtube_id': flat_metadata.get('youtube_id', ''),
        'uploader': flat_metadata.get('uploader', ''),
        'uploader_id': flat_metadata.get('uploader_id', ''),
        'uploader_url': flat_metadata.get('uploader_url', ''),
        
        'channel_info': channel_info,
        'content_info': content_info,
        'technical_info': technical_info,
        'statistics': statistics,
        'dates': dates,
        'media_info': media_info,
        'thumbnails': thumbnails,
        'playlist_info': playlist_info if playlist_info else None,
        'subtitles_info': subtitles_info,
        'chapters': flat_metadata.get('chapters', []),
        'live_info': live_info,
        
        'age_limit': flat_metadata.get('age_limit'),
        'availability': flat_metadata.get('availability', ''),
        'format': flat_metadata.get('format', ''),
        'format_id': flat_metadata.get('format_id', ''),
        'ext': flat_metadata.get('ext', ''),
        'filesize': flat_metadata.get('filesize'),
        'filesize_approx': flat_metadata.get('filesize_approx'),
        'location': flat_metadata.get('location'),
        'license': flat_metadata.get('license', ''),
        'languages': flat_metadata.get('languages', []),
        'extractor': flat_metadata.get('extractor', ''),
        'extractor_key': flat_metadata.get('extractor_key', ''),
        
        'youtube_metadata': flat_metadata
    }
    
    for key in ['playlist_info', 'channel_info', 'content_info', 'technical_info', 
                'statistics', 'dates', 'media_info', 'thumbnails', 'subtitles_info', 'live_info']:
        if structured.get(key) is None:
            del structured[key]
        elif isinstance(structured.get(key), dict):
            structured[key] = {k: v for k, v in structured[key].items() if v is not None}
    
    return structured

# ── Chiffrement de fichiers (UENC format) ────────────────────────────────────

_UENC_MAGIC = b"UENC"
_UENC_VERSION = 0x01
_UENC_TYPE_AES256GCM = 0x01
_UENC_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

def _encrypt_aes256gcm(data: bytes, key_hex: str) -> tuple[bytes, str]:
    """Chiffre `data` avec AES-256-GCM. Retourne (payload_uenc, iv_hex)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise ValueError("La clef doit faire 32 octets (64 hex chars)")
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(iv, data, None)  # ciphertext + 16 bytes tag
    payload = _UENC_MAGIC + bytes([_UENC_VERSION, _UENC_TYPE_AES256GCM]) + iv + ciphertext
    return payload, iv.hex()


@router.post("/api/fileupload/encrypted")
async def upload_encrypted_file_to_ipfs(
    request: Request,
    file: UploadFile = File(...),
    encryption_key: str = Form(...),
    encryption_type: str = Form("aes256gcm"),
    npub: Optional[str] = Form(None),
):
    """Upload d'un fichier chiffré sur IPFS.

    Le client fournit une clef AES-256 (64 hex chars = 32 bytes).
    Le serveur chiffre le fichier avec AES-256-GCM, l'upload sur IPFS,
    et retourne le CID du fichier chiffré.

    Format UENC : MAGIC(4) + VERSION(1) + ENC_TYPE(1) + IV(12) + CIPHERTEXT+TAG
    """
    if not file:
        raise HTTPException(status_code=400, detail="Aucun fichier fourni.")

    # Valider la clef
    if encryption_type != "aes256gcm":
        raise HTTPException(status_code=400, detail=f"Type de chiffrement non supporté: {encryption_type}. Utilisez 'aes256gcm'.")
    try:
        key_bytes = bytes.fromhex(encryption_key)
        if len(key_bytes) != 32:
            raise ValueError("La clef doit faire exactement 32 octets (64 hex chars)")
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"encryption_key invalide: {e}")

    # Lire et valider la taille
    file_content = await file.read()
    if len(file_content) > _UENC_MAX_FILE_SIZE:
        size_mb = len(file_content) // 1048576
        raise HTTPException(status_code=413, detail=f"Fichier trop grand ({size_mb} MB, max 20 MB)")
    if not file_content:
        raise HTTPException(status_code=400, detail="Fichier vide.")

    try:
        encrypted_payload, iv_hex = _encrypt_aes256gcm(file_content, encryption_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur de chiffrement: {e}")

    # Upload sur IPFS via API locale
    try:
        import aiohttp as _aiohttp
        original_filename = sanitize_filename_python(file.filename or "file")
        enc_filename = f"enc_{os.urandom(4).hex()}_{original_filename}"
        form_data = _aiohttp.FormData()
        form_data.add_field(
            "file", encrypted_payload,
            filename=enc_filename,
            content_type="application/octet-stream",
        )
        ipfs_api = "http://127.0.0.1:5001"
        timeout = _aiohttp.ClientTimeout(total=30)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{ipfs_api}/api/v0/add", data=form_data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise HTTPException(status_code=502, detail=f"Erreur IPFS: {body[:200]}")
                result = await resp.json()
                cid = result["Hash"]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upload IPFS échoué: {e}")

    logger.info(
        f"[encrypted-upload] {original_filename} → CID={cid[:16]}… "
        f"enc={encryption_type} iv={iv_hex[:8]}… "
        f"size={len(file_content)}→{len(encrypted_payload)} "
        f"npub={npub[:16] if npub else 'anon'}…"
    )

    response = JSONResponse(content={
        "success": True,
        "cid": cid,
        "encryption_type": encryption_type,
        "iv_hex": iv_hex,
        "original_filename": original_filename,
        "original_size": len(file_content),
        "encrypted_size": len(encrypted_payload),
    }, status_code=200)
    response.headers["X-Encryption-Type"] = encryption_type
    response.headers["X-Encrypted-CID"] = cid
    response.headers["X-Encryption-IV"] = iv_hex
    return response


@router.post("/api/fileupload", response_model=UploadResponse)
@router.post("/api/upload", response_model=UploadResponse)
async def upload_file_to_ipfs(
    request: Request,
    file: UploadFile = File(...),
    npub: Optional[str] = Form(None),
    youtube_metadata: Optional[UploadFile] = File(None)
):
    """Upload file to IPFS with NIP-42 or NIP-98 authentication."""
    npub = await require_nostr_auth(request, npub, force_check=True)

    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    try:
        max_size_bytes = get_max_file_size_for_user(npub)
        if file.size and file.size > max_size_bytes:
            max_size_mb = max_size_bytes // 1048576
            file_size_mb = file.size // 1048576
            raise HTTPException(
                status_code=413,
                detail=f"File size ({file_size_mb}MB) exceeds maximum allowed size ({max_size_mb}MB)"
            )
        
        max_size_mb = max_size_bytes // 1048576
        validation_result = await validate_uploaded_file(file, max_size_mb=max_size_mb)
        if not validation_result["is_valid"]:
            raise HTTPException(status_code=400, detail=validation_result["error"])
        
        try:
            user_NOSTR_path = get_authenticated_user_directory(npub)
        except HTTPException:
            # Roaming user authenticated via NIP-98 only (no NIP-42 → no email dir created
            # by 22242.sh).  We cannot update the manifest on their behalf because:
            #   1. We don't have their email → can't create an @-based dir that
            #      NOSTRCARD.refresh.sh's roaming loop would process.
            #   2. We don't have their IPNS key → can't publish.
            # The safest response is to refuse the upload so the client knows to retry
            # on the home station rather than silently losing the manifest entry.
            raise HTTPException(
                status_code=403,
                detail="User not registered on this station. Upload directly to your home station or authenticate with NIP-42 first."
            )
        user_drive_path = user_NOSTR_path / "APP" / "uDRIVE"

        file_content = await file.read()
        file_type = detect_file_type(file_content, file.filename or "untitled")
        
        if file.filename and file.filename.endswith('.txt'):
            try:
                content_text = file_content.decode('utf-8')
                is_netscape_format = False
                detected_domain = None
                
                if '# Netscape HTTP Cookie File' in content_text or '# HTTP Cookie File' in content_text:
                    is_netscape_format = True
                elif '\t' in content_text:
                    lines = [l.strip() for l in content_text.split('\n') if l.strip() and not l.strip().startswith('#')]
                    if lines:
                        first_line = lines[0]
                        parts = first_line.split('\t')
                        if len(parts) >= 7:
                            is_netscape_format = True
                
                if is_netscape_format:
                    domains = set()
                    lines = content_text.split('\n')
                    for line in lines:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        parts = line.split('\t')
                        if len(parts) >= 7:
                            domain = parts[0].strip()
                            if domain.startswith('.'):
                                domain = domain[1:]
                            domains.add(domain)
                    
                    if domains:
                        base_domains = set()
                        for domain in domains:
                            parts = domain.split('.')
                            if len(parts) >= 2:
                                base_domain = '.'.join(parts[-2:])
                                base_domains.add(base_domain)
                            else:
                                base_domains.add(domain)
                        
                        if len(base_domains) > 1:
                            raise HTTPException(
                                status_code=400, 
                                detail=f"Multi-domain cookie files are not supported."
                            )
                        
                        # LCA : sous-domaine commun le plus spécifique
                        # ex: {notebooklm.google.com} → "notebooklm.google.com"
                        #     {notebooklm.google.com, google.com} → "google.com"
                        _parts_list = [list(reversed(d.split("."))) for d in domains]
                        _common = []
                        _min_len = min(len(p) for p in _parts_list)
                        for _i in range(_min_len):
                            _labels = {p[_i] for p in _parts_list}
                            if len(_labels) == 1:
                                _common.append(next(iter(_labels)))
                            else:
                                break
                        detected_domain = ".".join(reversed(_common)) if _common else next(iter(domains))
                    else:
                        raise HTTPException(
                            status_code=400, 
                            detail="Invalid cookie file: no domains detected"
                        )
                    
                    hex_pubkey = npub_to_hex(npub)
                    user_root_dir = find_user_directory_by_hex(hex_pubkey)
                    
                    cookie_filename = f".{detected_domain}.cookie"
                    cookie_path = user_root_dir / cookie_filename

                    async with aiofiles.open(cookie_path, 'wb') as cookie_file:
                        await cookie_file.write(file_content)

                    os.chmod(cookie_path, 0o600)

                    # ── Split sous-domaines (flag=FALSE = host-only) ───────────────────
                    # Ex: fichier avec .google.com (TRUE) + notebooklm.google.com (FALSE)
                    # → crée aussi .notebooklm.google.com.cookie pour sessions privées
                    _subdomains: dict[str, list[str]] = {}
                    _header_lines = []
                    for _line in content_text.split('\n'):
                        _ls = _line.strip()
                        if not _ls or _ls.startswith('#'):
                            _header_lines.append(_line)
                            continue
                        _parts = _ls.split('\t')
                        if len(_parts) < 7:
                            continue
                        _col_domain = _parts[0].lstrip('.')
                        _col_flag   = _parts[1].strip().upper()
                        if _col_flag == 'FALSE' and _col_domain != detected_domain:
                            _subdomains.setdefault(_col_domain, []).append(_line)

                    for _subdomain, _lines in _subdomains.items():
                        _sub_path = user_root_dir / f".{_subdomain}.cookie"
                        _sub_bytes = ('\n'.join(_header_lines) + '\n' + '\n'.join(_lines) + '\n').encode()
                        async with aiofiles.open(_sub_path, 'wb') as _sf:
                            await _sf.write(_sub_bytes)
                        os.chmod(_sub_path, 0o600)
                        logger.info(f"Cookie sous-domaine {_subdomain} → {_sub_path.name}")
                        try:
                            await store_cookie_encrypted(user_root_dir, _subdomain, _sub_bytes)
                        except Exception as _e:
                            logger.warning(f"Cookie subdomain IPFS store failed (non-fatal): {_e}")

                    # Encrypt with user G1 key → IPFS pin → manifest + NOSTR kind 31903
                    cid = None
                    try:
                        cid = await store_cookie_encrypted(user_root_dir, detected_domain, file_content)
                    except Exception as _e:
                        logger.warning(f"Cookie IPFS/NOSTR store failed (non-fatal): {_e}")

                    _sub_info = f" + sous-domaines: {', '.join(_subdomains)}" if _subdomains else ""
                    return UploadResponse(
                        success=True,
                        message=f"Cookie file uploaded successfully for {detected_domain}{_sub_info}",
                        file_path=str(cookie_path.relative_to(user_root_dir.parent)),
                        file_type="netscape_cookies",
                        target_directory=str(user_root_dir),
                        new_cid=cid,
                        timestamp=datetime.now().isoformat(),
                        auth_verified=True,
                        description=(
                            f"Domain: {detected_domain} — IPFS: {cid[:20]}…"
                            if cid else f"Domain: {detected_domain or 'unknown'}"
                        ),
                    )
            except UnicodeDecodeError:
                pass
            except Exception as e:
                logger.warning(f"Cookie file processing error: {e}")

        if file_type == 'image':
            target_dir = user_drive_path / "Images"
        elif file_type == 'video':
            target_dir = user_drive_path / "Videos"
        elif file_type == 'audio':
            target_dir = user_drive_path / "Music"
        elif file_type == 'document':
            target_dir = user_drive_path / "Documents"
        elif file_type == 'application':
            target_dir = user_drive_path / "Apps"
        else:
            target_dir = user_drive_path / "Documents"
        
        target_dir.mkdir(parents=True, exist_ok=True)
        
        original_filename = file.filename if file.filename else "untitled_file"
        sanitized_filename = sanitize_filename_python(original_filename)
        
        description = None
        if file_type == 'image':
            try:
                temp_image_path = target_dir / f"temp_{uuid.uuid4()}_{sanitized_filename}"
                async with aiofiles.open(temp_image_path, 'wb') as out_file:
                    await out_file.write(file_content)
                
                describe_script = settings.ZEN_PATH / "Astroport.ONE" / "IA" / "describe_image.py"
                
                custom_prompt = "Décris ce qui se trouve sur cette image en 10-30 mots clés concis et précis. Ne génère qu'une description courte sans phrase complète, ni introduction."
                desc_process = await asyncio.create_subprocess_exec(
                    "python3", describe_script, str(temp_image_path), "--json", "--prompt", custom_prompt,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    desc_stdout, desc_stderr = await asyncio.wait_for(desc_process.communicate(), timeout=60)
                except asyncio.TimeoutError:
                    try:
                        desc_process.kill()
                    except ProcessLookupError:
                        pass
                    raise

                if desc_process.returncode == 0:
                    try:
                        desc_json = safe_json_load(desc_stdout.decode())
                        description = desc_json.get('description', '')
                        if description:
                            description = description.strip()
                    except ValueError:
                        pass
                
                if os.path.exists(temp_image_path):
                    os.remove(temp_image_path)
            except Exception:
                pass
        
        file_path = target_dir / sanitized_filename
        
        async with aiofiles.open(file_path, 'wb') as out_file:
            await out_file.write(file_content)
        
        tmp_dir = settings.ZEN_PATH / "tmp"
        os.makedirs(tmp_dir, exist_ok=True)
        temp_file_path = os.path.join(tmp_dir, f"temp_{uuid.uuid4()}.json")
        
        script_path = None
        possible_paths = [
            "./upload2ipfs.sh",
            str(settings.ZEN_PATH / "Astroport.ONE" / "UPassport" / "upload2ipfs.sh"),
            os.path.join(os.path.dirname(__file__), "..", "upload2ipfs.sh")
        ]
        for path in possible_paths:
            if os.path.exists(path) and os.path.isfile(path):
                script_path = os.path.abspath(path)
                break
        
        if not script_path:
            raise HTTPException(status_code=500, detail="upload2ipfs.sh script not found")
        
        user_pubkey_hex = ""
        try:
            if npub and npub != "anonymous":
                user_pubkey_hex = npub_to_hex(npub)
        except Exception:
            pass
        
        youtube_metadata_file = None
        if youtube_metadata:
            try:
                youtube_metadata_content = await youtube_metadata.read()
                youtube_metadata_json = json.loads(youtube_metadata_content.decode('utf-8'))
                structured_metadata = transform_youtube_metadata_to_structured(youtube_metadata_json)
                
                youtube_metadata_file = os.path.join(tmp_dir, f"youtube_metadata_{uuid.uuid4()}.json")
                async with aiofiles.open(youtube_metadata_file, 'w') as metadata_file:
                    await metadata_file.write(json.dumps(structured_metadata, indent=2))
            except Exception:
                youtube_metadata_file = None
        
        if youtube_metadata_file:
            return_code, last_line = await run_script(
                script_path, 
                "--metadata", youtube_metadata_file,
                str(file_path), 
                temp_file_path, 
                user_pubkey_hex
            )
            if os.path.exists(youtube_metadata_file):
                os.remove(youtube_metadata_file)
        else:
            return_code, last_line = await run_script(script_path, str(file_path), temp_file_path, user_pubkey_hex)
        
        if return_code == 0:
            try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip())
                
                response_fileName = json_output.get('fileName') or sanitized_filename
                info_cid = json_output.get('info')
                cidirect = json_output.get('cidirect') or ''
                thumbnail_cid = json_output.get('thumbnail_ipfs') or ''
                gifanim_cid = json_output.get('gifanim_ipfs') or ''
                file_hash = json_output.get('fileHash') or ''
                mime_type = json_output.get('mimeType') or ''
                duration = json_output.get('duration')
                dimensions = json_output.get('dimensions') or ''
                provenance_info = json_output.get('provenance', {})
                upload_chain = provenance_info.get('upload_chain') or ''
                is_reupload = provenance_info.get('is_reupload', False)
                
                file_mime = mime_type or json_output.get('mimeType', '')
                
                if not file_mime.startswith('video/') and not file_mime.startswith('audio/') and user_pubkey_hex:
                    try:
                        user_dir = get_authenticated_user_directory(npub)
                        secret_file = user_dir / ".secret.nostr"
                        
                        if secret_file.exists():
                            publish_script = settings.TOOLS_PATH / "publish_nostr_file.sh"
                            
                            if os.path.exists(publish_script):
                                file_type_display = file_type.capitalize()
                                event_description = f"{file_type_display}: {response_fileName}"
                                if description:
                                    event_description = f"{description}"
                                
                                if is_reupload:
                                    original_author = provenance_info.get('original_author', '')[:16]
                                    event_description = f"📤 Re-upload: {event_description} (Original: {original_author}...)"
                                
                                publish_cmd = [
                                    "bash", publish_script,
                                    "--auto", temp_file_path,
                                    "--nsec", str(secret_file),
                                    "--title", _safe_arg(response_fileName),
                                    "--description", _safe_arg(event_description),
                                    "--json"
                                ]
                                
                                process = await asyncio.create_subprocess_exec(
                                    *publish_cmd,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE
                                )
                                try:
                                    await asyncio.wait_for(process.communicate(), timeout=30)
                                except asyncio.TimeoutError:
                                    try:
                                        process.kill()
                                    except ProcessLookupError:
                                        pass
                    except Exception:
                        pass
                
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)

                file_cid = json_output.get('cid')
                udrive_cid = file_cid

                # ── Roaming : DM direct vers la home station ─────────────────
                # Activer si : marqueur .roaming explicite OU pas de uDRIVE local.
                # Dans les deux cas, la home station se charge du manifest et de la publication IPNS.
                is_roaming = (user_NOSTR_path / ".roaming").exists() or not user_drive_path.exists()
                if is_roaming and file_cid:
                    dm_sent = await _maybe_send_roaming_dm(
                        user_dir=user_NOSTR_path,
                        file_cid=file_cid,
                        sanitized_filename=sanitized_filename,
                        file_type=file_type,
                    )
                    if dm_sent:
                        try:
                            file_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        if os.path.exists(temp_file_path):
                            os.remove(temp_file_path)
                        return UploadResponse(
                            success=True,
                            message="Fichier relayé à la home station via DM",
                            file_path=str(file_path),
                            file_type=file_type,
                            target_directory=str(target_dir),
                            new_cid=file_cid,
                            file_cid=file_cid if file_cid else None,
                            timestamp=datetime.now().isoformat(),
                            auth_verified=True,
                            fileName=sanitized_filename,
                            description=description,
                            info=info_cid,
                            cidirect=cidirect if cidirect else None,
                            thumbnail_ipfs=thumbnail_cid if thumbnail_cid else None,
                            gifanim_ipfs=gifanim_cid if gifanim_cid else None,
                            fileHash=file_hash if file_hash else None,
                            mimeType=mime_type if mime_type else None,
                            duration=int(duration) if duration is not None else None,
                            dimensions=dimensions if dimensions else None,
                        )
                    # DM échoué → conserver le fichier localement, retourner le CID brut
                    # La home station seule publie l'IPNS — pas de régénération locale
                    return UploadResponse(
                        success=True,
                        message="Fichier IPFS uploadé. Sync home station en attente (DM différé).",
                        file_path=str(file_path),
                        file_type=file_type,
                        target_directory=str(target_dir),
                        new_cid=file_cid,
                        file_cid=file_cid if file_cid else None,
                        timestamp=datetime.now().isoformat(),
                        auth_verified=True,
                        fileName=sanitized_filename,
                        description=description,
                        info=info_cid,
                        cidirect=cidirect if cidirect else None,
                        thumbnail_ipfs=thumbnail_cid if thumbnail_cid else None,
                        gifanim_ipfs=gifanim_cid if gifanim_cid else None,
                        fileHash=file_hash if file_hash else None,
                        mimeType=mime_type if mime_type else None,
                        duration=int(duration) if duration is not None else None,
                        dimensions=dimensions if dimensions else None,
                    )

                # ── Régénérer la structure uDRIVE (utilisateurs locaux uniquement) ──
                # On conserve le CID du fichier seul en fallback si le script échoue.
                try:
                    ipfs_result = await run_uDRIVE_generation_script(user_drive_path)
                    udrive_cid = ipfs_result.get("final_cid") or file_cid
                    logger.info(f"uDRIVE regenerated after upload: {udrive_cid}")
                except Exception as gen_err:
                    logger.warning(f"uDRIVE generation failed, returning file CID: {gen_err}")

                return UploadResponse(
                    success=True,
                    message=f"File uploaded successfully to IPFS",
                    file_path=str(file_path),
                    file_type=file_type,
                    target_directory=str(target_dir),
                    new_cid=udrive_cid,
                    file_cid=file_cid if file_cid else None,
                    timestamp=datetime.now().isoformat(),
                    auth_verified=True,
                    fileName=response_fileName,
                    description=description,
                    info=info_cid,
                    cidirect=cidirect if cidirect else None,
                    thumbnail_ipfs=thumbnail_cid if thumbnail_cid else None,
                    gifanim_ipfs=gifanim_cid if gifanim_cid else None,
                    fileHash=file_hash if file_hash else None,
                    mimeType=mime_type if mime_type else None,
                    duration=int(duration) if duration is not None else None,
                    dimensions=dimensions if dimensions else None,
                    upload_chain=upload_chain if upload_chain else None
                )
                
            except (json.JSONDecodeError, FileNotFoundError) as e:
                raise HTTPException(status_code=500, detail=f"Failed to process IPFS upload: {e}")
            finally:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
        else:
            raise HTTPException(status_code=500, detail=f"IPFS upload failed: {last_line.strip()}")
            
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

from models.schemas import UploadFromDriveRequest, UploadFromDriveResponse

@router.post("/api/upload_from_drive", response_model=UploadFromDriveResponse)
async def upload_from_drive(request: Request, payload: UploadFromDriveRequest):
    if payload.owner_hex_pubkey or payload.owner_email:
        logger.info(f"Sync from drive - Source owner: {payload.owner_email} (hex: {payload.owner_hex_pubkey[:12] if payload.owner_hex_pubkey else 'N/A'}...)")
    
    payload.npub = await require_nostr_auth(request, payload.npub)

    try:
        user_NOSTR_path = get_authenticated_user_directory(payload.npub)
        user_drive_path = user_NOSTR_path / "APP" / "uDRIVE"

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error determining user directory: {e}")

    parts = payload.ipfs_link.split('/')
    extracted_filename = parts[-1] if parts else "downloaded_file"

    sanitized_filename = sanitize_filename_python(extracted_filename)

    file_type = detect_file_type(b'', sanitized_filename)

    target_directory_name = "Documents"
    if file_type == "image":
        target_directory_name = "Images"
    elif file_type == "audio":
        target_directory_name = "Music"
    elif file_type == "video":
        target_directory_name = "Videos"

    target_directory = user_drive_path / target_directory_name
    target_directory.mkdir(parents=True, exist_ok=True)

    target_file_path = (target_directory / sanitized_filename).resolve()

    if not target_file_path.is_relative_to(user_drive_path):
        raise HTTPException(status_code=400, detail="Invalid file path operation: attempted to write outside user's directory.")

    try:
        full_ipfs_url = f"/ipfs/{payload.ipfs_link}"
        logger.info(f"Attempting to download IPFS link: {full_ipfs_url} to {target_file_path}")

        ipfs_get_command = ["ipfs", "get", "-o", str(target_file_path), full_ipfs_url]
        process = await asyncio.create_subprocess_exec(
            *ipfs_get_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="IPFS download timeout")

        if process.returncode != 0:
            error_message = stderr.decode().strip()
            logger.error(f"IPFS download failed for {full_ipfs_url}: {error_message}")
            raise Exception(f"IPFS download failed: {error_message}")

        file_size = target_file_path.stat().st_size
        logger.info(f"File '{sanitized_filename}' downloaded from IPFS and saved to '{target_file_path}' (Size: {file_size} bytes)")

        ipfs_result = await run_uDRIVE_generation_script(user_drive_path)
        new_cid_info = ipfs_result.get("final_cid")
        logger.info(f"New IPFS CID generated: {new_cid_info}")

        return UploadFromDriveResponse(
            success=True,
            message="File synchronized successfully from IPFS",
            file_path=str(target_file_path.relative_to(user_drive_path)),
            file_type=file_type,
            new_cid=new_cid_info,
            timestamp=datetime.now().isoformat(),
            auth_verified=True
        )
    except Exception as e:
        logger.error(f"Error downloading from IPFS or saving file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to synchronize file: {e}")

@router.post("/upload2ipfs")
async def upload_to_ipfs(request: Request, file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    original_filename = file.filename or "unknown"
    file_location = f"tmp/{original_filename}"
    
    user_pubkey_hex = ""
    user_npub = None
    try:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Nostr "):
            auth_base64 = auth_header.replace("Nostr ", "").strip()
            auth_json = base64.b64decode(auth_base64).decode('utf-8')
            auth_event = json.loads(auth_json)
            
            if auth_event.get("kind") == 27235 and "pubkey" in auth_event:
                user_pubkey_hex = auth_event["pubkey"]
                user_npub = hex_to_npub(user_pubkey_hex) if user_pubkey_hex else None
                logger.info(f"🔑 NIP-98 Auth: Provenance tracking enabled for user: {user_pubkey_hex[:16]}...")
            else:
                logger.warning(f"⚠️ Invalid NIP-98 event: kind={auth_event.get('kind')}")
        else:
            logger.info(f"ℹ️ No NIP-98 Authorization header, uploading without provenance tracking")
    except Exception as e:
        logger.warning(f"⚠️ Could not extract pubkey from NIP-98 Authorization header: {e}")
    
    if user_npub:
        max_size_bytes = get_max_file_size_for_user(user_npub)
    else:
        max_size_bytes = 104857600
    
    if file.size and file.size > max_size_bytes:
        max_size_mb = max_size_bytes // 1048576
        file_size_mb = file.size // 1048576
        raise HTTPException(
            status_code=413,
            detail={
                "status": "error",
                "message": f"File size ({file_size_mb}MB) exceeds maximum allowed size ({max_size_mb}MB per UPlanet_FILE_CONTRACT.md)"
            }
        )
    
    try:
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        temp_file_path = f"tmp/temp_{uuid.uuid4()}.json"

        script_path = "./upload2ipfs.sh"
        
        return_code, last_line = await run_script(script_path, file_location, temp_file_path, user_pubkey_hex)

        if return_code == 0:
            try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip())

                new_cid = json_output.get("new_cid") or json_output.get("cid", "")
                file_hash = json_output.get("fileHash") or json_output.get("file_hash") or json_output.get("x", "")
                mime_type = json_output.get("mimeType") or json_output.get("file_type") or json_output.get("m", "")
                file_name = json_output.get("fileName") or original_filename or ""
                file_size = json_output.get("file_size") or json_output.get("size", "")
                dimensions = json_output.get("dimensions") or json_output.get("dim", "")
                info_cid = json_output.get("info") or ""
                thumbnail_ipfs = json_output.get("thumbnail_ipfs") or ""
                gifanim_ipfs = json_output.get("gifanim_ipfs") or ""
                upload_chain = json_output.get("upload_chain") or ""

                if new_cid and file_name:
                    ipfs_url = f"/ipfs/{new_cid}/{file_name}"
                elif new_cid:
                    ipfs_url = f"/ipfs/{new_cid}"
                else:
                    ipfs_url = ""

                tags = []
                if ipfs_url:
                    tags.append(["url", ipfs_url])
                if file_hash:
                    tags.append(["ox", file_hash])
                    tags.append(["x", file_hash])
                if mime_type:
                    tags.append(["m", mime_type])
                if file_size:
                    tags.append(["size", str(file_size)])
                if dimensions:
                    tags.append(["dim", dimensions])
                if info_cid:
                    tags.append(["info", info_cid])
                if thumbnail_ipfs:
                    tags.append(["thumbnail_ipfs", thumbnail_ipfs])
                if gifanim_ipfs:
                    tags.append(["gifanim_ipfs", gifanim_ipfs])
                if upload_chain:
                    tags.append(["upload_chain", upload_chain])

                nip96_response = {
                    "status": "success",
                    "message": json_output.get("message", "File uploaded successfully"),
                    "nip94_event": {"tags": tags, "content": ""}
                }
                if new_cid:
                    nip96_response["new_cid"] = new_cid
                if file_hash:
                    nip96_response["file_hash"] = file_hash
                if mime_type:
                    nip96_response["file_type"] = mime_type
                if info_cid:
                    nip96_response["info"] = info_cid
                if thumbnail_ipfs:
                    nip96_response["thumbnail_ipfs"] = thumbnail_ipfs
                if gifanim_ipfs:
                    nip96_response["gifanim_ipfs"] = gifanim_ipfs
                if dimensions:
                    nip96_response["dimensions"] = dimensions
                if json_output.get("duration"):
                    nip96_response["duration"] = json_output.get("duration")

                return JSONResponse(content=nip96_response)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logger.error(f"Failed to decode JSON from temp file: {temp_file_path}, Error: {e}")
                raise HTTPException(status_code=500, detail="Failed to process script output.")
            finally:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                if os.path.exists(file_location):
                    os.remove(file_location)
        else:
            logger.error(f"Script execution failed: {last_line.strip()}")
            if os.path.exists(file_location):
                os.remove(file_location)
            raise HTTPException(status_code=500, detail="Script execution failed.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        if os.path.exists(file_location):
            os.remove(file_location)
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

# ==================== IMAGE UPLOAD (TrocZen/ZENBOX compatible) ====================

IMAGE_MAGIC_BYTES = {
    'png': [b'\x89PNG\r\n\x1a\n'],
    'jpg': [b'\xFF\xD8\xFF'],
    'jpeg': [b'\xFF\xD8\xFF'],
    'webp': [b'RIFF'],
    'gif': [b'GIF87a', b'GIF89a'],
}

IMAGE_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

IMAGE_MIME_TYPES = {
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'webp': 'image/webp',
    'gif': 'image/gif',
}

MAX_IMAGE_SIZE = 5 * 1024 * 1024

def _validate_image_magic_bytes(file_content: bytes, extension: str) -> bool:
    ext = extension.lower()
    if ext not in IMAGE_MAGIC_BYTES:
        return False
    if ext == 'webp':
        return len(file_content) >= 12 and file_content[:4] == b'RIFF' and file_content[8:12] == b'WEBP'
    for sig in IMAGE_MAGIC_BYTES[ext]:
        if file_content.startswith(sig):
            return True
    return False

def _validate_image_file(filename: str, file_content: bytes) -> tuple:
    if '.' not in filename:
        return False, "File has no extension"
    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in IMAGE_ALLOWED_EXTENSIONS:
        return False, f"Extension '{ext}' not allowed. Allowed: {', '.join(IMAGE_ALLOWED_EXTENSIONS)}"
    if len(file_content) < 12:
        return False, "File too small for validation"
    if not _validate_image_magic_bytes(file_content[:12], ext):
        return False, f"Magic bytes don't match extension '{ext}'. Potentially malicious file."
    return True, None

async def _upload_image_to_ipfs(filepath: str) -> tuple:
    try:
        import aiohttp
        ipfs_api_url = "http://127.0.0.1:5001"
        with open(filepath, 'rb') as f:
            file_content = f.read()
        data = aiohttp.FormData()
        data.add_field('file', file_content,
                       filename=os.path.basename(filepath),
                       content_type='application/octet-stream')
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f'{ipfs_api_url}/api/v0/add', data=data) as response:
                if response.status == 200:
                    result = await response.json()
                    cid = result['Hash']
                    ipfs_gateway = (await get_myipfs_gateway()).rstrip('/')
                    ipfs_url = f"{ipfs_gateway}/ipfs/{cid}"
                    return cid, ipfs_url
        return None, None
    except Exception as e:
        logger.error(f"IPFS image upload error: {e}")
        return None, None

@router.post("/api/upload/image")
async def upload_image(
    file: UploadFile = File(...),
    npub: Optional[str] = Form(default=None),   # optionnel : Coracle n'envoie pas npub
    type: str = Form(default="media"),
):
    """Upload d'image générique depuis Coracle (éditeur de notes) ou autre client.

    Le champ `npub` est optionnel : lorsque Coracle envoie juste le fichier via
    FormData, on accepte l'upload anonyme et on génère un nom de fichier basé sur
    le hash SHA-256. Le résultat est toujours de la forme ``{"url": "<url>", ...}``
    pour être compatible avec le format attendu par l'éditeur Coracle.
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # Lire l'en-tête pour la validation magique
    file_header = await file.read(12)
    await file.seek(0)

    # Pour les uploads génériques (hors avatar/banner/logo), on accepte tout type
    # d'image sans restriction stricte sur le type
    valid_image_types = ('avatar', 'banner', 'logo', 'media', 'note')
    image_type = type.lower() if type.lower() in valid_image_types else "media"

    is_valid, error_msg = _validate_image_file(file.filename, file_header)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=413,
                            detail=f"File size ({len(content) // 1024}KB) exceeds maximum (5MB)")

    file_hash = hashlib.sha256(content).hexdigest()
    original_name = sanitize_filename_python(file.filename)
    _parts = original_name.rsplit('.', 1)
    ext = _parts[1].lower() if len(_parts) == 2 and _parts[1] else 'bin'
    timestamp = int(datetime.now().timestamp())

    # Préfixe : npub court si fourni, sinon les 16 premiers chars du hash du fichier
    prefix = (npub[:16] if npub and len(npub) >= 16 else npub) if npub else file_hash[:16]
    new_filename = f"{prefix}_{image_type}_{timestamp}.{ext}"

    uploads_dir = Path("uploads")
    uploads_dir.mkdir(exist_ok=True)
    filepath = uploads_dir / new_filename

    async with aiofiles.open(str(filepath), 'wb') as f:
        await f.write(content)

    local_url = f"/uploads/{new_filename}"
    cid, ipfs_url = await _upload_image_to_ipfs(str(filepath))

    return JSONResponse(content={
        'success': True,
        'url': ipfs_url or local_url,
        'local_url': local_url,
        'ipfs_url': ipfs_url,
        'ipfs_cid': cid,
        'ipfs_status': 'completed' if cid else 'failed',
        'filename': new_filename,
        'checksum': file_hash,
        'size': len(content),
        'uploaded_at': datetime.now().isoformat(),
        'storage': 'ipfs' if cid else 'local',
        'type': image_type,
        'mime_type': IMAGE_MIME_TYPES.get(ext, 'application/octet-stream'),
    }, status_code=201)


# ---------------------------------------------------------------------------
# Blossom-compatible endpoint (NIP-24242)
# PUT /upload  – utilisé par Coracle comme fallback Blossom si l'upload UPlanet échoue
# Authorization: Nostr <base64url(signed kind-24242 event)>
# Body: raw file bytes
# ---------------------------------------------------------------------------

@router.put("/upload")
async def blossom_upload(request: Request):
    """Endpoint de dépôt compatible Blossom (NIP-24242).

    Accepte les uploads via ``PUT /upload`` avec un header
    ``Authorization: Nostr <base64url(event)>``.  Le fichier est transmis
    en corps brut.  Retourne un objet ``{url, sha256, size, type, ...}``
    conforme au protocole Blossom.
    """
    # ── 1. Lire le header d'autorisation ──────────────────────────────────
    auth_header = request.headers.get("Authorization", "")
    pubkey_hex: Optional[str] = None
    expected_sha256: Optional[str] = None

    if auth_header.startswith("Nostr "):
        try:
            raw = auth_header[6:]
            # base64url ou base64 standard
            padded = raw + "=" * (-len(raw) % 4)
            event_json = base64.urlsafe_b64decode(padded).decode("utf-8")
            event = json.loads(event_json)
            pubkey_hex = event.get("pubkey")
            # Le tag ["x", "<sha256>"] contient le hash attendu du fichier
            for tag in event.get("tags", []):
                if tag[0] == "x" and len(tag) > 1:
                    expected_sha256 = tag[1]
                    break
            logger.info(f"[Blossom] Auth event from pubkey={pubkey_hex[:16] if pubkey_hex else 'unknown'}…")
        except Exception as e:
            logger.warning(f"[Blossom] Could not parse Authorization header: {e}")

    # ── 2. Lire le corps brut ─────────────────────────────────────────────
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="Empty body — file content required")

    # ── 3. Vérifier le SHA-256 si fourni dans l'event ────────────────────
    file_hash = hashlib.sha256(content).hexdigest()
    if expected_sha256 and file_hash != expected_sha256:
        logger.warning(f"[Blossom] SHA-256 mismatch: expected={expected_sha256} got={file_hash}")
        raise HTTPException(status_code=400, detail="File hash does not match authorization")

    # ── 4. Déterminer le type MIME depuis les headers ────────────────────
    content_type = request.headers.get("Content-Type", "application/octet-stream")
    # Déduire l'extension depuis le Content-Type
    _ct_ext_map = {
        "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
        "image/webp": "webp", "image/avif": "avif", "image/svg+xml": "svg",
        "video/mp4": "mp4", "video/webm": "webm",
        "audio/mpeg": "mp3", "audio/ogg": "ogg",
    }
    ext = _ct_ext_map.get(content_type.split(";")[0].strip(), "bin")

    # ── 5. Construire le nom de fichier ───────────────────────────────────
    prefix = pubkey_hex[:16] if pubkey_hex else file_hash[:16]
    timestamp = int(datetime.now().timestamp())
    new_filename = f"{prefix}_blossom_{timestamp}.{ext}"

    uploads_dir = Path("uploads")
    uploads_dir.mkdir(exist_ok=True)
    filepath = uploads_dir / new_filename

    async with aiofiles.open(str(filepath), 'wb') as f_out:
        await f_out.write(content)

    logger.info(f"[Blossom] Saved {len(content)} bytes → {filepath}")

    # ── 6. Uploader sur IPFS ──────────────────────────────────────────────
    cid, ipfs_url = await _upload_image_to_ipfs(str(filepath))
    final_url = ipfs_url or f"/uploads/{new_filename}"

    # ── 7. Réponse format Blossom ─────────────────────────────────────────
    return JSONResponse(content={
        "url": final_url,
        "sha256": file_hash,
        "size": len(content),
        "type": content_type.split(";")[0].strip(),
        "uploaded": timestamp,
        # Extensions non-standard utiles pour Coracle
        "ipfs_cid": cid,
        "ipfs_url": ipfs_url,
    }, status_code=200)

@router.get("/uploads/{filename}")
async def serve_upload(filename: str):
    uploads_dir = Path("uploads").resolve()
    filepath = (uploads_dir / sanitize_filename_python(filename)).resolve()
    
    if not str(filepath).startswith(str(uploads_dir.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden")
        
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    from fastapi.responses import FileResponse
    return FileResponse(str(filepath))
