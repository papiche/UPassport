import os
import json
import time
import hashlib
import logging
import secrets
import string
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from utils.helpers import run_script, get_env_from_mysh, get_myipfs_gateway, is_origin_mode, get_oc_tier_urls, get_uplanet_home_url
from utils.security import is_safe_node_id, get_safe_swarm_path, is_safe_ssh_key, is_multipass_user
from utils.crypto import npub_to_hex, hex_to_npub

router = APIRouter()
templates = Jinja2Templates(directory="templates")

from pydantic import BaseModel

from utils.helpers import as_form

@as_form
class G1NostrForm(BaseModel):
    email: str
    lang: str
    lat: str
    lon: str
    salt: str = ""
    pepper: str = ""
    format: str = "html"

@router.post("/g1nostr")
async def scan_qr(
    request: Request,
    form_data: G1NostrForm = Form(...)
):
    email = form_data.email
    lang = form_data.lang
    lat = form_data.lat
    lon = form_data.lon
    salt = form_data.salt
    pepper = form_data.pepper
    format = form_data.format
    """
    Endpoint to execute the g1.sh script and return the generated file.
    Supports both regular users and swarm subscription aliases.
    """
    
    # Generate random salt and pepper if not provided or empty
    if not salt or salt.strip() == "":
        salt = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(42))
        logging.info(f"Generated random salt for {email}: {salt[:10]}...")
    
    if not pepper or pepper.strip() == "":
        pepper = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(42))
        logging.info(f"Generated random pepper for {email}: {pepper[:10]}...")
    
    # Détecter si c'est un email d'abonnement inter-node (contient un +)
    is_swarm_subscription = '+' in email and '-' in email.split('@')[0]
    
    if is_swarm_subscription:
        logging.info(f"🌐 Swarm subscription detected: {email}")
        
        # Extraire les informations de l'alias
        local_part = email.split('@')[0]
        base_email = local_part.split('+')[0] + '@' + email.split('@')[1]
        node_info = local_part.split('+')[1]  # format: nodeid-suffix
        node_id = node_info.split('-')[0]  # Extraire le node ID
        
        logging.info(f"   Base email: {base_email}")
        logging.info(f"   Node info: {node_info}")
        logging.info(f"   Node ID: {node_id}")
        
        # Enregistrer la notification d'abonnement
        ipfs_node_id = await get_env_from_mysh("IPFSNODEID", "unknown")
        if not ipfs_node_id or ipfs_node_id == "unknown":
            from core.config import settings
            ipfs_node_id = settings.IPFSNODEID or "unknown"
        from core.config import settings
        subscription_dir = settings.ZEN_PATH / "tmp" / ipfs_node_id
        os.makedirs(subscription_dir, exist_ok=True)
        
        subscription_log = os.path.join(subscription_dir, "swarm_subscriptions_received.json")
        
        # Charger ou créer le fichier de notifications
        if os.path.exists(subscription_log):
            with open(subscription_log, 'r') as f:
                notifications = json.load(f)
        else:
            notifications = {"received_subscriptions": []}
        
        # Ajouter la nouvelle notification
        new_notification = {
            "subscription_email": email,
            "base_email": base_email,
            "node_info": node_info,
            "node_id": node_id,
            "received_at": datetime.now().isoformat(),
            "lat": lat,
            "lon": lon,
            "salt": hashlib.sha256(salt.encode()).hexdigest(),  # Stocker le hash pour la sécurité
            "status": "received"
        }
        
        notifications["received_subscriptions"].append(new_notification)
        
        # Sauvegarder les notifications
        with open(subscription_log, 'w') as f:
            json.dump(notifications, f, indent=2)
        
        logging.info(f"   Subscription notification saved to: {subscription_log}")
        
        #######################################################################
        # Y LEVEL : Ajouter automatiquement la clé SSH du node distant
        #######################################################################
        
        # Vérifier si on est en Y Level
        from core.config import settings
        y_level_files = [
            settings.GAME_PATH / "secret.dunikey",
            settings.GAME_PATH / "secret.june"
        ]
        
        is_y_level = any(os.path.exists(f) for f in y_level_files)
        
        if is_y_level:
            logging.info(f"🔑 Y Level detected - Processing SSH key for node: {node_id}")
            
            # Chercher le fichier JSON du node distant
            # Validation de sécurité pour node_id
            node_json_path = None
            if not is_safe_node_id(node_id):
                logging.warning(f"❌ Node ID format invalide: {node_id}")
                new_notification["node_id_invalid"] = True
            else:
                node_json_path = get_safe_swarm_path(node_id, "12345.json")
                if not node_json_path:
                    logging.warning(f"❌ Chemin swarm invalide pour {node_id}")
                    new_notification["swarm_path_invalid"] = True
            
            if node_json_path and os.path.exists(node_json_path):
                try:
                    with open(node_json_path, 'r') as f:
                        node_data = json.load(f)
                    
                    ssh_pub_key = node_data.get('SSHPUB', '').strip()
                    actual_node_id = node_data.get('ipfsnodeid', '').strip()
                    captain_email = node_data.get('captain', '').strip()
                    
                    if ssh_pub_key and actual_node_id:
                        logging.info(f"   Found SSH key: [REDACTED - {len(ssh_pub_key)} chars]")
                        logging.info(f"   Node ID from JSON: {actual_node_id}")
                        logging.info(f"   Captain: {captain_email}")
                        
                        # Vérifier que le node ID correspond
                        if actual_node_id == node_id:
                            # Vérifier la clé SSH avec ssh_to_g1ipfs.py
                            try:
                                ssh_to_g1_script = settings.TOOLS_PATH / "ssh_to_g1ipfs.py"
                                if os.path.exists(ssh_to_g1_script):
                                    # Validation de sécurité pour la clé SSH
                                    if not is_safe_ssh_key(ssh_pub_key):
                                        logging.warning(f"❌ SSH key format invalide pour {node_id}")
                                        new_notification["ssh_key_invalid"] = True
                                    else:
                                        import asyncio
                                        process = await asyncio.create_subprocess_exec(
                                            "python3", str(ssh_to_g1_script), ssh_pub_key,
                                            stdout=asyncio.subprocess.PIPE,
                                            stderr=asyncio.subprocess.PIPE
                                        )
                                        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
                                    
                                    if process.returncode == 0:
                                        computed_ipns = stdout.decode().strip()
                                        logging.info(f"   Computed IPNS: {computed_ipns}")
                                        
                                        if computed_ipns == actual_node_id:
                                            logging.info(f"✅ SSH key verification successful for {node_id}")
                                            
                                            # Ajouter la clé SSH au fichier My_boostrap_ssh.txt
                                            bootstrap_ssh_file = settings.GAME_PATH / "My_boostrap_ssh.txt"
                                            
                                            # Créer le fichier s'il n'existe pas
                                            if not os.path.exists(bootstrap_ssh_file):
                                                with open(bootstrap_ssh_file, 'w') as f:
                                                    f.write("# My Bootstrap SSH Keys\n")
                                                    f.write("# Generated automatically by UPlanet swarm system\n\n")
                                            
                                            # Vérifier si la clé existe déjà
                                            key_exists = False
                                            try:
                                                with open(bootstrap_ssh_file, 'r') as f:
                                                    existing_content = f.read()
                                                    if ssh_pub_key in existing_content:
                                                        key_exists = True
                                                        logging.info(f"   SSH key already exists in bootstrap file")
                                            except Exception as e:
                                                logging.warning(f"   Error reading bootstrap file: {e}")
                                            
                                            # Ajouter la clé si elle n'existe pas déjà
                                            if not key_exists:
                                                try:
                                                    with open(bootstrap_ssh_file, 'a') as f:
                                                        f.write(f"\n# Node: {node_id} - Captain: {captain_email}\n")
                                                        f.write(f"# Added on: {datetime.now().isoformat()}\n")
                                                        f.write(f"{ssh_pub_key}\n")
                                                    
                                                    logging.info(f"✅ SSH key added to: {bootstrap_ssh_file}")
                                                    
                                                    # Mettre à jour la notification avec le statut SSH
                                                    new_notification["ssh_key_added"] = True
                                                    new_notification["ssh_key"] = f"[REDACTED - {len(ssh_pub_key)} chars]"
                                                    
                                                except Exception as e:
                                                    logging.error(f"❌ Error writing SSH key to bootstrap file: {e}")
                                                    new_notification["ssh_key_error"] = str(e)
                                            else:
                                                new_notification["ssh_key_exists"] = True
                                        else:
                                            logging.warning(f"❌ SSH key verification failed: {computed_ipns} != {actual_node_id}")
                                            new_notification["ssh_verification_failed"] = f"{computed_ipns} != {actual_node_id}"
                                    else:
                                        logging.error(f"❌ ssh_to_g1ipfs.py failed: {stderr.decode()}")
                                        new_notification["ssh_script_error"] = stderr.decode()
                                else:
                                    logging.warning(f"❌ ssh_to_g1ipfs.py script not found: {ssh_to_g1_script}")
                                    new_notification["ssh_script_missing"] = True
                                    
                            except subprocess.TimeoutExpired:
                                logging.error(f"❌ SSH verification timeout for {node_id}")
                                new_notification["ssh_verification_timeout"] = True
                            except Exception as e:
                                logging.error(f"❌ SSH verification error: {e}")
                                new_notification["ssh_verification_error"] = str(e)
                        else:
                            logging.warning(f"❌ Node ID mismatch: expected {node_id}, got {actual_node_id}")
                            new_notification["node_id_mismatch"] = f"expected {node_id}, got {actual_node_id}"
                    else:
                        logging.warning(f"❌ Missing SSH key or node ID in JSON for {node_id}")
                        new_notification["missing_ssh_data"] = True
                        
                except json.JSONDecodeError as e:
                    logging.error(f"❌ Invalid JSON in {node_json_path}: {e}")
                    new_notification["json_parse_error"] = str(e)
                except Exception as e:
                    logging.error(f"❌ Error processing node JSON {node_json_path}: {e}")
                    new_notification["json_processing_error"] = str(e)
            else:
                logging.warning(f"❌ Node JSON not found: {node_json_path}")
                new_notification["node_json_missing"] = node_json_path
        else:
            logging.info(f"📝 Not Y Level - SSH key processing skipped")
            new_notification["y_level"] = False
        
        # Mettre à jour la notification avec les informations SSH
        notifications["received_subscriptions"][-1] = new_notification
        
        # Sauvegarder les notifications mises à jour
        with open(subscription_log, 'w') as f:
            json.dump(notifications, f, indent=2)
    
    script_path = "./g1.sh" # Make sure g1.sh is in the same directory or adjust path
    return_code, last_line = await run_script(script_path, email, lang, lat, lon, salt, pepper)

    if return_code == 0:
        returned_file_path = last_line.strip()
        logging.info(f"Returning file: {returned_file_path}")

        if is_swarm_subscription:
            logging.info(f"✅ Swarm subscription processed successfully: {email}")

        # JSON format: return MULTIPASS data for app onboarding
        if format == "json":
            multipass_json = settings.GAME_PATH / "nostr" / email / ".multipass.json"
            if os.path.exists(multipass_json):
                with open(multipass_json, 'r') as f:
                    data = json.load(f)
                
                data["is_origin"] = is_origin_mode()
                data["oc_urls"] = get_oc_tier_urls()
                data["uplanet_home"] = get_uplanet_home_url()
                return JSONResponse(data)
            else:
                raise HTTPException(status_code=500, detail="MULTIPASS created but JSON sidecar not found")

        return FileResponse(returned_file_path)
    else:
        error_message = f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs. Script output: {last_line}"
        logging.error(error_message)
        raise HTTPException(status_code=500, detail=error_message)

