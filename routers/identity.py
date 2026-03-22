import os
import json
import time
import hashlib
import logging
import secrets
import string
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from core.config import settings
from utils.helpers import run_script, get_myipfs_gateway, is_origin_mode, get_oc_tier_urls, get_uplanet_home_url
from utils.security import is_multipass_user
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

from fastapi import Depends

@router.post("/g1nostr")
async def scan_qr(
    request: Request,
    form_data: G1NostrForm = Depends(G1NostrForm.as_form)
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
    
    # ARCHITECTURE MULTIPASS v1→v2 :
    # • salt/pepper fournis → créent la ZEN Card (VISA/astronaute) via make_NOSTRCARD.sh → VISA.new.sh
    #   Pas de limite SSSS : ces clés ne vont PAS dans le DISCO du MULTIPASS
    # • MULTIPASS DISCO → toujours aléatoire (généré dans make_NOSTRCARD.sh)
    # • Si salt/pepper vides → on génère des valeurs aléatoires de 24 chars pour la ZEN Card aussi
    _DISCO_RAND = 24   # longueur auto-générée si non fournis

    # Generate random salt and pepper if not provided or empty
    if not salt or salt.strip() == "":
        salt = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(_DISCO_RAND))
        logging.info(f"Generated random ZEN Card salt for {email}: {salt[:10]}...")

    if not pepper or pepper.strip() == "":
        pepper = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(_DISCO_RAND))
        logging.info(f"Generated random ZEN Card pepper for {email}: {pepper[:10]}...")

    # Aucune limite de taille : salt/pepper vont vers la ZEN Card (VISA), pas le DISCO SSSS
    logging.info(f"ZEN Card credentials: salt={len(salt)} chars, pepper={len(pepper)} chars")
    
    script_path = "./g1.sh" # Make sure g1.sh is in the same directory or adjust path
    return_code, last_line = await run_script(script_path, email, lang, lat, lon, salt, pepper)

    if return_code == 0:
        returned_file_path = last_line.strip()
        logging.info(f"Returning file: {returned_file_path}")

        # JSON format: return MULTIPASS data for app onboarding
        if format == "json":
            multipass_json = settings.GAME_PATH / "nostr" / email / ".multipass.json"
            logging.info(f"Checking for JSON sidecar at: {multipass_json}")
            
            # Retry logic for file existence (in case of FS latency)
            import asyncio
            for i in range(5):
                if os.path.exists(multipass_json):
                    break
                logging.warning(f"JSON sidecar not found, retrying ({i+1}/5)...")
                await asyncio.sleep(0.5)
                
            if os.path.exists(multipass_json):
                try:
                    with open(multipass_json, 'r') as f:
                        data = json.load(f)
                    
                    data["is_origin"] = is_origin_mode()
                    data["oc_urls"] = get_oc_tier_urls()
                    data["uplanet_home"] = await get_uplanet_home_url()
                    return JSONResponse(data)
                except Exception as e:
                    logging.error(f"Error reading JSON sidecar: {e}")
                    raise HTTPException(status_code=500, detail=f"Error reading JSON sidecar: {str(e)}")
            else:
                # Check if directory exists to give better error
                parent_dir = multipass_json.parent
                dir_exists = os.path.exists(parent_dir)
                logging.error(f"JSON sidecar not found at {multipass_json}. Parent dir exists: {dir_exists}")
                if dir_exists:
                    try:
                        logging.error(f"Contents of {parent_dir}: {os.listdir(parent_dir)}")
                    except Exception as e:
                        logging.error(f"Could not list directory contents: {e}")
                
                raise HTTPException(status_code=500, detail=f"MULTIPASS created but JSON sidecar not found at {multipass_json}")

        return FileResponse(returned_file_path)
    else:
        error_message = f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs. Script output: {last_line}"
        logging.error(error_message)
        raise HTTPException(status_code=500, detail=error_message)

@as_form
class UPassportForm(BaseModel):
    parametre: str
    imageData: Optional[str] = None
    zlat: Optional[str] = None
    zlon: Optional[str] = None

@router.post("/upassport")
async def scan_qr_upassport(
    form_data: UPassportForm = Depends(UPassportForm.as_form)
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

@as_form
class SSSSForm(BaseModel):
    cardns: str
    ssss: str
    zerocard: Optional[str] = None

@router.post("/ssss")
async def ssss(request: Request, form_data: SSSSForm = Depends(SSSSForm.as_form)):
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
