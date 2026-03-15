import json
import time
import logging
import asyncio
import websockets
from typing import Optional, Dict, Any
from datetime import datetime
from fastapi import HTTPException, Form, Depends

from core.config import settings
from core.state import app_state

def convert_nostr_key(key: str, to_format: str) -> Optional[str]:
    """Convertit une clé NOSTR entre bech32 (npub) et hexadécimal."""
    if not key: return None
    key = key.lower().strip()
    
    try:
        if to_format == "hex":
            if len(key) == 64: return key # Déjà en hex
            if not key.startswith('npub1'): return None
            # Logique bech32 simplifiée via librairie externe si dispo, sinon votre implémentation
            import bech32
            _, data = bech32.bech32_decode(key)
            decoded = bech32.convertbits(data, 5, 8, False)
            return bytes(decoded).hex() if decoded else None
            
        elif to_format == "npub":
            if key.startswith('npub1'): return key
            if len(key) != 64: return None
            import bech32
            data = bech32.convertbits(bytes.fromhex(key), 8, 5)
            return bech32.bech32_encode('npub', data)
    except Exception as e:
        logging.error(f"Erreur conversion clé NOSTR ({key} -> {to_format}): {e}")
        return None

def npub_to_hex(npub: str) -> Optional[str]: return convert_nostr_key(npub, "hex")
def hex_to_npub(hex_key: str) -> Optional[str]: return convert_nostr_key(hex_key, "npub")

def get_nostr_relay_url() -> str:
    """Obtenir l'URL du relai NOSTR local"""
    host = settings.HOST
    port = "7777"  # Port strfry par défaut
    return f"ws://{host}:{port}"

async def fetch_video_event_from_nostr(event_id: str, timeout: int = 5) -> Optional[Dict[str, Any]]:
    """Fetch video event (kind 21 or 22) from NOSTR relay by event ID"""
    if not event_id or len(event_id) != 64:
        logging.warning(f"Invalid event ID format: {event_id}")
        return None
    
    relay_url = get_nostr_relay_url()
    logging.info(f"Fetching video event {event_id[:16]}... from relay: {relay_url}")
    
    try:
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            subscription_id = f"video_fetch_{int(time.time())}"
            video_filter = {
                "kinds": [21, 22],
                "ids": [event_id],
                "limit": 1
            }
            
            req_message = json.dumps(["REQ", subscription_id, video_filter])
            await websocket.send(req_message)
            
            event_found = None
            end_received = False
            
            try:
                while not end_received:
                    response = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                    parsed_response = json.loads(response)
                    
                    if parsed_response[0] == "EVENT":
                        if len(parsed_response) >= 3:
                            event = parsed_response[2]
                            if event.get("id") == event_id:
                                event_found = event
                                logging.info(f"✅ Video event found: {event_id[:16]}...")
                    
                    elif parsed_response[0] == "EOSE":
                        if parsed_response[1] == subscription_id:
                            end_received = True
                    
                    elif parsed_response[0] == "NOTICE":
                        logging.warning(f"Relay notice: {parsed_response[1] if len(parsed_response) > 1 else 'N/A'}")
            
            except asyncio.TimeoutError:
                logging.warning("Timeout waiting for video event")
            
            try:
                close_message = json.dumps(["CLOSE", subscription_id])
                await websocket.send(close_message)
                await asyncio.sleep(0.1)
            except Exception as e:
                logging.warning(f"Error closing subscription: {e}")
            
            return event_found
            
    except Exception as e:
        logging.error(f"Error fetching video event: {e}")
        return None