class UPassportForm(BaseModel):
    parametre: str
    imageData: Optional[str] = None
    zlat: Optional[str] = None
    zlon: Optional[str] = None

@router.post("/upassport")
async def scan_qr_upassport(
    form_data: UPassportForm = Form(...)
):
    parametre = form_data.parametre
    imageData = form_data.imageData
    zlat = form_data.zlat
    zlon = form_data.zlon
    import base64
    image_dir = "./tmp"
    # Assign default 0.00 values if zlat or zlon are empty
    zlat = zlat if zlat is not None else "0.00"
    zlon = zlon if zlon is not None else "0.00"

    # Ensure the image directory exists
    os.makedirs(image_dir, exist_ok=True)

    # Vérification si imageData est un PIN de 4 chiffres
    if imageData and imageData.isdigit() and len(imageData) == 4:
        logging.info(f"Received a PIN: {imageData}")
        image_path = imageData
    else:
        # Génération du nom de fichier à partir du hash de parametre
        image_filename = f"qr_image_{hashlib.sha256(parametre.encode()).hexdigest()[:10]}.png"
        image_path = os.path.join(image_dir, image_filename)

        if imageData:
            try:
                # Remove the data URL prefix if present
                if ',' in imageData:
                    image_data = imageData.split(',')[1]
                else:
                    image_data = imageData

                # Decode and save the image
                with open(image_path, "wb") as image_file:
                    image_file.write(base64.b64decode(image_data))
                    logging.info("Saved image to: %s", image_path)

            except Exception as e:
                logging.error("Error saving image: %s", e)

    # Log zlat and zlon values
    logging.info(f"zlat: {zlat}, zlon: {zlon}")

    ## Running External Script > get last line > send file content back to client.
    script_path = "./upassport.sh"
    return_code, last_line = await run_script(script_path, parametre, image_path, zlat, zlon)

    if return_code == 0:
        returned_file_path = last_line.strip()
        logging.info(f"Returning file: {returned_file_path}")
        
        # Vérifier si le fichier existe
        if not os.path.exists(returned_file_path):
            error_message = f"Le fichier {returned_file_path} n'existe pas"
            logging.error(error_message)
            raise HTTPException(status_code=404, detail=error_message)
            
        # Vérifier si c'est bien un fichier HTML
        if not returned_file_path.endswith('.html'):
            error_message = f"Le fichier {returned_file_path} n'est pas un fichier HTML"
            logging.error(error_message)
            raise HTTPException(status_code=400, detail=error_message)
            
        try:
            return FileResponse(
                returned_file_path,
                media_type='text/html',
                filename=os.path.basename(returned_file_path)
            )
        except Exception as e:
            error_message = f"Erreur lors de l'envoi du fichier: {str(e)}"
            logging.error(error_message)
            raise HTTPException(status_code=500, detail=error_message)
    else:
        error_message = f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs."
        logging.error(error_message)
        raise HTTPException(status_code=500, detail=error_message)

