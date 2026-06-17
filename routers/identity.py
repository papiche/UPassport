import os
import re
import json
import time
import asyncio
import hashlib
import logging
logger = logging.getLogger(__name__)
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

from pydantic import BaseModel, field_validator

from utils.helpers import as_form

# Caractères autorisés dans salt/pepper : tout caractère imprimable sauf ceux dangereux pour le shell
# Exclus : guillemets simples/doubles, backtick, $, \, retours à la ligne, null
# Permet : espaces, ponctuation courante (;!?.,()+-=*%&#@~), BIP39, emails…
_SAFE_CREDENTIAL_RE = re.compile(r'^[^\x00-\x1f"\'`$\\]{0,56}$')

@as_form
class G1NostrForm(BaseModel):
    email: str
    lang: str
    lat: str
    lon: str
    salt: str = ""
    pepper: str = ""
    format: str = "html"
    pass_code: str = ""
    birth_datetime: str = ""
    birth_place: str = ""
    birth_weight: str = ""
    conception_datetime: str = ""
    conception_place: str = ""
    polarity: str = "0"  # 0=homme, 1=femme — encodé dans saltRaw côté client

    @field_validator('salt', 'pepper', mode='before')
    @classmethod
    def validate_no_shell_injection(cls, v: str) -> str:
        """Rejette tout salt/pepper contenant des métacaractères shell dangereux."""
        if v and not _SAFE_CREDENTIAL_RE.match(v):
            raise ValueError(
                'Caractères non autorisés (exclus : guillemets " \' ` $ \\ et caractères de contrôle ; max 512 chars)'
            )
        return v

from fastapi import Depends