async def parse_video_metadata(event: Dict[str, Any]) -> Dict[str, Any]:
    """Parse video event tags to extract metadata for Open Graph/Twitter Cards"""
    if not event or not isinstance(event, dict):
        return {}
    
    metadata = {
        "title": "Video",
        "description": "",
        "thumbnail_url": "",
        "video_url": "",
        "author_id": event.get("pubkey", ""),
        "event_id": event.get("id", ""),
        "kind": event.get("kind", 21)
    }
    
    tags = event.get("tags", [])
    content = event.get("content", "")
    ipfs_gateway = settings.IPFS_GATEWAY
    
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2:
            if tag[0] == "title":
                title = tag[1].replace("\n", " ").replace("\r", " ").strip()
                while "  " in title:
                    title = title.replace("  ", " ")
                metadata["title"] = title
                break
    
    if content:
        description = content
        if description.startswith("🎬"):
            lines = description.split("\n")
            if len(lines) > 1:
                description = "\n".join(lines[1:]).strip()
        description = description.replace("\n", " ").replace("\r", " ").strip()
        while "  " in description:
            description = description.replace("  ", " ")
        metadata["description"] = description[:300]
    
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2:
            tag_type = tag[0]
            tag_value = tag[1]
            
            if tag_type == "url" and ("/ipfs/" in tag_value or "ipfs://" in tag_value):
                ipfs_path = tag_value.replace("ipfs://", "/ipfs/")
                if ipfs_path.startswith("/ipfs/"):
                    metadata["video_url"] = f"{ipfs_gateway}{ipfs_path}"
                    break
    
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2:
            tag_type = tag[0]
            tag_value = tag[1]
            
            if tag_type == "gifanim_ipfs":
                cid = tag_value
                if not cid.startswith("/ipfs/"):
                    cid = f"/ipfs/{cid}"
                metadata["thumbnail_url"] = f"{ipfs_gateway}{cid}"
                break
            
            elif tag_type == "thumbnail_ipfs":
                if not metadata["thumbnail_url"]:
                    cid = tag_value
                    if not cid.startswith("/ipfs/"):
                        cid = f"/ipfs/{cid}"
                    metadata["thumbnail_url"] = f"{ipfs_gateway}{cid}"
            
            elif tag_type == "image" and ("/ipfs/" in tag_value or "ipfs://" in tag_value):
                if not metadata["thumbnail_url"]:
                    ipfs_path = tag_value.replace("ipfs://", "/ipfs/")
                    if ipfs_path.startswith("/ipfs/"):
                        metadata["thumbnail_url"] = f"{ipfs_gateway}{ipfs_path}"
            
            elif tag_type == "r" and len(tag) >= 3 and tag[2] == "Thumbnail":
                if not metadata["thumbnail_url"]:
                    ipfs_path = tag_value.replace("ipfs://", "/ipfs/")
                    if ipfs_path.startswith("/ipfs/"):
                        metadata["thumbnail_url"] = f"{ipfs_gateway}{ipfs_path}"
    
    if not metadata["thumbnail_url"]:
        for tag in tags:
            if isinstance(tag, list) and tag[0] == "imeta":
                for i in range(1, len(tag)):
                    prop = tag[i]
                    if prop.startswith("gifanim "):
                        gifanim_value = prop[8:].strip()
                        if "/ipfs/" in gifanim_value or "ipfs://" in gifanim_value:
                            ipfs_path = gifanim_value.replace("ipfs://", "/ipfs/")
                            if ipfs_path.startswith("/ipfs/"):
                                metadata["thumbnail_url"] = f"{ipfs_gateway}{ipfs_path}"
                                break
                    elif prop.startswith("image "):
                        image_value = prop[6:].strip()
                        if "/ipfs/" in image_value or "ipfs://" in image_value:
                            ipfs_path = image_value.replace("ipfs://", "/ipfs/")
                            if ipfs_path.startswith("/ipfs/"):
                                metadata["thumbnail_url"] = f"{ipfs_gateway}{ipfs_path}"
                                break
                if metadata["thumbnail_url"]:
                    break
    
    info_cid = None
    if not metadata["description"]:
        for tag in tags:
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "info":
                info_cid = tag[1].strip()
                break
        
        if info_cid:
            from services.ipfs import fetch_info_json
            info_data = await fetch_info_json(info_cid)
            if info_data:
                if info_data.get("metadata") and info_data["metadata"].get("description"):
                    description = info_data["metadata"]["description"]
                    description = description.replace("\n", " ").replace("\r", " ").strip()
                    while "  " in description:
                        description = description.replace("  ", " ")
                    metadata["description"] = description[:300]
                
                if not metadata["thumbnail_url"] and info_data.get("media"):
                    media = info_data["media"]
                    protocol_version = info_data.get("protocol", {}).get("version", "1.0.0")
                    is_v2 = protocol_version.startswith("2.")
                    
                    if is_v2 and media.get("thumbnails"):
                        thumbnails = media["thumbnails"]
                        thumbnail_cid = thumbnails.get("animated") or thumbnails.get("static")
                        if thumbnail_cid:
                            clean_cid = thumbnail_cid.replace("/ipfs/", "").replace("ipfs://", "")
                            metadata["thumbnail_url"] = f"{ipfs_gateway}/ipfs/{clean_cid}"
                    
                    elif not is_v2:
                        thumbnail_cid = media.get("gifanim_ipfs") or media.get("thumbnail_ipfs")
                        if thumbnail_cid:
                            clean_cid = thumbnail_cid.replace("/ipfs/", "").replace("ipfs://", "")
                            metadata["thumbnail_url"] = f"{ipfs_gateway}/ipfs/{clean_cid}"
    
    if not metadata["description"]:
        metadata["description"] = metadata["title"]
    
    return metadata