class SSSSForm(BaseModel):
    cardns: str
    ssss: str
    zerocard: Optional[str] = None

@router.post("/ssss")
async def ssss(request: Request, form_data: SSSSForm = Form(...)):
    cardns = form_data.cardns
    ssss = form_data.ssss
    zerocard = form_data.zerocard

    logging.info(f"Received Card NS: {cardns}")
    logging.info(f"Received SSSS key: [REDACTED - {len(ssss)} chars]")
    logging.info(f"ZEROCARD: {zerocard}")

    script_path = "./check_ssss.sh"
    return_code, last_line = await run_script(script_path, cardns, ssss, zerocard)

    if return_code == 0:
        returned_file_path = last_line.strip()
        return FileResponse(returned_file_path)
    else:
        raise HTTPException(status_code=500, detail="Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs.")

@router.get("/.well-known/nostr/nip96.json")
async def nip96_discovery(request: Request):
    """
    NIP-96 discovery endpoint for file storage server.
    Returns server capabilities and configuration for NOSTR clients.
    """
    import base64
    
    # Get base URL from request
    base_url = str(request.base_url).rstrip('/')
    
    # Try to extract pubkey from NIP-98 Authorization header
    user_pubkey_hex = None
    is_multipass = False
    
    try:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Nostr "):
            # Decode the base64-encoded NIP-98 event
            auth_base64 = auth_header.replace("Nostr ", "").strip()
            auth_json = base64.b64decode(auth_base64).decode('utf-8')
            auth_event = json.loads(auth_json)
            
            # Extract pubkey from the NIP-98 event (kind 27235)
            if auth_event.get("kind") == 27235 and "pubkey" in auth_event:
                user_pubkey_hex = auth_event["pubkey"]
                logging.info(f"🔑 NIP-96 Discovery: Checking MULTIPASS status for: {user_pubkey_hex[:16]}...")
                
                # Check if user is recognized as MULTIPASS by UPlanet
                is_multipass = is_multipass_user(user_pubkey_hex)
            else:
                logging.warning(f"⚠️  NIP-96 Discovery: Invalid NIP-98 event: kind={auth_event.get('kind')}")
    except Exception as e:
        logging.warning(f"⚠️  NIP-96 Discovery: Could not extract pubkey from NIP-98: {e}")
    
    # Determine plans based on MULTIPASS status
    if is_multipass:
        # MULTIPASS users: 650MB quota
        plans = {
            "multipass": {
                "name": "MULTIPASS IPFS Storage",
                "is_nip98_required": True,
                "max_byte_size": 681574400,  # 650MB
                "file_expiration": [0, 0],  # No expiration
                "media_transformations": {
                    "image": ["thumbnail"],
                    "video": ["thumbnail", "gif_animation"]
                }
            }
        }
    else:
        # Non-recognized NOSTR users: 100MB quota
        plans = {
            "free": {
                "name": "Free IPFS Storage (NOSTR)",
                "is_nip98_required": True,
                "max_byte_size": 104857600,  # 100MB
                "file_expiration": [0, 0],  # No expiration
                "media_transformations": {
                    "image": ["thumbnail"],
                    "video": ["thumbnail", "gif_animation"]
                }
            }
        }
    
    return {
        "api_url": f"{base_url}/api/fileupload",
        "download_url": "https://ipfs.copylaradio.com",
        "supported_nips": [96, 98, 94, 71],
        "tos_url": f"{base_url}/terms",
        "content_types": [
            "image/*",
            "video/*",
            "audio/*",
            "application/pdf"
        ],
        "plans": plans,
        "extensions": {
            "ipfs": True,
            "provenance": True,
            "twin_key": True,
            "info_json": True,
            "tmdb_metadata": True
        }
    }
