import json
import time
import logging
import asyncio
import websockets
from typing import Optional, Dict, Any, List
from datetime import datetime
from fastapi import HTTPException, Form, Depends

from core.config import settings
from core.state import app_state



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
    
    from utils.crypto import npub_to_hex
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
        from utils.crypto import npub_to_hex
        hex_pubkey = npub_to_hex(npub)
    elif npub.startswith('npub1'):
        from utils.crypto import npub_to_hex
        hex_pubkey = npub_to_hex(npub)
    else:
        return False
    
    if not hex_pubkey:
        return False
    
    auth_result = await check_nip42_auth(hex_pubkey)
    
    app_state.nostr_auth_cache[npub] = auth_result
    
    return auth_result

async def require_nostr_auth(npub: str = Form(...), force_check: bool = False) -> str:
    """
    FastAPI dependency to require NOSTR authentication.
    Returns the authenticated npub or raises HTTPException.
    """
    auth_verified = await verify_nostr_auth(npub, force_check=force_check)
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

# Cache pour les profils NOSTR (évite les requêtes répétées)
# Maps pubkey (hex) -> (profile_data, timestamp)
nostr_profile_cache = {}
NOSTR_PROFILE_CACHE_TTL = 3600  # 1 hour

async def fetch_nostr_profiles(pubkeys: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Fetch NOSTR profiles (kind 0) for a list of pubkeys with caching (1 hour TTL)
    Returns a dictionary mapping pubkey -> profile data
    """
    profiles = {}
    if not pubkeys:
        return profiles
    
    # Check cache first
    current_time = time.time()
    pubkeys_to_fetch = []
    for pubkey in pubkeys:
        if pubkey in nostr_profile_cache:
            cached_data, cached_time = nostr_profile_cache[pubkey]
            if current_time - cached_time < NOSTR_PROFILE_CACHE_TTL:
                profiles[pubkey] = cached_data
                logging.debug(f"✅ Profile cache hit for {pubkey[:12]}...")
                continue
        pubkeys_to_fetch.append(pubkey)
    
    if not pubkeys_to_fetch:
        logging.info(f"✅ All {len(pubkeys)} profiles found in cache")
        return profiles
    
    logging.info(f"📡 Fetching {len(pubkeys_to_fetch)} profiles from NOSTR (cache: {len(profiles)})")
    
    try:
        from pathlib import Path
        import json
        
        # Path to nostr_get_events.sh
        nostr_script_path = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "nostr_get_events.sh"
        
        if not nostr_script_path.exists():
            logging.warning(f"nostr_get_events.sh not found, skipping profile enrichment")
            return profiles
        
        # Fetch profiles in batches to avoid command line length issues
        batch_size = 50
        for i in range(0, len(pubkeys), batch_size):
            batch = pubkeys[i:i + batch_size]
            
            # Build command to fetch profile events (kind 0) for these pubkeys
            cmd = [
                str(nostr_script_path),
                "--kind", "0",
                "--authors", ",".join(batch),
                "--output", "json"
            ]
            
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(nostr_script_path.parent)
                )
                
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=10.0
                )
                
                if process.returncode == 0 and stdout:
                    # Parse JSON events (one per line)
                    for line in stdout.decode('utf-8', errors='ignore').strip().split('\n'):
                        if not line.strip():
                            continue
                        
                        try:
                            event = json.loads(line)
                            pubkey = event.get('pubkey', '')
                            
                            if pubkey and pubkey in batch:
                                # Parse profile content (JSON string)
                                content = event.get('content', '{}')
                                try:
                                    profile_data = json.loads(content) if content else {}
                                    
                                    # Convert hex pubkey to npub (bech32)
                                    from utils.crypto import hex_to_npub
                                    npub = hex_to_npub(pubkey)
                                    
                                    profile_data_dict = {
                                        'npub': npub,
                                        'email': profile_data.get('email') or profile_data.get('lud16') or profile_data.get('lud06'),
                                        'display_name': profile_data.get('display_name') or profile_data.get('displayName'),
                                        'name': profile_data.get('name'),
                                        'picture': profile_data.get('picture'),
                                        'about': profile_data.get('about')
                                    }
                                    profiles[pubkey] = profile_data_dict
                                    # Cache the profile
                                    nostr_profile_cache[pubkey] = (profile_data_dict, current_time)
                                except json.JSONDecodeError:
                                    # Profile content is not valid JSON, skip
                                    pass
                        except json.JSONDecodeError:
                            # Invalid event JSON, skip
                            continue
                            
            except asyncio.TimeoutError:
                logging.warning(f"Timeout fetching profiles for batch {i//batch_size + 1}")
            except Exception as e:
                logging.warning(f"Error fetching profiles batch: {e}")
        
        logging.info(f"✅ Fetched {len(profiles)} profiles (cached: {len([p for p in profiles if p in nostr_profile_cache])}, new: {len([p for p in profiles if p not in nostr_profile_cache])})")
        
    except Exception as e:
        logging.warning(f"Error in fetch_nostr_profiles: {e}")
    
    return profiles

async def get_n1_follows(pubkey_hex: str) -> List[str]:
    """Récupérer la liste N1 (personnes suivies) d'une clé publique"""
    try:
        from core.config import settings
        script_path = settings.TOOLS_PATH / "nostr_get_N1.sh"
        
        if not os.path.exists(script_path):
            logging.error(f"Script nostr_get_N1.sh non trouvé: {script_path}")
            return []
        
        process = await asyncio.create_subprocess_exec(
            script_path, pubkey_hex,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            follows = [line.strip() for line in stdout.decode().strip().split('\n') if line.strip()]
            logging.info(f"N1 follows pour {pubkey_hex[:12]}...: {len(follows)} clés")
            return follows
        else:
            logging.error(f"Erreur nostr_get_N1.sh: {stderr.decode()}")
            return []
            
    except Exception as e:
        logging.error(f"Erreur lors de la récupération N1: {e}")
        return []

async def get_followers(pubkey_hex: str) -> List[str]:
    """Récupérer la liste des followers d'une clé publique"""
    try:
        from core.config import settings
        script_path = settings.TOOLS_PATH / "nostr_followers.sh"
        
        if not os.path.exists(script_path):
            logging.error(f"Script nostr_followers.sh non trouvé: {script_path}")
            return []
        
        process = await asyncio.create_subprocess_exec(
            script_path, pubkey_hex,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            followers = [line.strip() for line in stdout.decode().strip().split('\n') if line.strip()]
            logging.info(f"Followers pour {pubkey_hex[:12]}...: {len(followers)} clés")
            return followers
        else:
            logging.error(f"Erreur nostr_followers.sh: {stderr.decode()}")
            return []
            
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des followers: {e}")
        return []

async def analyze_n2_network(center_pubkey: str, range_mode: str = "default") -> Dict[str, Any]:
    """Analyser le réseau N2 d'une clé publique"""
    start_time = time.time()
    
    # Récupérer N1 (personnes suivies par le centre)
    n1_follows_raw = await get_n1_follows(center_pubkey)
    
    # Filtrer le nœud central de sa propre liste (éviter l'auto-référence)
    n1_follows = [pubkey for pubkey in n1_follows_raw if pubkey != center_pubkey]
    
    # Récupérer les followers du centre
    center_followers = await get_followers(center_pubkey)
    
    # Créer les nœuds N1
    nodes = {}
    connections = []
    
    from models.schemas import N2NetworkNode
    
    # Nœud central
    nodes[center_pubkey] = N2NetworkNode(
        pubkey=center_pubkey,
        level=0,
        is_follower=False,
        is_followed=False,
        mutual=False,
        connections=n1_follows.copy()
    )
    
    # Ajouter les connexions du centre vers N1
    for follow in n1_follows:
        connections.append({"from": center_pubkey, "to": follow})
    
    # Traiter les nœuds N1 (exclure le nœud central)
    for pubkey in n1_follows:
        if pubkey != center_pubkey:  # Éviter d'écraser le nœud central
            is_follower = pubkey in center_followers
            nodes[pubkey] = N2NetworkNode(
                pubkey=pubkey,
                level=1,
                is_follower=is_follower,
                is_followed=True,
                mutual=is_follower,
                connections=[]
            )
    
    # Déterminer quelles clés N1 explorer pour N2
    if range_mode == "full":
        # Explorer toutes les clés N1
        keys_to_explore = n1_follows
        logging.info(f"Mode full: exploration de {len(keys_to_explore)} clés N1")
    else:
        # Explorer seulement les clés N1 qui sont aussi followers (mutuelles)
        keys_to_explore = [key for key in n1_follows if key in center_followers]
        logging.info(f"Mode default: exploration de {len(keys_to_explore)} clés mutuelles")
    
    # Analyser N2 pour chaque clé sélectionnée
    n2_keys = set()
    
    for n1_key in keys_to_explore:
        try:
            # Récupérer les follows de cette clé N1
            n1_key_follows = await get_n1_follows(n1_key)
            
            # Ajouter les connexions N1 -> N2
            nodes[n1_key].connections = n1_key_follows.copy()
            
            for n2_key in n1_key_follows:
                # Éviter d'ajouter le centre, les clés déjà en N1, ou l'auto-référence
                if (n2_key != center_pubkey and 
                    n2_key not in n1_follows and 
                    n2_key != n1_key):
                    n2_keys.add(n2_key)
                    connections.append({"from": n1_key, "to": n2_key})
                    
        except Exception as e:
            logging.warning(f"Erreur lors de l'analyse N2 pour {n1_key[:12]}...: {e}")
    
    # Créer les nœuds N2
    for n2_key in n2_keys:
        if n2_key not in nodes:
            nodes[n2_key] = N2NetworkNode(
                pubkey=n2_key,
                level=2,
                is_follower=False,
                is_followed=False,
                mutual=False,
                connections=[]
            )
    
    # Enrich nodes with profile information for vocals messaging
    all_pubkeys = list(nodes.keys())
    profiles = await fetch_nostr_profiles(all_pubkeys)
    
    # Enrich each node with profile data
    enriched_nodes = []
    for node in nodes.values():
        profile = profiles.get(node.pubkey, {})
        enriched_node = N2NetworkNode(
            pubkey=node.pubkey,
            level=node.level,
            is_follower=node.is_follower,
            is_followed=node.is_followed,
            mutual=node.mutual,
            connections=node.connections,
            npub=profile.get('npub'),
            email=profile.get('email'),
            display_name=profile.get('display_name'),
            name=profile.get('name'),
            picture=profile.get('picture'),
            about=profile.get('about')
        )
        enriched_nodes.append(enriched_node)
    
    processing_time_ms = int((time.time() - start_time) * 1000)
    
    return {
        "center_pubkey": center_pubkey,
        "total_n1": len(n1_follows),
        "total_n2": len(n2_keys),
        "total_nodes": len(nodes),
        "range_mode": range_mode,
        "nodes": enriched_nodes,
        "connections": connections,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "processing_time_ms": processing_time_ms
    }