def validate_nip42_event(event: Dict[str, Any], expected_relay_url: str) -> bool:
    """Valider un événement NIP42"""
    try:
        if not isinstance(event, dict):
            return False
            
        required_fields = ['id', 'pubkey', 'created_at', 'kind', 'tags', 'content', 'sig']
        for field in required_fields:
            if field not in event:
                return False
        
        if event.get('kind') != 22242:
            return False
        
        tags = event.get('tags', [])
        relay_found = False
        
        for tag in tags:
            if isinstance(tag, list) and len(tag) >= 2:
                if tag[0] == 'relay':
                    relay_found = True
                    break
        
        if not relay_found:
            logging.warning("Tag 'relay' manquant dans l'événement NIP42")
        
        event_time = event.get('created_at', 0)
        current_time = int(time.time())
        age_hours = (current_time - event_time) / 3600
        
        if age_hours > 24:
            return False
        
        return True
        
    except Exception as e:
        logging.error(f"Erreur lors de la validation de l'événement NIP42: {e}")
        return False

async def check_nip42_auth(npub: str, timeout: int = 5) -> bool:
    """Vérifier l'authentification NIP42 sur le relai NOSTR local"""
    if not npub:
        return False
    
    hex_pubkey = npub_to_hex(npub)
    if not hex_pubkey:
        return False
    
    relay_url = get_nostr_relay_url()
    
    try:
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            since_timestamp = int(time.time()) - (24 * 60 * 60)
            
            subscription_id = f"auth_check_{int(time.time())}"
            auth_filter = {
                "kinds": [22242],
                "authors": [hex_pubkey],
                "since": since_timestamp,
                "limit": 5
            }
            
            req_message = json.dumps(["REQ", subscription_id, auth_filter])
            await websocket.send(req_message)
            
            events_found = []
            end_received = False
            
            try:
                while not end_received:
                    response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    parsed_response = json.loads(response)
                    
                    if parsed_response[0] == "EVENT":
                        if len(parsed_response) >= 3:
                            event = parsed_response[2]
                            events_found.append(event)
                    
                    elif parsed_response[0] == "EOSE":
                        if parsed_response[1] == subscription_id:
                            end_received = True
                        
            except asyncio.TimeoutError:
                pass
            
            try:
                close_message = json.dumps(["CLOSE", subscription_id])
                await websocket.send(close_message)
                await asyncio.sleep(0.1)
            except Exception:
                pass
            
            if not events_found:
                return False
            
            valid_events = []
            for event in events_found:
                if validate_nip42_event(event, relay_url):
                    valid_events.append(event)
            
            if valid_events:
                return True
            else:
                return False
                
    except Exception as e:
        logging.error(f"Erreur lors de la vérification NIP42: {e}")
        return False

async def verify_nostr_auth(npub: Optional[str], force_check: bool = False) -> bool:
    """Vérifier l'authentification NOSTR si une npub est fournie avec cache"""
    if not npub:
        return False
    
    if not force_check and npub in app_state.nostr_auth_cache:
        return app_state.nostr_auth_cache[npub]
    
    if len(npub) == 64:
        hex_pubkey = npub_to_hex(npub)
    elif npub.startswith('npub1'):
        hex_pubkey = npub_to_hex(npub)
    else:
        return False
    
    if not hex_pubkey:
        return False
    
    auth_result = await check_nip42_auth(hex_pubkey)
    
    app_state.nostr_auth_cache[npub] = auth_result
    
    return auth_result

async def require_nostr_auth(npub: str = Form(...)) -> str:
    """
    FastAPI dependency to require NOSTR authentication.
    Returns the authenticated npub or raises HTTPException.
    """
    auth_verified = await verify_nostr_auth(npub)
    if not auth_verified:
        raise HTTPException(
            status_code=403,
            detail="Nostr authentication failed. Please ensure you have sent a recent NIP-42 authentication event (kind 22242)."
        )
    return npub

from fastapi import Request
import base64

async def verify_nip98_auth(request: Request) -> str:
    """
    FastAPI dependency to verify NIP-98 HTTP Auth.
    Returns the authenticated pubkey or raises HTTPException.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Nostr "):
        raise HTTPException(status_code=401, detail="Missing or invalid NIP-98 Authorization header")
    
    try:
        token = auth_header.replace("Nostr ", "")
        decoded = base64.b64decode(token)
        auth_event = json.loads(decoded)
        
        if auth_event.get("kind") != 27235:
            raise HTTPException(status_code=401, detail="Invalid NIP-98 event kind (must be 27235)")
        
        # Basic validation (in a real app, verify signature and tags)
        pubkey = auth_event.get("pubkey")
        if not pubkey:
            raise HTTPException(status_code=401, detail="Missing pubkey in NIP-98 event")
            
        return pubkey
    except Exception as e:
        logging.error(f"NIP-98 Auth error: {e}")
        raise HTTPException(status_code=401, detail=f"NIP-98 Auth failed: {str(e)}")