async def _derive_npub_from_credentials(salt: str, pepper: str) -> Optional[str]:
    """Dérive le npub NOSTR depuis stretchedSalt/stretchedPepper via keygen.
    Identique à keygen -t nostr : scrypt(pepper, salt) → ed25519."""
    keygen = settings.TOOLS_PATH / "keygen"
    if not keygen.exists():
        return None
    cred_file = Path(f"/dev/shm/.npub_{secrets.token_hex(8)}")
    try:
        cred_file.write_text(f"{salt}\n{pepper}\n")
        cred_file.chmod(0o600)
        proc = await asyncio.create_subprocess_exec(
            str(keygen), "-t", "nostr", "-i", str(cred_file),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        return stdout.decode().strip() or None
    except Exception as e:
        logger.warning(f"[npub_derive] {e}")
        return None
    finally:
        cred_file.unlink(missing_ok=True)


def _find_email_by_npub(npub: str) -> Optional[str]:
    """Scan ~/.zen/game/nostr/*/.secret.nostr → email existant pour ce npub."""
    pattern = f"NPUB={npub}"
    try:
        for secret in (settings.GAME_PATH / "nostr").glob("*/.secret.nostr"):
            try:
                if pattern in secret.read_text():
                    return secret.parent.name
            except OSError:
                continue
    except Exception as e:
        logger.warning(f"[npub_scan] {e}")
    return None


# Double-soumission : un seul appel /g1nostr actif par email
_g1nostr_in_progress: set[str] = set()

@router.post("/g1nostr")
async def scan_qr(
    request: Request,
    form_data: G1NostrForm = Depends(G1NostrForm.as_form)
):
    """
    Endpoint to execute the g1.sh script and return the generated file.
    Supports both regular users and swarm subscription aliases.

    Cas 1 — Nouveau email  : crée le MULTIPASS, retourne JSON ou HTML.
    Cas 2 — Email existant, format=json, sans pass_code  : retourne 409 (need_pass).
    Cas 3 — Email existant, format=json, pass_code fourni : vérifie PASS et retourne JSON.
    Cas 4 — Email existant, format=html  : comportement inchangé (retourne HTML).
    """
    email = form_data.email
    lang = form_data.lang
    lat = form_data.lat
    lon = form_data.lon
    salt = form_data.salt
    pepper = form_data.pepper
    format = form_data.format
    pass_code = (form_data.pass_code or "").strip()
    birth_datetime      = form_data.birth_datetime or ""
    birth_place         = form_data.birth_place or ""
    birth_weight        = form_data.birth_weight or ""
    conception_datetime = form_data.conception_datetime or ""
    conception_place    = form_data.conception_place or ""

    # ── Détection email existant ──────────────────────────────────────────────
    nostr_dir    = settings.GAME_PATH / "nostr" / email
    email_exists = nostr_dir.exists() and (nostr_dir / "G1PUBNOSTR").exists()

    # ── Pré-vérification identité (salt/pepper fournis) ──────────────────────
    # Dérive le npub localement avant toute création pour détecter les conflits.
    derived_npub: Optional[str] = None
    if salt and pepper and format == "json":
        derived_npub = await _derive_npub_from_credentials(salt, pepper)

    # ── Cas : email différent, même npub → IDENTITY_CONFLICT ─────────────────
    if derived_npub and not email_exists:
        conflict_email = _find_email_by_npub(derived_npub)
        if conflict_email and conflict_email != email:
            logger.warning(f"IDENTITY_CONFLICT: npub {derived_npub[:20]}… already owned by {conflict_email}")
            return JSONResponse(
                status_code=409,
                content={
                    "error": "IDENTITY_CONFLICT",
                    "message": "Ces données biométriques sont déjà associées à une autre identité."
                }
            )

    # ── Cas 2 & 3 : email existant + format JSON ──────────────────────────────
    if email_exists and format == "json":
        multipass_json = nostr_dir / ".multipass.json"
        pass_file      = settings.GAME_PATH / "players" / email / ".pass"

        # Récupération silencieuse : même email + même npub dérivé → pas de PASS requis
        if derived_npub and not pass_code:
            secret_nostr = nostr_dir / ".secret.nostr"
            if secret_nostr.exists():
                try:
                    stored_npub = next(
                        (p.split("=", 1)[1] for p in secret_nostr.read_text().split(";")
                         if p.strip().startswith("NPUB=")), None
                    )
                    if stored_npub and stored_npub.strip() == derived_npub:
                        if multipass_json.exists():
                            with open(multipass_json) as f:
                                data = json.load(f)
                            data["is_origin"]      = is_origin_mode()
                            data["oc_urls"]        = get_oc_tier_urls()
                            data["uplanet_home"]   = await get_uplanet_home_url()
                            data["uplanetname_g1"] = settings.UPLANETNAME_G1
                            logger.info(f"Silent recovery for {email} (npub match)")
                            return JSONResponse(data)
                except Exception as e:
                    logger.warning(f"Silent recovery check failed for {email}: {e}")

        if not pass_code:
            # Cas 2 — demander le PASS
            logger.info(f"MULTIPASS exists for {email}, requesting PASS")
            return JSONResponse(
                status_code=409,
                content={
                    "error": "MULTIPASS_EXISTS",
                    "need_pass": True,
                    "message": "Ce MULTIPASS existe déjà. Saisissez le code PASS reçu par email lors de la création."
                }
            )

        # Cas 3 — vérifier le PASS
        if not pass_file.exists():
            logger.warning(f"PASS file missing for {email}: {pass_file}")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "PASS_UNAVAILABLE",
                    "message": "Code PASS non disponible sur ce nœud. Contactez le support."
                }
            )

        stored_pass = pass_file.read_text().strip()
        if pass_code != stored_pass:
            logger.warning(f"Wrong PASS attempt for {email}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "INVALID_PASS",
                    "message": "Code PASS incorrect."
                }
            )

        # PASS correct — regénérer .multipass.json si absent puis retourner
        if not multipass_json.exists():
            logger.info(f"Regenerating .multipass.json for {email} via g1.sh")
            _salt = salt or ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
            _pep  = pepper or ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
            await run_script("./g1.sh", email, lang, lat, lon, _salt, _pep,
                             birth_datetime, birth_place, birth_weight,
                             conception_datetime, conception_place)
            for _ in range(6):
                if multipass_json.exists():
                    break
                await asyncio.sleep(0.5)

        if not multipass_json.exists():
            return JSONResponse(
                status_code=500,
                content={"error": "MULTIPASS_JSON_MISSING",
                         "message": "Reconstruction du JSON échouée. Contactez le support."}
            )

        try:
            with open(multipass_json, 'r') as f:
                data = json.load(f)
            data["is_origin"]      = is_origin_mode()
            data["oc_urls"]        = get_oc_tier_urls()
            data["uplanet_home"]   = await get_uplanet_home_url()
            data["uplanetname_g1"] = settings.UPLANETNAME_G1
            logger.info(f"MULTIPASS recovery via PASS OK for {email}")
            return JSONResponse(data)
        except Exception as e:
            logger.error(f"Error reading .multipass.json for {email}: {e}")
            raise HTTPException(status_code=500, detail=f"Erreur lecture JSON : {e}")

    # ── Anti double-soumission (création uniquement) ──────────────────────────
    if email in _g1nostr_in_progress:
        return JSONResponse(
            status_code=429,
            content={
                "error": "CREATION_IN_PROGRESS",
                "message": "Création déjà en cours pour cet email. Veuillez patienter."
            }
        )
    _g1nostr_in_progress.add(email)

    _DISCO_RAND = 24
    if not salt or salt.strip() == "":
        salt = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(_DISCO_RAND))
        logger.info(f"Generated random ZEN Card salt for {email}")
    if not pepper or pepper.strip() == "":
        pepper = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(_DISCO_RAND))
        logger.info(f"Generated random ZEN Card pepper for {email}")

    logger.info(f"ZEN Card credentials: salt={len(salt)} chars, pepper={len(pepper)} chars")

    multipass_json = settings.GAME_PATH / "nostr" / email / ".multipass.json"

    if format == "json":
        # ── Retour anticipé : lance g1.sh en arrière-plan, poll .multipass.json ──
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "./g1.sh",
                email, lang, lat, lon, salt, pepper,
                birth_datetime, birth_place, birth_weight,
                conception_datetime, conception_place,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Nettoyage zombie sans bloquer
            asyncio.ensure_future(proc.wait())
        except Exception as e:
            _g1nostr_in_progress.discard(email)
            logger.error(f"Failed to launch g1.sh for {email}: {e}")
            raise HTTPException(status_code=500, detail=f"Erreur lancement g1.sh : {e}")

        # Poll jusqu'à 40 s (80 × 0.5 s) — .multipass.json apparaît vers 10–15 s
        for i in range(80):
            if multipass_json.exists():
                break
            await asyncio.sleep(0.5)

        _g1nostr_in_progress.discard(email)

        if not multipass_json.exists():
            logger.error(f"JSON sidecar not found after 40s for {email}")
            raise HTTPException(status_code=500,
                                detail="MULTIPASS non initialisé après 40 s. Contactez le support.")
        try:
            with open(multipass_json) as f:
                data = json.load(f)
            data["is_origin"]      = is_origin_mode()
            data["oc_urls"]        = get_oc_tier_urls()
            data["uplanet_home"]   = await get_uplanet_home_url()
            data["uplanetname_g1"] = settings.UPLANETNAME_G1
            # Indiquer si la publication IPFS/DID est encore en cours
            if proc.returncode is None:
                data["status"] = "creating"
            logger.info(f"Early return for {email} (status={data.get('status','done')})")
            return JSONResponse(data)
        except Exception as e:
            logger.error(f"Error reading JSON sidecar for {email}: {e}")
            raise HTTPException(status_code=500, detail=f"Erreur lecture JSON : {e}")

    # ── Format HTML : attendre la fin complète du script ─────────────────────
    try:
        return_code, last_line = await run_script(
            "./g1.sh", email, lang, lat, lon, salt, pepper,
            birth_datetime, birth_place, birth_weight, conception_datetime, conception_place
        )
    finally:
        _g1nostr_in_progress.discard(email)

    if return_code != 0:
        logger.error(f"Erreur script g1.sh : {last_line}")
        raise HTTPException(status_code=500, detail=f"Erreur script g1.sh : {last_line}")

    return FileResponse(last_line.strip())

@as_form
class UPassportForm(BaseModel):
    parametre: str
    imageData: Optional[str] = None
    zlat: Optional[str] = None
    zlon: Optional[str] = None
    format: str = "html"

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
        logger.info(f"Received a PIN: {imageData}")
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
                    logger.info("Saved image to: %s", image_path)

            except Exception as e:
                logger.error("Error saving image: %s", e)

    # Log zlat and zlon values
    logger.info(f"zlat: {zlat}, zlon: {zlon}")

    ## Running External Script > get last line > send file content back to client.
    script_path = "./upassport.sh"
    return_code, last_line = await run_script(script_path, parametre, image_path, zlat, zlon)

    if return_code == 0:
        returned_file_path = last_line.strip()
        logger.info(f"Returning file: {returned_file_path}")
        
        # Vérifier si le fichier existe
        if not os.path.exists(returned_file_path):
            error_message = f"Le fichier {returned_file_path} n'existe pas"
            logger.error(error_message)
            raise HTTPException(status_code=404, detail=error_message)
            
        # Vérifier si c'est bien un fichier HTML
        if not returned_file_path.endswith('.html'):
            error_message = f"Le fichier {returned_file_path} n'est pas un fichier HTML"
            logger.error(error_message)
            raise HTTPException(status_code=400, detail=error_message)
            
        try:
            return FileResponse(
                returned_file_path,
                media_type='text/html',
                filename=os.path.basename(returned_file_path)
            )
        except Exception as e:
            error_message = f"Erreur lors de l'envoi du fichier: {str(e)}"
            logger.error(error_message)
            raise HTTPException(status_code=500, detail=error_message)
    else:
        error_message = f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs."
        logger.error(error_message)
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

    logger.info(f"Received Card NS: {cardns}")
    logger.info(f"Received SSSS key: [REDACTED - {len(ssss)} chars]")
    logger.info(f"ZEROCARD: {zerocard}")

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
                logger.info(f"🔑 NIP-96 Discovery: Checking MULTIPASS status for: {user_pubkey_hex[:16]}...")
                
                # Check if user is recognized as MULTIPASS by UPlanet
                is_multipass = is_multipass_user(user_pubkey_hex)
            else:
                logger.warning(f"⚠️  NIP-96 Discovery: Invalid NIP-98 event: kind={auth_event.get('kind')}")
    except Exception as e:
        logger.warning(f"⚠️  NIP-96 Discovery: Could not extract pubkey from NIP-98: {e}")
    
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
