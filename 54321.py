#!/usr/bin/env python3

import sys
import os

# This is a workaround for systemd environments where the virtualenv's site-packages
# might not be in the Python path.
# It ensures that the packages installed in the '.astro' virtual environment are found.
try:
    # Find the python executable's path to derive the site-packages path
    # sys.executable should be something like /home/user/.astro/bin/python
    venv_python_path = sys.executable
    if '.astro/bin/python' in venv_python_path:
        # Go from /home/user/.astro/bin/python to /home/user/.astro/lib/pythonX.Y/site-packages
        path_parts = venv_python_path.split(os.sep)
        # Find 'lib' directory at the same level as 'bin'
        astro_base_index = path_parts.index('.astro')
        astro_base_path = os.sep.join(path_parts[:astro_base_index+1])
        lib_path = os.path.join(astro_base_path, 'lib')

        if os.path.isdir(lib_path):
            python_version_dir = [d for d in os.listdir(lib_path) if d.startswith('python')]
            if python_version_dir:
                site_packages = os.path.join(lib_path, python_version_dir[0], 'site-packages')
                if os.path.isdir(site_packages) and site_packages not in sys.path:
                    sys.path.insert(0, site_packages)
                    # Use print for logging at this early stage as logger is not configured yet
                    print(f"INFO: Manually added '{site_packages}' to sys.path for systemd compatibility.")
except Exception as e:
    print(f"WARNING: Could not dynamically add site-packages to path. This might be fine if running in an activated venv. Error: {e}")

import uuid
import hmac
import re
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, ValidationError
from dotenv import load_dotenv
from typing import Optional, Dict, Any, List
import asyncio
import aiofiles
import json
import logging
import base64
from datetime import datetime
import subprocess
import traceback
import magic
import time
import hashlib
import websockets
import shutil
import zipfile
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote, urlparse, parse_qs
from pathlib import Path
import mimetypes
import sys
import unicodedata
from collections import defaultdict, deque
import threading
import ipaddress
import secrets

# Obtenir le timestamp Unix actuel
unix_timestamp = int(time.time())

# Configure le logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Import oracle_system for permit management
try:
    from oracle_system import (
        OracleSystem, 
        PermitDefinition, 
        PermitRequest, 
        PermitAttestation,
        PermitStatus
    )
    ORACLE_ENABLED = True
except ImportError as e:
    logging.warning(f"Oracle system not available: {e}")
    ORACLE_ENABLED = False

# Charger les variables d'environnement depuis le fichier .env
load_dotenv()
# Récupérer la valeur de OBSkey depuis l'environnement
OBSkey = os.getenv("OBSkey")

DEFAULT_PORT = 54321
DEFAULT_HOST = "127.0.0.1"
SCRIPT_DIR = Path(__file__).parent
DEFAULT_SOURCE_DIR = SCRIPT_DIR

# Rate Limiting Configuration
RATE_LIMIT_REQUESTS = 20  # Maximum requests per minute (increased for better UX)
RATE_LIMIT_WINDOW = 60    # Time window in seconds (1 minute)
RATE_LIMIT_CLEANUP_INTERVAL = 300  # Cleanup old entries every 5 minutes

# Trusted IPs that are exempt from rate limiting (add your trusted IPs here)
TRUSTED_IPS = {
    "127.0.0.1",      # localhost
    "::1",            # localhost IPv6
    "192.168.1.1",    # Example: your router
    # Add more trusted IPs as needed
}
# Trusted IP ranges (CIDR)
TRUSTED_IP_RANGES = [
    "10.99.99.0/24",
]

def is_trusted_ip(ip: str) -> bool:
    # Check direct match
    if ip in TRUSTED_IPS:
        return True
    # Check CIDR ranges
    try:
        ip_obj = ipaddress.ip_address(ip)
        for cidr in TRUSTED_IP_RANGES:
            if ip_obj in ipaddress.ip_network(cidr):
                return True
    except Exception:
        pass
    return False

# Global rate limiting storage
class RateLimiter:
    def __init__(self):
        self.requests = defaultdict(deque)  # IP -> deque of timestamps
        self.lock = threading.Lock()
        self.last_cleanup = time.time()
    
    def is_allowed(self, ip: str) -> bool:
        """Check if the IP is allowed to make a request"""
        current_time = time.time()
        
        with self.lock:
            # Cleanup old entries periodically
            if current_time - self.last_cleanup > RATE_LIMIT_CLEANUP_INTERVAL:
                self._cleanup_old_entries(current_time)
                self.last_cleanup = current_time
            
            # Get timestamps for this IP
            timestamps = self.requests[ip]
            
            # Remove timestamps older than the window
            while timestamps and timestamps[0] < current_time - RATE_LIMIT_WINDOW:
                timestamps.popleft()
            
            # Check if we're under the limit
            if len(timestamps) < RATE_LIMIT_REQUESTS:
                timestamps.append(current_time)
                return True
            
            return False
    
    def get_remaining_requests(self, ip: str) -> int:
        """Get remaining requests for an IP"""
        current_time = time.time()
        
        with self.lock:
            timestamps = self.requests[ip]
            
            # Remove old timestamps
            while timestamps and timestamps[0] < current_time - RATE_LIMIT_WINDOW:
                timestamps.popleft()
            
            return max(0, RATE_LIMIT_REQUESTS - len(timestamps))
    
    def get_reset_time(self, ip: str) -> Optional[float]:
        """Get the time when the rate limit will reset for an IP"""
        with self.lock:
            timestamps = self.requests[ip]
            if not timestamps:
                return None
            
            # Return the time when the oldest request will expire
            return timestamps[0] + RATE_LIMIT_WINDOW
    
    def _cleanup_old_entries(self, current_time: float):
        """Remove old entries to prevent memory leaks"""
        cutoff_time = current_time - RATE_LIMIT_WINDOW
        
        # Remove IPs with no recent requests
        ips_to_remove = []
        for ip, timestamps in self.requests.items():
            # Remove old timestamps
            while timestamps and timestamps[0] < cutoff_time:
                timestamps.popleft()
            
            # If no timestamps left, mark for removal
            if not timestamps:
                ips_to_remove.append(ip)
        
        # Remove empty entries
        for ip in ips_to_remove:
            del self.requests[ip]
        
        logging.info(f"Rate limiter cleanup: removed {len(ips_to_remove)} IPs, {len(self.requests)} active IPs")
        
        # Nettoyer aussi le cache NOSTR
        current_time = time.time()
        expired_npub = [npub for npub, (_, cached_time) in nostr_auth_cache.items() 
                       if current_time - cached_time > NOSTR_CACHE_TTL]
        for npub in expired_npub:
            del nostr_auth_cache[npub]
        if expired_npub:
            logging.info(f"NOSTR cache cleanup: removed {len(expired_npub)} expired entries")
        
        # Clean up expired profile cache
        expired_profiles = [pubkey for pubkey, (_, cached_time) in nostr_profile_cache.items()
                           if current_time - cached_time > NOSTR_PROFILE_CACHE_TTL]
        for pubkey in expired_profiles:
            del nostr_profile_cache[pubkey]
        if expired_profiles:
            logging.info(f"NOSTR profile cache cleanup: removed {len(expired_profiles)} expired entries")

# Create global rate limiter instance
rate_limiter = RateLimiter()

# Cache pour les authentifications NOSTR (évite les requêtes répétées)
nostr_auth_cache = {}
NOSTR_CACHE_TTL = 300  # 5 minutes

# Cache pour les profils NOSTR (évite les requêtes répétées)
# Maps pubkey (hex) -> (profile_data, timestamp)
nostr_profile_cache = {}
NOSTR_PROFILE_CACHE_TTL = 3600  # 1 hour

# Cache pour le mapping hex -> email (MULTIPASS detection)
# Maps hex_pubkey (lowercase) -> email directory name
hex_to_email_cache = {}
# Cache pour les répertoires utilisateur (évite les scans répétés)
# Maps hex_pubkey (lowercase) -> Path to user directory
hex_to_directory_cache = {}
hex_cache_lock = threading.Lock()
hex_cache_built = False

def get_client_ip(request: Request) -> str:
    """Extract the real client IP address, handling proxies"""
    # Check for forwarded headers (common with proxies/load balancers)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP in the chain (original client)
        return forwarded_for.split(",")[0].strip()
    
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # Fallback to direct connection IP
    return request.client.host if request.client else "unknown"

def check_rate_limit(request: Request) -> Dict[str, Any]:
    """Check rate limit for the current request"""
    client_ip = get_client_ip(request)
    
    # Skip rate limiting for trusted IPs
    if is_trusted_ip(client_ip):
        logging.info(f"Trusted IP {client_ip} - skipping rate limiting")
        return {
            "remaining_requests": float('inf'),  # Unlimited for trusted IPs
            "reset_time": None,
            "client_ip": client_ip,
            "trusted": True
        }
    
    if not rate_limiter.is_allowed(client_ip):
        reset_time = rate_limiter.get_reset_time(client_ip)
        remaining_time = int(reset_time - time.time()) if reset_time else 0
        
        # Log the rate limit violation
        logging.warning(f"Rate limit exceeded for IP {client_ip}: {RATE_LIMIT_REQUESTS} requests per minute limit")
        
        raise HTTPException(
            status_code=429,  # Too Many Requests
            detail={
                "error": "Rate limit exceeded",
                "message": f"Too many requests. Limit: {RATE_LIMIT_REQUESTS} requests per minute.",
                "remaining_time": remaining_time,
                "reset_time": reset_time,
                "client_ip": client_ip,
                "trusted": False
            }
        )
    
    return {
        "remaining_requests": rate_limiter.get_remaining_requests(client_ip),
        "reset_time": rate_limiter.get_reset_time(client_ip),
        "client_ip": client_ip,
        "trusted": False
    }

# Configuration pour les types de fichiers et répertoires
FILE_TYPE_MAPPING = {
    # Images
    'image/jpeg': 'Images',
    'image/jpg': 'Images',
    'image/png': 'Images',
    'image/gif': 'Images',
    'image/webp': 'Images',
    'image/bmp': 'Images',
    'image/svg+xml': 'Images',
    'image/tiff': 'Images',
    
    # Music/Audio
    'audio/mpeg': 'Music',
    'audio/mp3': 'Music',
    'audio/wav': 'Music',
    'audio/ogg': 'Music',
    'audio/flac': 'Music',
    'audio/aac': 'Music',
    'audio/m4a': 'Music',
    'audio/wma': 'Music',
    
    # Videos
    'video/mp4': 'Videos',
    'video/avi': 'Videos',
    'video/mov': 'Videos',
    'video/wmv': 'Videos',
    'video/flv': 'Videos',
    'video/webm': 'Videos',
    'video/mkv': 'Videos',
    'video/m4v': 'Videos',
    
    # Documents
    'application/pdf': 'Documents',
    'application/msword': 'Documents',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'Documents',
    'application/vnd.ms-excel': 'Documents',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Documents',
    'application/vnd.ms-powerpoint': 'Documents',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'Documents',
    'text/plain': 'Documents',
    'text/rtf': 'Documents',
    'application/rtf': 'Documents',
    'application/zip': 'Documents',
    'application/x-rar-compressed': 'Documents',
    'application/x-7z-compressed': 'Documents',
}

# Extensions de fichiers pour fallback
EXTENSION_MAPPING = {
    # Images
    '.jpg': 'Images', '.jpeg': 'Images', '.png': 'Images', '.gif': 'Images',
    '.webp': 'Images', '.bmp': 'Images', '.svg': 'Images', '.tiff': 'Images',
    '.ico': 'Images',
    
    # Music
    '.mp3': 'Music', '.wav': 'Music', '.ogg': 'Music', '.flac': 'Music',
    '.aac': 'Music', '.m4a': 'Music', '.wma': 'Music',
    
    # Videos
    '.mp4': 'Videos', '.avi': 'Videos', '.mov': 'Videos', '.wmv': 'Videos',
    '.flv': 'Videos', '.webm': 'Videos', '.mkv': 'Videos', '.m4v': 'Videos',
    
    # Documents
    '.pdf': 'Documents', '.doc': 'Documents', '.docx': 'Documents',
    '.xls': 'Documents', '.xlsx': 'Documents', '.ppt': 'Documents',
    '.pptx': 'Documents', '.txt': 'Documents', '.rtf': 'Documents',
    '.zip': 'Documents', '.rar': 'Documents', '.7z': 'Documents',
}

app = FastAPI()
# Mount the directory containing static files
app.mount("/static", StaticFiles(directory="static"), name="static")
# Mount UPlanet/earth for local JS development
earth_path = os.path.expanduser("~/.zen/workspace/UPlanet/earth")
if os.path.exists(earth_path):
    app.mount("/earth", StaticFiles(directory=earth_path), name="earth")
templates = Jinja2Templates(directory="templates")

# Initialize Oracle System (Permit Management)
if ORACLE_ENABLED:
    oracle_system = OracleSystem()
    
    # Load permit definitions from NOSTR if definitions are empty
    if len(oracle_system.definitions) == 0:
        try:
            definitions = oracle_system.fetch_permit_definitions_from_nostr()
            for definition in definitions:
                oracle_system.definitions[definition.id] = definition
            
            if definitions:
                oracle_system.save_data()
                logging.info(f"✅ Loaded {len(definitions)} permit definitions from NOSTR")
            else:
                logging.info("ℹ️  No permit definitions found in NOSTR (will load on demand)")
        except Exception as e:
            logging.warning(f"⚠️  Could not load permit definitions from NOSTR: {e}")
else:
    oracle_system = None

# ~ # Configurer CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins or restrict
    # ~ allow_origins=["https://ipfs.astroport.com", "https://u.astroport.com"],
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# --- Coinflip server-authoritative state ---
COINFLIP_SECRET = os.getenv("COINFLIP_SECRET") or base64.urlsafe_b64encode(os.urandom(32)).decode()
COINFLIP_SESSIONS: Dict[str, Dict[str, Any]] = {}

def sign_token(payload: Dict[str, Any]) -> str:
    """Create a compact HMAC token for small payloads (npub, exp, sessionId)."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(COINFLIP_SECRET.encode(), body, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(body).decode().rstrip('=') + "." + base64.urlsafe_b64encode(sig).decode().rstrip('=')
    return token

def verify_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        body_b64, sig_b64 = parts
        # pad
        pad = lambda s: s + "=" * (-len(s) % 4)
        body = base64.urlsafe_b64decode(pad(body_b64))
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        expected = hmac.new(COINFLIP_SECRET.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(body.decode())
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None

# Middleware pour appliquer le rate limiting sur toutes les routes
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Middleware to apply rate limiting to all requests"""
    try:
        # Skip rate limiting for static files only
        if request.url.path.startswith("/static"):
            response = await call_next(request)
            return response
        
        # Apply rate limiting (including health endpoint for testing)
        rate_info = check_rate_limit(request)
        
        # Add rate limit headers to response
        response = await call_next(request)
        
        # Add rate limiting headers
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_REQUESTS)
        
        # Handle trusted IPs (infinite remaining)
        if rate_info.get("trusted", False):
            response.headers["X-RateLimit-Remaining"] = "unlimited"
        else:
            response.headers["X-RateLimit-Remaining"] = str(rate_info["remaining_requests"])
        
        if rate_info["reset_time"]:
            response.headers["X-RateLimit-Reset"] = str(int(rate_info["reset_time"]))
        response.headers["X-RateLimit-Client-IP"] = rate_info["client_ip"]
        
        return response
        
    except HTTPException as e:
        if e.status_code == 429:
            # Rate limit exceeded - return proper headers
            response = JSONResponse(
                status_code=429,
                content=e.detail
            )
            response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_REQUESTS)
            response.headers["X-RateLimit-Remaining"] = "0"
            if e.detail.get("reset_time"):
                response.headers["X-RateLimit-Reset"] = str(int(e.detail["reset_time"]))
            response.headers["X-RateLimit-Client-IP"] = e.detail.get("client_ip", "unknown")
            return response
        raise e

# Modèles Pydantic existants
class MessageData(BaseModel):
    ulat: str
    ulon: str
    pubkey: str
    uid: str
    relation: str
    pubkeyUpassport: str
    email: str
    message: str

# Nouveaux modèles pour uDRIVE functionality
class UploadResponse(BaseModel):
    success: bool
    message: str
    file_path: str
    file_type: str
    target_directory: str
    new_cid: Optional[str] = None
    timestamp: str
    auth_verified: Optional[bool] = False
    fileName: Optional[str] = None
    description: Optional[str] = None  # Description for images (AI-generated) or other files
    info: Optional[str] = None  # CID of info.json file containing all metadata (from upload2ipfs.sh)
    thumbnail_ipfs: Optional[str] = None  # CID of thumbnail image (for videos, generated by upload2ipfs.sh)
    gifanim_ipfs: Optional[str] = None  # CID of animated GIF (for videos, generated by upload2ipfs.sh)
    fileHash: Optional[str] = None  # SHA256 hash of the file (for provenance tracking, from upload2ipfs.sh)
    mimeType: Optional[str] = None  # MIME type of the file (from upload2ipfs.sh)
    duration: Optional[int] = None  # Duration in seconds (for videos, from upload2ipfs.sh)
    dimensions: Optional[str] = None  # Video dimensions (e.g., "640x480", from upload2ipfs.sh)
    upload_chain: Optional[str] = None  # Upload chain for provenance tracking (from upload2ipfs.sh)

class DeleteRequest(BaseModel):
    file_path: str
    npub: str  # Authentification NOSTR obligatoire

class DeleteResponse(BaseModel):
    success: bool
    message: str
    deleted_file: str
    new_cid: Optional[str] = None
    timestamp: str
    auth_verified: bool

class CoinflipStartRequest(BaseModel):
    token: str

class CoinflipStartResponse(BaseModel):
    ok: bool
    sid: str
    exp: int

class CoinflipFlipRequest(BaseModel):
    token: str

class CoinflipFlipResponse(BaseModel):
    ok: bool
    sid: str
    result: str  # "Heads" or "Tails"
    consecutive: int

class CoinflipPayoutRequest(BaseModel):
    token: str
    player_id: Optional[str] = None  # email or hex when paying to PLAYER

class CoinflipPayoutResponse(BaseModel):
    ok: bool
    sid: str
    zen: int
    g1_amount: str
    tx: Optional[str] = None

class UploadFromDriveRequest(BaseModel):
    ipfs_link: str # Format attendu : QmHASH/filename.ext
    npub: str
    owner_hex_pubkey: Optional[str] = None  # Clé publique hex du propriétaire du drive source
    owner_email: Optional[str] = None       # Email du propriétaire du drive source

class UploadFromDriveResponse(BaseModel):
    success: bool
    message: str
    file_path: str
    file_type: str
    new_cid: Optional[str] = None
    timestamp: str
    auth_verified: bool

# Créez le dossier 'tmp' s'il n'existe pas
if not os.path.exists('tmp'):
    os.makedirs('tmp')

def find_user_directory_by_hex(hex_pubkey: str) -> Path:
    """Trouver le répertoire utilisateur correspondant à la clé publique hex (with caching)"""
    if not hex_pubkey:
        raise HTTPException(status_code=400, detail="Clé publique hex manquante")
    
    # Normaliser la clé hex
    hex_pubkey = hex_pubkey.lower().strip()
    
    # Check cache first
    with hex_cache_lock:
        if hex_pubkey in hex_to_directory_cache:
            cached_dir = hex_to_directory_cache[hex_pubkey]
            # Verify cache is still valid (directory still exists)
            if cached_dir.exists():
                logging.info(f"✅ Répertoire trouvé dans le cache pour {hex_pubkey}: {cached_dir}")
                return cached_dir
            else:
                # Cache invalid, remove it
                del hex_to_directory_cache[hex_pubkey]
                logging.warning(f"Cache invalide pour {hex_pubkey}, répertoire n'existe plus")
    
    # Chemin de base pour les utilisateurs NOSTR
    nostr_base_path = Path.home() / ".zen" / "game" / "nostr"
    
    if not nostr_base_path.exists():
        raise HTTPException(
            status_code=404, 
            detail=f"Répertoire NOSTR non trouvé: {nostr_base_path}"
        )
    
    logging.info(f"Recherche du répertoire pour la clé hex: {hex_pubkey}")
    logging.info(f"Recherche dans: {nostr_base_path}")
    
    # Parcourir tous les dossiers email dans nostr/
    for email_dir in nostr_base_path.iterdir():
        if email_dir.is_dir() and '@' in email_dir.name:
            hex_file_path = email_dir / "HEX"
            
            if hex_file_path.exists():
                try:
                    with open(hex_file_path, 'r') as f:
                        stored_hex = f.read().strip().lower()
                    
                    logging.info(f"Vérification {email_dir.name}: {stored_hex}")
                    
                    if stored_hex == hex_pubkey:
                        logging.info(f"✅ Répertoire trouvé pour {hex_pubkey}: {email_dir}")
                        
                        # S'assurer que le répertoire APP/uDRIVE existe
                        app_dir = email_dir / "APP/uDRIVE"
                        app_dir.mkdir(exist_ok=True)
                        
                        # Vérifier la présence du script IPFS et le copier si nécessaire
                        user_script = app_dir / "generate_ipfs_structure.sh"
                        if not user_script.exists():
                            generic_script = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "generate_ipfs_structure.sh"
                            if generic_script.exists():
                                # Créer un lien symbolique
                                user_script.symlink_to(generic_script)
                                logging.info(f"Lien symbolique créé vers {user_script}")
                            else:
                                logging.warning(f"Script générique non trouvé dans {generic_script}")
                        
                        # Cache the result
                        with hex_cache_lock:
                            hex_to_directory_cache[hex_pubkey] = email_dir
                        
                        return email_dir
                        
                except Exception as e:
                    logging.warning(f"Erreur lors de la lecture de {hex_file_path}: {e}")
                    continue
    
    # Si aucun répertoire trouvé
    raise HTTPException(
        status_code=404, 
        detail=f"Aucun répertoire utilisateur trouvé pour la clé publique: {hex_pubkey}. "
               f"Vérifiez que l'utilisateur est enregistré dans ~/.zen/game/nostr/"
    )

def get_authenticated_user_directory(npub: str) -> Path:
    """Obtenir le répertoire APP de l'utilisateur authentifié basé sur sa clé publique NOSTR uniquement"""
    
    # Convertir npub en hex
    hex_pubkey = npub_to_hex(npub)
    if not hex_pubkey:
        raise HTTPException(
            status_code=400, 
            detail="Impossible de convertir la clé publique en format hexadécimal"
        )
    
    # Trouver le répertoire correspondant à cette clé
    user_root_dir = find_user_directory_by_hex(hex_pubkey)
    
    # Retourner le répertoire APP (où doivent aller les fichiers uploadés)
    app_dir = user_root_dir
    app_dir.mkdir(exist_ok=True)  # S'assurer que APP/ existe
    
    logging.info(f"Répertoire APP utilisateur (sécurisé): {app_dir}")
    return app_dir

def extract_nsec_from_keyfile(keyfile_path: str) -> str:
    """
    Extract NSEC key from .secret.nostr file
    
    Expected format: NSEC=nsec1...; NPUB=npub1...; HEX=...;
    
    Args:
        keyfile_path: Path to .secret.nostr file
        
    Returns:
        str: NSEC key (nsec1...)
        
    Raises:
        FileNotFoundError: If keyfile doesn't exist
        ValueError: If NSEC not found or invalid format
    """
    if not os.path.exists(keyfile_path):
        raise FileNotFoundError(f"Keyfile not found: {keyfile_path}")
    
    with open(keyfile_path, 'r') as f:
        content = f.read().strip()
    
    # Parse the keyfile format: NSEC=nsec1...; NPUB=npub1...; HEX=...;
    for part in content.split(';'):
        part = part.strip()
        if part.startswith('NSEC='):
            nsec = part[5:].strip()
            if nsec.startswith('nsec1'):
                return nsec
            raise ValueError(f"Invalid NSEC format in keyfile: {nsec[:15]}...")
    
    raise ValueError("No NSEC key found in keyfile")

def _build_hex_index() -> None:
    """
    Build the hex -> email cache by scanning ~/.zen/game/nostr/ directories.
    This is called once (lazy initialization) and the cache is reused for all subsequent calls.
    Thread-safe with lock.
    """
    global hex_to_email_cache, hex_cache_built
    
    with hex_cache_lock:
        # Double-check pattern: another thread might have built it while we waited
        if hex_cache_built:
            return
        
        logging.info("🔍 Building hex -> email index cache for MULTIPASS detection...")
        nostr_base_path = Path.home() / ".zen" / "game" / "nostr"
        
        if not nostr_base_path.exists():
            logging.debug(f"ℹ️  NOSTR directory not found: {nostr_base_path}")
            hex_cache_built = True
            return
        
        count = 0
        for email_dir in nostr_base_path.iterdir():
            if email_dir.is_dir() and '@' in email_dir.name:
                hex_file_path = email_dir / "HEX"
                
                if hex_file_path.exists():
                    try:
                        with open(hex_file_path, 'r') as f:
                            stored_hex = f.read().strip().lower()
                        
                        if stored_hex:
                            hex_to_email_cache[stored_hex] = email_dir.name
                            count += 1
                            
                    except Exception as e:
                        logging.warning(f"⚠️  Error reading {hex_file_path}: {e}")
                        continue
        
        hex_cache_built = True
        logging.info(f"✅ Hex index cache built: {count} users indexed")

def is_multipass_user(hex_pubkey: str) -> bool:
    """
    Verify if a user is recognized as MULTIPASS by checking if their account exists in ~/.zen/game/nostr/.
    A user is considered MULTIPASS if their hex pubkey is found in any ~/.zen/game/nostr/{email}/HEX file.
    
    Uses an in-memory cache (built once) for O(1) lookup instead of scanning all directories.
    This is optimized for systems with thousands of users (e.g., 2500+ directories).
    
    Args:
        hex_pubkey: User's hexadecimal public key
        
    Returns:
        bool: True if user is MULTIPASS (exists in ~/.zen/game/nostr/), False otherwise
    """
    if not hex_pubkey:
        return False
    
    # Normalize the hex key
    hex_pubkey = hex_pubkey.lower().strip()
    
    # Build cache on first call (lazy initialization)
    if not hex_cache_built:
        _build_hex_index()
    
    # O(1) lookup in cache
    if hex_pubkey in hex_to_email_cache:
        email = hex_to_email_cache[hex_pubkey]
        logging.info(f"✅ User is recognized MULTIPASS (650MB quota) - found in {email}")
        return True
    
    # User not found in cache (not in ~/.zen/game/nostr/)
    logging.debug(f"ℹ️  User is not recognized MULTIPASS (100MB quota) - hex not in index")
    return False

def get_max_file_size_for_user(npub: str) -> int:
    """
    Get the maximum file size limit for a user according to UPlanet_FILE_CONTRACT.md.
    
    MULTIPASS users (recognized by UPlanet): 650MB
    Other NOSTR users: 100MB
    
    Args:
        npub: User's NOSTR public key (npub format)
        
    Returns:
        int: Maximum file size in bytes (650MB for MULTIPASS, 100MB for others)
    """
    hex_pubkey = npub_to_hex(npub) if npub else None
    if hex_pubkey and is_multipass_user(hex_pubkey):
        return 681574400  # 650MB (aligned with NIP-96 Discovery)
    else:
        return 104857600  # 100MB (default per UPlanet_FILE_CONTRACT.md section 6.2)

def sanitize_filename(filename: str) -> str:
    """
    DEPRECATED: Use sanitize_filename_python() instead.
    This function is kept for backward compatibility but redirects to the more secure version.
    """
    return sanitize_filename_python(filename)

def detect_file_type(file_content: bytes, filename: str) -> str:
    """
    Détecte le type de fichier basé sur le contenu ou l'extension.
    Note: Pour les détections basées sur le contenu, le contenu doit être non vide.
    """
    # Détection par extension en premier
    ext = filename.split('.')[-1].lower()

    # Types de base par extension
    if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'ico', 'tiff']:
        return "image"
    elif ext in ['mp4', 'avi', 'mov', 'webm', 'wmv', 'flv', 'mkv', 'm4v']:
        return "video"
    elif ext in ['mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a', 'wma']:
        return "audio"
    elif ext in ['html', 'htm']:
        return "html"
    elif ext in ['js', 'mjs']:
        return "javascript"
    elif ext in ['css']:
        return "stylesheet"
    elif ext in ['json']:
        return "json"
    elif ext in ['txt', 'md', 'rst', 'log', 'conf', 'ini', 'cfg', 'yaml', 'yml']:
        return "text"
    elif ext in ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx']:
        return "document"
    elif ext in ['zip', 'tar', 'gz', '7z', 'rar']:
        return "archive"
    elif ext in ['py', 'sh', 'bash', 'pl', 'rb', 'php', 'c', 'cpp', 'java', 'go', 'rs']:
        return "script"
    else:
        return "file"

def sanitize_filename_python(filename: str) -> str:
    """
    Sanitizes a filename to prevent directory traversal and invalid characters.
    """
    # Remove null bytes
    filename = filename.replace('\0', '')

    # Normalize unicode characters to their closest ASCII equivalents and remove non-ASCII
    # This helps prevent issues with different file systems and encoding attacks.
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('utf-8')

    # Remove/replace directory traversal attempts by removing '..' and '.' path segments
    # This specifically targets malicious path components.
    filename = str(Path(filename).name) # Get just the filename, stripping any path components

    # Replace invalid characters with an underscore
    # Common invalid characters on Windows: < > : " / \ | ? *
    # Linux/macOS typically only restrict / and null byte.
    # We'll be conservative and replace common problematic characters.
    invalid_chars_re = re.compile(r'[<>:"/\\|?*\x00]')
    filename = invalid_chars_re.sub('_', filename)

    # Ensure filename is not empty after sanitization
    if not filename:
        return "unnamed_file" # Or raise an error, depending on desired strictness

    # Limit filename length if desired (optional but good practice)
    # Windows max path length is 260, max filename is 255.
    # Linux typically 255 bytes for a component.
    filename = filename[:250] # Leave some buffer for potential extensions/prefixes

    return filename

async def run_uDRIVE_generation_script(source_dir: Path, enable_logging: bool = False) -> Dict[str, Any]:
    """Exécuter le script de génération IPFS spécifique à l'utilisateur dans le répertoire de son uDRIVE."""
    
    # source_dir est déjà le chemin complet vers APP/uDRIVE
    app_udrive_path = source_dir 
        
    script_path = app_udrive_path / "generate_ipfs_structure.sh"
    
    # Créer le répertoire APP/uDRIVE s'il n'existe pas (par sécurité, devrait déjà être fait)
    app_udrive_path.mkdir(parents=True, exist_ok=True)
    
    if not script_path.exists() or not script_path.is_symlink():
        generic_script_path = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "generate_ipfs_structure.sh"
        
        if generic_script_path.exists():
            # Supprimer un fichier existant si ce n'est pas un lien symbolique valide
            if script_path.exists():
                script_path.unlink() # Supprime le fichier ou lien cassé
                logging.warning(f"Fichier existant non symlinké ou cassé supprimé: {script_path}")

            # Créer un lien symbolique. Nous ne copions plus.
            script_path.symlink_to(generic_script_path)
            logging.info(f"Lien symbolique créé vers {script_path}")
        else:
            # Fallback vers le script générique du SCRIPT_DIR si pas trouvé dans Astroport.ONE
            fallback_script_path = SCRIPT_DIR / "generate_ipfs_structure.sh"
            if fallback_script_path.exists():
                if script_path.exists():
                    script_path.unlink() # Supprime le fichier ou lien cassé
                    logging.warning(f"Fichier existant non symlinké ou cassé supprimé: {script_path} (fallback)")
                script_path.symlink_to(fallback_script_path)
                logging.info(f"Lien symbolique créé (fallback) de {fallback_script_path} vers {script_path}")
            else:
                raise HTTPException(
                    status_code=500, 
                    detail=f"Script generate_ipfs_structure.sh non trouvé dans {generic_script_path} ni dans {fallback_script_path}"
                )
    else:
        logging.info(f"Utilisation du script utilisateur existant (lien symbolique): {script_path}")
    
    # S'assurer que le script cible du lien symbolique est exécutable
    if not os.access(script_path.resolve(), os.X_OK):
        # Tenter de rendre exécutable le script cible
        try:
            os.chmod(script_path.resolve(), 0o755)
            logging.info(f"Rendu exécutable le script cible: {script_path.resolve()}")
        except Exception as e:
            logging.error(f"Impossible de rendre exécutable le script cible {script_path.resolve()}: {e}")
            raise HTTPException(status_code=500, detail=f"Script IPFS non exécutable: {e}")

    # Construire la commande
    cmd = [str(script_path)]
    if enable_logging:
        cmd.append("--log")
    
    # L'argument pour le script shell doit être le répertoire actuel (.),
    # car le script sera exécuté avec cwd=app_udrive_path
    cmd.append(".") 
    
    try:
        # La fonction run_script elle-même doit s'assurer que cwd est défini sur app_udrive_path
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=app_udrive_path, # S'assurer que cwd est le répertoire uDRIVE
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return_code = process.returncode

        if return_code == 0:
            # Le CID final est sur la dernière ligne de stdout
            final_cid = stdout.decode().strip().split('\n')[-1] if stdout.strip() else None
            
            logging.info(f"Script IPFS exécuté avec succès depuis {app_udrive_path}")
            logging.info(f"Nouveau CID généré: {final_cid}")
            logging.info(f"Répertoire traité: {source_dir}")
            
            return {
                "success": True,
                "final_cid": final_cid,
                "stdout": stdout.decode() if enable_logging else None,
                "stderr": stderr.decode() if stderr.strip() else None,
                "script_used": str(script_path),
                "working_directory": str(app_udrive_path),
                "processed_directory": str(source_dir)
            }
        else:
            logging.error(f"Script failed with return code {return_code}")
            logging.error(f"Stderr: {stderr.decode()}")
            raise HTTPException(
                status_code=500, 
                detail=f"Erreur lors de l'exécution du script: {stderr.decode()}"
            )
            
    except Exception as e:
        logging.error(f"Exception lors de l'exécution du script: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

# NOSTR and NIP42 Functions
def hex_to_npub(hex_pubkey: str) -> Optional[str]:
    """
    Convert hex pubkey to npub (bech32 format).
    
    Args:
        hex_pubkey: 64-character hexadecimal public key
        
    Returns:
        npub string (npub1...) or None if conversion fails
    """
    if not hex_pubkey or len(hex_pubkey) != 64:
        return None
    
    try:
        # Try using nostr_sdk first (preferred)
        from nostr_sdk import PublicKey
        return PublicKey.from_hex(hex_pubkey).to_bech32()
    except Exception:
        try:
            # Fallback to bech32 library
            import bech32
            # Convert hex to bytes
            pubkey_bytes = bytes.fromhex(hex_pubkey)
            # Encode as npub
            return bech32.bech32_encode('npub', bech32.convertbits(pubkey_bytes, 8, 5))
        except Exception:
            return None

def npub_to_hex(npub: str) -> Optional[str]:
    """Convertir une clé publique npub en format hexadécimal"""
    try:
        # Si c'est déjà du hex (64 caractères), le valider et le retourner
        if len(npub) == 64:
            try:
                int(npub, 16)  # Vérifier que c'est du hex valide
                return npub.lower()  # Normaliser en minuscules
            except ValueError:
                logging.error(f"Clé de 64 caractères mais pas en hexadécimal valide: {npub}")
                return None
        
        # Si ça ne commence pas par npub1, on ne peut pas traiter
        if not npub.startswith('npub1'):
            logging.error(f"Format non supporté: {npub} (doit être npub1... ou hex 64 chars)")
            return None
        
        # Décoder bech32 basique (implémentation simplifiée)
        # Dans un environnement de production, utiliser une vraie lib bech32
        
        # Table bech32
        BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        
        # Enlever le préfixe 'npub1'
        data = npub[5:]
        
        # Décoder en base32
        decoded = []
        for char in data[:-6]:  # Enlever les 6 derniers chars (checksum)
            if char in BECH32_CHARSET:
                decoded.append(BECH32_CHARSET.index(char))
            else:
                logging.error(f"Caractère invalide dans npub: {char}")
                return None
        
        # Convertir de 5-bit à 8-bit
        bits = []
        for value in decoded:
            bits.extend([(value >> i) & 1 for i in range(4, -1, -1)])
        
        # Grouper par 8 bits et convertir en hex
        hex_bytes = []
        for i in range(0, len(bits) - len(bits) % 8, 8):
            byte_value = 0
            for j in range(8):
                byte_value = (byte_value << 1) | bits[i + j]
            hex_bytes.append(f"{byte_value:02x}")
        
        hex_pubkey = ''.join(hex_bytes)
        
        # Validation de la longueur (32 bytes = 64 hex chars)
        if len(hex_pubkey) == 64:
            logging.info(f"npub décodée avec succès: {npub} -> {hex_pubkey}")
            return hex_pubkey.lower()  # Normaliser en minuscules
        else:
            logging.error(f"Longueur incorrecte après décodage: {len(hex_pubkey)} chars")
            return None
        
    except Exception as e:
        logging.error(f"Erreur lors de la conversion npub: {e}")
        return None

def get_nostr_relay_url() -> str:
    """Obtenir l'URL du relai NOSTR local"""
    # Logique similaire à detectNOSTRws() du frontend
    host = DEFAULT_HOST.replace("127.0.0.1", "127.0.0.1")  # ou détecter depuis la requête
    port = "7777"  # Port strfry par défaut
    return f"ws://{host}:{port}"

async def fetch_video_event_from_nostr(event_id: str, timeout: int = 5) -> Optional[Dict[str, Any]]:
    """Fetch video event (kind 21 or 22) from NOSTR relay by event ID
    
    Args:
        event_id: NOSTR event ID (64 hex characters)
        timeout: Timeout in seconds
        
    Returns:
        Video event dict or None if not found
    """
    if not event_id or len(event_id) != 64:
        logging.warning(f"Invalid event ID format: {event_id}")
        return None
    
    relay_url = get_nostr_relay_url()
    logging.info(f"Fetching video event {event_id[:16]}... from relay: {relay_url}")
    
    try:
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            # Create subscription for video events (kind 21 or 22)
            subscription_id = f"video_fetch_{int(time.time())}"
            video_filter = {
                "kinds": [21, 22],  # Video events (normal and short)
                "ids": [event_id],
                "limit": 1
            }
            
            req_message = json.dumps(["REQ", subscription_id, video_filter])
            await websocket.send(req_message)
            
            # Wait for event or EOSE
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
            
            # Close subscription
            try:
                close_message = json.dumps(["CLOSE", subscription_id])
                await websocket.send(close_message)
                await asyncio.sleep(0.1)
            except Exception as e:
                logging.warning(f"Error closing subscription: {e}")
            
            return event_found
            
    except websockets.exceptions.ConnectionClosed:
        logging.error("Connection closed by relay")
        return None
    except websockets.exceptions.WebSocketException as e:
        logging.error(f"WebSocket error: {e}")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"JSON decode error: {e}")
        return None
    except Exception as e:
        logging.error(f"Error fetching video event: {e}")
        return None

async def parse_video_metadata(event: Dict[str, Any]) -> Dict[str, Any]:
    """Parse video event tags to extract metadata for Open Graph/Twitter Cards
    
    This function minimizes IPFS usage by prioritizing NOSTR event tags.
    IPFS is only used as a last resort if critical data (description) is missing.
    
    Strategy:
    1. Extract all available data from NOSTR tags first (no IPFS call)
    2. Only fetch info.json from IPFS if description is missing AND info tag exists
    3. Follow INFO_JSON_FORMATS.md v2.0 specification when parsing info.json
    
    Args:
        event: NOSTR video event (kind 21 or 22)
        
    Returns:
        Dict with title, description, thumbnail_url, video_url, etc.
    """
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
    ipfs_gateway = get_myipfs_gateway()
    
    # Step 1: Extract ALL available data from NOSTR tags first (NO IPFS call)
    # Extract title from title tag
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2:
            if tag[0] == "title":
                # Clean title: remove newlines and extra spaces
                title = tag[1].replace("\n", " ").replace("\r", " ").strip()
                while "  " in title:
                    title = title.replace("  ", " ")
                metadata["title"] = title
                break
    
    # Extract description from content
    if content:
        description = content
        if description.startswith("🎬"):
            lines = description.split("\n")
            if len(lines) > 1:
                description = "\n".join(lines[1:]).strip()
        # Clean description for meta tags: remove newlines, limit length
        description = description.replace("\n", " ").replace("\r", " ").strip()
        # Remove multiple spaces
        while "  " in description:
            description = description.replace("  ", " ")
        metadata["description"] = description[:300]  # Limit to 300 chars for meta tags
    
    # Extract IPFS URL from tags
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2:
            tag_type = tag[0]
            tag_value = tag[1]
            
            if tag_type == "url" and ("/ipfs/" in tag_value or "ipfs://" in tag_value):
                # Convert IPFS URL to gateway URL
                ipfs_path = tag_value.replace("ipfs://", "/ipfs/")
                if ipfs_path.startswith("/ipfs/"):
                    metadata["video_url"] = f"{ipfs_gateway}{ipfs_path}"
                    break
    
    # Extract thumbnail from tags - PRIORITIZE GIFANIM (animated GIF) for Twitter/Open Graph
    # Priority order: gifanim_ipfs > thumbnail_ipfs > image > r > imeta
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2:
            tag_type = tag[0]
            tag_value = tag[1]
            
            if tag_type == "gifanim_ipfs":
                # Animated GIF CID (preferred for Twitter/Open Graph)
                cid = tag_value
                if not cid.startswith("/ipfs/"):
                    cid = f"/ipfs/{cid}"
                metadata["thumbnail_url"] = f"{ipfs_gateway}{cid}"
                logging.info(f"🎬 Using animated GIF from gifanim_ipfs tag for Open Graph")
                break
            
            elif tag_type == "thumbnail_ipfs":
                # Static thumbnail CID (fallback if no gifanim)
                if not metadata["thumbnail_url"]:  # Only if gifanim not found
                    cid = tag_value
                    if not cid.startswith("/ipfs/"):
                        cid = f"/ipfs/{cid}"
                    metadata["thumbnail_url"] = f"{ipfs_gateway}{cid}"
            
            elif tag_type == "image" and ("/ipfs/" in tag_value or "ipfs://" in tag_value):
                # Image/thumbnail from imeta or direct tag
                if not metadata["thumbnail_url"]:  # Only if gifanim not found
                    ipfs_path = tag_value.replace("ipfs://", "/ipfs/")
                    if ipfs_path.startswith("/ipfs/"):
                        metadata["thumbnail_url"] = f"{ipfs_gateway}{ipfs_path}"
            
            elif tag_type == "r" and len(tag) >= 3 and tag[2] == "Thumbnail":
                # Reference tag with Thumbnail marker
                if not metadata["thumbnail_url"]:  # Only if gifanim not found
                    ipfs_path = tag_value.replace("ipfs://", "/ipfs/")
                    if ipfs_path.startswith("/ipfs/"):
                        metadata["thumbnail_url"] = f"{ipfs_gateway}{ipfs_path}"
    
    # Parse imeta tags for gifanim first, then thumbnail (if not found yet)
    if not metadata["thumbnail_url"]:
        for tag in tags:
            if isinstance(tag, list) and tag[0] == "imeta":
                for i in range(1, len(tag)):
                    prop = tag[i]
                    # Check for gifanim first (preferred)
                    if prop.startswith("gifanim "):
                        gifanim_value = prop[8:].strip()
                        if "/ipfs/" in gifanim_value or "ipfs://" in gifanim_value:
                            ipfs_path = gifanim_value.replace("ipfs://", "/ipfs/")
                            if ipfs_path.startswith("/ipfs/"):
                                metadata["thumbnail_url"] = f"{ipfs_gateway}{ipfs_path}"
                                logging.info(f"🎬 Using animated GIF from imeta gifanim for Open Graph")
                                break
                    # Fallback to image if no gifanim
                    elif prop.startswith("image "):
                        image_value = prop[6:].strip()
                        if "/ipfs/" in image_value or "ipfs://" in image_value:
                            ipfs_path = image_value.replace("ipfs://", "/ipfs/")
                            if ipfs_path.startswith("/ipfs/"):
                                metadata["thumbnail_url"] = f"{ipfs_gateway}{ipfs_path}"
                                break
                if metadata["thumbnail_url"]:
                    break
    
    # Step 2: Only fetch info.json from IPFS if critical data is missing
    # We only fetch if description is missing (title and thumbnail are usually in tags)
    info_cid = None
    if not metadata["description"]:
        # Check if info tag exists
        for tag in tags:
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "info":
                info_cid = tag[1].strip()
                break
        
        # Only make IPFS call if info_cid exists and description is still missing
        if info_cid:
            try:
                import httpx
                # Remove /ipfs/ prefix if present
                clean_cid = info_cid.replace("/ipfs/", "").replace("ipfs://", "")
                info_url = f"{ipfs_gateway}/ipfs/{clean_cid}/info.json"
                logging.info(f"📋 Fetching info.json from IPFS (description missing): {info_url}")
                
                async with httpx.AsyncClient(timeout=5.0) as client:
                    info_response = await client.get(info_url)
                    if info_response.status_code == 200:
                        info_data = info_response.json()
                        protocol_version = info_data.get("protocol", {}).get("version", "1.0.0")
                        logging.info(f"✅ Loaded info.json (protocol version: {protocol_version})")
                        
                        # Extract description from metadata.description
                        if info_data.get("metadata") and info_data["metadata"].get("description"):
                            description = info_data["metadata"]["description"]
                            # Clean description for meta tags: remove newlines, limit length
                            description = description.replace("\n", " ").replace("\r", " ").strip()
                            while "  " in description:
                                description = description.replace("  ", " ")
                            metadata["description"] = description[:300]  # Limit to 300 chars for meta tags
                            logging.info(f"📝 Description extracted from info.json")
                        
                        # Only extract thumbnail from info.json if still missing (shouldn't happen often)
                        # PRIORITIZE animated GIF (gifanim) for Twitter/Open Graph
                        if not metadata["thumbnail_url"] and info_data.get("media"):
                            media = info_data["media"]
                            is_v2 = protocol_version.startswith("2.")
                            
                            # v2.0 format: Prefer animated GIF, fallback to static
                            if is_v2 and media.get("thumbnails"):
                                thumbnails = media["thumbnails"]
                                # Prefer animated GIF for Twitter/Open Graph (more engaging)
                                thumbnail_cid = thumbnails.get("animated") or thumbnails.get("static")
                                if thumbnail_cid:
                                    clean_cid = thumbnail_cid.replace("/ipfs/", "").replace("ipfs://", "")
                                    metadata["thumbnail_url"] = f"{ipfs_gateway}/ipfs/{clean_cid}"
                                    if thumbnails.get("animated"):
                                        logging.info(f"🎬 Animated GIF from info.json (v2.0): {clean_cid[:16]}...")
                                    else:
                                        logging.info(f"🖼️  Static thumbnail from info.json (v2.0): {clean_cid[:16]}...")
                            
                            # v1.0 format fallback: Prefer gifanim_ipfs, fallback to thumbnail_ipfs
                            elif not is_v2:
                                thumbnail_cid = media.get("gifanim_ipfs") or media.get("thumbnail_ipfs")
                                if thumbnail_cid:
                                    clean_cid = thumbnail_cid.replace("/ipfs/", "").replace("ipfs://", "")
                                    metadata["thumbnail_url"] = f"{ipfs_gateway}/ipfs/{clean_cid}"
                                    if media.get("gifanim_ipfs"):
                                        logging.info(f"🎬 Animated GIF from info.json (v1.0): {clean_cid[:16]}...")
                                    else:
                                        logging.info(f"🖼️  Static thumbnail from info.json (v1.0): {clean_cid[:16]}...")
            except Exception as e:
                logging.warning(f"⚠️ Could not fetch info.json from IPFS: {e}")
                # Continue without info.json - we already have data from tags
    
    # Final fallback: if no description, use title
    if not metadata["description"]:
        metadata["description"] = metadata["title"]
    
    return metadata

async def check_nip42_auth(npub: str, timeout: int = 5) -> bool:
    """Vérifier l'authentification NIP42 sur le relai NOSTR local"""
    if not npub:
        logging.warning("check_nip42_auth: npub manquante")
    
    # Convertir npub en hex
    hex_pubkey = npub_to_hex(npub)
    if not hex_pubkey:
        logging.error("Impossible de convertir npub en hex")
        return False
    
    relay_url = get_nostr_relay_url()
    logging.info(f"Vérification NIP42 sur le relai: {relay_url} pour pubkey: {hex_pubkey}")
    
    try:
        # Se connecter au relai WebSocket avec timeout plus court
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            logging.info(f"Connecté au relai NOSTR: {relay_url}")
            
            # Calculer timestamp pour les 24 dernières heures
            since_timestamp = int(time.time()) - (24 * 60 * 60)  # 24h ago
            
            # Créer une requête pour les événements NIP42 récents de cette pubkey
            subscription_id = f"auth_check_{int(time.time())}"
            auth_filter = {
                "kinds": [22242],  # NIP42 auth events
                "authors": [hex_pubkey],  # Événements de cette pubkey
                "since": since_timestamp,  # Dans les dernières 24h
                "limit": 5  # Réduire la limite pour éviter trop de trafic
            }
            
            req_message = json.dumps(["REQ", subscription_id, auth_filter])
            logging.info(f"Envoi de la requête: {req_message}")
            
            await websocket.send(req_message)
            
            # Collecter les événements pendant un temps réduit
            events_found = []
            end_received = False
            
            try:
                while not end_received:
                    response = await asyncio.wait_for(websocket.recv(), timeout=3.0)  # Timeout réduit
                    parsed_response = json.loads(response)
                    
                    logging.info(f"Réponse reçue: {parsed_response[0] if parsed_response else 'Invalid'}")
                    
                    if parsed_response[0] == "EVENT":
                        # C'est un événement
                        if len(parsed_response) >= 3:
                            event = parsed_response[2]
                            events_found.append(event)
                            logging.info(f"Événement NIP42 trouvé: {event.get('id', 'N/A')} "
                                      f"du {datetime.fromtimestamp(event.get('created_at', 0))}")
                    
                    elif parsed_response[0] == "EOSE":
                        # Fin des événements pour cette subscription
                        if parsed_response[1] == subscription_id:
                            end_received = True
                            logging.info("Fin de la réception des événements (EOSE)")
                    
                    elif parsed_response[0] == "NOTICE":
                        # Message d'information du relai
                        logging.warning(f"Notice du relai: {parsed_response[1] if len(parsed_response) > 1 else 'N/A'}")
                        
            except asyncio.TimeoutError:
                logging.warning("Timeout lors de la réception des événements")
            
            # Fermer la subscription proprement
            try:
                close_message = json.dumps(["CLOSE", subscription_id])
                await websocket.send(close_message)
                # Petit délai pour que le serveur traite la fermeture
                await asyncio.sleep(0.1)
            except Exception as e:
                logging.warning(f"Erreur lors de la fermeture de subscription: {e}")
            
            # Analyser les événements trouvés
            if not events_found:
                logging.warning("Aucun événement NIP42 récent trouvé pour cette pubkey")
                return False
            
            # Vérifier la validité des événements NIP42
            valid_events = []
            for event in events_found:
                if validate_nip42_event(event, relay_url):
                    valid_events.append(event)
            
            if valid_events:
                logging.info(f"✅ {len(valid_events)} événement(s) NIP42 valide(s) trouvé(s)")
                # Afficher le plus récent
                latest_event = max(valid_events, key=lambda e: e.get('created_at', 0))
                latest_time = datetime.fromtimestamp(latest_event.get('created_at', 0))
                logging.info(f"   Dernière auth: {latest_time} (ID: {latest_event.get('id', 'N/A')})")
                return True
            else:
                logging.warning("❌ Aucun événement NIP42 valide trouvé")
                return False
                
    except websockets.exceptions.ConnectionClosed:
        logging.error("Connexion fermée par le relai")
        return False
    except websockets.exceptions.WebSocketException as e:
        logging.error(f"Erreur WebSocket: {e}")
        return False
    except json.JSONDecodeError as e:
        logging.error(f"Erreur de parsing JSON: {e}")
        return False
    except Exception as e:
        logging.error(f"Erreur lors de la vérification NIP42: {e}")
        return False

def validate_nip42_event(event: Dict[str, Any], expected_relay_url: str) -> bool:
    """Valider un événement NIP42"""
    try:
        # Vérifications de base
        if not isinstance(event, dict):
            return False
            
        required_fields = ['id', 'pubkey', 'created_at', 'kind', 'tags', 'content', 'sig']
        for field in required_fields:
            if field not in event:
                logging.warning(f"Champ manquant dans l'événement NIP42: {field}")
                return False
        
        # Vérifier le kind
        if event.get('kind') != 22242:
            logging.warning(f"Kind incorrect: {event.get('kind')} (attendu: 22242)")
            return False
        
        # Vérifier la présence du tag relay
        tags = event.get('tags', [])
        relay_found = False
        
        for tag in tags:
            if isinstance(tag, list) and len(tag) >= 2:
                if tag[0] == 'relay':
                    relay_found = True
                    relay_in_tag = tag[1]
                    logging.info(f"Tag relay trouvé: {relay_in_tag}")
                    
                    # Le relai peut être spécifié différemment, on est flexible
                    if '7777' in relay_in_tag or 'relay' in relay_in_tag:
                        logging.info("Tag relay valide trouvé")
                    else:
                        logging.info(f"Tag relay différent de l'attendu: {relay_in_tag}")
                    break
        
        if not relay_found:
            logging.warning("Tag 'relay' manquant dans l'événement NIP42")
            # On peut être flexible et accepter quand même
            # return False
        
        # Vérifier que l'événement est récent (moins de 24h)
        event_time = event.get('created_at', 0)
        current_time = int(time.time())
        age_hours = (current_time - event_time) / 3600
        
        if age_hours > 24:
            logging.warning(f"Événement NIP42 trop ancien: {age_hours:.1f}h")
            return False
        
        logging.info(f"✅ Événement NIP42 valide (âge: {age_hours:.1f}h)")
        return True
        
    except Exception as e:
        logging.error(f"Erreur lors de la validation de l'événement NIP42: {e}")
        return False

async def require_nostr_auth(npub: str = Form(...), force_check: bool = False) -> str:
    """
    FastAPI dependency to require NOSTR authentication.
    Returns the authenticated npub or raises HTTPException.
    
    Usage:
        @app.post("/api/endpoint")
        async def my_endpoint(npub: str = Depends(require_nostr_auth)):
            # npub is guaranteed to be authenticated
            ...
    """
    auth_verified = await verify_nostr_auth(npub, force_check=force_check)
    if not auth_verified:
        raise HTTPException(
            status_code=403, 
            detail="Nostr authentication failed. Please ensure you have sent a recent NIP-42 authentication event (kind 22242)."
        )
    return npub

async def verify_nostr_auth(npub: Optional[str], force_check: bool = False) -> bool:
    """Vérifier l'authentification NOSTR si une npub est fournie avec cache
    
    Args:
        npub: Clé publique NOSTR (hex ou npub format)
        force_check: Si True, ignore le cache et force la vérification sur le relay
    """
    if not npub:
        logging.info("Aucune npub fournie, pas de vérification NOSTR")
        return False
    
    # Vérifier le cache d'abord (sauf si force_check est activé)
    current_time = time.time()
    if not force_check and npub in nostr_auth_cache:
        cached_result, cached_time = nostr_auth_cache[npub]
        if current_time - cached_time < NOSTR_CACHE_TTL:
            logging.info(f"✅ Authentification NOSTR depuis le cache pour {npub}")
            return cached_result
        else:
            logging.info(f"⚠️ Cache expiré pour {npub}, vérification forcée")
    elif force_check:
        logging.info(f"🔍 Vérification forcée sans cache pour {npub}")
    
    logging.info(f"Vérification de l'authentification NOSTR pour: {npub}")
    
    # Déterminer si c'est une npub ou déjà du hex
    if len(npub) == 64:
        logging.info("Clé fournie semble être en format hex (64 caractères)")
        hex_pubkey = npub_to_hex(npub)  # Va la valider et normaliser
    elif npub.startswith('npub1'):
        logging.info("Clé fournie est en format npub, conversion nécessaire")
        hex_pubkey = npub_to_hex(npub)
    else:
        logging.error(f"Format de clé non reconnu: {npub} (longueur: {len(npub)})")
        return False
    
    if not hex_pubkey:
        logging.error("Impossible de convertir la clé en format hex")
        return False
    
    logging.info(f"Clé publique hex validée: {hex_pubkey}")
    
    # Vérifier NIP42 sur le relai local
    auth_result = await check_nip42_auth(hex_pubkey)
    logging.info(f"Résultat de la vérification NIP42: {auth_result}")
    
    # Mettre en cache le résultat
    nostr_auth_cache[npub] = (auth_result, current_time)
    
    return auth_result

async def run_script(script_path, *args, log_file_path=os.path.expanduser("~/.zen/tmp/54321.log")):
    """
    Fonction générique pour exécuter des scripts shell avec gestion des logs

    Args:
        script_path (str): Chemin du script à exécuter
        *args: Arguments à passer au script
        log_file_path (str): Chemin du fichier de log

    Returns:
        tuple: Code de retour et dernière ligne de sortie
    """
    logging.info(f"Running script: {script_path} with args: {args}")

    # Ensure log directory exists
    os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
    
    # Ensure log file exists - create it if it doesn't exist
    if not os.path.exists(log_file_path):
        try:
            # Create the log file with initial timestamp
            with open(log_file_path, 'w') as f:
                f.write(f"Log file created at {datetime.now().isoformat()}\n")
            logging.info(f"Created log file: {log_file_path}")
        except Exception as e:
            logging.error(f"Failed to create log file {log_file_path}: {e}")
            # Continue without failing - we'll try to open it anyway

    process = await asyncio.create_subprocess_exec(
        script_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    last_line = ""
    try:
        async with aiofiles.open(log_file_path, "a") as log_file:
            async for line in process.stdout:
                line = line.decode().strip()
                last_line = line
                await log_file.write(line + "\n")
                logging.info(f"Script output: {line}")
    except Exception as e:
        logging.error(f"Error writing to log file {log_file_path}: {e}")
        # Continue processing even if logging fails
        async for line in process.stdout:
            line = line.decode().strip()
            last_line = line
            logging.info(f"Script output (no log file): {line}")

    return_code = await process.wait()
    logging.info(f"Script finished with return code: {return_code}")

    return return_code, last_line

## CHECK G1PUB BALANCE
def check_balance(g1pub: str):
    """Vérifier le solde d'une g1pub donnée"""
    # Vérifier le solde avec la g1pub
    result = subprocess.run([os.path.expanduser("~/.zen/Astroport.ONE/tools/G1check.sh"), g1pub], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("Erreur dans COINScheck.sh: " + result.stderr)
    balance_line = result.stdout.strip().splitlines()[-1]
    return balance_line

def is_safe_email(email: str) -> bool:
    """Valider qu'un email est sûr et ne contient pas de caractères dangereux"""
    if not email or len(email) > 254:  # RFC 5321 limite
        return False
    
    # Vérifier qu'il n'y a pas de caractères dangereux pour les chemins
    dangerous_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '..']
    for char in dangerous_chars:
        if char in email:
            return False
    
    # Vérifier qu'il y a exactement un @ et qu'il n'est pas au début ou à la fin
    if email.count('@') != 1 or email.startswith('@') or email.endswith('@'):
        return False
    
    # Vérifier que les parties avant et après @ ne sont pas vides
    local_part, domain_part = email.split('@', 1)
    if not local_part or not domain_part:
        return False
    
    # Vérifier que le domaine contient au moins un point
    if '.' not in domain_part:
        return False
    
    return True

def is_safe_g1pub(g1pub: str) -> bool:
    """Valider qu'une g1pub est sûre et ne contient pas de caractères dangereux"""
    if not g1pub or len(g1pub) > 100:  # Limite raisonnable pour une g1pub
        return False
    
    # Vérifier qu'il n'y a que des caractères alphanumériques et quelques caractères spéciaux
    safe_pattern = re.compile(r'^[a-zA-Z0-9+/=]+(:ZEN)?$')
    return bool(safe_pattern.match(g1pub))

def get_safe_user_path(user_type: str, email: str, filename: str) -> Optional[str]:
    """Construire un chemin sûr pour un fichier utilisateur"""
    try:
        # Validation des paramètres
        if not is_safe_email(email) or not filename or '/' in filename or '\\' in filename:
            return None
        
        # Construire le chemin de manière sûre
        base_path = os.path.expanduser(f"~/.zen/game/{user_type}")
        user_dir = os.path.join(base_path, email)
        
        # Vérifier que le chemin final est bien dans le répertoire autorisé
        final_path = os.path.join(user_dir, filename)
        resolved_path = os.path.realpath(final_path)
        base_resolved = os.path.realpath(base_path)
        
        # Vérifier que le chemin résolu est bien dans le répertoire de base
        if not resolved_path.startswith(base_resolved):
            logging.warning(f"Tentative d'accès hors répertoire autorisé: {resolved_path}")
            return None
        
        return final_path
        
    except Exception as e:
        logging.error(f"Erreur construction chemin sûr: {e}")
        return None

def is_safe_ssh_key(ssh_key: str) -> bool:
    """Valider qu'une clé SSH publique est sûre"""
    if not ssh_key or len(ssh_key) > 2000:  # Limite raisonnable pour une clé SSH
        return False
    
    # Vérifier qu'il n'y a que des caractères autorisés dans une clé SSH
    # Format: ssh-rsa AAAAB3NzaC1yc2E... comment@host
    ssh_pattern = re.compile(r'^ssh-ed25519 [A-Za-z0-9+/=]+(\s+[^@\s]+@[^@\s]+)?$')
    return bool(ssh_pattern.match(ssh_key))

def is_safe_node_id(node_id: str) -> bool:
    """Valider qu'un node ID est sûr"""
    if not node_id or len(node_id) > 100:  # Limite raisonnable pour un node ID
        return False
    
    # Vérifier qu'il n'y a que des caractères alphanumériques et quelques caractères spéciaux
    node_pattern = re.compile(r'^[a-zA-Z0-9_-]+$')
    return bool(node_pattern.match(node_id))

def get_safe_swarm_path(node_id: str, filename: str) -> Optional[str]:
    """Construire un chemin sûr pour un fichier swarm"""
    try:
        # Validation des paramètres
        if not is_safe_node_id(node_id) or not filename or '/' in filename or '\\' in filename:
            return None
        
        # Construire le chemin de manière sûre
        base_path = os.path.expanduser("~/.zen/tmp/swarm")
        node_dir = os.path.join(base_path, node_id)
        
        # Vérifier que le chemin final est bien dans le répertoire autorisé
        final_path = os.path.join(node_dir, filename)
        resolved_path = os.path.realpath(final_path)
        base_resolved = os.path.realpath(base_path)
        
        # Vérifier que le chemin résolu est bien dans le répertoire de base
        if not resolved_path.startswith(base_resolved):
            logging.warning(f"Tentative d'accès hors répertoire swarm autorisé: {resolved_path}")
            return None
        
        return final_path
        
    except Exception as e:
        logging.error(f"Erreur construction chemin swarm sûr: {e}")
        return None

async def validate_uploaded_file(file: UploadFile, max_size_mb: int = 100) -> Dict[str, Any]:
    """Valider un fichier uploadé de manière sécurisée"""
    validation_result = {
        "is_valid": False,
        "error": None,
        "file_type": None,
        "file_size": 0,
        "mime_type": None
    }
    
    try:
        # 1. Validation de la taille du fichier
        if not file.size or file.size > max_size_mb * 1024 * 1024:
            validation_result["error"] = f"File size exceeds maximum allowed size of {max_size_mb}MB"
            return validation_result
        
        # 2. Validation du nom de fichier
        if not file.filename or len(file.filename) > 255:
            validation_result["error"] = "Invalid filename"
            return validation_result
        
        # 3. Validation des types MIME autorisés (sécurité renforcée)
        allowed_mime_types = {
            # Images (sécurisées)
            "image/jpeg", "image/png", "image/gif", "image/webp",
            # Documents (sécurisés)
            "application/pdf", "text/plain", "text/markdown", "text/html",
            "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            # Audio (sécurisé)
            "audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4", "audio/webm", "audio/flac",
            # Vidéo (sécurisé)
            "video/mp4", "video/webm", "video/ogg", "video/avi", "video/mov",
            # Archives (sécurisées)
            "application/zip", "application/x-7z-compressed",
            # Code (sécurisé)
            "text/javascript", "application/json", "text/css", "text/xml",
            "application/x-python-code", "text/x-python", "text/markdown"
            # Note: SVG et RAR supprimés pour sécurité
        }
        
        # Détecter le type MIME réel du contenu
        content_sample = await file.read(1024)
        await file.seek(0)  # Reset position
        
        detected_mime = magic.from_buffer(content_sample, mime=True)
        
        # Fallback: Si le type détecté est application/octet-stream, vérifier l'extension du fichier
        if detected_mime == "application/octet-stream" and file.filename:
            # Mapping des extensions vers types MIME autorisés
            extension_mime_map = {
                # Audio
                ".mp3": "audio/mpeg",
                ".mpeg": "audio/mpeg",
                ".wav": "audio/wav",
                ".ogg": "audio/ogg",
                ".flac": "audio/flac",
                ".aac": "audio/mp4",
                ".m4a": "audio/mp4",
                # Video
                ".mp4": "video/mp4",
                ".webm": "video/webm",
                ".mov": "video/mov",
                ".avi": "video/avi",
                ".mkv": "video/webm",  # Fallback to webm
                # Images
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }
            
            # Extraire l'extension du fichier
            file_ext = os.path.splitext(file.filename.lower())[1]
            if file_ext in extension_mime_map:
                # Utiliser le type MIME basé sur l'extension
                detected_mime = extension_mime_map[file_ext]
                logging.info(f"📎 File detected as 'application/octet-stream', using extension-based MIME type: {detected_mime} for {file.filename}")
            else:
                # Extension non reconnue, rejeter
                validation_result["error"] = f"File type 'application/octet-stream' with extension '{file_ext}' is not allowed"
                return validation_result
        
        if detected_mime not in allowed_mime_types:
            validation_result["error"] = f"File type '{detected_mime}' is not allowed"
            return validation_result
        
        # 4. Validation du contenu (vérification de signature de fichier)
        if not is_safe_file_content(content_sample, detected_mime):
            validation_result["error"] = "File content validation failed"
            return validation_result
        
        # 5. Validation réussie
        validation_result.update({
            "is_valid": True,
            "file_type": detected_mime,
            "file_size": file.size,
            "mime_type": detected_mime
        })
        
        return validation_result
        
    except Exception as e:
        validation_result["error"] = f"Validation error: {str(e)}"
        return validation_result

def is_safe_file_content(content_sample: bytes, mime_type: str) -> bool:
    """Vérifier que le contenu du fichier est sûr"""
    try:
        # Vérifier les signatures de fichiers pour les types critiques
        if mime_type.startswith("image/"):
            # Vérifier les signatures d'images
            image_signatures = {
                b'\xff\xd8\xff': 'JPEG',
                b'\x89PNG\r\n\x1a\n': 'PNG',
                b'GIF87a': 'GIF',
                b'GIF89a': 'GIF',
                b'RIFF': 'WEBP'
            }
            
            for signature, format_name in image_signatures.items():
                if content_sample.startswith(signature):
                    return True
            
            # Si c'est une image mais pas de signature reconnue, rejeter
            return False
        
        elif mime_type == "application/pdf":
            # Vérifier signature PDF
            return content_sample.startswith(b'%PDF')
        
        elif mime_type.startswith("text/"):
            # Pour les fichiers texte, vérifier qu'ils ne contiennent pas de caractères binaires
            try:
                content_sample.decode('utf-8')
                return True
            except UnicodeDecodeError:
                return False
        
        # Pour les autres types, accepter (validation basée sur MIME type)
        return True
        
    except Exception:
        return False

## DEFAULT = UPlanet Status (specify lat, lon, deg to select grid level)
@app.get("/")
async def ustats(request: Request, lat: str = None, lon: str = None, deg: str = None):
    script_path = os.path.expanduser("~/.zen/Astroport.ONE/Ustats.sh")

    # Préparer les arguments en fonction des paramètres reçus
    args = []
    if lat is not None and lon is not None:
        args.extend([lat, lon, deg])

    return_code, last_line = await run_script(script_path, *args)

    if return_code == 0:
        # Vérifier si last_line est un chemin de fichier ou du JSON
        if os.path.exists(last_line.strip()):
            # Si c'est un chemin de fichier, lire son contenu
            try:
                async with aiofiles.open(last_line.strip(), 'r') as f:
                    content = await f.read()
                return JSONResponse(content=json.loads(content))
            except Exception as e:
                logging.error(f"Error reading file: {e}")
                return JSONResponse(
                    status_code=500,
                    content={"error": f"Error reading file: {str(e)}"}
                )
        else:
            # Si c'est du JSON direct, le parser et le retourner
            try:
                return JSONResponse(content=json.loads(last_line))
            except json.JSONDecodeError as e:
                logging.error(f"Error parsing JSON: {e}")
                return JSONResponse(
                    status_code=500,
                    content={"error": f"Error parsing JSON: {str(e)}"}
                )
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans ./tmp/54321.log."}
        )

@app.get("/astro")
async def get_astro(request: Request):
    """Display the Astro Base template with IPFS gateway configuration"""
    myipfs_gateway = get_myipfs_gateway()
    return templates.TemplateResponse("astro_base.html", {
        "request": request,
        "myIPFS": myipfs_gateway
    })

@app.get("/cookie", response_class=HTMLResponse)
async def get_cookie_guide(request: Request):
    """Serve cookie export guide template with IPFS gateway"""
    myipfs_gateway = get_myipfs_gateway()
    logging.info(f"Serving cookie guide template with IPFS gateway: {myipfs_gateway}")
    return templates.TemplateResponse("cookie.html", {
        "request": request,
        "myIPFS": myipfs_gateway
    })

@app.get("/terms", response_class=HTMLResponse)
async def get_terms_of_service(request: Request):
    """Serve Terms of Service template"""
    from datetime import datetime
    current_date = datetime.now().strftime("%B %d, %Y")
    current_year = datetime.now().strftime("%Y")
    logging.info("Serving Terms of Service template")
    return templates.TemplateResponse("terms.html", {
        "request": request,
        "current_date": current_date,
        "current_year": current_year
    })

@app.get("/n8n", response_class=HTMLResponse)
async def get_n8n_workflow_builder(request: Request):
    """N8N-style workflow builder for cookie-based automation"""
    myipfs_gateway = get_myipfs_gateway()
    logging.info(f"Serving n8n workflow builder template with IPFS gateway: {myipfs_gateway}")
    return templates.TemplateResponse("n8n.html", {
        "request": request,
        "myIPFS": myipfs_gateway
    })

# Proxy route for 12345
@app.get("/12345")
async def proxy_12345(request: Request):
    import httpx
    
    # Get query parameters from the original request
    query_params = str(request.url.query)
    target_url = f"http://127.0.0.1:12345"
    if query_params:
        target_url += f"?{query_params}"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(target_url)
            return JSONResponse(
                content=response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
                status_code=response.status_code
            )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"error": "Timeout connecting to 127.0.0.1:12345"}
        )
    except httpx.ConnectError:
        return JSONResponse(
            status_code=502,
            content={"error": "Cannot connect to 127.0.0.1:12345"}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Proxy error: {str(e)}"}
        )

# UPlanet Geo Message
@app.get("/nostr")
async def get_nostr(request: Request, type: str = "default"):
    """
    Route NOSTR avec support de différents types de templates
    
    Paramètres:
    - type: "default" (nostr.html) ou "uplanet" (nostr_uplanet.html)
    """
    try:
        # Validation du paramètre type
        if type not in ["default", "uplanet"]:
            raise HTTPException(
                status_code=400, 
                detail=f"Type invalide: '{type}'. Types supportés: 'default', 'uplanet'"
            )
        
        # Déterminer le template à utiliser
        if type == "default":
            template_name = "nostr.html"
        elif type == "uplanet":
            template_name = "nostr_uplanet.html"
        
        # Vérifier que le template existe
        template_path = Path(__file__).parent / "templates" / template_name
        if not template_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Template '{template_name}' non trouvé. Vérifiez que le fichier existe dans le répertoire templates."
            )
        
        logging.info(f"Serving NOSTR template: {template_name} (type={type})")
        
        return templates.TemplateResponse(template_name, {"request": request})
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors du chargement du template NOSTR: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Erreur interne lors du chargement du template: {str(e)}"
        )

# ---DEV--- NOSTR BLOG MESSAGE
@app.get("/blog")
async def get_root(request: Request):
    return templates.TemplateResponse("nostr_blog.html", {"request": request})

# UPlanet Oracle - Permit Management Interface
@app.get("/wotx2", response_class=HTMLResponse)
async def get_wotx2(request: Request, npub: Optional[str] = None, permit_id: Optional[str] = None):
    """WoTx2 Permit Interface - Evolving Web of Trust for Professional Permits
    
    This interface reads all data from Nostr relays. The API only serves to initialize the page.
    All permit requests (30501) and attestations (30502) are managed directly via Nostr by each MULTIPASS.
    Only permit definitions (30500) and credentials (30503) are managed by UPLANETNAME_G1 via the API.
    
    Args:
        npub: Optional NOSTR public key for authenticated users
        permit_id: Optional permit ID to display (default: PERMIT_DE_NAGER or first available)
    """
    try:
        myipfs_gateway = get_myipfs_gateway()
        
        # Initialize empty data - will be loaded from Nostr by the frontend
        all_permits = []
        selected_permit_data = {}
        selected_permit_id = permit_id or "PERMIT_DE_NAGER"
        
        # Fetch permit definitions from Nostr (kind 30500) and local definitions
        if ORACLE_ENABLED and oracle_system is not None:
            try:
                # Fetch permit definitions from Nostr (includes MULTIPASS directories)
                nostr_definitions = oracle_system.fetch_permit_definitions_from_nostr()
                
                # Also include local definitions (from oracle_system.definitions)
                # This ensures permits created locally but not yet in Nostr are visible
                seen_permit_ids = set()
                
                # First, add Nostr definitions
                for permit_def in nostr_definitions:
                    seen_permit_ids.add(permit_def.id)
                    all_permits.append({
                        "id": permit_def.id,
                        "name": permit_def.name,
                        "description": permit_def.description,
                        "min_attestations": permit_def.min_attestations,
                        "holders_count": 0,  # Will be calculated from Nostr by frontend
                        "pending_count": 0,  # Will be calculated from Nostr by frontend
                        "category": permit_def.metadata.get("category", "general") if permit_def.metadata else "general"
                    })
                
                # Then, add local definitions not found in Nostr
                for def_id, local_def in oracle_system.definitions.items():
                    if def_id not in seen_permit_ids:
                        all_permits.append({
                            "id": local_def.id,
                            "name": local_def.name,
                            "description": local_def.description,
                            "min_attestations": local_def.min_attestations,
                            "holders_count": 0,
                            "pending_count": 0,
                            "category": local_def.metadata.get("category", "general") if local_def.metadata else "general"
                        })
                        # Also add to nostr_definitions list for selection logic
                        nostr_definitions.append(local_def)
                
                # Find selected permit (from Nostr or local)
                selected_permit = next((p for p in nostr_definitions if p.id == selected_permit_id), None)
                if not selected_permit and nostr_definitions:
                    selected_permit = nostr_definitions[0]
                    selected_permit_id = selected_permit.id if selected_permit else None
                
                if selected_permit:
                    selected_permit_data = {
                        "id": selected_permit.id,
                        "name": selected_permit.name,
                        "description": selected_permit.description,
                        "min_attestations": selected_permit.min_attestations,
                        "valid_duration_days": selected_permit.valid_duration_days,
                        "required_license": selected_permit.required_license,
                        "revocable": selected_permit.revocable,
                        "verification_method": selected_permit.verification_method,
                        "metadata": selected_permit.metadata
                    }
            except Exception as e:
                logging.warning(f"Error fetching permits from Nostr: {e}")
        
        # Detect if this is the primary station (ORACLE des ORACLES)
        # Same logic as ORACLE.refresh.sh - check if IPFSNODEID matches first STRAP in A_boostrap_nodes.txt
        is_primary_station = False
        ipfs_node_id = get_env_from_mysh("IPFSNODEID", "")
        if not ipfs_node_id:
            # Fallback to environment variable
            ipfs_node_id = os.getenv("IPFSNODEID", "")
        if ipfs_node_id:
            strapfile = None
            if os.path.exists(os.path.expanduser("~/.zen/game/MY_boostrap_nodes.txt")):
                strapfile = os.path.expanduser("~/.zen/game/MY_boostrap_nodes.txt")
            elif os.path.exists(os.path.expanduser("~/.zen/Astroport.ONE/A_boostrap_nodes.txt")):
                strapfile = os.path.expanduser("~/.zen/Astroport.ONE/A_boostrap_nodes.txt")
            
            if strapfile and os.path.exists(strapfile):
                try:
                    with open(strapfile, 'r') as f:
                        straps = []
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                # Extract IPFSNODEID from line (same logic as bash: rev | cut -d '/' -f 1 | rev)
                                strap_id = line.split('/')[-1].strip()
                                if strap_id:
                                    straps.append(strap_id)
                        
                        if straps and straps[0] == ipfs_node_id:
                            is_primary_station = True
                            logging.info(f"⭐ PRIMARY STATION DETECTED - IPFSNODEID {ipfs_node_id} matches first STRAP")
                except Exception as e:
                    logging.warning(f"Error reading bootstrap nodes file: {e}")
        
        return templates.TemplateResponse("wotx2.html", {
            "request": request,
            "myIPFS": myipfs_gateway,
            "permit_data": selected_permit_data,
            "all_permits": all_permits,
            "selected_permit_id": permit_id or "PERMIT_DE_NAGER",
            "npub": npub,
            "uSPOT": os.getenv("uSPOT", "http://127.0.0.1:54321"),
            "nostr_relay": os.getenv("myRELAY", "ws://127.0.0.1:7777").split()[0] if os.getenv("NOSTR_RELAYS") else "ws://127.0.0.1:7777",
            "IPFSNODEID": ipfs_node_id,
            "is_primary_station": is_primary_station
        })
        
    except Exception as e:
        logging.error(f"Error in get_wotx2: {e}", exc_info=True)
        return HTMLResponse(
            content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", 
            status_code=500
        )

@app.get("/oracle")
async def get_oracle(
    request: Request, 
    html: Optional[str] = None,
    type: Optional[str] = None,
    npub: Optional[str] = None
):
    """Oracle System Interface - Multi-signature permit management
    
    Args:
        html: If present, return HTML page instead of JSON
        type: Filter by type ('requests', 'credentials', 'definitions')
        npub: Filter by specific NOSTR public key
    """
    try:
        # Check if Oracle system is available
        if not ORACLE_ENABLED or oracle_system is None:
            error_msg = "Oracle system not available"
            if html is not None:
                return HTMLResponse(
                    content=f"<html><body><h1>Oracle System</h1><p>{error_msg}</p></body></html>", 
                    status_code=503
                )
            raise HTTPException(status_code=503, detail=error_msg)
        
        # Gather Oracle data
        oracle_data = {
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
        
        # Get permit definitions
        definitions = []
        for def_id, definition in oracle_system.definitions.items():
            definitions.append({
                "id": def_id,
                "name": definition.name,
                "description": definition.description,
                "min_attestations": definition.min_attestations,
                "required_license": definition.required_license,
                "valid_duration_days": definition.valid_duration_days,
                "revocable": definition.revocable,
                "verification_method": definition.verification_method,
                "metadata": definition.metadata
            })
        
        # Get permit requests (from Nostr)
        # Note: Since v2.1, requests are stored in Nostr, not oracle_system.requests
        requests_list = []
        try:
            nostr_requests = oracle_system.fetch_permit_requests_from_nostr()
            for req in nostr_requests:
                # Filter by npub if specified
                if npub and req.applicant_npub != npub:
                    continue
                
                requests_list.append({
                    "id": req.request_id,
                    "permit_definition_id": req.permit_definition_id,
                    "applicant_npub": req.applicant_npub,
                    "statement": req.statement,
                    "evidence": req.evidence if hasattr(req, 'evidence') else [],
                    "status": req.status.value if hasattr(req.status, 'value') else str(req.status),
                    "attestations": req.attestations if hasattr(req, 'attestations') else [],
                    "created_at": req.created_at.isoformat() if req.created_at else None,
                    "issued_credential_id": None  # Will be set when credential is issued
                })
        except Exception as e:
            logging.warning(f"Could not fetch requests from Nostr: {e}")
        
        # Get credentials
        credentials_list = []
        for cred_id, cred in oracle_system.credentials.items():
            # Filter by npub if specified
            if npub and cred.subject_npub != npub:
                continue
            
            credentials_list.append({
                "id": cred_id,
                "permit_definition_id": cred.permit_definition_id,
                "subject_npub": cred.subject_npub,
                "issued_at": cred.issued_at.isoformat() if cred.issued_at else None,
                "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                "revoked": cred.revoked,
                "revoked_at": cred.revoked_at.isoformat() if cred.revoked_at else None,
                "revocation_reason": cred.revocation_reason,
                "attestations": cred.attestations,
                "nostr_event_id": cred.nostr_event_id
            })
        
        # Populate oracle data based on type filter
        if type == "definitions" or type is None:
            oracle_data["definitions"] = definitions
            oracle_data["total_definitions"] = len(definitions)
        
        if type == "requests" or type is None:
            oracle_data["requests"] = requests_list
            oracle_data["total_requests"] = len(requests_list)
        
        if type == "credentials" or type is None:
            oracle_data["credentials"] = credentials_list
            oracle_data["total_credentials"] = len(credentials_list)
        
        # Add filter information
        oracle_data["filters"] = {
            "type": type,
            "npub": npub
        }
        
        # Return HTML page if requested
        if html is not None:
            myipfs_gateway = get_myipfs_gateway()
            return templates.TemplateResponse("oracle.html", {
                "request": request,
                "myIPFS": myipfs_gateway,
                "oracle_data": oracle_data
            })
        
        # Return JSON response
        return JSONResponse(content=oracle_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error in get_oracle: {e}", exc_info=True)
        if html is not None:
            return HTMLResponse(
                content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", 
                status_code=500
            )
        raise HTTPException(status_code=500, detail=str(e))

######################################### MULTIPASS CREATION
# UPlanet G1 MULTIPASS Registration
@app.get("/g1", response_class=HTMLResponse)
async def get_root(request: Request):
    return templates.TemplateResponse("g1nostr.html", {"request": request})

# Beside /g1
@app.post("/g1nostr")
async def scan_qr(request: Request, email: str = Form(...), lang: str = Form(...), lat: str = Form(...), lon: str = Form(...), salt: str = Form(default=""), pepper: str = Form(default="")):
    """
    Endpoint to execute the g1.sh script and return the generated file.
    Supports both regular users and swarm subscription aliases.
    """
    
    # Generate random salt and pepper if not provided or empty
    if not salt or salt.strip() == "":
        import secrets
        import string
        salt = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(42))
        logging.info(f"Generated random salt for {email}: {salt[:10]}...")
    
    if not pepper or pepper.strip() == "":
        import secrets
        import string
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
        ipfs_node_id = get_env_from_mysh("IPFSNODEID", "unknown")
        if not ipfs_node_id or ipfs_node_id == "unknown":
            ipfs_node_id = os.getenv("IPFSNODEID", "unknown")
        subscription_dir = os.path.expanduser(f"~/.zen/tmp/{ipfs_node_id}")
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
        y_level_files = [
            os.path.expanduser("~/.zen/game/secret.dunikey"),
            os.path.expanduser("~/.zen/game/secret.june")
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
                                ssh_to_g1_script = os.path.expanduser("~/.zen/Astroport.ONE/tools/ssh_to_g1ipfs.py")
                                if os.path.exists(ssh_to_g1_script):
                                    # Validation de sécurité pour la clé SSH
                                    if not is_safe_ssh_key(ssh_pub_key):
                                        logging.warning(f"❌ SSH key format invalide pour {node_id}")
                                        new_notification["ssh_key_invalid"] = True
                                    else:
                                        result = subprocess.run(
                                            ["python3", ssh_to_g1_script, ssh_pub_key],
                                            capture_output=True,
                                            text=True,
                                            timeout=10
                                        )
                                    
                                    if result.returncode == 0:
                                        computed_ipns = result.stdout.strip()
                                        logging.info(f"   Computed IPNS: {computed_ipns}")
                                        
                                        if computed_ipns == actual_node_id:
                                            logging.info(f"✅ SSH key verification successful for {node_id}")
                                            
                                            # Ajouter la clé SSH au fichier My_boostrap_ssh.txt
                                            bootstrap_ssh_file = os.path.expanduser("~/.zen/game/My_boostrap_ssh.txt")
                                            
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
                                        logging.error(f"❌ ssh_to_g1ipfs.py failed: {result.stderr}")
                                        new_notification["ssh_script_error"] = result.stderr
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
        
        return FileResponse(returned_file_path)
    else:
        error_message = f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs. Script output: {last_line}"
        logging.error(error_message)
        return JSONResponse({"error": error_message}, status_code=500) # Return 500 for server error

@app.get("/check_balance")
async def check_balance_route(g1pub: str, html: Optional[str] = None):
    try:
        # Si c'est un email (contient '@'), récupérer les 2 g1pub et leurs balances
        if '@' in g1pub:
            email = g1pub
            logging.info(f"Check balance pour email: {email}")
            
            # Validation de sécurité pour l'email
            if not is_safe_email(email):
                logging.error(f"Email non sécurisé: {email}")
                raise HTTPException(status_code=400, detail="Format d'email invalide")
            
            # Récupérer la g1pub du joueur (NOSTR)
            nostr_g1pub = None
            nostr_g1pub_path = get_safe_user_path("nostr", email, "G1PUBNOSTR")
            
            if nostr_g1pub_path and os.path.exists(nostr_g1pub_path):
                try:
                    with open(nostr_g1pub_path, 'r') as f:
                        nostr_g1pub = f.read().strip()
                except Exception as e:
                    logging.error(f"Erreur lecture fichier NOSTR: {e}")
            
            # Récupérer la g1pub du zencard
            zencard_g1pub = None
            zencard_g1pub_path = get_safe_user_path("players", email, ".g1pub")
            
            if zencard_g1pub_path and os.path.exists(zencard_g1pub_path):
                try:
                    with open(zencard_g1pub_path, 'r') as f:
                        zencard_g1pub = f.read().strip()
                except Exception as e:
                    logging.error(f"Erreur lecture fichier ZENCARD: {e}")
            
            # Vérifier qu'on a au moins une g1pub
            if not nostr_g1pub and not zencard_g1pub:
                logging.error(f"Aucune g1pub trouvée pour {email}")
                raise HTTPException(status_code=404, detail="Aucune g1pub trouvée pour cet email")
            
            # Récupérer les balances
            result = {}
            
            if nostr_g1pub:
                try:
                    nostr_balance = check_balance(nostr_g1pub)
                    result.update({
                        "balance": nostr_balance,
                        "g1pub": nostr_g1pub
                    })
                except Exception as e:
                    logging.error(f"Erreur balance NOSTR: {e}")
                    result.update({
                        "balance": "error",
                        "g1pub": nostr_g1pub
                    })
            
            if zencard_g1pub:
                try:
                    zencard_balance = check_balance(zencard_g1pub)
                    result.update({
                        "g1pub_zencard": zencard_g1pub,
                        "balance_zencard": zencard_balance
                    })
                except Exception as e:
                    logging.error(f"Erreur balance ZENCARD: {e}")
                    result.update({
                        "g1pub_zencard": zencard_g1pub,
                        "balance_zencard": "error"
                    })
            
            return generate_balance_html_page(email, result)
            
        else:
            # Si c'est une g1pub, faire directement la demande de balance
            # Validation de sécurité pour la g1pub
            if not is_safe_g1pub(g1pub):
                logging.error(f"G1PUB non sécurisée: {g1pub}")
                raise HTTPException(status_code=400, detail="Format de g1pub invalide")
            
            balance = check_balance(g1pub)
            result = {"balance": balance, "g1pub": g1pub}
            
            return result
            
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error(f"Erreur inattendue dans check_balance_route: {e}")
        raise HTTPException(status_code=500, detail="Erreur interne du serveur")

@app.get("/check_society")
async def check_society_route(request: Request, html: Optional[str] = None, nostr: Optional[str] = None):
    """Check transaction history of SOCIETY wallet to see capital contributions
    
    Args:
        html: If present, return HTML page instead of JSON
        nostr: If present, include Nostr DID data in the response
    """
    try:
        # Call G1society.sh to get filtered and calculated society data
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/G1society.sh")
        
        # Build command with optional --nostr flag
        cmd = [script_path]
        if nostr is not None:
            cmd.append("--nostr")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)  # Increased timeout for Nostr queries
        
        if result.returncode != 0:
            logging.error(f"G1society.sh failed with return code {result.returncode}: {result.stderr}")
            raise ValueError(f"Error in G1society.sh: {result.stderr}")
        
        # Parse JSON output from G1society.sh
        try:
            society_data = json.loads(result.stdout.strip())
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse G1society.sh output: {e}")
            logging.error(f"Raw output: {result.stdout[:500]}")
            raise ValueError(f"Invalid JSON from G1society.sh: {e}")
        
        # Check for errors in the response
        if "error" in society_data:
            logging.error(f"G1society.sh returned error: {society_data['error']}")
            raise HTTPException(status_code=500, detail=society_data['error'])
        
        # If html parameter is provided, return HTML page
        if html is not None:
            g1pub = society_data.get("g1pub", "N/A")
            return generate_society_html_page(request, g1pub, society_data)
        
        # Otherwise return JSON
        return society_data
        
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Transaction history retrieval timeout")
    except Exception as e:
        logging.error(f"Error checking society history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/check_revenue")
async def check_revenue_route(request: Request, html: Optional[str] = None, year: Optional[str] = None):
    """Check revenue history from ZENCOIN transactions (Chiffre d'Affaires)
    
    Args:
        html: If present, return HTML page instead of JSON
        year: Optional year filter (e.g. "2024", "2025"). Default: "all"
    """
    try:
        # Call G1revenue.sh to get filtered and calculated revenue data
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/G1revenue.sh")
        
        # Pass year filter as argument (default: "all")
        year_filter = year if year else "all"
        result = subprocess.run([script_path, year_filter], capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            logging.error(f"G1revenue.sh failed with return code {result.returncode}: {result.stderr}")
            raise ValueError(f"Error in G1revenue.sh: {result.stderr}")
        
        # Parse JSON output from G1revenue.sh
        try:
            revenue_data = json.loads(result.stdout.strip())
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse G1revenue.sh output: {e}")
            logging.error(f"Raw output: {result.stdout[:500]}")
            raise ValueError(f"Invalid JSON from G1revenue.sh: {e}")
        
        # Check for errors in the response
        if "error" in revenue_data:
            logging.error(f"G1revenue.sh returned error: {revenue_data['error']}")
            raise HTTPException(status_code=500, detail=revenue_data['error'])
        
        # If html parameter is provided, return HTML page
        if html is not None:
            g1pub = revenue_data.get("g1pub", "N/A")
            return generate_revenue_html_page(request, g1pub, revenue_data)
        
        # Otherwise return JSON
        return revenue_data
        
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Revenue history retrieval timeout")
    except Exception as e:
        logging.error(f"Error checking revenue history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/check_zencard")
async def check_zencard_route(request: Request, email: str, html: Optional[str] = None):
    """Check ZEN Card social shares history for a given email
    
    Args:
        email: Email of the ZEN Card holder (required)
        html: If present, return HTML page instead of JSON
    """
    try:
        # Validate email parameter
        if not email:
            raise HTTPException(status_code=400, detail="Email parameter is required")
        
        # Call G1zencard_history.sh to get filtered and calculated ZEN Card data
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/G1zencard_history.sh")
        result = subprocess.run([script_path, email, "true"], capture_output=True, text=True, timeout=60)
        
        # Parse JSON output from G1zencard_history.sh (script always outputs JSON, even on error)
        try:
            zencard_data = json.loads(result.stdout.strip())
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse G1zencard_history.sh output: {e}")
            logging.error(f"Return code: {result.returncode}, stderr: {result.stderr}")
            logging.error(f"Raw stdout: {result.stdout[:500]}")
            raise ValueError(f"Invalid JSON from G1zencard_history.sh: {e}")
        
        # Check for errors in the response
        if "error" in zencard_data:
            error_msg = zencard_data.get('error', 'Unknown error')
            logging.warning(f"G1zencard_history.sh returned error: {error_msg}")
            # Return 404 for "not found" errors, 500 for other errors
            status_code = 404 if "not found" in error_msg.lower() or "not configured" in error_msg.lower() else 500
            raise HTTPException(status_code=status_code, detail=error_msg)
        
        # If html parameter is provided, return HTML page
        if html is not None:
            return generate_zencard_html_page(request, email, zencard_data)
        
        # Otherwise return JSON
        return zencard_data
        
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="ZEN Card history retrieval timeout")
    except Exception as e:
        logging.error(f"Error checking ZEN Card history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/check_impots")
async def check_impots_route(request: Request, html: Optional[str] = None):
    """Check tax provisions history (TVA + IS)
    
    Args:
        html: If present, return HTML page instead of JSON
    """
    try:
        # Call G1impots.sh to get tax provisions data
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/G1impots.sh")
        
        if not os.path.exists(script_path):
            logging.error(f"G1impots.sh script not found at: {script_path}")
            raise HTTPException(status_code=500, detail="G1impots.sh script not found")
        
        result = subprocess.run([script_path], capture_output=True, text=True, timeout=60)
        
        # Log stderr if present (for debugging)
        if result.stderr:
            logging.warning(f"G1impots.sh stderr: {result.stderr}")
        
        if result.returncode != 0:
            logging.error(f"G1impots.sh failed with return code {result.returncode}")
            logging.error(f"stdout: {result.stdout[:500]}")
            logging.error(f"stderr: {result.stderr[:500]}")
            raise ValueError(f"Error in G1impots.sh: return code {result.returncode}")
        
        # Check if output is empty
        if not result.stdout or not result.stdout.strip():
            logging.error("G1impots.sh returned empty output")
            # Return default empty structure
            impots_data = {
                "wallet": "N/A",
                "total_provisions_g1": 0,
                "total_provisions_zen": 0,
                "total_transactions": 0,
                "breakdown": {
                    "tva": {"total_g1": 0, "total_zen": 0, "transactions": 0, "description": "TVA collectée sur locations ZENCOIN (20%)"},
                    "is": {"total_g1": 0, "total_zen": 0, "transactions": 0, "description": "Impôt sur les Sociétés provisionné (15% ou 25%)"}
                },
                "provisions": []
            }
        else:
            # Parse JSON output from G1impots.sh
            try:
                impots_data = json.loads(result.stdout.strip())
            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse G1impots.sh output: {e}")
                logging.error(f"Raw output: {result.stdout[:500]}")
                logging.error(f"Raw stderr: {result.stderr[:500]}")
                raise ValueError(f"Invalid JSON from G1impots.sh: {e}")
        
        # If html parameter is provided, return HTML page
        if html is not None:
            return generate_impots_html_page(request, impots_data)
        
        # Otherwise return JSON
        return impots_data
        
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Tax provisions retrieval timeout")
    except Exception as e:
        logging.error(f"Error in check_impots_route: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

async def send_server_side_analytics(analytics_data: Dict[str, Any], request: Request) -> None:
    """Send analytics data server-side (for clients without JavaScript)
    
    This function sends analytics to the /ping endpoint asynchronously
    without blocking the main request.
    
    Args:
        analytics_data: Analytics data dictionary
        request: FastAPI Request object for context
    """
    try:
        # Add server-side context
        analytics_data.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        analytics_data.setdefault("source", "server")
        analytics_data.setdefault("current_url", str(request.url))
        analytics_data.setdefault("referer", request.headers.get("referer", ""))
        analytics_data.setdefault("user_agent", request.headers.get("user-agent", ""))
        
        # Get client IP
        client_ip = get_client_ip(request)
        if client_ip:
            analytics_data["client_ip"] = client_ip
        
        # Send to /ping endpoint asynchronously (non-blocking)
        import httpx
        base_url = str(request.base_url).rstrip('/')
        ping_url = f"{base_url}/ping"
        
        # Use asyncio.create_task to send in background (non-blocking)
        async def send_ping():
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.post(ping_url, json=analytics_data)
                    logging.debug(f"📊 Server-side analytics sent: {analytics_data.get('type', 'unknown')}")
            except Exception as e:
                logging.debug(f"Analytics ping failed (non-blocking): {e}")
        
        # Fire and forget - don't await
        asyncio.create_task(send_ping())
        
    except Exception as e:
        # Never block on analytics errors
        logging.debug(f"Server-side analytics error (non-blocking): {e}")

@app.get("/theater", response_class=HTMLResponse)
async def theater_modal_route(request: Request, video: Optional[str] = None):
    """Theater mode modal for immersive video viewing
    
    Args:
        video: Optional NOSTR event ID to load a specific video directly
    """
    # Allow local JS files for development (set to True to test modifications before IPNS publish)
    use_local_js = True  # Change to False for IPNS source
    
    # Fetch video metadata for Open Graph/Twitter Cards if video ID is provided
    video_metadata = None
    video_title = "Unknown"
    video_author = None
    video_kind = None
    video_duration = 0
    video_channel = ""
    video_source_type = ""
    
    if video:
        try:
            video_event = await fetch_video_event_from_nostr(video, timeout=5)
            if video_event:
                video_metadata = await parse_video_metadata(video_event)
                video_title = video_metadata.get('title', 'Unknown')
                video_author = video_metadata.get('author_id', '')
                video_kind = video_metadata.get('kind', 21)
                logging.info(f"✅ Video metadata loaded for Open Graph: {video_title}")
                
                # Extract additional metadata from event tags if available
                tags = video_event.get("tags", [])
                for tag in tags:
                    if isinstance(tag, list) and len(tag) >= 2:
                        if tag[0] == "t" and tag[1] not in ["analytics", "encrypted", "ipfs"]:
                            video_channel = tag[1]
                        elif tag[0] == "source":
                            video_source_type = tag[1]
        except Exception as e:
            logging.warning(f"⚠️ Could not fetch video metadata for Open Graph: {e}")
            # Continue without metadata - page will still work
    
    # Send server-side analytics for clients without JavaScript
    # This tracks the page view even if JavaScript is disabled
    analytics_data = {
        "type": "theater_page_view",
        "video_event_id": video or "",
        "video_title": video_title,
        "video_author": video_author or "",
        "video_kind": video_kind or 21,
        "video_duration": video_duration,
        "video_channel": video_channel,
        "video_source_type": video_source_type,
        "has_javascript": True  # Will be overridden by client-side if JS is enabled
    }
    await send_server_side_analytics(analytics_data, request)
    
    # Get base URL for Open Graph tags
    base_url = str(request.base_url).rstrip('/')
    theater_url = f"{base_url}/theater"
    if video:
        theater_url = f"{theater_url}?video={video}"
    
    return templates.TemplateResponse("theater-modal.html", {
        "request": request,
        "myIPFS": get_myipfs_gateway() if not use_local_js else "",
        "use_local_js": use_local_js,
        "video_id": video,  # Pass video ID to template
        "video_metadata": video_metadata,  # Pass metadata for Open Graph/Twitter Cards
        "theater_url": theater_url  # Full URL for Open Graph og:url
    })

@app.get("/mp3-modal", response_class=HTMLResponse)
async def mp3_modal_route(request: Request, track: Optional[str] = None):
    """MP3 modal for immersive music listening with comments and related tracks
    
    Args:
        track: Optional NOSTR event ID to load a specific track directly
    """
    # Allow local JS files for development (set to True to test modifications before IPNS publish)
    use_local_js = True  # Change to False for production
    
    return templates.TemplateResponse("mp3-modal.html", {
        "request": request,
        "myIPFS": get_myipfs_gateway() if not use_local_js else "",
        "use_local_js": use_local_js,
        "track_id": track  # Pass track ID to template
    })

@app.get("/chat", response_class=HTMLResponse)
async def chat_route(request: Request, room: Optional[str] = None):
    """UMAP Chat Room - Real-time messaging for geographic locations
    
    Args:
        room: UMAP coordinates in format "lat,lon" (e.g., "48.86,2.35")
              If not provided, will try to fetch from user's GPS or default to "0.00,0.00"
    """
    try:
        myipfs_gateway = get_myipfs_gateway()
        
        # Parse room coordinates and get UMAP hex
        umap_hex = None
        umap_lat = 0.00
        umap_lon = 0.00
        
        if room:
            try:
                # Parse coordinates from room parameter
                coords = room.split(',')
                if len(coords) == 2:
                    umap_lat = float(coords[0].strip())
                    umap_lon = float(coords[1].strip())
                    
                    # Round to 2 decimals (UMAP precision)
                    umap_lat = round(umap_lat * 100) / 100
                    umap_lon = round(umap_lon * 100) / 100
                    
                    # Get UMAP, SECTOR, and REGION hex keys using get_umap_geolinks
                    logging.info(f"🔍 Fetching UMAP/SECTOR/REGION hex keys for coordinates: {umap_lat}, {umap_lon}")
                    geolinks_result = await get_umap_geolinks(umap_lat, umap_lon)
                    
                    # Extract hex keys for each distance level
                    umap_hex = None
                    sector_hex = None
                    region_hex = None
                    
                    if geolinks_result.get('success'):
                        # UMAP level (0.01° precision)
                        if geolinks_result.get('umaps'):
                            umap_hex = geolinks_result['umaps'].get('here')
                            if umap_hex:
                                logging.info(f"✅ Found UMAP hex: {umap_hex[:16]}...")
                        
                        # SECTOR level (0.1° precision)
                        if geolinks_result.get('sectors'):
                            sector_hex = geolinks_result['sectors'].get('here')
                            if sector_hex:
                                logging.info(f"✅ Found SECTOR hex: {sector_hex[:16]}...")
                        
                        # REGION level (1.0° precision)
                        if geolinks_result.get('regions'):
                            region_hex = geolinks_result['regions'].get('here')
                            if region_hex:
                                logging.info(f"✅ Found REGION hex: {region_hex[:16]}...")
                    else:
                        logging.warning(f"⚠️ Failed to get UMAP geolinks: {geolinks_result.get('message', 'Unknown error')}")
                else:
                    logging.warning(f"⚠️ Invalid room format: {room}, expected 'lat,lon'")
            except (ValueError, TypeError) as e:
                logging.warning(f"⚠️ Error parsing room coordinates '{room}': {e}")
        else:
            # Default to 0.00,0.00 if no room provided
            umap_lat = 0.00
            umap_lon = 0.00
            logging.info(f"📍 No room parameter, using default coordinates: {umap_lat}, {umap_lon}")
            
            # Get UMAP, SECTOR, and REGION hex keys for default coordinates
            geolinks_result = await get_umap_geolinks(umap_lat, umap_lon)
            
            # Extract hex keys for each distance level
            umap_hex = None
            sector_hex = None
            region_hex = None
            
            if geolinks_result.get('success'):
                # UMAP level (0.01° precision)
                if geolinks_result.get('umaps'):
                    umap_hex = geolinks_result['umaps'].get('here')
                    if umap_hex:
                        logging.info(f"✅ Found default UMAP hex: {umap_hex[:16]}...")
                
                # SECTOR level (0.1° precision)
                if geolinks_result.get('sectors'):
                    sector_hex = geolinks_result['sectors'].get('here')
                    if sector_hex:
                        logging.info(f"✅ Found default SECTOR hex: {sector_hex[:16]}...")
                
                # REGION level (1.0° precision)
                if geolinks_result.get('regions'):
                    region_hex = geolinks_result['regions'].get('here')
                    if region_hex:
                        logging.info(f"✅ Found default REGION hex: {region_hex[:16]}...")
        
        return templates.TemplateResponse("chat.html", {
            "request": request,
            "myIPFS": myipfs_gateway,
            "room": room,  # Pass room parameter to template
            "umap_lat": umap_lat,  # Pass parsed latitude
            "umap_lon": umap_lon,  # Pass parsed longitude
            "umap_hex": umap_hex,  # Pass UMAP hex (0.01° precision)
            "sector_hex": sector_hex,  # Pass SECTOR hex (0.1° precision)
            "region_hex": region_hex  # Pass REGION hex (1.0° precision)
        })
    except Exception as e:
        logging.error(f"Error serving chat page: {e}")
        raise HTTPException(status_code=500, detail=f"Error loading chat page: {str(e)}")

@app.get("/playlist", response_class=HTMLResponse)
async def playlist_manager_route(request: Request, id: Optional[str] = None):
    """Playlist manager for creating and managing video playlists
    
    Args:
        id: Optional playlist ID to open a specific playlist directly
    """
    return templates.TemplateResponse("playlist-manager.html", {
        "request": request,
        "myIPFS": get_myipfs_gateway(),
        "playlist_id": id
    })

@app.get("/video")
async def video_route(request: Request):
    """Redirect to /youtube?html=1"""
    return RedirectResponse(url="/youtube?html=1", status_code=302)

@app.get("/audio")
async def audio_route(request: Request):
    """Redirect to /mp3?html=1"""
    return RedirectResponse(url="/mp3?html=1", status_code=302)

@app.get("/tags", response_class=HTMLResponse)
async def tags_route(request: Request, video: Optional[str] = None):
    """Tags management page for NOSTR videos
    
    Args:
        video: Optional video event ID to display tags for a specific video
    """
    # Calculate IPFS gateway from request hostname
    hostname = request.headers.get("host", "u.copylaradio.com")
    if hostname.startswith("u."):
        ipfs_gateway = f"https://ipfs.{hostname[2:]}"
    elif hostname.startswith("127.0.0.1") or hostname.startswith("localhost"):
        ipfs_gateway = "http://127.0.0.1:8080"
    else:
        ipfs_gateway = "https://ipfs.copylaradio.com"
    
    return templates.TemplateResponse("tags.html", {
        "request": request,
        "myIPFS": ipfs_gateway,
        "video_id": video
    })

@app.get("/contrib", response_class=HTMLResponse)
async def contrib_route(request: Request, video: Optional[str] = None, kind: Optional[str] = None):
    """TMDB metadata enrichment contribution page for NOSTR videos
    
    Args:
        video: Video event ID to contribute metadata for
        kind: Video kind (21 for regular videos, 22 for shorts)
    """
    # Calculate IPFS gateway from request hostname
    hostname = request.headers.get("host", "u.copylaradio.com")
    if hostname.startswith("u."):
        ipfs_gateway = f"https://ipfs.{hostname[2:]}"
    elif hostname.startswith("127.0.0.1") or hostname.startswith("localhost"):
        ipfs_gateway = "http://127.0.0.1:8080"
    else:
        ipfs_gateway = "https://ipfs.copylaradio.com"
    
    return templates.TemplateResponse("contrib.html", {
        "request": request,
        "myIPFS": ipfs_gateway,
        "video_id": video,
        "video_kind": kind or "21"
    })

@app.get("/cloud", response_class=HTMLResponse)
async def cloud_route(request: Request):
    """Cloud Drive - Professional file management interface"""
    return templates.TemplateResponse("cloud.html", {
        "request": request,
        "myIPFS": get_myipfs_gateway()
    })

@app.get("/youtube")
async def youtube_route(
    request: Request, 
    html: Optional[str] = None, 
    channel: Optional[str] = None, 
    search: Optional[str] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    duration_min: Optional[int] = None,
    duration_max: Optional[int] = None,
    sort_by: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius: Optional[float] = None,
    video: Optional[str] = None
):
    """YouTube video channels and search from NOSTR events
    
    Args:
        html: If present Pinpoint HTML page instead of JSON
        channel: Filter by specific channel name
        search: Search in video titles and descriptions
        keyword: Search by specific keywords (comma-separated)
        date_from: Filter videos from this date (YYYY-MM-DD)
        date_to: Filter videos to this date (YYYY-MM-DD)
        duration_min: Minimum duration in seconds
        duration_max: Maximum duration in seconds
        sort_by: Sort by 'date', 'duration', 'title', 'channel'
        lat: Latitude for geographic filtering (decimal degrees)
        lon: Longitude for geographic filtering (decimal degrees)
        radius: Radius in kilometers for geographic filtering (default: 2.0km if lat/lon provided)
    """
    # Allow local JS files for development (set to True to test modifications before IPNS publish)
    use_local_js = True  # Change to False for IPNS source
    
    try:
        # Import the video channel functions
        import sys
        sys.path.append(os.path.expanduser("~/.zen/Astroport.ONE/IA"))
        from create_video_channel import fetch_and_process_nostr_events, create_channel_playlist
        
        # Fetch NOSTR events with timeout to prevent hanging
        try:
            video_messages = await asyncio.wait_for(
                fetch_and_process_nostr_events("ws://127.0.0.1:7777", 200),
                timeout=15.0  # 15 second timeout
            )
            # Extract relay statistics from first video if available
            relay_stats = {}
            if video_messages and video_messages[0].get('_relay_statistics'):
                relay_stats = video_messages[0].get('_relay_statistics', {})
                logging.info(f"✅ Fetched {len(video_messages)} video events from NOSTR")
                logging.info(f"📊 Relay statistics: {relay_stats.get('total_videos_count', 0)} videos, {relay_stats.get('total_size_formatted', 'N/A')}, {relay_stats.get('total_duration_formatted', 'N/A')}")
            else:
                logging.info(f"✅ Fetched {len(video_messages)} video events from NOSTR")
        except asyncio.TimeoutError:
            logging.warning("⚠️ Timeout fetching NOSTR events, using empty list")
            video_messages = []
        except Exception as fetch_error:
            logging.error(f"❌ Error fetching NOSTR events: {fetch_error}")
            video_messages = []
        
        # Validate and normalize video data
        validated_videos = []
        skipped_count = 0
        for video in video_messages:
            # Ensure required fields exist
            if not video.get('title') or not video.get('ipfs_url'):
                skipped_count += 1
                logging.debug(f"⚠️ Skipping video (missing title or IPFS URL): {video.get('title', 'N/A')[:30]}...")
                continue
            
            # Normalize field names for consistency
            # IPFS URLs are kept as CID pur for client-side gateway detection
            
            # Get info_cid and use it for metadata_ipfs if metadata_ipfs is empty
            info_cid = video.get('info_cid', '')
            metadata_ipfs = video.get('metadata_ipfs', '') or info_cid  # Use info_cid as fallback
            
            normalized_video = {
                'title': video.get('title', ''),
                'uploader': video.get('uploader', ''),
                'content': video.get('content', ''),  # Comment/description from NOSTR event (NIP-71)
                'duration': int(video.get('duration', 0)) if str(video.get('duration', 0)).isdigit() else 0,
                'ipfs_url': video.get('ipfs_url', ''),
                'youtube_url': video.get('youtube_url', '') or video.get('original_url', ''),
                'thumbnail_ipfs': video.get('thumbnail_ipfs', ''),
                'gifanim_ipfs': video.get('gifanim_ipfs', ''),  # Animated GIF CID from upload2ipfs.sh
                'metadata_ipfs': metadata_ipfs,
                'subtitles': video.get('subtitles', []),
                'channel_name': video.get('channel_name', ''),
                'topic_keywords': video.get('topic_keywords', ''),
                'created_at': video.get('created_at', ''),
                'download_date': video.get('download_date', '') or video.get('created_at', ''),
                'file_size': int(video.get('file_size', 0)) if str(video.get('file_size', 0)).isdigit() else 0,
                'message_id': video.get('message_id', ''),
                'author_id': video.get('author_id', ''),
                'latitude': video.get('latitude'),  # GPS coordinates
                'longitude': video.get('longitude'),  # GPS coordinates
                # Provenance and compliance information
                'provenance': video.get('provenance', 'unknown'),  # Provenance: youtube_download, video_channel, webcam, etc.
                'source_type': video.get('source_type', 'webcam'),  # Source type: youtube, webcam, film, serie
                'compliance': video.get('compliance', {}),  # Compliance check with UPlanet_FILE_CONTRACT.md
                'compliance_score': video.get('compliance_score', 0),  # Number of compliant fields
                'compliance_percent': video.get('compliance_percent', 0),  # Percentage of compliance
                'compliance_level': video.get('compliance_level', 'non-compliant'),  # Compliance level: compliant, partial, non-compliant
                'is_compliant': video.get('is_compliant', False),  # Boolean indicating if video is compliant (>=80%)
                'file_hash': video.get('file_hash', ''),  # File hash for provenance tracking
                'info_cid': info_cid,  # Info.json CID
                'upload_chain': video.get('upload_chain', ''),  # Upload chain for provenance
                'upload_chain_list': video.get('upload_chain_list', [])  # Parsed upload chain list
            }
            validated_videos.append(normalized_video)
        
        if skipped_count > 0:
            logging.info(f"⚠️ Skipped {skipped_count} invalid video(s)")
        logging.info(f"✅ Validated {len(validated_videos)} video(s)")
        
        video_messages = validated_videos
        
        # Apply filters
        filtered_videos = []
        
        for video in video_messages:
            # Filter by channel if specified
            if channel and video.get('channel_name', '').lower() != channel.lower():
                continue
            
            # Filter by search term if specified
            if search:
                search_lower = search.lower()
                if not (search_lower in video.get('title', '').lower() or 
                       search_lower in video.get('topic_keywords', '').lower()):
                    continue
            
            # Filter by keywords if specified
            if keyword:
                keywords = [k.strip().lower() for k in keyword.split(',')]
                video_keywords = video.get('topic_keywords', '').lower()
                if not any(k in video_keywords for k in keywords):
                    continue
            
            # Filter by date range if specified
            if date_from or date_to:
                video_date = video.get('created_at', '')
                if video_date:
                    try:
                        from datetime import datetime as dt
                        video_datetime = dt.fromisoformat(video_date.replace('Z', '+00:00'))
                        video_date_str = video_datetime.strftime('%Y-%m-%d')
                        
                        if date_from and video_date_str < date_from:
                            continue
                        if date_to and video_date_str > date_to:
                            continue
                    except:
                        continue  # Skip videos with invalid dates
            
            # Filter by duration if specified
            if duration_min is not None or duration_max is not None:
                video_duration = video.get('duration', 0)
                if isinstance(video_duration, str):
                    try:
                        video_duration = int(video_duration)
                    except:
                        video_duration = 0
                
                if duration_min is not None and video_duration < duration_min:
                    continue
                if duration_max is not None and video_duration > duration_max:
                    continue
            
            # Filter by geographic location if specified
            if lat is not None and lon is not None:
                video_lat = video.get('latitude')
                video_lon = video.get('longitude')
                
                # Skip videos without location data (but include videos with 0.00, 0.00 if explicitly at that location)
                if video_lat is None or video_lon is None:
                    continue
                
                # Calculate distance using Haversine formula
                from math import radians, sin, cos, sqrt, atan2
                
                def haversine_distance(lat1, lon1, lat2, lon2):
                    """Calculate distance between two points in kilometers using Haversine formula"""
                    R = 6371  # Earth radius in kilometers
                    lat1_rad = radians(lat1)
                    lat2_rad = radians(lat2)
                    delta_lat = radians(lat2 - lat1)
                    delta_lon = radians(lon2 - lon1)
                    
                    a = sin(delta_lat/2)**2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon/2)**2
                    c = 2 * atan2(sqrt(a), sqrt(1-a))
                    
                    return R * c
                
                distance = haversine_distance(lat, lon, video_lat, video_lon)
                filter_radius = radius if radius is not None else 2.0  # Default 2km radius
                
                if distance > filter_radius:
                    continue
            
            filtered_videos.append(video)
        
        logging.info(f"✅ After filtering: {len(filtered_videos)} video(s) (from {len(video_messages)} total)")
        if len(filtered_videos) == 0 and len(video_messages) > 0:
            logging.warning(f"⚠️ All {len(video_messages)} videos were filtered out. Available channels: {set(v.get('channel_name', 'unknown') for v in video_messages)}")
        
        video_messages = filtered_videos
        
        # Sort videos if specified
        if sort_by:
            if sort_by == 'date':
                video_messages.sort(key=lambda x: x.get('created_at', ''), reverse=True)
            elif sort_by == 'duration':
                video_messages.sort(key=lambda x: int(x.get('duration', 0)) if str(x.get('duration', 0)).isdigit() else 0, reverse=True)
            elif sort_by == 'title':
                video_messages.sort(key=lambda x: x.get('title', '').lower())
            elif sort_by == 'channel':
                video_messages.sort(key=lambda x: x.get('channel_name', '').lower())
        
        # Group videos by channel
        channels = {}
        for video in video_messages:
            channel_name = video.get('channel_name', 'unknown')
            if channel_name not in channels:
                channels[channel_name] = []
            channels[channel_name].append(video)
        
        # Log channel grouping for debugging
        for channel_name, videos in channels.items():
            logging.info(f"📺 Channel '{channel_name}': {len(videos)} video(s) before playlist creation")
            if channel_name == 'frenault_linkeo_com':
                for v in videos:
                    logging.info(f"  - {v.get('title', 'N/A')} (id: {v.get('message_id', 'N/A')[:16]}...)")
        
        # Create channel playlists
        channel_playlists = {}
        for channel_name, videos in channels.items():
            playlist = create_channel_playlist(videos, channel_name)
            channel_playlists[channel_name] = playlist
            # Log playlist creation for debugging
            playlist_videos = playlist.get('videos', [])
            logging.info(f"📺 Channel '{channel_name}': {len(playlist_videos)} video(s) after playlist creation")
            if channel_name == 'frenault_linkeo_com':
                for v in playlist_videos:
                    logging.info(f"  - {v.get('title', 'N/A')} (id: {v.get('message_id', 'N/A')[:16]}...)")
        
        # Debug: Check if target video is in playlists (only if DEBUG_VIDEO_ID env var is set)
        target_video_id = os.getenv("DEBUG_VIDEO_ID")
        
        if target_video_id:
            # First check if video exists in raw video_messages
            target_video_in_raw = None
            for video in video_messages:
                if video.get('message_id') == target_video_id:
                    target_video_in_raw = video
                    break
            
            if target_video_in_raw:
                logging.info(f"✅ DEBUG: Target video found in raw video_messages: '{target_video_in_raw.get('title', 'N/A')}'")
                logging.info(f"   - Channel: '{target_video_in_raw.get('channel_name', 'N/A')}'")
                logging.info(f"   - Has title: {bool(target_video_in_raw.get('title'))}")
                logging.info(f"   - Has ipfs_url: {bool(target_video_in_raw.get('ipfs_url'))}")
                logging.info(f"   - IPFS URL: {target_video_in_raw.get('ipfs_url', 'N/A')[:50]}...")
            else:
                logging.warning(f"⚠️ DEBUG: Target video {target_video_id[:16]}... NOT found in raw video_messages")
                logging.warning(f"   - Total videos in video_messages: {len(video_messages)}")
            
            # Check if video exists in channels dict (before playlist creation)
            target_video_in_channels = None
            target_channel_name = None
            for channel_name, videos in channels.items():
                for video in videos:
                    if video.get('message_id') == target_video_id:
                        target_video_in_channels = video
                        target_channel_name = channel_name
                        break
                if target_video_in_channels:
                    break
            
            if target_video_in_channels:
                logging.info(f"✅ DEBUG: Target video found in channels dict: channel '{target_channel_name}' with {len(channels.get(target_channel_name, []))} videos")
            else:
                logging.warning(f"⚠️ DEBUG: Target video NOT found in channels dict")
                logging.warning(f"   - Available channels: {list(channels.keys())}")
            
            # Check if video exists in playlists (after playlist creation)
            found_in_playlists = False
            for channel_name, playlist in channel_playlists.items():
                for video in playlist.get('videos', []):
                    if video.get('message_id') == target_video_id:
                        found_in_playlists = True
                        logging.info(f"✅ DEBUG: Target video '{video.get('title', 'N/A')}' found in channel '{channel_name}' playlist with {len(playlist.get('videos', []))} videos")
                        break
                if found_in_playlists:
                    break
            
            if not found_in_playlists:
                logging.warning(f"⚠️ DEBUG: Target video {target_video_id[:16]}... NOT found in any channel playlist")
                logging.warning(f"⚠️ DEBUG: Available channels: {list(channel_playlists.keys())}")
                logging.warning(f"⚠️ DEBUG: Total videos in all channels: {sum(len(p.get('videos', [])) for p in channel_playlists.values())}")
                
                # If video was in channels but not in playlists, it was filtered out by create_channel_playlist
                if target_video_in_channels:
                    logging.warning(f"⚠️ DEBUG: Video was in channels dict but filtered out during playlist creation")
                    logging.warning(f"   - Likely missing 'title' or 'ipfs_url' field")
                    logging.warning(f"   - Title: '{target_video_in_channels.get('title', 'MISSING')}'")
                    logging.warning(f"   - IPFS URL: '{target_video_in_channels.get('ipfs_url', 'MISSING')}'")
        
        # Prepare response data
        response_data = {
            "success": True,
            "total_videos": len(video_messages),
            "total_channels": len(channels),
            "channels": channel_playlists,
            "filters": {
                "channel": channel,
                "search": search,
                "keyword": keyword,
                "date_from": date_from,
                "date_to": date_to,
                "duration_min": duration_min,
                "duration_max": duration_max,
                "sort_by": sort_by,
                "lat": lat,
                "lon": lon,
                "radius": radius if radius is not None else 2.0 if lat is not None and lon is not None else None
            },
            "timestamp": datetime.now().isoformat()
        }
        
        # Return HTML page if requested
        if html is not None:
            # Calculate IPFS gateway from request hostname
            hostname = request.headers.get("host", "u.copylaradio.com")
            if hostname.startswith("u."):
                ipfs_gateway = f"https://ipfs.{hostname[2:]}"
            elif hostname.startswith("127.0.0.1") or hostname.startswith("localhost"):
                ipfs_gateway = "http://127.0.0.1:8080"
            else:
                ipfs_gateway = "https://ipfs.copylaradio.com"
            
            # If video parameter is provided, find the video and pass it to template
            auto_open_video = None
            if video:
                # Find video by message_id (event_id) in channel_playlists
                for channel_name, channel_playlist in channel_playlists.items():
                    # channel_playlist should have a 'videos' attribute
                    playlist_videos = channel_playlist.get('videos', []) if isinstance(channel_playlist, dict) else getattr(channel_playlist, 'videos', [])
                    for v in playlist_videos:
                        if v.get('message_id') == video:
                            auto_open_video = {
                                'event_id': v.get('message_id', ''),
                                'title': v.get('title', ''),
                                'ipfs_url': v.get('ipfs_url', ''),
                                'thumbnail_ipfs': v.get('thumbnail_ipfs', ''),
                                'gifanim_ipfs': v.get('gifanim_ipfs', ''),  # Animated GIF CID
                                'author_id': v.get('author_id', ''),
                                'uploader': v.get('uploader', ''),
                                'channel': v.get('channel_name', ''),
                                'duration': v.get('duration', 0),
                                'content': v.get('content', '')  # Comment/description from event
                            }
                            break
                    if auto_open_video:
                        break
                
                # If not found in playlists, search in original video_messages
                if not auto_open_video:
                    for v in video_messages:
                        if v.get('message_id') == video:
                            auto_open_video = {
                                'event_id': v.get('message_id', ''),
                                'title': v.get('title', ''),
                                'ipfs_url': v.get('ipfs_url', ''),
                                'thumbnail_ipfs': v.get('thumbnail_ipfs', ''),
                                'gifanim_ipfs': v.get('gifanim_ipfs', ''),  # Animated GIF CID
                                'author_id': v.get('author_id', ''),
                                'uploader': v.get('uploader', ''),
                                'channel': v.get('channel_name', ''),
                                'duration': v.get('duration', 0),
                                'content': v.get('content', '')  # Comment/description from event
                            }
                            break
            
            # Extract user pubkey from request if available (for delete button display)
            # Try to get from NIP-98 auth header (same method as in /api/fileupload)
            user_pubkey = None
            try:
                # Check for NIP-98 Authorization header
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Nostr "):
                    try:
                        # Decode the base64-encoded NIP-98 event
                        token = auth_header.replace("Nostr ", "")
                        decoded = base64.b64decode(token)
                        auth_event = json.loads(decoded)
                        
                        # Verify it's a valid NIP-98 auth event (kind 27235)
                        if auth_event.get("kind") == 27235:
                            user_pubkey = auth_event.get("pubkey")
                            logging.info(f"🔑 NIP-98 Auth: User pubkey extracted for delete button: {user_pubkey[:16] if user_pubkey else 'N/A'}...")
                    except Exception as e:
                        logging.debug(f"Could not extract pubkey from NIP-98 header: {e}")
            except Exception as e:
                logging.debug(f"Error extracting user pubkey: {e}")
            
            # Send server-side analytics for clients without JavaScript
            # This tracks the page view even if JavaScript is disabled
            analytics_data = {
                "type": "youtube_page_view",
                "video_event_id": video or "",
                "total_videos": len(video_messages),
                "total_channels": len(channels),
                "channel_filter": channel or "",
                "search_filter": search or "",
                "keyword_filter": keyword or "",
                "date_from": date_from or "",
                "date_to": date_to or "",
                "duration_min": duration_min,
                "duration_max": duration_max,
                "sort_by": sort_by or "",
                "has_location_filter": lat is not None and lon is not None,
                "has_javascript": True  # Will be overridden by client-side if JS is enabled
            }
            await send_server_side_analytics(analytics_data, request)
            
            return templates.TemplateResponse("youtube.html", {
                "request": request,
                "youtube_data": response_data,
                "myIPFS": ipfs_gateway,
                "auto_open_video": auto_open_video,
                "user_pubkey": user_pubkey,  # Pass user pubkey to template for delete button
                "use_local_js": use_local_js  # Allow local JS files for development
            })
        
        # Send server-side analytics for JSON requests
        analytics_data = {
            "type": "youtube_api_view",
            "video_event_id": video or "",
            "total_videos": len(video_messages),
            "total_channels": len(channels),
            "channel_filter": channel or "",
            "search_filter": search or "",
            "keyword_filter": keyword or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
            "duration_min": duration_min,
            "duration_max": duration_max,
            "sort_by": sort_by or "",
            "has_location_filter": lat is not None and lon is not None,
            "has_javascript": True  # Will be overridden by client-side if JS is enabled
        }
        await send_server_side_analytics(analytics_data, request)
        
        # Return JSON response
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logging.error(f"Error in youtube_route: {e}", exc_info=True)
        if html is not None:
            return HTMLResponse(content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", status_code=500)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/mp3")
async def mp3_route(
    request: Request,
    html: Optional[str] = None,
    search: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    sort_by: Optional[str] = None,
    limit: Optional[int] = 100
):
    """MP3 music library from NOSTR events (kind 1063 - NIP-94)
    
    Args:
        html: If present, return HTML page instead of JSON
        search: Search in track titles, artists, and albums
        artist: Filter by artist name
        album: Filter by album name
        sort_by: Sort by 'date', 'title', 'artist', 'album'
        limit: Limit number of results (default: 100)
    """
    try:
        import json
        from pathlib import Path
        
        # Get IPFS gateway
        ipfs_gateway = get_myipfs_gateway()
        
        # Path to nostr_get_events.sh
        nostr_script_path = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "nostr_get_events.sh"
        
        if not nostr_script_path.exists():
            logging.error(f"nostr_get_events.sh not found at {nostr_script_path}")
            if html is not None:
                return HTMLResponse(
                    content=f"<html><body><h1>Error</h1><p>nostr_get_events.sh not found</p></body></html>",
                    status_code=500
                )
            raise HTTPException(status_code=500, detail="nostr_get_events.sh not found")
        
        # Build command to fetch MP3 events (kind 1063)
        cmd = [
            str(nostr_script_path),
            "--kind", "1063",
            "--limit", str(limit or 100),
            "--output", "json"
        ]
        
        logging.info(f"Fetching MP3 events with command: {' '.join(cmd)}")
        
        # Execute script with timeout
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(nostr_script_path.parent)
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=15.0
            )
            
            if process.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='ignore') if stderr else "Unknown error"
                logging.warning(f"nostr_get_events.sh returned non-zero exit code: {error_msg}")
                mp3_events_raw = ""
            else:
                mp3_events_raw = stdout.decode('utf-8', errors='ignore')
                
        except asyncio.TimeoutError:
            logging.warning("Timeout fetching MP3 events from NOSTR")
            mp3_events_raw = ""
        except Exception as fetch_error:
            logging.error(f"Error executing nostr_get_events.sh: {fetch_error}")
            mp3_events_raw = ""
        
        # Parse MP3 events
        mp3_tracks = []
        
        if mp3_events_raw:
            # Parse JSON events (one per line)
            for line in mp3_events_raw.strip().split('\n'):
                if not line.strip():
                    continue
                    
                try:
                    event = json.loads(line)
                    
                    # Extract MP3 metadata from NIP-94 event (kind 1063)
                    # Tags are arrays, get first value for each tag type
                    tags = {}
                    for tag in event.get('tags', []):
                        if len(tag) > 0:
                            tag_name = tag[0]
                            # For tags with multiple values, take the first one
                            tag_value = tag[1] if len(tag) > 1 else None
                            # Some tags might have multiple entries, keep the first one
                            if tag_name not in tags:
                                tags[tag_name] = tag_value
                    
                    # Get MIME type - only process audio files
                    mime_type = tags.get('m', '').lower()
                    if not mime_type or 'audio' not in mime_type:
                        continue
                    
                    # Extract URL
                    url = tags.get('url', '')
                    if not url:
                        continue
                    
                    # Convert IPFS URL if needed
                    if url.startswith('ipfs://'):
                        url = url.replace('ipfs://', f'{ipfs_gateway}/ipfs/')
                    elif url.startswith('/ipfs/'):
                        url = f'{ipfs_gateway}{url}'
                    elif not url.startswith('http'):
                        # Assume it's a CID
                        url = f'{ipfs_gateway}/ipfs/{url}'
                    
                    # Extract metadata
                    title_tag = tags.get('title')
                    title = title_tag if title_tag else event.get('content', '').strip() or 'Unknown Title'
                    
                    # Try to get artist from 'artist' tag, 'p' tag, or author pubkey
                    artist_name = tags.get('artist')
                    if not artist_name:
                        # Try to get from 'p' tag (mentioned pubkey)
                        p_tag = tags.get('p')
                        if p_tag:
                            artist_name = f"Artist ({p_tag[:8]}...)"
                        else:
                            # Use author pubkey
                            author_hex = event.get('pubkey', '')
                            artist_name = f"Artist ({author_hex[:8]}...)" if author_hex else "Unknown Artist"
                    
                    album_name = tags.get('album', '—')
                    
                    # Get thumbnail/image
                    thumbnail = tags.get('thumb') or tags.get('image')
                    if thumbnail:
                        if thumbnail.startswith('ipfs://'):
                            thumbnail = thumbnail.replace('ipfs://', f'{ipfs_gateway}/ipfs/')
                        elif thumbnail.startswith('/ipfs/'):
                            thumbnail = f'{ipfs_gateway}{thumbnail}'
                        elif not thumbnail.startswith('http'):
                            thumbnail = f'{ipfs_gateway}/ipfs/{thumbnail}'
                    
                    # Get duration (if available in tags or metadata)
                    duration = None
                    duration_tag = tags.get('duration')
                    if duration_tag:
                        try:
                            duration = float(duration_tag)
                        except:
                            pass
                    
                    # Get size
                    size = None
                    size_tag = tags.get('size')
                    if size_tag:
                        try:
                            size = int(size_tag)
                        except:
                            pass
                    
                    # Get hash for provenance
                    file_hash = tags.get('x', '')
                    
                    # Get source type from 'i' tag
                    source_type = None
                    for tag in event.get('tags', []):
                        if tag[0] == 'i' and len(tag) > 1 and tag[1].startswith('source:'):
                            source_type = tag[1].replace('source:', '')
                            break
                    
                    # Get summary/description
                    summary = tags.get('summary', '')
                    description = summary if summary else event.get('content', '').strip()
                    
                    # Create track object
                    track = {
                        'event_id': event.get('id', ''),
                        'author_id': event.get('pubkey', ''),
                        'title': title,
                        'artist': artist_name,
                        'album': album_name,
                        'url': url,
                        'thumbnail': thumbnail,
                        'description': description,
                        'duration': duration,
                        'size': size,
                        'hash': file_hash,
                        'mime_type': mime_type,
                        'source_type': source_type,
                        'created_at': event.get('created_at', 0),
                        'date': datetime.fromtimestamp(event.get('created_at', 0)).isoformat() if event.get('created_at') else None
                    }
                    
                    mp3_tracks.append(track)
                    
                except json.JSONDecodeError as e:
                    logging.warning(f"Error parsing JSON event: {e}")
                    continue
                except Exception as e:
                    logging.warning(f"Error processing MP3 event: {e}")
                    continue
        
        logging.info(f"✅ Parsed {len(mp3_tracks)} MP3 tracks from NOSTR")
        
        # Apply filters
        filtered_tracks = []
        
        for track in mp3_tracks:
            # Filter by search term
            if search:
                search_lower = search.lower()
                if not (search_lower in track.get('title', '').lower() or
                      search_lower in track.get('artist', '').lower() or
                      search_lower in track.get('album', '').lower() or
                      search_lower in track.get('description', '').lower()):
                    continue
            
            # Filter by artist
            if artist:
                if artist.lower() not in track.get('artist', '').lower():
                    continue
            
            # Filter by album
            if album:
                if album.lower() not in track.get('album', '').lower():
                    continue
            
            filtered_tracks.append(track)
        
        logging.info(f"✅ After filtering: {len(filtered_tracks)} track(s) (from {len(mp3_tracks)} total)")
        
        # Sort tracks if specified
        if sort_by:
            if sort_by == 'date':
                filtered_tracks.sort(key=lambda x: x.get('created_at', 0), reverse=True)
            elif sort_by == 'title':
                filtered_tracks.sort(key=lambda x: x.get('title', '').lower())
            elif sort_by == 'artist':
                filtered_tracks.sort(key=lambda x: x.get('artist', '').lower())
            elif sort_by == 'album':
                filtered_tracks.sort(key=lambda x: x.get('album', '').lower())
        else:
            # Default: sort by date (newest first)
            filtered_tracks.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        
        # Prepare response data
        response_data = {
            'tracks': filtered_tracks,
            'total_tracks': len(filtered_tracks),
            'total_all': len(mp3_tracks),
            'filters': {
                'search': search,
                'artist': artist,
                'album': album,
                'sort_by': sort_by,
                'limit': limit
            }
        }
        
        # Return HTML template if requested
        if html is not None:
            # Allow local JS files for development (set to True to test modifications before IPNS publish)
            use_local_js = True  # Change to False for production
            
            return templates.TemplateResponse("mp3.html", {
                "request": request,
                "mp3_data": response_data,
                "myIPFS": ipfs_gateway if not use_local_js else "",
                "use_local_js": use_local_js
            })
        
        # Return JSON response
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logging.error(f"Error in mp3_route: {e}", exc_info=True)
        if html is not None:
            return HTMLResponse(
                content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>",
                status_code=500
            )
        raise HTTPException(status_code=500, detail=str(e))


####################### MULTI SCAN 
@app.get("/scan")
async def get_root(request: Request):
    return templates.TemplateResponse("scan_new.html", {"request": request})

## DECODER email, G1PUB, SSSS:nodeid ... url ...
@app.post("/upassport")
async def scan_qr(
    parametre: str = Form(...),
    imageData: str = Form(None),
    zlat: str = Form(None),
    zlon: str = Form(None)
):
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
            return JSONResponse({"error": error_message}, status_code=404)
            
        # Vérifier si c'est bien un fichier HTML
        if not returned_file_path.endswith('.html'):
            error_message = f"Le fichier {returned_file_path} n'est pas un fichier HTML"
            logging.error(error_message)
            return JSONResponse({"error": error_message}, status_code=400)
            
        try:
            return FileResponse(
                returned_file_path,
                media_type='text/html',
                filename=os.path.basename(returned_file_path)
            )
        except Exception as e:
            error_message = f"Erreur lors de l'envoi du fichier: {str(e)}"
            logging.error(error_message)
            return JSONResponse({"error": error_message}, status_code=500)
    else:
        error_message = f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs."
        logging.error(error_message)
        return JSONResponse({"error": error_message}, status_code=500)


### NEED DEBUG
@app.get("/scan_multipass_payment.html")
async def get_scan_multipass_payment(request: Request):
    """MULTIPASS Payment Terminal - Internal route for authenticated payments between MULTIPASS wallets"""
    return templates.TemplateResponse("scan_multipass_payment.html", {"request": request})

###############################################################################
## Collect UPassport SSSS KEY and match ot with CAPTAIN parts or SWARM key copy
## Can also receive DRIVE KEY IPNS httt.../12D
##################################################./check_ssss.sh #############
@app.post("/ssss")
async def ssss(request: Request):
    form_data = await request.form()
    cardns = form_data.get("cardns")
    ssss = form_data.get("ssss")
    zerocard = form_data.get("zerocard")

    logging.info(f"Received Card NS: {cardns}")
    logging.info(f"Received SSSS key: [REDACTED - {len(ssss)} chars]")
    logging.info(f"ZEROCARD: {zerocard}")

    script_path = "./check_ssss.sh"
    return_code, last_line = await run_script(script_path, cardns, ssss, zerocard)

    if return_code == 0:
        returned_file_path = last_line.strip()
        return FileResponse(returned_file_path)
    else:
        return JSONResponse({"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs."})

@app.post("/zen_send")
async def zen_send(request: Request):
    """
    Send ZEN using the sender's ZEN card. Nostr authentication (NIP-42) is mandatory.

    Required form fields:
      - zen: amount in ZEN
      - g1dest: destination G1 pubkey; if omitted or equal to 'CAPTAIN', uses env CAPTAIN_G1PUB
      - npub: sender pubkey (npub1... or 64-hex). We verify NIP-42 and resolve the sender's
              G1 source wallet from ~/.zen/game/nostr/<email>/G1PUBNOSTR when available.

    Optional form fields:
      - g1source: suggested source G1 pubkey; will be validated against npub mapping by the shell
    """
    form_data = await request.form()
    zen = form_data.get("zen")
    g1source = form_data.get("g1source")
    g1dest = form_data.get("g1dest")
    npub = form_data.get("npub")  # mandatory; NIP-42 verification and source resolution

    logging.info(f"Zen Amount : {zen}")
    logging.info(f"Source (pre) : {g1source}")
    logging.info(f"Destination (pre) : {g1dest}")
    logging.info(f"Nostr pubkey provided: {('yes' if npub else 'no')}")

    # npub is mandatory
    if not npub or not str(npub).strip():
        raise HTTPException(status_code=400, detail="Nostr public key (npub) is required")

    # Do not resolve CAPTAIN here; let zen_send.sh handle CAPTAIN/PLAYER mapping
    if not g1dest:
        raise HTTPException(status_code=400, detail="Missing destination (g1dest)")

    sender_hex = None
    # Convert to hex if needed
    is_hex_format = len(npub) == 64 and all(c in '0123456789abcdefABCDEF' for c in npub)
    sender_hex = npub.lower() if is_hex_format else npub_to_hex(npub)
    if not sender_hex or len(sender_hex) != 64:
        logging.error("Invalid npub/hex provided to /zen_send")
        raise HTTPException(status_code=400, detail="Invalid Nostr public key format")

    # Verify NIP-42 recent auth
    auth_ok = await verify_nostr_auth(sender_hex)
    if not auth_ok:
        logging.warning("NIP-42 verification failed for /zen_send request")
        raise HTTPException(status_code=401, detail="Nostr authentication failed (NIP-42)")

    # Map Nostr pubkey to user directory and G1 source if available
    try:
        user_dir = find_user_directory_by_hex(sender_hex)
        g1pubnostr_path = user_dir / "G1PUBNOSTR"
        if g1pubnostr_path.exists():
            with open(g1pubnostr_path, 'r') as f:
                resolved_g1source = f.read().strip()
            logging.info(f"Resolved sender G1SOURCE from Nostr: {resolved_g1source[:12]}...")
            g1source = resolved_g1source
        else:
            logging.warning(f"G1PUBNOSTR not found for user {user_dir}. Will validate provided g1source in shell")
    except HTTPException as e:
        logging.error(f"Failed to locate user directory for hex {sender_hex}: {e.detail}")
    except Exception as e:
        logging.error(f"Unexpected error resolving G1 source from npub: {e}")

    # Create a short-lived auth marker for the shell script to verify
    try:
        marker_path = os.path.expanduser(f"~/.zen/tmp/nostr_auth_ok_{sender_hex}")
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, 'w') as marker:
            marker.write(str(int(time.time())))
        logging.info(f"Created Nostr auth marker: {marker_path}")
    except Exception as e:
        logging.warning(f"Failed to write Nostr auth marker: {e}")

    # Final validations
    if not zen or not g1dest:
        raise HTTPException(status_code=400, detail="Missing required fields: zen, g1dest, npub")

    # Execute payment script, passing sender_hex when available so shell can double-check mapping
    script_path = "./zen_send.sh"
    args = [zen, g1source or "", g1dest, sender_hex]

    return_code, last_line = await run_script(script_path, *args)

    try:
        # Parse the JSON response from zen_send.sh
        script_output = last_line.strip()
        if script_output.startswith('{'):
            # JSON response from zen_send.sh
            result = json.loads(script_output)
            if result.get("success"):
                # Build a short-lived game token (5 minutes) after successful payment
                session_id = uuid.uuid4().hex
                exp = int(time.time()) + 300
                token = sign_token({"npub": sender_hex, "sid": session_id, "exp": exp})
                # Initialize session server state
                COINFLIP_SESSIONS[session_id] = {
                    "npub": sender_hex,
                    "consecutive": 1,
                    "paid": True,
                    "created_at": int(time.time()),
                }
                # Return the JSON response from zen_send.sh along with the token/session
                return JSONResponse({
                    "ok": True,
                    "zen_send_result": result,
                    "token": token,
                    "sid": session_id,
                    "exp": exp
                })
            else:
                # Error from zen_send.sh
                return JSONResponse({
                    "ok": False,
                    "error": result.get("error", "Unknown error"),
                    "type": result.get("type", "unknown_error")
                })
        else:
            # Fallback for non-JSON responses (legacy compatibility)
            returned_file_path = script_output
            return JSONResponse({
                "ok": True,
                "html": returned_file_path,
                "message": "Legacy response format"
            })
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse zen_send.sh output as JSON: {e}")
        return JSONResponse({
            "ok": False,
            "error": f"Failed to parse script output: {script_output}",
            "type": "parse_error"
        })
    except Exception as e:
        logging.error(f"Error processing zen_send.sh response: {e}")
        return JSONResponse({
            "ok": False,
            "error": f"Script execution failed: {last_line.strip()}",
            "type": "execution_error"
        })

### UPlanet Media Recorder
@app.get("/webcam", response_class=HTMLResponse)
async def rec_form(request: Request):
    return templates.TemplateResponse("webcam.html", {
        "request": request, 
        "recording": False,
        "myIPFS": get_myipfs_gateway()
    })

@app.get("/vocals", response_class=HTMLResponse)
async def vocals_form(request: Request):
    """Voice messages interface with encryption support"""
    return templates.TemplateResponse("vocals.html", {
        "request": request,
        "myIPFS": get_myipfs_gateway()
    })

@app.get("/vocals-read", response_class=HTMLResponse)
async def vocals_read(request: Request):
    """Voice messages reader interface - decrypt and play encrypted messages"""
    return templates.TemplateResponse("vocals-read.html", {
        "request": request,
        "myIPFS": get_myipfs_gateway()
    })

@app.post("/webcam", response_class=HTMLResponse)
async def process_webcam_video(
    request: Request,
    player: str = Form(...),
    ipfs_cid: str = Form(...),  # IPFS CID from /api/fileupload (REQUIRED)
    title: str = Form(...),  # Video title (REQUIRED for NIP-71)
    npub: str = Form(...),  # NOSTR public key (REQUIRED for authentication)
    file_hash: str = Form(...),  # SHA256 hash (REQUIRED for provenance tracking)
    info_cid: str = Form(...),  # Info.json CID (REQUIRED for complete metadata)
    thumbnail_ipfs: str = Form(default=""),  # Thumbnail CID from upload2ipfs.sh (optional, generated by backend)
    gifanim_ipfs: str = Form(default=""),  # Animated GIF CID from upload2ipfs.sh (optional, generated by backend)
    mime_type: str = Form(default="video/webm"),  # MIME type from upload2ipfs.sh (default: video/webm)
    upload_chain: str = Form(default=""),  # Upload chain from upload2ipfs.sh provenance (for re-uploads)
    duration: str = Form(default="0"),  # Duration from upload2ipfs.sh (optional, for video kind determination)
    video_dimensions: str = Form(default="640x480"),  # Video dimensions from upload2ipfs.sh (optional, for imeta tag)
    file_size: str = Form(default="0"),  # File size in bytes from upload2ipfs.sh (REQUIRED, must not be 0)
    description: str = Form(default=""),  # Video description (optional)
    publish_nostr: str = Form(default="false"),  # Publish to NOSTR (default: false)
    latitude: str = Form(default=""),  # Geographic latitude (optional)
    longitude: str = Form(default=""),  # Geographic longitude (optional)
    youtube_url: str = Form(default=""),  # YouTube URL (optional, for source:youtube tag)
    genres: str = Form(default="")  # Genres as JSON array (optional, for kind 1985 tags)
):
    """
    Process webcam video and publish to NOSTR as NIP-71 video event
    
    This endpoint implements Phase 2 of the video upload workflow as defined in UPlanet_FILE_CONTRACT.md
    Video must be uploaded via /api/fileupload first (Phase 1) to obtain IPFS CID and metadata.
    
    REQUIRED Parameters (per UPlanet_FILE_CONTRACT.md section 3.2):
    - ipfs_cid: IPFS Content Identifier (from Phase 1)
    - title: Video title (user-provided, essential for NIP-71)
    - npub: NOSTR public key (authentication, NIP-42)
    - file_hash: SHA256 hash (provenance tracking)
    - info_cid: info.json CID (complete metadata archive)
    - player: User identifier/email
    
    OPTIONAL Parameters:
    - thumbnail_ipfs, gifanim_ipfs: Generated in Phase 1
    - description: User-provided video description
    - latitude, longitude: Geolocation tags
    - mime_type, duration, video_dimensions: Auto-detected metadata
    - upload_chain: Provenance chain (for re-uploads)
    - publish_nostr: Flag to publish event (default: false)
    - genres: JSON array of genres (e.g., '["Action","Sci-Fi"]') for kind 1985 tag events (NIP-32 Labeling)
    
    Returns: HTML response with NOSTR event publication status
    """
    
    # Log function entry FIRST
    print(f"\n🎬 POST /webcam endpoint called")
    print(f"   - Player: {player}")
    print(f"   - IPFS CID: {ipfs_cid}")
    logging.info(f"🎬 POST /webcam endpoint called with player={player}, ipfs_cid={ipfs_cid}")

    # Validate IPFS CID is provided
    if not ipfs_cid or not ipfs_cid.strip():
        print(f"❌ No IPFS CID provided")
        logging.error("No IPFS CID provided")
        return templates.TemplateResponse("webcam.html", {
            "request": request, 
            "error": "No IPFS CID provided. Video must be uploaded via /api/fileupload first.", 
            "recording": False,
            "myIPFS": get_myipfs_gateway()
        })

    try:
        # Use both print and logging to ensure visibility
        print(f"\n{'='*60}")
        print(f"========== WEBCAM VIDEO PROCESSING START ==========")
        print(f"{'='*60}")
        logging.info(f"========== WEBCAM VIDEO PROCESSING START ==========")
        
        print(f"📥 Input parameters:")
        print(f"   - Player: {player}")
        print(f"   - IPFS CID: {ipfs_cid}")
        print(f"   - Title: {title}")
        print(f"   - Description: {description[:50] if description else '(empty)'}")
        print(f"   - NPUB: {npub[:16] + '...' if npub else '(empty)'}")
        print(f"   - Publish to NOSTR: {publish_nostr}")
        print(f"   - Latitude: {latitude}")
        print(f"   - Longitude: {longitude}")
        
        logging.info(f"📥 Input parameters:")
        logging.info(f"   - Player: {player}")
        logging.info(f"   - IPFS CID: {ipfs_cid}")
        logging.info(f"   - Title: {title}")
        logging.info(f"   - Description: {description[:50]}..." if description else "   - Description: (empty)")
        logging.info(f"   - NPUB: {npub[:16]}..." if npub else "   - NPUB: (empty)")
        logging.info(f"   - Publish to NOSTR: {publish_nostr}")
        logging.info(f"   - Latitude: {latitude}")
        logging.info(f"   - Longitude: {longitude}")
        
        ipfs_url = None
        filename = None
        # Extract file_size from form parameter first, then from info.json, then from file
        file_size_from_form = 0
        try:
            file_size_from_form = int(file_size) if file_size and file_size != "0" else 0
        except (ValueError, TypeError):
            file_size_from_form = 0
        
        file_size = file_size_from_form
        # Use metadata from upload2ipfs.sh (passed via form parameters or loaded from info.json)
        # Default values if not provided
        video_dimensions_param = video_dimensions if video_dimensions and video_dimensions != "640x480" else "640x480"
        try:
            duration_param = int(float(duration)) if duration else 0
        except (ValueError, TypeError):
            duration_param = 0
        user_dir = None
        
        # Try to load metadata from info.json if info_cid is provided
        video_dimensions = video_dimensions_param
        duration = duration_param
        # Also load thumbnail_ipfs and gifanim_ipfs from info.json if not provided via form
        thumbnail_ipfs_from_info = thumbnail_ipfs if thumbnail_ipfs else ""
        gifanim_ipfs_from_info = gifanim_ipfs if gifanim_ipfs else ""
        file_hash = None
        upload_chain = None
        
        if info_cid:
            try:
                import httpx
                gateway = get_myipfs_gateway()
                info_url = f"{gateway}/ipfs/{info_cid}"
                logging.info(f"📋 Loading metadata from info.json: {info_url}")
                
                async with httpx.AsyncClient(timeout=10.0) as client:
                    info_response = await client.get(info_url)
                    if info_response.status_code == 200:
                        info_data = info_response.json()
                        
                        # Extract file hash from info.json
                        if info_data.get("file") and info_data["file"].get("hash"):
                            file_hash = info_data["file"]["hash"]
                            logging.info(f"🔐 File hash from info.json: {file_hash[:16]}...")
                        
                        # Extract upload chain from provenance
                        if info_data.get("provenance") and info_data["provenance"].get("upload_chain"):
                            upload_chain = info_data["provenance"]["upload_chain"]
                            logging.info(f"🔗 Upload chain from info.json: {upload_chain[:50]}...")
                        
                        # Extract metadata from info.json (supporting both v1.0 and v2.0 formats)
                        protocol_version = info_data.get("protocol", {}).get("version", "1.0.0")
                        is_v2 = protocol_version.startswith("2.")
                        
                        if info_data.get("media"):
                            media = info_data["media"]
                            if media.get("dimensions"):
                                video_dimensions = media["dimensions"]
                                logging.info(f"📐 Video dimensions from info.json: {video_dimensions}")
                            if media.get("duration"):
                                # Keep full precision for duration
                                duration = float(media["duration"])
                                logging.info(f"⏱️  Video duration from info.json: {duration}s")
                            # Extract file_size from info.json if not provided via form
                            if file_size == 0 and media.get("file_size"):
                                try:
                                    file_size = int(media["file_size"])
                                    logging.info(f"📦 File size from info.json: {file_size} bytes")
                                except (ValueError, TypeError):
                                    pass
                            
                            # Load thumbnail and gifanim from info.json if not provided
                            # Support both v2.0 (media.thumbnails.static/animated) and v1.0 (media.thumbnail_ipfs/gifanim_ipfs)
                            if not thumbnail_ipfs:
                                if is_v2 and media.get("thumbnails"):
                                    # v2.0 format: media.thumbnails.static or media.thumbnails.animated
                                    thumbnails = media["thumbnails"]
                                    thumbnail_cid = thumbnails.get("static") or thumbnails.get("animated")
                                    if thumbnail_cid:
                                        thumbnail_ipfs_from_info = thumbnail_cid.replace("/ipfs/", "").replace("ipfs://", "")
                                        logging.info(f"🖼️  Thumbnail CID from info.json (v2.0): {thumbnail_ipfs_from_info}")
                                elif not is_v2 and media.get("thumbnail_ipfs"):
                                    # v1.0 format: media.thumbnail_ipfs
                                    thumbnail_ipfs_from_info = media["thumbnail_ipfs"].replace("/ipfs/", "").replace("ipfs://", "")
                                    logging.info(f"🖼️  Thumbnail CID from info.json (v1.0): {thumbnail_ipfs_from_info}")
                            
                            if not gifanim_ipfs:
                                if is_v2 and media.get("thumbnails"):
                                    # v2.0 format: media.thumbnails.animated
                                    thumbnails = media["thumbnails"]
                                    gifanim_cid = thumbnails.get("animated")
                                    if gifanim_cid:
                                        gifanim_ipfs_from_info = gifanim_cid.replace("/ipfs/", "").replace("ipfs://", "")
                                        logging.info(f"🎬 Animated GIF CID from info.json (v2.0): {gifanim_ipfs_from_info}")
                                elif not is_v2 and media.get("gifanim_ipfs"):
                                    # v1.0 format: media.gifanim_ipfs
                                    gifanim_ipfs_from_info = media["gifanim_ipfs"].replace("/ipfs/", "").replace("ipfs://", "")
                                    logging.info(f"🎬 Animated GIF CID from info.json (v1.0): {gifanim_ipfs_from_info}")
                        
                        # Also check root level file_size in info.json
                        if file_size == 0 and info_data.get("file_size"):
                            try:
                                file_size = int(info_data["file_size"])
                                logging.info(f"📦 File size from info.json (root): {file_size} bytes")
                            except (ValueError, TypeError):
                                pass
                        
                        # Check fileSize (camelCase) as well
                        if file_size == 0 and info_data.get("fileSize"):
                            try:
                                file_size = int(info_data["fileSize"])
                                logging.info(f"📦 File size from info.json (fileSize): {file_size} bytes")
                            except (ValueError, TypeError):
                                pass
            except Exception as e:
                logging.warning(f"⚠️ Could not load metadata from info.json: {e}")
        
        # Use form parameters first, fallback to info.json
        final_thumbnail_ipfs = thumbnail_ipfs if thumbnail_ipfs else thumbnail_ipfs_from_info
        final_gifanim_ipfs = gifanim_ipfs if gifanim_ipfs else gifanim_ipfs_from_info
        
        logging.info(f"📊 Final video metadata: dimensions={video_dimensions}, duration={duration}s, thumbnail={final_thumbnail_ipfs}, gifanim={final_gifanim_ipfs} (from {'info.json' if info_cid else 'form parameters'})")
        
        # Extract filename and metadata from user directory structure
        hex_pubkey = npub_to_hex(npub) if npub else None
        logging.info(f"🔑 Converted NPUB to HEX: {hex_pubkey[:16]}..." if hex_pubkey else "⚠️ No HEX pubkey available")
        
        # Try to find user directory and determine email from it
        if hex_pubkey:
            try:
                user_dir = find_user_directory_by_hex(hex_pubkey)
                # Extract email from directory name (directory name is the email)
                directory_email = user_dir.name if '@' in user_dir.name else None
                
                # If player is not provided or not a valid email, use the email from directory
                if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player):
                    if directory_email and is_safe_email(directory_email):
                        player = directory_email
                        logging.info(f"✅ Using email from user directory: {player}")
                        print(f"✅ Using email from user directory: {player}")
                    else:
                        logging.warning(f"⚠️ No valid email found in directory: {directory_email if directory_email else 'none'}")
                        print(f"⚠️ No valid email found in directory")
                elif not is_safe_email(player):
                    # If player was provided but is not safe (e.g., hex key), try to use directory email
                    if directory_email and is_safe_email(directory_email):
                        player = directory_email
                        logging.info(f"✅ Player field contains unsafe value ({player[:20]}...), using email from directory: {player}")
                        print(f"✅ Player field contains unsafe value, using email from directory: {player}")
                    else:
                        logging.warning(f"⚠️ Invalid email address in player field and no valid directory email found")
                        print(f"⚠️ Invalid email address in player field")
                
                user_drive_path = user_dir / "APP" / "uDRIVE" / "Videos"
                logging.info(f"📂 User drive path: {user_drive_path}")
                
                # Find the most recent video file
                if user_drive_path.exists():
                    video_files = sorted(user_drive_path.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
                    logging.info(f"📹 Found {len(video_files)} video file(s) in user drive")
                    if video_files:
                        filename = video_files[0].name  # Only the filename, not the full path
                        # Only use file size from disk if not already set from form or info.json
                        if file_size == 0:
                            file_size = video_files[0].stat().st_size
                            logging.info(f"✅ Selected video file: {filename} ({file_size} bytes from disk)")
                        else:
                            logging.info(f"✅ Selected video file: {filename} (size already set: {file_size} bytes)")
            except Exception as e:
                logging.warning(f"Could not find user directory: {e}")
                # If we couldn't find user directory and player is not valid, return error
                if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player) or not is_safe_email(player):
                    logging.error(f"❌ Could not determine user email: no valid player provided and directory lookup failed")
                    print(f"❌ Could not determine user email: no valid player provided and directory lookup failed")
                    return templates.TemplateResponse("webcam.html", {
                        "request": request, 
                        "error": "Could not determine user email. Please ensure your NOSTR profile is set up correctly or provide a valid email address.", 
                        "recording": False,
                        "myIPFS": get_myipfs_gateway()
                    })
        
        # Final validation that we have a valid email
        if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player) or not is_safe_email(player):
            logging.error(f"❌ No valid email address available after all attempts")
            print(f"❌ No valid email address available after all attempts")
            return templates.TemplateResponse("webcam.html", {
                "request": request, 
                "error": "No valid email address could be determined. Please ensure your NOSTR profile is set up correctly.", 
                "recording": False,
                "myIPFS": get_myipfs_gateway()
            })
        
        if not filename:
            filename = f"video_{int(time.time())}.webm"
            logging.info(f"⚠️ No filename found, using default: {filename}")
        
        # Validate that file_size is not 0
        if file_size == 0:
            logging.error(f"❌ file_size is 0, cannot proceed with NOSTR publication")
            return templates.TemplateResponse("webcam.html", {
                "request": request, 
                "error": "File size is missing or invalid. Please ensure the file was uploaded correctly.", 
                "recording": False,
                "myIPFS": get_myipfs_gateway()
            })
        
        ipfs_url = f"/ipfs/{ipfs_cid}/{filename}"
        logging.info(f"🔗 IPFS URL: {ipfs_url}")
        logging.info(f"📦 File size: {file_size} bytes")
        
        # Generate title if not provided
        if not title:
            title = f"Webcam recording {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            logging.info(f"📝 No title provided, using default: {title}")
        
        # Use thumbnail and gifanim from upload2ipfs.sh (centralized generation)
        # These are either from form parameters or loaded from info.json
        if final_thumbnail_ipfs:
            logging.info(f"✅ Using thumbnail from upload2ipfs.sh: {final_thumbnail_ipfs}")
        else:
            logging.info(f"⚠️ No thumbnail provided. Thumbnail generation is now centralized in upload2ipfs.sh.")
        
        if final_gifanim_ipfs:
            logging.info(f"✅ Using animated GIF from upload2ipfs.sh: {final_gifanim_ipfs}")
        else:
            logging.info(f"⚠️ No animated GIF provided. GIF generation is now centralized in upload2ipfs.sh.")

        # Publish to NOSTR if requested
        nostr_event_id = None
        logging.info(f"🔍 NOSTR Publishing Check - publish_nostr: '{publish_nostr}', npub: '{npub[:16] if npub else None}...'")
        
        if publish_nostr.lower() == "true" and npub:
            logging.info(f"✅ Starting NOSTR publishing process...")
            try:
                # Verify NOSTR authentication
                if not await verify_nostr_auth(npub):
                    return templates.TemplateResponse("webcam.html", {
                        "request": request, 
                        "error": "NOSTR authentication failed. Please check your npub.", 
                        "recording": False
                    })
                
                # Get user's NOSTR keys
                user_dir = get_authenticated_user_directory(npub)
                secret_file = user_dir / ".secret.nostr"
                
                logging.info(f"🔑 Checking for secret file: {secret_file}")
                
                if not secret_file.exists():
                    logging.error(f"❌ Secret file does NOT exist: {secret_file}")
                    return templates.TemplateResponse("webcam.html", {
                        "request": request, 
                        "error": "NOSTR secret file not found. Please check your configuration.", 
                        "recording": False
                    })
                
                logging.info(f"✅ Secret file found, publishing via unified script...")
                
                # Prepare latitude and longitude
                try:
                    lat = float(latitude) if latitude else 0.00
                    lon = float(longitude) if longitude else 0.00
                except (ValueError, TypeError):
                    lat = 0.00
                    lon = 0.00
                
                # Use unified publish_nostr_video.sh script
                publish_script = os.path.expanduser("~/.zen/Astroport.ONE/tools/publish_nostr_video.sh")
                
                if not os.path.exists(publish_script):
                    logging.error(f"❌ Unified publish script not found: {publish_script}")
                    return templates.TemplateResponse("webcam.html", {
                        "request": request, 
                        "error": "NOSTR publish script not found. Please check installation.", 
                        "recording": False
                    })
                
                # Build command for unified script
                publish_cmd = [
                    "bash", publish_script,
                    "--nsec", str(secret_file),
                    "--ipfs-cid", ipfs_cid,
                    "--filename", filename,
                    "--title", title,
                    "--json"
                ]
                
                # Add optional parameters
                if description:
                    publish_cmd.extend(["--description", description])
                if final_thumbnail_ipfs:
                    publish_cmd.extend(["--thumbnail-cid", final_thumbnail_ipfs])
                if final_gifanim_ipfs:
                    publish_cmd.extend(["--gifanim-cid", final_gifanim_ipfs])
                if info_cid:
                    publish_cmd.extend(["--info-cid", info_cid])
                if file_hash:
                    publish_cmd.extend(["--file-hash", file_hash])
                if mime_type:
                    publish_cmd.extend(["--mime-type", mime_type])
                if upload_chain:
                    # Convert upload_chain to JSON string if it's a list/dict
                    if isinstance(upload_chain, (list, dict)):
                        upload_chain_str = json.dumps(upload_chain)
                    else:
                        upload_chain_str = str(upload_chain)
                    publish_cmd.extend(["--upload-chain", upload_chain_str])
                
                # Convert video_dimensions to string format expected by script
                # Format: "WIDTHxHEIGHT" or JSON string if dict
                if isinstance(video_dimensions, dict):
                    # Extract width and height from dict
                    width = video_dimensions.get('width', '')
                    height = video_dimensions.get('height', '')
                    if width and height:
                        dimensions_str = f"{width}x{height}"
                    else:
                        # Fallback: convert to JSON string
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
                
                # Add source type tag (default: webcam, or youtube if youtube_url provided)
                if youtube_url:
                    publish_cmd.extend(["--source-type", "youtube"])
                    logging.info(f"📺 YouTube URL: {youtube_url} → source:youtube")
                else:
                    publish_cmd.extend(["--source-type", "webcam"])
                    logging.info(f"📹 Source type: webcam")
                
                # Add genres for kind 1985 tag events (NIP-32 Labeling)
                if genres and genres.strip():
                    try:
                        # Validate that genres is a valid JSON array
                        genres_json = json.loads(genres)
                        if isinstance(genres_json, list) and len(genres_json) > 0:
                            # Ensure it's a compact JSON string (no spaces/newlines)
                            genres_compact = json.dumps(genres_json, ensure_ascii=False, separators=(',', ':'))
                            publish_cmd.extend(["--genres", genres_compact])
                            logging.info(f"🏷️  Genres for kind 1985 tags: {genres_compact}")
                        else:
                            logging.warning(f"⚠️  Genres provided but not a valid non-empty array: {genres}")
                    except json.JSONDecodeError as e:
                        logging.warning(f"⚠️  Invalid JSON format for genres, skipping kind 1985 tags: {genres} (error: {e})")
                    except Exception as e:
                        logging.warning(f"⚠️  Error processing genres, skipping kind 1985 tags: {e}")
                
                logging.info(f"🚀 Executing unified NOSTR publish script...")
                logging.info(f"📝 Title: {title}")
                logging.info(f"⏱️  Duration: {duration}s")
                logging.info(f"📍 Location: {lat:.2f}, {lon:.2f}")
                logging.info(f"🔐 File hash: {file_hash[:16] if file_hash else 'N/A'}...")
                # Log upload_chain safely (handle both string and list/dict)
                if upload_chain:
                    if isinstance(upload_chain, (list, dict)):
                        upload_chain_log = json.dumps(upload_chain)[:50]
                    else:
                        upload_chain_log = str(upload_chain)[:50]
                    logging.info(f"🔗 Upload chain: {upload_chain_log}...")
                else:
                    logging.info(f"🔗 Upload chain: N/A")
                # Log command safely (convert all items to strings)
                try:
                    cmd_str = ' '.join(str(arg) for arg in publish_cmd)
                    logging.info(f"🔧 Full command: {cmd_str}")
                except Exception as cmd_err:
                    logging.warning(f"⚠️ Could not log full command: {cmd_err}")
                    logging.info(f"🔧 Command has {len(publish_cmd)} arguments")
                
                # Execute unified script
                publish_result = subprocess.run(publish_cmd, capture_output=True, text=True, timeout=30)
                
                logging.info(f"📊 Publish script return code: {publish_result.returncode}")
                
                if publish_result.returncode == 0:
                    try:
                        # Parse JSON output
                        result_json = json.loads(publish_result.stdout)
                        nostr_event_id = result_json.get('event_id', '')
                        relays_success = result_json.get('relays_success', 0)
                        relays_total = result_json.get('relays_total', 0)
                        video_kind = result_json.get('kind', 21)
                        
                        logging.info(f"✅ NOSTR video event (kind {video_kind}) published: {nostr_event_id}")
                        logging.info(f"📡 Published to {relays_success}/{relays_total} relay(s)")
                        logging.info(f"🎉 Event successfully sent to NOSTR network!")
                        
                        print(f"✅ NOSTR event published: {nostr_event_id[:16]}...")
                    except json.JSONDecodeError as json_err:
                        logging.warning(f"⚠️ Failed to parse JSON output: {json_err}")
                        logging.info(f"📤 Script output: {publish_result.stdout}")
                        # Try to extract event ID from output using regex (more robust)
                        # Note: re is already imported globally at line 34
                        event_id_match = re.search(r'"event_id"\s*:\s*"([a-f0-9]{64})"', publish_result.stdout)
                        if event_id_match:
                            nostr_event_id = event_id_match.group(1)
                            logging.info(f"✅ NOSTR video event published (extracted from invalid JSON): {nostr_event_id}")
                        else:
                            # Fallback: try to extract from last line
                            nostr_event_id = publish_result.stdout.strip().split('\n')[-1] if publish_result.stdout else ""
                            # Validate it's a hex string
                            if not re.match(r'^[a-f0-9]{64}$', nostr_event_id):
                                nostr_event_id = ""
                            if nostr_event_id:
                                logging.info(f"✅ NOSTR video event published (extracted from output): {nostr_event_id}")
                            else:
                                logging.warning(f"⚠️ Could not extract event ID from output")
                                nostr_event_id = ""
                else:
                    logging.error(f"❌ Failed to publish NOSTR event (return code: {publish_result.returncode})")
                    logging.error(f"❌ stderr ({len(publish_result.stderr)} chars): {publish_result.stderr if publish_result.stderr else '(empty)'}")
                    logging.error(f"❌ stdout ({len(publish_result.stdout)} chars): {publish_result.stdout if publish_result.stdout else '(empty)'}")
                    logging.error(f"❌ Script path: {publish_script}")
                    logging.error(f"❌ Script exists: {os.path.exists(publish_script)}")
                    logging.error(f"❌ Script executable: {os.access(publish_script, os.X_OK)}")
                    print(f"❌ NOSTR publishing failed with code {publish_result.returncode}")
                    
            except subprocess.TimeoutExpired:
                logging.error(f"❌ NOSTR publishing timeout (>30s)")
                print(f"❌ NOSTR publishing timeout")
            except Exception as e:
                logging.error(f"❌ Error during NOSTR publishing: {e}")
                logging.error(f"❌ Traceback: {traceback.format_exc()}")
                print(f"❌ Exception in NOSTR publishing: {e}")
        else:
            logging.info(f"⚠️ NOSTR publishing skipped - Conditions not met")
            logging.info(f"   - publish_nostr.lower() == 'true': {publish_nostr.lower() == 'true'}")
            logging.info(f"   - npub exists: {bool(npub)}")


        # Return success response
        success_message = f"Video processed successfully! IPFS: {ipfs_url}"
        if nostr_event_id:
            success_message += f" | NOSTR Event: {nostr_event_id}"
        
        logging.info(f"========== WEBCAM VIDEO PROCESSING COMPLETE ==========")
        logging.info(f"✅ Success message: {success_message}")
        logging.info(f"📊 Final stats: filename={filename}, size={file_size}, duration={duration}s, dimensions={video_dimensions}")
        if nostr_event_id:
            logging.info(f"🎉 NOSTR event published: {nostr_event_id}")
        
        return templates.TemplateResponse("webcam.html", {
            "request": request,
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
        logging.error(f"========== WEBCAM VIDEO PROCESSING FAILED ==========")
        logging.error(f"❌ Error processing webcam video: {e}")
        logging.error(f"❌ Traceback: {traceback.format_exc()}")
        return templates.TemplateResponse("webcam.html", {
            "request": request, 
            "error": f"Error processing video: {str(e)}", 
            "recording": False
        })

@app.post("/vocals", response_class=HTMLResponse)
async def process_vocals_message(
    request: Request,
    player: str = Form(...),
    ipfs_cid: str = Form(...),  # IPFS CID from /api/fileupload (REQUIRED)
    title: str = Form(...),  # Voice message title (REQUIRED)
    npub: str = Form(...),  # NOSTR public key (REQUIRED for authentication)
    file_hash: str = Form(...),  # SHA256 hash (REQUIRED for provenance tracking)
    info_cid: str = Form(default=""),  # Info.json CID (optional for voice messages)
    mime_type: str = Form(default="audio/mpeg"),  # MIME type from upload2ipfs.sh
    file_name: str = Form(default=""),  # Filename from upload2ipfs.sh (REQUIRED for correct IPFS URL)
    duration: str = Form(default="0"),  # Duration in seconds
    description: str = Form(default=""),  # Voice message description (optional)
    publish_nostr: str = Form(default="false"),  # Publish to NOSTR (default: false)
    latitude: str = Form(default=""),  # Geographic latitude (optional)
    longitude: str = Form(default=""),  # Geographic longitude (optional)
    encrypted: str = Form(default="false"),  # Encrypt message (default: false)
    encryption_method: str = Form(default="nip44"),  # Encryption method: nip44 or nip04
    recipients: str = Form(default=""),  # JSON array of recipient pubkeys for encryption
    waveform: str = Form(default=""),  # Waveform data (optional, for imeta tag)
    kind: str = Form(default="1222"),  # Event kind: 1222 (root) or 1244 (reply)
    reply_to_event_id: str = Form(default=""),  # Event ID being replied to (for kind 1244)
    reply_to_pubkey: str = Form(default=""),  # Pubkey of the event being replied to (for kind 1244)
    expiration: str = Form(default="")  # Expiration timestamp (NIP-40)
):
    """
    Process voice message and publish to NOSTR as NIP-A0 voice event (kind 1222 or 1244)
    
    Supports both public and encrypted voice messages per A0-encryption-extension.md
    
    REQUIRED Parameters:
    - ipfs_cid: IPFS Content Identifier (from /api/fileupload)
    - title: Voice message title
    - npub: NOSTR public key (authentication, NIP-42)
    - file_hash: SHA256 hash (provenance tracking)
    - player: User identifier/email
    
    OPTIONAL Parameters:
    - encrypted: "true" to enable encryption (default: "false")
    - encryption_method: "nip44" (recommended) or "nip04" (legacy)
    - recipients: JSON array of recipient pubkeys (required if encrypted=true)
    - duration: Audio duration in seconds
    - waveform: Waveform data for visual preview
    - latitude, longitude: Geolocation tags
    - description: Voice message description
    - publish_nostr: Flag to publish event (default: false)
    
    Returns: HTML response with NOSTR event publication status
    """
    logging.info(f"🎤 POST /vocals endpoint called with player={player}, ipfs_cid={ipfs_cid}, encrypted={encrypted}")
    
    # Validate IPFS CID
    if not ipfs_cid or not ipfs_cid.strip():
        logging.error("No IPFS CID provided")
        return templates.TemplateResponse("vocals.html", {
            "request": request,
            "error": "No IPFS CID provided. Audio must be uploaded via /api/fileupload first.",
            "myIPFS": get_myipfs_gateway()
        })
    
    # Validate encryption parameters
    is_encrypted = encrypted.lower() == "true"
    if is_encrypted:
        if not recipients or not recipients.strip():
            logging.error("Recipients required for encrypted messages")
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": "Recipients required for encrypted voice messages. Please specify at least one recipient pubkey.",
                "myIPFS": get_myipfs_gateway()
            })
        try:
            recipients_list = json.loads(recipients)
            if not isinstance(recipients_list, list) or len(recipients_list) == 0:
                raise ValueError("Recipients must be a non-empty array")
        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"Invalid recipients format: {e}")
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": f"Invalid recipients format. Expected JSON array of pubkeys: {e}",
                "myIPFS": get_myipfs_gateway()
            })
    
    try:
        # Get user secret file for NOSTR signing
        # Use the same method as webcam endpoint: get directory from npub
        try:
            user_dir = get_authenticated_user_directory(npub)
            # Try .secret.dunikey first (correct location), fallback to .secret.nostr
            secret_file = user_dir / ".secret.nostr"
        except Exception as e:
            logging.warning(f"Could not get user directory from npub, trying fallback: {e}")
            # Fallback: try to find user directory by email
            user_dir = os.path.expanduser(f"~/.zen/game/nostr/{player}")
            secret_file = os.path.join(user_dir, ".secret.dunikey")
            if not os.path.exists(secret_file):
                secret_file = os.path.join(user_dir, ".secret.nostr")
        
        if not os.path.exists(secret_file):
            logging.error(f"Secret file not found: {secret_file}")
            logging.error(f"Searched in: {user_dir}")
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": "NOSTR authentication required. Please connect with NIP-42 first.",
                "myIPFS": get_myipfs_gateway()
            })
        
        # Prepare location
        try:
            lat = float(latitude) if latitude else 0.00
            lon = float(longitude) if longitude else 0.00
        except (ValueError, TypeError):
            lat = 0.00
            lon = 0.00
        
        # Determine kind: 1222 for root, 1244 for reply (if replying to another voice message)
        try:
            voice_kind = int(kind) if kind else 1222
            if voice_kind not in [1222, 1244]:
                voice_kind = 1222  # Default to root if invalid kind
        except (ValueError, TypeError):
            voice_kind = 1222
        
        # For kind 1244 (reply), validate reply parameters
        if voice_kind == 1244:
            if not reply_to_event_id or not reply_to_event_id.strip():
                logging.warning("Kind 1244 specified but no reply_to_event_id provided, defaulting to kind 1222")
                voice_kind = 1222
            elif not reply_to_pubkey or not reply_to_pubkey.strip():
                logging.warning("Kind 1244 specified but no reply_to_pubkey provided, defaulting to kind 1222")
                voice_kind = 1222
        
        # Build IPFS URL
        gateway = get_myipfs_gateway()
        ipfs_url = f"{gateway}/ipfs/{ipfs_cid}"
        
        # Prepare voice message metadata
        voice_metadata = {
            "url": ipfs_url,
            "duration": float(duration) if duration else 0.0,
            "title": title,
            "description": description
        }
        
        if waveform:
            voice_metadata["waveform"] = waveform
        
        if lat != 0.00 or lon != 0.00:
            voice_metadata["latitude"] = lat
            voice_metadata["longitude"] = lon
        
        # If encrypted, the frontend should have already encrypted the content
        # For now, we'll publish the event with encrypted content if provided
        # The encryption should be done client-side using window.nostr.nip44.encrypt
        
        # Use publish_nostr_vocal.sh script (dedicated for voice messages)
        publish_script = os.path.expanduser("~/.zen/Astroport.ONE/tools/publish_nostr_vocal.sh")
        
        if not os.path.exists(publish_script):
            logging.error(f"Publish script not found: {publish_script}")
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": "NOSTR publish script not found. Please check installation.",
                "myIPFS": get_myipfs_gateway()
            })
        
        # Build command for voice message publication
        # Pass the keyfile path directly - publish_nostr_vocal.sh can handle both file paths and direct NSEC keys
        secret_file_str = str(secret_file)
        
        # Use the actual filename from upload2ipfs.sh if provided, otherwise generate one
        if file_name and file_name.strip():
            actual_filename = file_name.strip()
        else:
            # Fallback: generate filename from timestamp and mime type
            extension = mime_type.split('/')[-1] if '/' in mime_type else 'mp3'
            actual_filename = f"voice_{int(time.time())}.{extension}"
            logging.warning(f"No filename provided, using generated: {actual_filename}")
        
        publish_cmd = [
            "bash", publish_script,
            "--nsec", secret_file_str,
            "--ipfs-cid", ipfs_cid,
            "--filename", actual_filename,
            "--title", title,
            "--json",
            "--kind", str(voice_kind)  # 1222 for root, 1244 for reply
        ]
        
        if description:
            publish_cmd.extend(["--description", description])
        if file_hash:
            publish_cmd.extend(["--file-hash", file_hash])
        if mime_type:
            publish_cmd.extend(["--mime-type", mime_type])
        if duration:
            publish_cmd.extend(["--duration", str(duration)])
        if lat != 0.00 or lon != 0.00:
            publish_cmd.extend(["--latitude", str(lat), "--longitude", str(lon)])
        if waveform:
            publish_cmd.extend(["--waveform", waveform])
        if info_cid:
            publish_cmd.extend(["--info-cid", info_cid])
        
        # Add reply tags for kind 1244 (NIP-22)
        if voice_kind == 1244 and reply_to_event_id and reply_to_pubkey:
            publish_cmd.extend(["--reply-to-event-id", reply_to_event_id])
            publish_cmd.extend(["--reply-to-pubkey", reply_to_pubkey])
        
        # Add expiration tag (NIP-40)
        if expiration and expiration.strip():
            try:
                exp_timestamp = int(expiration)
                if exp_timestamp > 0:
                    publish_cmd.extend(["--expiration", str(exp_timestamp)])
            except (ValueError, TypeError):
                logging.warning(f"Invalid expiration timestamp: {expiration}")
        
        # Add encryption parameters if encrypted
        if is_encrypted:
            publish_cmd.extend(["--encrypted", "true"])
            publish_cmd.extend(["--encryption-method", encryption_method])
            if recipients:
                publish_cmd.extend(["--recipients", recipients])
        
        publish_cmd.extend(["--channel", player])
        
        logging.info(f"🚀 Publishing voice message (kind {voice_kind})...")
        
        # Execute script
        publish_result = subprocess.run(publish_cmd, capture_output=True, text=True, timeout=30)
        
        if publish_result.returncode == 0:
            try:
                result_json = json.loads(publish_result.stdout)
                nostr_event_id = result_json.get('event_id', '')
                relays_success = result_json.get('relays_success', 0)
                relays_total = result_json.get('relays_total', 0)
                
                logging.info(f"✅ NOSTR voice message (kind {voice_kind}) published: {nostr_event_id}")
                logging.info(f"📡 Published to {relays_success}/{relays_total} relay(s)")
                
                return templates.TemplateResponse("vocals.html", {
                    "request": request,
                    "success": f"Voice message published successfully! Event ID: {nostr_event_id[:16]}...",
                    "event_id": nostr_event_id,
                    "myIPFS": get_myipfs_gateway()
                })
            except json.JSONDecodeError:
                logging.warning("Failed to parse JSON output")
                return templates.TemplateResponse("vocals.html", {
                    "request": request,
                    "error": "Voice message published but could not parse response.",
                    "myIPFS": get_myipfs_gateway()
                })
        else:
            logging.error(f"Failed to publish voice message: {publish_result.stderr}")
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": f"Failed to publish voice message: {publish_result.stderr}",
                "myIPFS": get_myipfs_gateway()
            })
            
    except Exception as e:
        logging.error(f"Error processing voice message: {e}", exc_info=True)
        return templates.TemplateResponse("vocals.html", {
            "request": request,
            "error": f"Error processing voice message: {str(e)}",
            "myIPFS": get_myipfs_gateway()
        })

############# API DESCRIPTION PAGE
@app.get("/health")
async def health_check():
    """Health check endpoint that doesn't count towards rate limits"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "rate_limiter_stats": {
            "active_ips": len(rate_limiter.requests),
            "rate_limit": RATE_LIMIT_REQUESTS,
            "window_seconds": RATE_LIMIT_WINDOW
        }
    }

@app.get("/rate-limit-status")
async def rate_limit_status(request: Request):
    """Get current rate limit status for the requesting IP"""
    client_ip = get_client_ip(request)
    remaining = rate_limiter.get_remaining_requests(client_ip)
    reset_time = rate_limiter.get_reset_time(client_ip)
    
    return {
        "client_ip": client_ip,
        "remaining_requests": remaining,
        "rate_limit": RATE_LIMIT_REQUESTS,
        "window_seconds": RATE_LIMIT_WINDOW,
        "reset_time": reset_time,
        "reset_time_iso": datetime.fromtimestamp(reset_time).isoformat() if reset_time else None,
        "is_blocked": remaining == 0
    }

@app.get("/dev", response_class=HTMLResponse)
async def welcomeuplanet(request: Request, console: Optional[str] = None):
    if console:
        return templates.TemplateResponse("relay_console.html", {
            "request": request,
            "myIPFS": get_myipfs_gateway()
        })
    return templates.TemplateResponse("dev.html", {
        "request": request,
        "myIPFS": get_myipfs_gateway()
    })

@app.post('/ping')
async def get_webhook(request: Request):
    """Receive analytics data and send as NOSTR message to CAPTAINEMAIL
    
    This endpoint receives analytics data and sends it as a NOSTR event (kind 10000)
    to the captain email using nostr_send_note.py instead of mailjet.sh.
    """
    if request.method == 'POST':
        try:
            # Récupérer les données de la requête
            data = await request.json()  # Récupérer le corps de la requête en JSON
            referer = request.headers.get("referer")  # Récupérer l'en-tête Referer

            # Get current player email from ~/.zen/game/players/.current (symbolic link)
            current_player_link = Path.home() / ".zen" / "game" / "players" / ".current"
            captain_email = None
            
            # Try to read the symbolic link first
            if current_player_link.exists() and current_player_link.is_symlink():
                try:
                    # Read the symbolic link to get the target directory path
                    target_path = current_player_link.readlink()
                    # Extract email from directory name (the symlink points to a directory named with the email)
                    captain_email = target_path.name
                    if captain_email:
                        logging.debug(f"📧 Using current player email from .current symlink: {captain_email}")
                except Exception as e:
                    logging.warning(f"⚠️ Could not read .current symlink: {e}")
            
            # Fallback to CAPTAINEMAIL from my.sh if .current is not available
            if not captain_email:
                captain_email = get_env_from_mysh("CAPTAINEMAIL", "")
                if captain_email:
                    logging.debug(f"📧 Using CAPTAINEMAIL from my.sh: {captain_email}")
                else:
                    # Last fallback to environment variable
                    captain_email = os.getenv("CAPTAINEMAIL", "")
                    if captain_email:
                        logging.debug(f"📧 Using CAPTAINEMAIL from environment variable: {captain_email}")
            
            if not captain_email:
                logging.warning("⚠️ No current player email found (.current symlink or CAPTAINEMAIL env var), skipping NOSTR notification")
                return {"received": data, "referer": referer, "note": "Current player email not configured"}
            
            # Find keyfile for current player email: ~/.zen/game/nostr/{email}/.secret.nostr
            captain_keyfile = Path.home() / ".zen" / "game" / "nostr" / captain_email / ".secret.nostr"
            
            if not captain_keyfile.exists():
                logging.warning(f"⚠️ Keyfile not found for current player ({captain_email}): {captain_keyfile}")
                return {"received": data, "referer": referer, "note": f"Keyfile not found for {captain_email}"}
            
            # Format analytics data as JSON string for NOSTR message
            analytics_json = json.dumps(data, indent=2, ensure_ascii=False)
            
            # Build message content
            message_lines = [
                "📊 Analytics Data Received",
                "",
                f"Type: {data.get('type', 'unknown')}",
                f"Source: {data.get('source', 'unknown')}",
                f"Timestamp: {data.get('timestamp', datetime.now(timezone.utc).isoformat())}",
            ]
            
            # Add referer if available
            if referer:
                message_lines.append(f"Referer: {referer}")

            # Add URL if available
            if data.get('current_url'):
                message_lines.append(f"URL: {data.get('current_url')}")
            
            # Add video-specific data if present
            if data.get('video_event_id'):
                message_lines.append(f"Video Event ID: {data.get('video_event_id')}")
            if data.get('video_title'):
                message_lines.append(f"Video Title: {data.get('video_title')}")
            
            message_lines.extend([
                "",
                "--- Full Data ---",
                analytics_json
            ])
            
            message_content = "\n".join(message_lines)
            
            # Build tags for NOSTR event (kind 10000 - Analytics)
            tags = [
                ["t", "analytics"],
                ["t", data.get("type", "unknown")]
            ]
            
            # Add source tag if available
            if data.get("source"):
                tags.append(["source", data.get("source")])
            
            # Add URL tag if available
            if data.get("current_url"):
                tags.append(["url", data.get("current_url")])
            
            # Get NOSTR relay from environment or use default
            nostr_relay = os.getenv("myRELAY", "ws://127.0.0.1:7777").split()[0]
            
            # Call nostr_send_note.py to send the message
            nostr_script = os.path.expanduser("~/.zen/Astroport.ONE/tools/nostr_send_note.py")
            
            if not os.path.exists(nostr_script):
                logging.error(f"❌ nostr_send_note.py not found at: {nostr_script}")
                return {"received": data, "referer": referer, "note": "nostr_send_note.py not found"}
            
            # Prepare command
            tags_json = json.dumps(tags)
            cmd = [
                "python3",
                nostr_script,
                "--keyfile", str(captain_keyfile),
                "--content", message_content,
                "--kind", "10000",  # Analytics event kind
                "--tags", tags_json,
                "--relays", nostr_relay,
                "--json"  # JSON output mode
            ]
            
            # Execute command (non-blocking, fire and forget)
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode == 0:
                    logging.info(f"✅ Analytics sent to captain via NOSTR: {data.get('type', 'unknown')}")
                else:
                    logging.warning(f"⚠️ NOSTR send failed: {result.stderr}")
                    
            except subprocess.TimeoutExpired:
                logging.warning("⚠️ NOSTR send timeout")
            except Exception as e:
                logging.warning(f"⚠️ NOSTR send error: {e}")

            return {"received": data, "referer": referer, "sent_via": "nostr"}
            
        except Exception as e:
            logging.error(f"❌ Error in /ping endpoint: {e}", exc_info=True)
            raise HTTPException(status_code=400, detail=f"Invalid request data: {e}")

    else:
        raise HTTPException(status_code=400, detail="Invalid method.")

### NIP-96 Discovery Endpoint
@app.get("/.well-known/nostr/nip96.json")
async def nip96_discovery(request: Request):
    """
    NIP-96 discovery endpoint for file storage server.
    Returns server capabilities and configuration for NOSTR clients.
    
    Plans are determined by user authentication:
    - MULTIPASS users (recognized by UPlanet): 650MB quota
    - Non-recognized NOSTR users: 100MB quota
    """
    import subprocess
    
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
                # Try multiple possible paths for the script
                possible_paths = [
                    os.path.join(SCRIPT_DIR.parent, "Astroport.ONE", "tools", "search_for_this_hex_in_uplanet.sh"),
                    os.path.join(os.path.expanduser("~"), ".zen", "Astroport.ONE", "tools", "search_for_this_hex_in_uplanet.sh"),
                    os.path.join(os.path.expanduser("~"), "workspace", "AAA", "Astroport.ONE", "tools", "search_for_this_hex_in_uplanet.sh"),
                ]
                search_script = None
                for path in possible_paths:
                    if os.path.exists(path):
                        search_script = path
                        break
                
                if search_script:
                    try:
                        result = subprocess.run(
                            [search_script, user_pubkey_hex],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        # If script found the HEX (returns G1PUBNOSTR or exits with 0 and has output), user is MULTIPASS
                        if result.returncode == 0 and result.stdout.strip():
                            is_multipass = True
                            logging.info(f"✅ NIP-96 Discovery: User is recognized MULTIPASS (650MB quota)")
                        else:
                            logging.info(f"ℹ️  NIP-96 Discovery: User is not recognized MULTIPASS (100MB quota)")
                    except (subprocess.TimeoutExpired, Exception) as e:
                        logging.warning(f"⚠️  NIP-96 Discovery: Could not check MULTIPASS status: {e}")
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
        "api_url": f"{base_url}/upload2ipfs",
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

### GENERIC UPLOAD - Free & Anonymous 
@app.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request):
    return templates.TemplateResponse("upload2ipfs.html", {"request": request})

# Old NIP96 method, still used by coracle.copylaradio.com
@app.post("/upload2ipfs")
async def upload_to_ipfs(request: Request, file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    # Preserve filename before reading file content
    original_filename = file.filename or "unknown"
    file_location = f"tmp/{original_filename}"
    
    # Get user pubkey for provenance tracking and file size validation from NIP-98 Authorization header
    user_pubkey_hex = ""
    user_npub = None
    try:
        # Extract Authorization header (NIP-98)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Nostr "):
            # Decode the base64-encoded NIP-98 event
            auth_base64 = auth_header.replace("Nostr ", "").strip()
            auth_json = base64.b64decode(auth_base64).decode('utf-8')
            auth_event = json.loads(auth_json)
            
            # Extract pubkey from the NIP-98 event (kind 27235 dans le auth_header)
            if auth_event.get("kind") == 27235 and "pubkey" in auth_event:
                user_pubkey_hex = auth_event["pubkey"]
                # Convert hex to npub for get_max_file_size_for_user
                user_npub = hex_to_npub(user_pubkey_hex) if user_pubkey_hex else None
                logging.info(f"🔑 NIP-98 Auth: Provenance tracking enabled for user: {user_pubkey_hex[:16]}...")
            else:
                logging.warning(f"⚠️ Invalid NIP-98 event: kind={auth_event.get('kind')}")
        else:
            logging.info(f"ℹ️ No NIP-98 Authorization header, uploading without provenance tracking")
    except Exception as e:
        logging.warning(f"⚠️ Could not extract pubkey from NIP-98 Authorization header: {e}")
    
    # Validate file size according to UPlanet_FILE_CONTRACT.md section 6.2
    # Use default 100MB if no auth or if npub conversion failed
    if user_npub:
        max_size_bytes = get_max_file_size_for_user(user_npub)
    else:
        max_size_bytes = 104857600  # Default 100MB for non-authenticated or unrecognized users
    
    if file.size and file.size > max_size_bytes:
        max_size_mb = max_size_bytes // 1048576
        file_size_mb = file.size // 1048576
        raise HTTPException(
            status_code=413,
            content={
                "status": "error",
                "message": f"File size ({file_size_mb}MB) exceeds maximum allowed size ({max_size_mb}MB per UPlanet_FILE_CONTRACT.md)"
            }
        )
    
    try:
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        # Generate a unique temporary file path
        temp_file_path = f"tmp/temp_{uuid.uuid4()}.json"

        script_path = "./upload2ipfs.sh"
        
        # Pass user pubkey as 3rd parameter to upload2ipfs.sh
        return_code, last_line = await run_script(script_path, file_location, temp_file_path, user_pubkey_hex)

        if return_code == 0:
          try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip()) # Remove extra spaces/newlines

                # Transform to NIP-96 compliant format
                # Extract fields from script output
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
                
                # Build IPFS URL - use /ipfs/CID/filename format
                ipfs_gateway = get_myipfs_gateway().rstrip('/')
                if new_cid and file_name:
                    ipfs_url = f"/ipfs/{new_cid}/{file_name}"
                elif new_cid:
                    ipfs_url = f"/ipfs/{new_cid}"
                else:
                    ipfs_url = ""
                
                # Build NIP-94 event tags
                tags = []
                
                # Required tags
                if ipfs_url:
                    tags.append(["url", ipfs_url])
                
                # Original file hash (ox) - same as x if no transformation
                if file_hash:
                    tags.append(["ox", file_hash])
                    tags.append(["x", file_hash])  # Transformed hash (same if no transformation)
                
                # MIME type
                if mime_type:
                    tags.append(["m", mime_type])
                
                # File size
                if file_size:
                    tags.append(["size", str(file_size)])
                
                # Dimensions (for images/videos)
                if dimensions:
                    tags.append(["dim", dimensions])
                
                # Info.json CID
                if info_cid:
                    tags.append(["info", info_cid])
                
                # Thumbnail IPFS CID
                if thumbnail_ipfs:
                    tags.append(["thumbnail_ipfs", thumbnail_ipfs])
                
                # GIF animation IPFS CID
                if gifanim_ipfs:
                    tags.append(["gifanim_ipfs", gifanim_ipfs])
                
                # Upload chain for provenance
                if upload_chain:
                    tags.append(["upload_chain", upload_chain])
                
                # Build NIP-96 compliant response
                nip96_response = {
                    "status": "success",
                    "message": json_output.get("message", "File uploaded successfully"),
                    "nip94_event": {
                        "tags": tags,
                        "content": ""
                    }
                }
                
                # Add extended fields for backward compatibility
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
                
                # Delete the temporary files
                os.remove(temp_file_path)
                os.remove(file_location)
                return JSONResponse(content=nip96_response)
          except (json.JSONDecodeError, FileNotFoundError) as e:
                logging.error(f"Failed to decode JSON from temp file: {temp_file_path}, Error: {e}")
                return JSONResponse(
                  content={
                      "status": "error",
                      "message": "Failed to process script output, JSON decode error.",
                       "exception": str(e),
                       "temp_file_path": temp_file_path,
                      },
                   status_code=500
               )
          finally:
                if os.path.exists(temp_file_path):
                   os.remove(temp_file_path) # Ensure file deletion in case of error
                if os.path.exists(file_location):
                  os.remove(file_location) # Ensure file deletion in case of error
        else:
           logging.error(f"Script execution failed: {last_line.strip()}")
           return JSONResponse(
                content={
                    "status": "error",
                    "message": "Script execution failed.",
                    "raw_output": last_line.strip()
                  },
                  status_code=500
               )
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        return JSONResponse(
            content={
                "status": "error",
                "message": "An unexpected error occurred.",
                "exception": str(e)
                },
            status_code=500
        )

# Upload after NIP-42 NOSTR authentication
def transform_youtube_metadata_to_structured(flat_metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform flat YouTube metadata (from ajouter_media.sh) to structured format
    expected by enrichTrackWithInfoJson in nostrify.enhancements.js.
    
    Args:
        flat_metadata: Flat YouTube metadata dictionary from yt-dlp .info.json
        
    Returns:
        Structured metadata dictionary with nested objects (channel_info, content_info, etc.)
    """
    # Extract channel information
    channel_name = flat_metadata.get('channel') or flat_metadata.get('uploader', '')
    channel_info = {
        'display_name': channel_name,
        'name': channel_name,
        'channel_id': flat_metadata.get('channel_id', ''),
        'channel_url': flat_metadata.get('channel_url', ''),
        'channel_follower_count': flat_metadata.get('channel_follower_count')
    }
    
    # Extract content information
    content_info = {
        'description': flat_metadata.get('description', ''),
        'language': flat_metadata.get('language', ''),
        'license': flat_metadata.get('license', ''),
        'tags': flat_metadata.get('tags', []),
        'categories': flat_metadata.get('categories', [])
    }
    
    # Extract technical information
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
    
    # Extract statistics
    statistics = {
        'view_count': flat_metadata.get('view_count', 0),
        'like_count': flat_metadata.get('like_count', 0),
        'comment_count': flat_metadata.get('comment_count', 0),
        'average_rating': flat_metadata.get('average_rating')
    }
    
    # Extract dates
    dates = {
        'upload_date': flat_metadata.get('upload_date', ''),
        'release_date': flat_metadata.get('release_date', ''),
        'timestamp': flat_metadata.get('timestamp'),
        'release_timestamp': flat_metadata.get('release_timestamp')
    }
    
    # Extract media information
    media_info = {
        'artist': flat_metadata.get('artist', ''),
        'album': flat_metadata.get('album', ''),
        'track': flat_metadata.get('track', ''),
        'creator': flat_metadata.get('creator', '')
    }
    
    # Extract thumbnails
    thumbnails = {
        'thumbnail': flat_metadata.get('thumbnail', ''),
        'thumbnails': flat_metadata.get('thumbnails', [])
    }
    
    # Extract playlist information (if applicable)
    playlist_info = {}
    if flat_metadata.get('playlist') or flat_metadata.get('playlist_id'):
        playlist_info = {
            'playlist': flat_metadata.get('playlist', ''),
            'playlist_id': flat_metadata.get('playlist_id', ''),
            'playlist_title': flat_metadata.get('playlist_title', ''),
            'playlist_index': flat_metadata.get('playlist_index'),
            'n_entries': flat_metadata.get('n_entries')
        }
    
    # Extract subtitles information
    subtitles_info = {
        'subtitles': flat_metadata.get('subtitles', {}),
        'automatic_captions': flat_metadata.get('automatic_captions', {})
    }
    
    # Extract live information
    live_info = {
        'live_status': flat_metadata.get('live_status', ''),
        'was_live': flat_metadata.get('was_live', False),
        'is_live': flat_metadata.get('is_live', False)
    }
    
    # Build structured metadata
    structured = {
        # Top-level fields
        'title': flat_metadata.get('title', ''),
        'description': flat_metadata.get('description', ''),
        'duration': flat_metadata.get('duration', 0),
        'youtube_url': flat_metadata.get('youtube_url', ''),
        'youtube_short_url': flat_metadata.get('youtube_short_url', ''),
        'youtube_id': flat_metadata.get('youtube_id', ''),
        'uploader': flat_metadata.get('uploader', ''),
        'uploader_id': flat_metadata.get('uploader_id', ''),
        'uploader_url': flat_metadata.get('uploader_url', ''),
        
        # Nested structures
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
        
        # Additional fields
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
        
        # Keep full original metadata for reference
        'youtube_metadata': flat_metadata
    }
    
    # Remove None values from nested structures to keep JSON clean
    for key in ['playlist_info', 'channel_info', 'content_info', 'technical_info', 
                'statistics', 'dates', 'media_info', 'thumbnails', 'subtitles_info', 'live_info']:
        if structured.get(key) is None:
            del structured[key]
        elif isinstance(structured.get(key), dict):
            # Remove None values from nested dicts
            structured[key] = {k: v for k, v in structured[key].items() if v is not None}
    
    return structured


@app.post("/api/fileupload", response_model=UploadResponse)
async def upload_file_to_ipfs(
    file: UploadFile = File(...),
    npub: str = Form(...),  # Seule npub ou hex est acceptée
    youtube_metadata: Optional[UploadFile] = File(None)  # Optional YouTube metadata JSON file
):
    """
    Upload file to IPFS with NIP-42 authentication.
    Places file in appropriate IPFS structure based on file type.
    For images, generates AI description and renames file accordingly.
    """
    # Verify NIP-42 authentication with force_check to ensure fresh validation
    logging.info(f"🔐 Vérification NIP-42 pour upload (force_check=True)")
    npub = await require_nostr_auth(npub, force_check=True)

    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    try:
        # Validate file size according to UPlanet_FILE_CONTRACT.md section 6.2
        max_size_bytes = get_max_file_size_for_user(npub)
        if file.size and file.size > max_size_bytes:
            max_size_mb = max_size_bytes // 1048576
            file_size_mb = file.size // 1048576
            raise HTTPException(
                status_code=413,
                detail=f"File size ({file_size_mb}MB) exceeds maximum allowed size ({max_size_mb}MB per UPlanet_FILE_CONTRACT.md)"
            )
        
        # Additional validation using validate_uploaded_file for MIME type and content safety
        max_size_mb = max_size_bytes // 1048576
        validation_result = await validate_uploaded_file(file, max_size_mb=max_size_mb)
        if not validation_result["is_valid"]:
            raise HTTPException(status_code=400, detail=validation_result["error"])
        
        # Get user directory for file placement
        user_NOSTR_path = get_authenticated_user_directory(npub)
        user_drive_path = user_NOSTR_path  / "APP" / "uDRIVE"
        
        # Determine file type and target directory
        file_content = await file.read()
        file_type = detect_file_type(file_content, file.filename or "untitled")
        
        # Special handling for ALL .txt files - check if it's a Netscape cookie file
        if file.filename and file.filename.endswith('.txt'):
            try:
                # Try to decode as text to validate it's a text file
                content_text = file_content.decode('utf-8')
                
                # Check if it's a Netscape cookie file format
                is_netscape_format = False
                detected_domain = None
                
                if '# Netscape HTTP Cookie File' in content_text or '# HTTP Cookie File' in content_text:
                    is_netscape_format = True
                    logging.info("✅ Detected Netscape cookie file format (header)")
                elif '\t' in content_text:
                    # Check if lines have tab-separated values (cookie format)
                    lines = [l.strip() for l in content_text.split('\n') if l.strip() and not l.strip().startswith('#')]
                    if lines:
                        # Check first data line for tab-separated cookie format
                        first_line = lines[0]
                        parts = first_line.split('\t')
                        if len(parts) >= 7:  # domain, flag, path, secure, expiration, name, value
                            is_netscape_format = True
                            logging.info("✅ Detected cookie file format (tab-separated, 7+ columns)")
                
                if is_netscape_format:
                    # Extract domain from cookie file content
                    # Parse all cookie lines to find domains
                    domains = set()
                    lines = content_text.split('\n')
                    for line in lines:
                        line = line.strip()
                        # Skip comments and empty lines
                        if not line or line.startswith('#'):
                            continue
                        # Parse cookie line (domain is first field)
                        parts = line.split('\t')
                        if len(parts) >= 7:
                            domain = parts[0].strip()
                            # Remove leading dot from domain (e.g., .youtube.com -> youtube.com)
                            if domain.startswith('.'):
                                domain = domain[1:]
                            domains.add(domain)
                    
                    # Analyze domains - ONLY single-domain cookies are accepted
                    if domains:
                        # Get base domains (remove subdomains for comparison)
                        base_domains = set()
                        for domain in domains:
                            # Get the main domain (last 2 parts: example.com from music.example.com)
                            parts = domain.split('.')
                            if len(parts) >= 2:
                                base_domain = '.'.join(parts[-2:])
                                base_domains.add(base_domain)
                            else:
                                base_domains.add(domain)
                        
                        # REJECT multi-domain cookie files
                        if len(base_domains) > 1:
                            logging.warning(f"❌ Multi-domain cookie file rejected: {', '.join(sorted(base_domains))}")
                            raise HTTPException(
                                status_code=400, 
                                detail=f"Multi-domain cookie files are not supported. Please export cookies for a single domain only. Detected domains: {', '.join(sorted(base_domains))}"
                            )
                        
                        # Single domain (or subdomains of same domain)
                        # Sort domains by length (shorter = more general, e.g., youtube.com vs music.youtube.com)
                        sorted_domains = sorted(domains, key=len)
                        detected_domain = sorted_domains[0]
                        logging.info(f"🌐 Detected single-domain cookie: {detected_domain}")
                    else:
                        logging.error("❌ No domains found in cookie file")
                        raise HTTPException(
                            status_code=400, 
                            detail="Invalid cookie file: no domains detected"
                        )
                    
                    # Get the user's root directory (parent of APP)
                    hex_pubkey = npub_to_hex(npub)
                    user_root_dir = find_user_directory_by_hex(hex_pubkey)
                    
                    # Save cookie file with domain-specific name (hidden file with leading dot)
                    # Cookies are saved directly in user's NOSTR root directory
                    # Format: .domain.cookie (e.g., .leboncoin.fr.cookie, .youtube.com.cookie)
                    cookie_filename = f".{detected_domain}.cookie"
                    cookie_path = user_root_dir / cookie_filename
                    
                    # Save cookie file
                    async with aiofiles.open(cookie_path, 'wb') as cookie_file:
                        await cookie_file.write(file_content)
                    
                    # Set restrictive permissions (600 = owner read/write only)
                    os.chmod(cookie_path, 0o600)
                    logging.info(f"✅ Cookie file saved to: {cookie_path} (permissions: 600)")
                    
                    # Build user-friendly message
                    domain_message = f"{detected_domain}"
                    if 'youtube' in detected_domain.lower():
                        domain_message += " - YouTube downloads will now use your authentication"
                    elif 'leboncoin' in detected_domain.lower():
                        domain_message += " - Leboncoin scraping will now use your authentication"
                    else:
                        domain_message += " - Services for this domain will now use your authentication"
                    
                    # Return success response without generating IPFS structure
                    return UploadResponse(
                        success=True,
                        message=f"Cookie file uploaded successfully for {domain_message}",
                        file_path=str(cookie_path.relative_to(user_root_dir.parent)),
                        file_type="netscape_cookies",
                        target_directory=str(user_root_dir),
                        new_cid=None,  # No IPFS generation for sensitive cookie files
                        timestamp=datetime.now().isoformat(),
                        auth_verified=True,
                        description=f"Domain: {detected_domain or 'unknown'}"
                    )
                else:
                    # Not a cookie file, will be processed as normal text file below
                    logging.info(f"📄 Text file '{file.filename}' is not Netscape format, treating as regular file")
            except UnicodeDecodeError:
                logging.warning("File is not valid UTF-8 text, treating as binary file")
            except Exception as e:
                logging.warning(f"Error checking cookie format: {e}, treating as regular file")
        
        # Use uDRIVE directory structure (Images, Music, Videos, Documents, Apps)
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
            target_dir = user_drive_path / "Documents"  # Default to Documents
        
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # Sanitize filename (keep original filename, don't rename)
        original_filename = file.filename if file.filename else "untitled_file"
        sanitized_filename = sanitize_filename_python(original_filename)
        
        # DEBUG: Log file type detection
        logging.info(f"📂 File type detected: '{file_type}' for file '{original_filename}'")
        
        # For images, generate AI description 
        description = None
        if file_type == 'image':
            try:
                logging.info(f"🎨 Starting AI description generation for: {sanitized_filename}")
                
                # Save temporary file first
                temp_image_path = target_dir / f"temp_{uuid.uuid4()}_{sanitized_filename}"
                async with aiofiles.open(temp_image_path, 'wb') as out_file:
                    await out_file.write(file_content)
                logging.info(f"💾 Temporary file saved: {temp_image_path}")
                
                # Generate image description using describe_image.py with LOCAL FILE
                describe_script = os.path.join(os.path.expanduser("~"), ".zen", "Astroport.ONE", "IA", "describe_image.py")
                logging.info(f"🤖 Calling describe_image.py: {describe_script}")
                
                # Get AI description with custom prompt for description generation
                # Pass the local file path directly (no IPFS upload needed)
                custom_prompt = "Décris ce qui se trouve sur cette image en 10-30 mots clés concis et précis. Ne génère qu'une description courte sans phrase complète, ni introduction."
                desc_process = await asyncio.create_subprocess_exec(
                    "python3", describe_script, str(temp_image_path), "--json", "--prompt", custom_prompt,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                desc_stdout, desc_stderr = await desc_process.communicate()
                
                logging.info(f"📤 describe_image.py returned with code: {desc_process.returncode}")
                if desc_process.returncode == 0:
                    desc_json = json.loads(desc_stdout.decode())
                    description = desc_json.get('description', '')
                    
                    if description:
                        # Store AI description
                        description = description.strip()
                        logging.info(f"✅ AI description generated: {description[:100]}...")
                else:
                    stderr_msg = desc_stderr.decode().strip()
                    logging.warning(f"❌ describe_image.py failed with code {desc_process.returncode}")
                    logging.warning(f"   stderr: {stderr_msg[:200]}")  # First 200 chars of error
                    # Check if it's a missing module error (less verbose logging)
                    if "ModuleNotFoundError" in stderr_msg or "No module named" in stderr_msg:
                        logging.debug(f"AI description unavailable (module missing), using original filename")
                    else:
                        logging.warning(f"Failed to generate image description: {stderr_msg}")
                
                # Remove temporary file
                logging.info(f"🗑️ Removing temporary file: {temp_image_path}")
                if os.path.exists(temp_image_path):
                    os.remove(temp_image_path)
                    
            except Exception as e:
                logging.error(f"❌ Exception in AI description generation: {type(e).__name__}: {str(e)}")
                logging.error(f"   Traceback: {traceback.format_exc()[:500]}")
                # Continue with original filename if AI description fails
        
        # Final file path (keep original filename)
        file_path = target_dir / sanitized_filename
        
        # Save file to target directory
        async with aiofiles.open(file_path, 'wb') as out_file:
            await out_file.write(file_content)
        
        # Generate IPFS CID using the upload2ipfs.sh script
        # Use absolute path for temp file
        tmp_dir = os.path.expanduser("~/.zen/tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        temp_file_path = os.path.join(tmp_dir, f"temp_{uuid.uuid4()}.json")
        
        # Find upload2ipfs.sh script (try multiple locations)
        script_path = None
        possible_paths = [
            "./upload2ipfs.sh",
            os.path.expanduser("~/.zen/Astroport.ONE/UPassport/upload2ipfs.sh"),
            os.path.expanduser("~/workspace/AAA/UPassport/upload2ipfs.sh"),
            os.path.join(os.path.dirname(__file__), "upload2ipfs.sh")
        ]
        for path in possible_paths:
            expanded_path = os.path.expanduser(path)
            if os.path.exists(expanded_path) and os.path.isfile(expanded_path):
                script_path = os.path.abspath(expanded_path)
                break
        
        if not script_path:
            logging.error("❌ upload2ipfs.sh not found in any expected location")
            raise HTTPException(status_code=500, detail="upload2ipfs.sh script not found")
        
        logging.info(f"📜 Using upload2ipfs.sh: {script_path}")
        
        # Get user pubkey for provenance tracking (if authenticated)
        user_pubkey_hex = ""
        try:
            if npub and npub != "anonymous":
                user_pubkey_hex = npub_to_hex(npub)
                logging.info(f"🔑 Provenance tracking enabled for user: {user_pubkey_hex[:16]}...")
        except Exception as e:
            logging.warning(f"⚠️ Could not convert npub to hex for provenance: {e}")
        
        # Handle YouTube metadata if provided
        youtube_metadata_file = None
        if youtube_metadata:
            try:
                # Read YouTube metadata JSON
                youtube_metadata_content = await youtube_metadata.read()
                youtube_metadata_json = json.loads(youtube_metadata_content.decode('utf-8'))
                
                # Transform flat YouTube metadata to structured format expected by enrichTrackWithInfoJson
                structured_metadata = transform_youtube_metadata_to_structured(youtube_metadata_json)
                
                # Create temporary metadata file for upload2ipfs.sh
                # Use absolute path in tmp directory
                tmp_dir = os.path.expanduser("~/.zen/tmp")
                os.makedirs(tmp_dir, exist_ok=True)
                youtube_metadata_file = os.path.join(tmp_dir, f"youtube_metadata_{uuid.uuid4()}.json")
                
                async with aiofiles.open(youtube_metadata_file, 'w') as metadata_file:
                    await metadata_file.write(json.dumps(structured_metadata, indent=2))
                
                logging.info(f"📋 YouTube metadata file created (structured): {youtube_metadata_file}")
                logging.info(f"   - Video ID: {structured_metadata.get('youtube_id', 'N/A')}")
                channel_name = (structured_metadata.get('channel_info', {}).get('display_name') or 
                              structured_metadata.get('channel_info', {}).get('name') or
                              structured_metadata.get('channel', 'N/A'))
                logging.info(f"   - Channel: {channel_name}")
                view_count = structured_metadata.get('statistics', {}).get('view_count', 
                          structured_metadata.get('view_count', 'N/A'))
                logging.info(f"   - Views: {view_count}")
            except Exception as e:
                logging.warning(f"⚠️ Failed to process YouTube metadata: {e}")
                youtube_metadata_file = None
        
        # Call upload2ipfs.sh with metadata if available
        if youtube_metadata_file:
            # Pass metadata file via --metadata option
            return_code, last_line = await run_script(
                script_path, 
                "--metadata", youtube_metadata_file,
                str(file_path), 
                temp_file_path, 
                user_pubkey_hex
            )
            # Clean up metadata file after upload
            if os.path.exists(youtube_metadata_file):
                os.remove(youtube_metadata_file)
        else:
            # Pass user pubkey as 3rd parameter to upload2ipfs.sh (no metadata)
            return_code, last_line = await run_script(script_path, str(file_path), temp_file_path, user_pubkey_hex)
        
        if return_code == 0:
            try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip())
                
                # Get fileName from json_output (from upload2ipfs.sh) or use original filename
                response_fileName = json_output.get('fileName') or sanitized_filename
                # Get info CID from json_output (info.json metadata file)
                info_cid = json_output.get('info')
                # Get thumbnail CID from json_output (generated by upload2ipfs.sh for videos)
                thumbnail_cid = json_output.get('thumbnail_ipfs') or ''
                # Get animated GIF CID from json_output (generated by upload2ipfs.sh for videos)
                gifanim_cid = json_output.get('gifanim_ipfs') or ''
                # Get file hash from json_output (for provenance tracking) - REQUIRED
                file_hash = json_output.get('fileHash') or ''
                # Get MIME type from json_output
                mime_type = json_output.get('mimeType') or ''
                # Get duration from json_output (for videos)
                duration = json_output.get('duration')
                # Get dimensions from json_output (for videos)
                dimensions = json_output.get('dimensions') or ''
                # Get upload chain from provenance (for re-uploads)
                provenance_info = json_output.get('provenance', {})
                upload_chain = provenance_info.get('upload_chain') or ''
                is_reupload = provenance_info.get('is_reupload', False)
                
                # Publish NOSTR event using unified publish_nostr_file.sh
                # This handles all file types: NIP-94 (kind 1063) for general files, delegates to video script for videos
                file_mime = mime_type or json_output.get('mimeType', '')
                
                # Publish for non-video and non-audio files
                # - Videos are published by /webcam endpoint (kind 21/22)
                # - Audio files are published by /vocals endpoint (kind 1222/1244)
                # - Only other file types (images, documents, etc.) should get kind 1063 here
                # Now includes RE-UPLOADS to establish provenance chain for new user
                if not file_mime.startswith('video/') and not file_mime.startswith('audio/') and user_pubkey_hex:
                    if is_reupload:
                        logging.info(f"🔗 Re-upload detected - Publishing NOSTR event with provenance for new user: {response_fileName}")
                    else:
                        logging.info(f"📝 Publishing NOSTR event for {file_type} file: {response_fileName}")
                    
                    try:
                        # Get user's NOSTR secret file
                        user_dir = get_authenticated_user_directory(npub)
                        secret_file = user_dir / ".secret.nostr"
                        
                        if secret_file.exists():
                            publish_script = os.path.expanduser("~/.zen/Astroport.ONE/tools/publish_nostr_file.sh")
                            
                            if os.path.exists(publish_script):
                                # Build description for the event
                                file_type_display = file_type.capitalize()
                                event_description = f"{file_type_display}: {response_fileName}"
                                if description:
                                    event_description = f"{description}"
                                
                                # Add provenance info to description for re-uploads
                                if is_reupload:
                                    original_author = provenance_info.get('original_author', '')[:16]
                                    event_description = f"📤 Re-upload: {event_description} (Original: {original_author}...)"
                                
                                # Use unified script with --auto mode (reads upload2ipfs.sh JSON output)
                                # IMPORTANT: temp_file_path must still exist for --auto mode
                                publish_cmd = [
                                    "bash", publish_script,
                                    "--auto", temp_file_path,
                                    "--nsec", str(secret_file),
                                    "--title", response_fileName,
                                    "--description", event_description,
                                    "--json"
                                ]
                                
                                result = subprocess.run(publish_cmd, capture_output=True, text=True, timeout=30)
                                
                                if result.returncode == 0:
                                    result_json = json.loads(result.stdout)
                                    event_id = result_json.get('event_id', '')
                                    kind = result_json.get('kind', 1063)
                                    relays_success = result_json.get('relays_success', 0)
                                    if is_reupload:
                                        logging.info(f"✅ Published re-upload NOSTR event (kind {kind}): {event_id} (to {relays_success} relays)")
                                        logging.info(f"   └─ Upload chain updated with new user")
                                    else:
                                        logging.info(f"✅ Published NOSTR event (kind {kind}): {event_id} (to {relays_success} relays)")
                                else:
                                    logging.warning(f"⚠️ Failed to publish NOSTR event (exit code: {result.returncode})")
                                    logging.warning(f"⚠️ Command: {' '.join(publish_cmd)}")
                                    logging.warning(f"⚠️ Stdout: {result.stdout}")
                                    logging.warning(f"⚠️ Stderr: {result.stderr}")
                                    # Log the JSON file content for debugging
                                    try:
                                        with open(temp_file_path, 'r') as f:
                                            logging.warning(f"⚠️ JSON content: {f.read()}")
                                    except:
                                        pass
                            else:
                                logging.debug(f"⚠️ publish_nostr_file.sh not found, skipping NOSTR publication")
                        else:
                            logging.debug(f"⚠️ No secret file found, skipping NOSTR publication")
                    except Exception as e:
                        logging.warning(f"⚠️ Could not publish NOSTR event: {e}")
                else:
                    if file_mime.startswith('video/'):
                        logging.info(f"📹 Video file - kind 21/22 will be published by /webcam endpoint")
                    elif not user_pubkey_hex:
                        logging.info(f"👤 No user pubkey - skipping NOSTR publication")
                
                # Clean up temporary file AFTER NOSTR publication
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                
                return UploadResponse(
                    success=True,
                    message=f"File uploaded successfully to IPFS",
                    file_path=str(file_path),
                    file_type=file_type,
                    target_directory=str(target_dir),
                    new_cid=json_output.get('cid'),
                    timestamp=datetime.now().isoformat(),
                    auth_verified=True,
                    fileName=response_fileName,  # Filename from IPFS upload (or original)
                    description=description,  # Description for images (AI-generated)
                    info=info_cid,  # CID of info.json containing all metadata
                    thumbnail_ipfs=thumbnail_cid if thumbnail_cid else None,  # CID of thumbnail (for videos)
                    gifanim_ipfs=gifanim_cid if gifanim_cid else None,  # CID of animated GIF (for videos)
                    fileHash=file_hash if file_hash else None,  # SHA256 hash for provenance tracking
                    mimeType=mime_type if mime_type else None,  # MIME type of the file
                    duration=int(duration) if duration is not None else None,  # Duration in seconds (for videos)
                    dimensions=dimensions if dimensions else None,  # Video dimensions
                    upload_chain=upload_chain if upload_chain else None  # Upload chain for provenance
                )
                
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logging.error(f"Failed to decode JSON from temp file: {temp_file_path}, Error: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to process IPFS upload: {e}")
            finally:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
        else:
            logging.error(f"IPFS upload script failed: {last_line.strip()}")
            raise HTTPException(status_code=500, detail=f"IPFS upload failed: {last_line.strip()}")
            
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Unexpected error in fileupload: {e}")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

# uDRIVE Endpoints - Upload and Delete with NOSTR authentication
@app.post("/api/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    npub: str = Depends(require_nostr_auth)
):

    try:
        user_NOSTR_path = get_authenticated_user_directory(npub)
        user_drive_path = user_NOSTR_path / "APP" / "uDRIVE"
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error determining user directory: {e}")

    # Validation sécurisée du fichier uploadé
    # Get max file size based on MULTIPASS status (aligned with NIP-96 Discovery and UPlanet_FILE_CONTRACT.md)
    max_size_bytes = get_max_file_size_for_user(npub)
    max_size_mb = max_size_bytes // 1048576
    validation_result = await validate_uploaded_file(file, max_size_mb=max_size_mb)
    if not validation_result["is_valid"]:
        raise HTTPException(status_code=400, detail=validation_result["error"])
    
    # Sanitize the original filename provided by the client
    original_filename = file.filename if file.filename else "untitled_file"
    sanitized_filename = sanitize_filename_python(original_filename)

    # Determine target directory based on validated file type
    mime_type = validation_result["file_type"]

    # Special handling for ZIP files
    if mime_type == 'application/zip' or sanitized_filename.lower().endswith('.zip'):
        apps_dir = user_drive_path / "Apps"
        apps_dir.mkdir(parents=True, exist_ok=True)
        
        temp_zip_path = apps_dir / sanitized_filename
        
        try:
            # Save the zip file temporarily
            with open(temp_zip_path, "wb") as buffer:
                await file.seek(0)  # Rewind file pointer after validation read
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    buffer.write(chunk)
            
            # Create subdirectory for unzipping
            unzip_dir_name = sanitized_filename.rsplit('.', 1)[0]
            unzip_path = apps_dir / unzip_dir_name
            unzip_path.mkdir(exist_ok=True)

            # Unzip the file
            with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
                zip_ref.extractall(unzip_path)
            logging.info(f"Unzipped '{temp_zip_path}' to '{unzip_path}'")

        except Exception as e:
            logging.error(f"Failed to process ZIP file: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to process ZIP file: {e}")
        finally:
            # Clean up the temporary zip file
            if temp_zip_path.exists():
                temp_zip_path.unlink()

        # Regenerate IPFS structure
        ipfs_result = await run_uDRIVE_generation_script(user_drive_path)
        new_cid_info = ipfs_result.get("final_cid")
        
        return UploadResponse(
            success=True,
            message="ZIP file uploaded and extracted successfully.",
            file_path=str(unzip_path.relative_to(user_drive_path)),
            file_type="application/zip",
            target_directory="Apps",
            new_cid=new_cid_info,
            timestamp=datetime.now().isoformat(),
            auth_verified=True
        )

    # Determine target directory for other files
    target_directory_name = "Documents"  # Default
    if mime_type == 'text/html' or sanitized_filename.lower().endswith('.html'):
        target_directory_name = "Apps"
    elif mime_type.startswith("image/"):
        target_directory_name = "Images"
    elif mime_type.startswith("audio/"):
        target_directory_name = "Music"
    elif mime_type.startswith("video/"):
        target_directory_name = "Videos"
    
    target_directory = user_drive_path / target_directory_name
    target_directory.mkdir(parents=True, exist_ok=True)

    # Construct the full path and perform crucial path validation
    # Use .resolve() to get the absolute, normalized path without symlinks
    # Ensure the resolved path is indeed within the user's drive directory
    target_file_path = (target_directory / sanitized_filename).resolve()

    if not target_file_path.is_relative_to(user_drive_path.resolve()):
        raise HTTPException(status_code=400, detail="Invalid file path operation: attempted to write outside user's directory.")

    try:
        with open(target_file_path, "wb") as buffer:
            # Read the file in chunks to handle large files efficiently
            await file.seek(0)
            while True:
                chunk = await file.read(1024 * 1024)  # Read 1MB chunks
                if not chunk:
                    break
                buffer.write(chunk)
        file_size = target_file_path.stat().st_size
        logging.info(f"File '{sanitized_filename}' saved to '{target_file_path}' (Size: {file_size} bytes)")

        # CORRECTION : Appeler la fonction spécialisée run_uDRIVE_generation_script
        # qui gère le changement de répertoire de travail (cwd) pour le script.
        ipfs_result = await run_uDRIVE_generation_script(user_drive_path)
        new_cid_info = ipfs_result.get("final_cid") # Accéder à "final_cid" depuis le dictionnaire de résultat
        logging.info(f"New IPFS CID generated: {new_cid_info}")

        return UploadResponse(
            success=True,
            message="File uploaded successfully",
            file_path=str(target_file_path.relative_to(user_drive_path)),
            file_type=mime_type,
            target_directory=target_directory_name,
            new_cid=new_cid_info,
            timestamp=datetime.now().isoformat(),
            auth_verified=True
        )
    except Exception as e:
        logging.error(f"Error saving file or running IPFS script: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process file: {e}")


@app.post("/api/upload_from_drive", response_model=UploadFromDriveResponse)
async def upload_from_drive(request: UploadFromDriveRequest):
    # Log les données du propriétaire du drive source si fournies
    if request.owner_hex_pubkey or request.owner_email:
        logging.info(f"Sync from drive - Source owner: {request.owner_email} (hex: {request.owner_hex_pubkey[:12] if request.owner_hex_pubkey else 'N/A'}...)")
    
    # Verify authentication
    request.npub = await require_nostr_auth(request.npub)

    try:
        user_NOSTR_path = get_authenticated_user_directory(request.npub)
        user_drive_path = user_NOSTR_path / "APP" / "uDRIVE"

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error determining user directory: {e}")

    # Extract filename from ipfs_link (e.g., "QmHASH/filename.ext")
    # We take the last component to handle cases where the link includes a path
    parts = request.ipfs_link.split('/')
    extracted_filename = parts[-1] if parts else "downloaded_file"

    sanitized_filename = sanitize_filename_python(extracted_filename)

    # Determine target directory (e.g., from file extension)
    # We pass an empty bytes string for content as we don't have it yet,
    # so type detection will rely solely on the filename extension.
    file_type = detect_file_type(b'', sanitized_filename)

    target_directory_name = "Documents" # Default
    if file_type == "image":
        target_directory_name = "Images"
    elif file_type == "audio":
        target_directory_name = "Music"
    elif file_type == "video":
        target_directory_name = "Videos"

    target_directory = user_drive_path / target_directory_name
    target_directory.mkdir(parents=True, exist_ok=True)

    # Construct the full path and perform crucial path validation
    # Use .resolve() to get the absolute, normalized path without symlinks
    # Ensure the resolved path is indeed within the user's drive directory
    target_file_path = (target_directory / sanitized_filename).resolve()

    if not target_file_path.is_relative_to(user_drive_path):
        raise HTTPException(status_code=400, detail="Invalid file path operation: attempted to write outside user's directory.")

    # --- IMPORTANT: Placeholder for IPFS download logic ---
    # This part assumes you have `ipfs` CLI installed on your server (thanks to Astroport.ONE)
    try:
        full_ipfs_url = f"/ipfs/{request.ipfs_link}" # Construct full IPFS path for `ipfs get` if needed
        logging.info(f"Attempting to download IPFS link: {full_ipfs_url} to {target_file_path}")

        # Execute `ipfs get` command to download the file
        # -o: specify output file path
        # Note: This is a blocking call, use an asynchronous IPFS client for production.
        ipfs_get_command = ["ipfs", "get", "-o", str(target_file_path), full_ipfs_url]
        process = await asyncio.create_subprocess_exec(
            *ipfs_get_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_message = stderr.decode().strip()
            logging.error(f"IPFS download failed for {full_ipfs_url}: {error_message}")
            raise Exception(f"IPFS download failed: {error_message}")

        file_size = target_file_path.stat().st_size
        logging.info(f"File '{sanitized_filename}' downloaded from IPFS and saved to '{target_file_path}' (Size: {file_size} bytes)")

        # CORRECTION : Appeler la fonction spécialisée run_uDRIVE_generation_script
        # qui gère le changement de répertoire de travail (cwd) pour le script.
        ipfs_result = await run_uDRIVE_generation_script(user_drive_path)
        new_cid_info = ipfs_result.get("final_cid") # Accéder à "final_cid" depuis le dictionnaire de résultat
        logging.info(f"New IPFS CID generated: {new_cid_info}")

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
        logging.error(f"Error downloading from IPFS or saving file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to synchronize file: {e}")

@app.post("/api/delete", response_model=DeleteResponse)
async def delete_file(request: DeleteRequest):
    """Supprimer un fichier avec authentification NOSTR obligatoire"""
    try:
        # Vérifier que la npub est fournie
        if not request.npub or not request.npub.strip():
            raise HTTPException(
                status_code=400, 
                detail="❌ Clé publique NOSTR (npub) obligatoire pour la suppression. "
                       "Connectez-vous à NOSTR dans l'interface et réessayez."
            )
        
        # Vérifier l'authentification NOSTR (obligatoire)
        logging.info(f"Vérification NOSTR obligatoire pour suppression - npub: {request.npub}")
        request.npub = await require_nostr_auth(request.npub)
        logging.info(f"✅ Authentification NOSTR réussie pour suppression - npub: {request.npub}")
        
        # Obtenir le répertoire source basé UNIQUEMENT sur la clé publique NOSTR
        base_dir = get_authenticated_user_directory(request.npub)
        
        # Valider et nettoyer le chemin du fichier
        file_path = request.file_path.strip()
        if not file_path:
            raise HTTPException(status_code=400, detail="Chemin de fichier manquant")
        
        # Éviter les chemins dangereux
        if '..' in file_path or file_path.startswith('/') or '\\' in file_path:
            raise HTTPException(
                status_code=400, 
                detail="Chemin de fichier non sécurisé. Utilisez un chemin relatif sans '..' ou '/'."
            )
        
        # Construire le chemin complet du fichier à supprimer
        full_file_path = base_dir / file_path
        
        # Vérifier que le fichier existe
        if not full_file_path.exists():
            raise HTTPException(
                status_code=404, 
                detail=f"Fichier non trouvé: {file_path}"
            )
        
        # Vérifier que c'est bien un fichier (pas un répertoire)
        if not full_file_path.is_file():
            raise HTTPException(
                status_code=400, 
                detail=f"Le chemin spécifié n'est pas un fichier: {file_path}"
            )
        
        # Vérifier que le fichier est dans le répertoire source (sécurité)
        try:
            full_file_path.resolve().relative_to(base_dir.resolve())
        except ValueError:
            raise HTTPException(
                status_code=403, 
                detail="Le fichier n'est pas dans le répertoire source autorisé"
            )
        
        logging.info(f"Suppression authentifiée du fichier: {full_file_path}")
        logging.info(f"NOSTR npub: {request.npub}")
        logging.info(f"Authentification NOSTR: ✅ Vérifiée et obligatoire")
        
        # Supprimer le fichier
        try:
            full_file_path.unlink()
            logging.info(f"Fichier supprimé avec succès: {full_file_path}")
        except OSError as e:
            logging.error(f"Erreur lors de la suppression du fichier: {e}")
            raise HTTPException(
                status_code=500, 
                detail=f"Erreur lors de la suppression du fichier: {str(e)}"
            )
        
        # Régénérer la structure IPFS
        logging.info("Régénération de la structure IPFS après suppression...")
        try:
            ipfs_result = await run_uDRIVE_generation_script(base_dir, enable_logging=False)
            new_cid = ipfs_result.get("final_cid") if ipfs_result["success"] else None
        except Exception as e:
            logging.warning(f"Erreur lors de la régénération IPFS: {e}")
            new_cid = None
        
        response = DeleteResponse(
            success=True,
            message=f"Fichier {file_path} supprimé avec succès (authentifié NOSTR)",
            deleted_file=file_path,
            new_cid=new_cid,
            timestamp=datetime.now(timezone.utc).isoformat(),
            auth_verified=True
        )
        
        logging.info(f"Suppression authentifiée terminée avec succès. Nouveau CID: {new_cid}")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors de la suppression authentifiée: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur lors de la suppression: {str(e)}")



# --- Coinflip endpoints ---
@app.post("/coinflip/start", response_model=CoinflipStartResponse)
async def coinflip_start(payload: CoinflipStartRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    if not sid or sid not in COINFLIP_SESSIONS:
        raise HTTPException(status_code=400, detail="Unknown session")
    sess = COINFLIP_SESSIONS[sid]
    # refresh exp short time to allow play window
    exp = int(time.time()) + 300
    token = sign_token({"npub": sess["npub"], "sid": sid, "exp": exp})
    return CoinflipStartResponse(ok=True, sid=sid, exp=exp)


@app.post("/coinflip/flip", response_model=CoinflipFlipResponse)
async def coinflip_flip(payload: CoinflipFlipRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    if not sid or sid not in COINFLIP_SESSIONS:
        raise HTTPException(status_code=400, detail="Unknown session")
    sess = COINFLIP_SESSIONS[sid]
    if not sess.get("paid"):
        raise HTTPException(status_code=402, detail="Payment required")
    # cryptographically strong coin flip
    result = 'Heads' if secrets.randbits(1) == 0 else 'Tails'
    if result == 'Heads':
        sess["consecutive"] = int(sess.get("consecutive", 1)) + 1
    return CoinflipFlipResponse(ok=True, sid=sid, result=result, consecutive=int(sess["consecutive"]))


@app.post("/coinflip/payout", response_model=CoinflipPayoutResponse)
async def coinflip_payout(payload: CoinflipPayoutRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    if not sid or sid not in COINFLIP_SESSIONS:
        raise HTTPException(status_code=400, detail="Unknown session")
    sess = COINFLIP_SESSIONS[sid]
    if not sess.get("paid"):
        raise HTTPException(status_code=402, detail="Payment required")
    consecutive = int(sess.get("consecutive", 1))
    raw = 2 ** (consecutive - 1)
    # Apply MAX cap if the client provided at payment time via future extension; for now no cap
    zen_amount = raw
    g1_amount = f"{zen_amount / 10:.1f}"
    # Trigger payout from captain to player if requested
    # We reuse zen_send.sh with g1dest=PLAYER; player_id can be email or hex
    player_id = payload.player_id or ""
    script_path = os.path.join(SCRIPT_DIR, "zen_send.sh")
    # captain sender hex is the npub asserted in token
    sender_hex = data.get("npub")
    args = [str(zen_amount), "", "PLAYER", sender_hex]
    if player_id:
        args.append(player_id)
    return_code, last_line = await run_script(script_path, *args)
    if return_code != 0:
        raise HTTPException(status_code=500, detail="Payout script failed")
    # Invalidate session
    try:
        del COINFLIP_SESSIONS[sid]
    except Exception:
        pass
    return CoinflipPayoutResponse(ok=True, sid=sid, zen=zen_amount, g1_amount=g1_amount, tx=last_line.strip())

# Nouveaux modèles pour l'analyse des réseaux NOSTR N2
class N2NetworkNode(BaseModel):
    pubkey: str
    level: int  # 0 = center, 1 = N1, 2 = N2
    is_follower: bool = False  # True si cette clé suit la clé centrale
    is_followed: bool = False  # True si la clé centrale suit cette clé
    mutual: bool = False  # True si c'est un suivi mutuel
    connections: List[str] = []  # Liste des pubkeys auxquels ce nœud est connecté
    # Profile information for vocals messaging
    npub: Optional[str] = None  # Bech32 encoded public key (npub1...)
    email: Optional[str] = None  # User email from profile
    display_name: Optional[str] = None  # Display name from profile
    name: Optional[str] = None  # Name from profile
    picture: Optional[str] = None  # Profile picture URL
    about: Optional[str] = None  # About/bio from profile

class N2NetworkResponse(BaseModel):
    center_pubkey: str
    total_n1: int
    total_n2: int
    total_nodes: int
    range_mode: str  # "default" ou "full"
    nodes: List[N2NetworkNode]
    connections: List[Dict[str, str]]  # Liste des connexions {from: pubkey, to: pubkey}
    timestamp: str
    processing_time_ms: int

# Nouveaux modèles pour les liens géographiques UMAP
class UmapGeolinksResponse(BaseModel):
    success: bool
    message: str
    umap_coordinates: Dict[str, float]  # lat, lon
    umaps: Dict[str, str]  # direction -> hex_pubkey (0.01°)
    sectors: Dict[str, str]  # direction -> hex_pubkey (0.1°)
    regions: Dict[str, str]  # direction -> hex_pubkey (1°)
    total_adjacent: int
    timestamp: str
    processing_time_ms: int

@app.get("/api/umap/geolinks", response_model=UmapGeolinksResponse)
async def get_umap_geolinks_api(lat: float, lon: float):
    """
    Récupérer les liens géographiques des UMAPs, SECTORs et REGIONs adjacentes
    
    Cette route utilise le script Umap_geonostr.sh v0.4+ pour calculer les clés hex
    des entités géographiques voisines à partir des coordonnées de l'UMAP centrale.
    
    L'application cliente peut ensuite utiliser ces clés hex pour faire des
    requêtes NOSTR directement sur les relais auxquels elle est connectée.
    
    Paramètres:
    - lat: Latitude de l'UMAP centrale (format décimal, -90 à 90)
    - lon: Longitude de l'UMAP centrale (format décimal, -180 à 180)
    
    Retourne:
    - umaps: Les clés hex des 9 UMAPs (0.01°) - ~1.1 km de rayon
    - sectors: Les clés hex des 9 SECTORs (0.1°) - ~11 km de rayon
    - regions: Les clés hex des 9 REGIONs (1°) - ~111 km de rayon
    - Métadonnées: coordonnées, timestamps, performance
    
    Format v0.4+ requis avec cache hiérarchique permanent.
    """
    try:
        logging.info(f"Requête liens UMAP pour coordonnées: ({lat}, {lon})")
        
        # Récupérer les liens géographiques
        result = await get_umap_geolinks(lat, lon)
        
        if not result["success"]:
            raise HTTPException(
                status_code=500,
                detail=result["message"]
            )
        
        # Convertir en modèle de réponse
        response = UmapGeolinksResponse(
            success=True,
            message=result["message"],
            umap_coordinates=result["umap_coordinates"],
            umaps=result["umaps"],
            sectors=result["sectors"],
            regions=result["regions"],
            total_adjacent=result["total_adjacentes"],
            timestamp=result["timestamp"],
            processing_time_ms=result["processing_time_ms"]
        )
        
        logging.info(f"Liens UMAP récupérés avec succès: {result['total_adjacentes']} UMAPs adjacentes")
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur inattendue dans get_umap_geolinks_api: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Erreur interne: {str(e)}"
        )

@app.get("/api/getN2", response_model=N2NetworkResponse)
async def get_n2_network(
    request: Request,
    hex: str,
    range: str = "default",
    output: str = "json"
):
    """
    Analyser le réseau N2 (amis d'amis) d'une clé publique NOSTR
    
    Cette endpoint enrichit les nœuds avec les informations de profil NOSTR (kind 0)
    pour permettre la sélection des destinataires de messages vocaux.
    
    Paramètres:
    - hex: Clé publique en format hexadécimal (64 caractères)
    - range: "default" (seulement les connexions mutuelles) ou "full" (toutes les connexions N1)
    - output: "json" (réponse JSON) ou "html" (visualisation avec p5.js)
    
    Retourne:
    - nodes: Liste de nœuds enrichis avec:
      - npub: Clé publique en format bech32 (npub1...)
      - email: Email de l'utilisateur (si disponible dans le profil)
      - display_name: Nom d'affichage
      - name: Nom
      - picture: URL de la photo de profil
      - about: Bio/description
      - mutual: True si connexion mutuelle (peut envoyer/recevoir des vocals)
      - is_follower: True si cette personne suit l'utilisateur central
      - is_followed: True si l'utilisateur central suit cette personne
    
    Utilisation pour vocals:
    - Filtrer les nœuds avec mutual=True pour les contacts mutuels
    - Utiliser npub pour le chiffrement des messages vocaux
    - Afficher display_name/name et picture pour l'interface utilisateur
    """
    try:
        # Validation de la clé hex
        if not hex or len(hex) != 64:
            raise HTTPException(
                status_code=400,
                detail="Paramètre 'hex' requis: clé publique hexadécimale de 64 caractères"
            )
        
        # Validation du hex
        try:
            int(hex, 16)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Format hexadécimal invalide pour le paramètre 'hex'"
            )
        
        # Validation des paramètres
        if range not in ["default", "full"]:
            raise HTTPException(
                status_code=400,
                detail="Paramètre 'range' doit être 'default' ou 'full'"
            )
        
        if output not in ["json", "html"]:
            raise HTTPException(
                status_code=400,
                detail="Paramètre 'output' doit être 'json' ou 'html'"
            )
        
        logging.info(f"Analyse N2 pour {hex[:12]}... (range={range}, output={output})")
        
        # Analyser le réseau N2
        network_data = await analyze_n2_network(hex, range)
        
        # Si output=html, retourner la page de visualisation
        if output == "html":
            # Convertir les objets Pydantic en dictionnaires pour la sérialisation JSON
            serializable_data = {
                "center_pubkey": network_data["center_pubkey"],
                "total_n1": network_data["total_n1"],
                "total_n2": network_data["total_n2"],
                "total_nodes": network_data["total_nodes"],
                "range_mode": network_data["range_mode"],
                "nodes": [node.dict() for node in network_data["nodes"]],
                "connections": network_data["connections"],
                "timestamp": network_data["timestamp"],
                "processing_time_ms": network_data["processing_time_ms"]
            }
            
            return templates.TemplateResponse(
                "n2.html",
                {
                    "request": request,
                    "network_data": json.dumps(serializable_data),
                    "center_pubkey": hex,
                    "range_mode": range,
                    "total_n1": network_data["total_n1"],
                    "total_n2": network_data["total_n2"],
                    "total_nodes": network_data["total_nodes"],
                    "processing_time": network_data["processing_time_ms"]
                }
            )
        
        # Retourner la réponse JSON
        return N2NetworkResponse(**network_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors de l'analyse N2: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'analyse: {str(e)}")

@app.post("/sendmsg")
async def send_invitation_message(
    friendEmail: str = Form(...),
    friendName: str = Form(default=""),
    yourName: str = Form(default=""),
    personalMessage: str = Form(default=""),
    memberInfo: str = Form(default=""),
    relation: str = Form(default=""),
    pubkeyUpassport: str = Form(default=""),
    ulat: str = Form(default=""),
    ulon: str = Form(default=""),
    pubkey: str = Form(default=""),
    uid: str = Form(default="")
):
    """
    Envoyer une invitation UPlanet à un ami via email
    
    Ce endpoint reçoit les données du formulaire N1 et génère un message d'invitation
    personnalisé qui sera envoyé via mailjet.sh
    """
    try:
        logging.info(f"Invitation UPlanet pour: {friendEmail} de la part de: {yourName}")
        
        # Validation de l'email ami
        if not friendEmail or not friendEmail.strip():
            raise HTTPException(status_code=400, detail="Email de l'ami requis")
        
        # Validation basique de l'email
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', friendEmail):
            raise HTTPException(status_code=400, detail="Format d'email invalide")
        
        # Préparer les informations pour le message
        friend_name = friendName.strip() if friendName else "Ami"
        sender_name = yourName.strip() if yourName else "Un membre UPlanet"
        personal_msg = personalMessage.strip() if personalMessage else ""
        
        # Utiliser directement le message prérempli (déjà clair et complet)
        invitation_html = f"""
        <html>
        <head>
            <meta charset="utf-8">
            <title>Invitation UPlanet</title>
        </head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; text-align: center;">
                <h1>🌍 Invitation UPlanet</h1>
                <p>De la part de {sender_name}</p>
            </div>
            
            <div style="background-color: #f9f9f9; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <pre style="white-space: pre-wrap; font-family: Arial, sans-serif; margin: 0;">{personal_msg}</pre>
            </div>
            
            <div style="text-align: center; margin: 30px 0;">
                <a href="https://qo-op.com" style="background-color: #4CAF50; color: white; padding: 15px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">🚀 Rejoindre UPlanet</a>
            </div>
            
            <footer style="text-align: center; color: #666; font-size: 12px; margin-top: 30px;">
                <p>Ce message a été envoyé via UPlanet - Réseau social décentralisé</p>
            </footer>
        </body>
        </html>
        """
        
        # Sauvegarder le message dans un fichier temporaire
        timestamp = int(time.time())
        temp_message_file = f"/tmp/uplanet_invitation_{timestamp}.html"
        
        with open(temp_message_file, 'w', encoding='utf-8') as f:
            f.write(invitation_html)
        
        # Préparer le sujet de l'email
        subject = f"🌍 {sender_name} vous invite à rejoindre UPlanet !"
        
        # Appeler mailjet.sh pour envoyer l'email
        mailjet_script = os.path.expanduser("~/.zen/Astroport.ONE/tools/mailjet.sh")
        
        if not os.path.exists(mailjet_script):
            raise HTTPException(status_code=500, detail="Script mailjet.sh non trouvé")
        
        # Exécuter mailjet.sh
        process = await asyncio.create_subprocess_exec(
            mailjet_script,
            friendEmail,
            temp_message_file,
            subject,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        # Nettoyer le fichier temporaire
        try:
            os.remove(temp_message_file)
        except Exception as e:
            logging.warning(f"Erreur suppression fichier temp: {e}")
        
        if process.returncode == 0:
            logging.info(f"✅ Invitation envoyée avec succès à {friendEmail}")
            return JSONResponse({
                "success": True,
                "message": f"Invitation envoyée avec succès à {friend_name} ({friendEmail}) !",
                "details": {
                    "recipient": friendEmail,
                    "sender": sender_name,
                    "subject": subject
                }
            })
        else:
            error_msg = stderr.decode().strip() if stderr else "Erreur inconnue"
            logging.error(f"❌ Erreur mailjet.sh: {error_msg}")
            raise HTTPException(
                status_code=500, 
                detail=f"Erreur lors de l'envoi: {error_msg}"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors de l'envoi d'invitation: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

def create_invitation_message(
    friend_name: str,
    sender_name: str,
    personal_message: str,
    member_info: str,
    relation: str,
    pubkey_passport: str,
    wot_member_uid: str,
    wot_member_pubkey: str,
    ulat: str,
    ulon: str
) -> str:
    """Créer le message d'invitation HTML personnalisé"""
    
    # Obtenir l'URL de la gateway IPFS
    myipfs_gateway = get_myipfs_gateway()
    
    # Créer le lien vers le passport si disponible
    passport_link = ""
    if pubkey_passport:
        passport_link = f'<p>🎫 <a href="{myipfs_gateway}/ipfs/HASH/{pubkey_passport}/" target="_blank">Voir mon UPassport</a></p>'
    
    # Informations sur le membre WoT trouvé
    wot_info = ""
    if wot_member_uid and relation:
        relation_text = {
            'p2p': 'nous nous certifions mutuellement',
            'certin': 'cette personne me certifie',
            'certout': 'je certifie cette personne'
        }.get(relation.replace('🤝 Relation mutuelle (P2P)', 'p2p')
              .replace('👥 Vous suit (12P)', 'certin')
              .replace('👤 Vous suivez (P21)', 'certout'), relation)
        
        wot_info = f"""
        <div style="background-color: #f0f8ff; padding: 15px; border-radius: 8px; margin: 15px 0;">
            <h3>🔗 Connexion via la Web of Trust</h3>
            <p>J'ai trouvé <strong>{wot_member_uid}</strong> dans mon réseau de confiance Ğ1.</p>
            <p>Notre relation : {relation_text}</p>
            <p><small>Clé publique : {wot_member_pubkey[:20]}...</small></p>
        </div>
        """
    
    # Message personnel
    personal_section = ""
    if personal_message:
        personal_section = f"""
        <div style="background-color: #fff8dc; padding: 15px; border-radius: 8px; margin: 15px 0; border-left: 4px solid #ffd700;">
            <h3>💬 Message personnel de {sender_name}</h3>
            <p style="font-style: italic;">"{personal_message}"</p>
        </div>
        """
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Invitation UPlanet</title>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; text-align: center; }}
            .content {{ padding: 20px 0; }}
            .cta-button {{ display: inline-block; background: #4CAF50; color: white; padding: 15px 30px; text-decoration: none; border-radius: 5px; font-weight: bold; margin: 20px 0; }}
            .footer {{ background-color: #f5f5f5; padding: 20px; border-radius: 8px; margin-top: 30px; text-align: center; font-size: 0.9em; color: #666; }}
            .highlight {{ background-color: #e8f5e8; padding: 10px; border-radius: 5px; margin: 10px 0; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🌍 Bienvenue dans UPlanet !</h1>
            <p>Vous êtes invité(e) à rejoindre le réseau social décentralisé</p>
        </div>
        
        <div class="content">
            <h2>Bonjour {friend_name} ! 👋</h2>
            
            <p><strong>{sender_name}</strong> vous invite à découvrir <strong>UPlanet</strong>, un réseau social révolutionnaire basé sur :</p>
            
            <div class="highlight">
                <ul>
                    <li>🔐 <strong>Blockchain Ğ1</strong> - Monnaie libre et décentralisée</li>
                    <li>🌐 <strong>IPFS</strong> - Stockage distribué et censure-résistant</li>
                    <li>⚡ <strong>NOSTR</strong> - Protocole de communication décentralisé</li>
                    <li>🤝 <strong>Web of Trust</strong> - Réseau de confiance humain</li>
                </ul>
            </div>
            
            {personal_section}
            
            {wot_info}
            
            <h3>🚀 Pourquoi rejoindre UPlanet ?</h3>
            <ul>
                <li>✅ <strong>Liberté totale</strong> - Vos données vous appartiennent</li>
                <li>✅ <strong>Pas de censure</strong> - Communication libre et ouverte</li>
                <li>✅ <strong>Économie circulaire</strong> - Échanges en monnaie libre Ğ1</li>
                <li>✅ <strong>Communauté bienveillante</strong> - Basée sur la confiance mutuelle</li>
                <li>✅ <strong>Innovation technologique</strong> - À la pointe du Web3</li>
            </ul>
            
            <div style="text-align: center; margin: 30px 0;">
                <a href="{myipfs_gateway}/scan" class="cta-button">
                    🎫 Créer mon UPassport maintenant !
                </a>
            </div>
            
            {passport_link}
            
            <div style="background-color: #fff3cd; padding: 15px; border-radius: 8px; margin: 20px 0;">
                <h4>📱 Comment commencer ?</h4>
                <ol>
                    <li>Cliquez sur le bouton ci-dessus</li>
                    <li>Scannez votre QR code Ğ1 (ou créez un compte)</li>
                    <li>Obtenez votre UPassport personnalisé</li>
                    <li>Rejoignez la communauté UPlanet !</li>
                </ol>
            </div>
        </div>
        
        <div class="footer">
            <p>Cette invitation vous a été envoyée par <strong>{sender_name}</strong></p>
            <p>UPlanet - Le réseau social du futur, décentralisé et libre</p>
            <p><small>Propulsé par Astroport.ONE - Technologie blockchain Ğ1</small></p>
        </div>
    </body>
    </html>
    """
    
    return html_content

@app.post("/api/test-nostr")
async def test_nostr_auth(npub: str = Form(...)):
    """Test NOSTR authentication for a given npub"""
    try:
        logging.info(f"Test d'authentification NOSTR pour: {npub}")
        
        # Validation du format plus flexible
        is_hex_format = len(npub) == 64
        is_npub_format = npub.startswith('npub1')
        
        if not is_hex_format and not is_npub_format:
            raise HTTPException(
                status_code=400, 
                detail=f"Format de clé invalide: '{npub}'. "
                       f"Doit être soit une npub (npub1...) soit une clé hex de 64 caractères. "
                       f"Longueur actuelle: {len(npub)} caractères."
            )
        
        # Convertir vers le format hex standardisé
        if is_hex_format:
            logging.info("Format détecté: Clé publique hexadécimale")
            hex_pubkey = npub_to_hex(npub)  # Va valider et normaliser
        else:
            logging.info("Format détecté: npub (bech32)")
            hex_pubkey = npub_to_hex(npub)
            
        if not hex_pubkey:
            raise HTTPException(
                status_code=400, 
                detail=f"Impossible de convertir la clé en format hexadécimal. "
                       f"Vérifiez que {'la clé hex est valide' if is_hex_format else 'la npub est correctement formatée'}."
            )
        
        # Tester la connexion au relai
        relay_url = get_nostr_relay_url()
        logging.info(f"Test de connexion au relai: {relay_url}")
        
        try:
            # Test de connexion basique
            async with websockets.connect(relay_url, timeout=5) as websocket:
                relay_connected = True
                logging.info("✅ Connexion au relai réussie")
        except Exception as e:
            relay_connected = False
            logging.error(f"❌ Connexion au relai échouée: {e}")
        
        # Vérifier l'authentification NIP42
        auth_result = await verify_nostr_auth(hex_pubkey)  # Utiliser la clé hex validée
        
        # Vérifier la présence du fichier HEX dans le répertoire MULTIPASS
        # SÉCURITÉ: Ne divulguer ces informations QUE si NIP-42 est valide
        multipass_registered = False
        multipass_email = None
        multipass_dir = None
        hex_file_path = None
        
        # Seulement si NIP-42 est valide, on vérifie et divulgue les infos MULTIPASS
        if auth_result:
            try:
                # Chercher le répertoire MULTIPASS correspondant à cette clé hex
                nostr_base_path = Path.home() / ".zen" / "game" / "nostr"
                
                if nostr_base_path.exists():
                    # Parcourir tous les dossiers email dans nostr/
                    for email_dir in nostr_base_path.iterdir():
                        if email_dir.is_dir() and '@' in email_dir.name:
                            hex_file = email_dir / "HEX"
                            
                            if hex_file.exists():
                                try:
                                    with open(hex_file, 'r') as f:
                                        stored_hex = f.read().strip().lower()
                                    
                                    if stored_hex == hex_pubkey.lower():
                                        multipass_registered = True
                                        multipass_email = email_dir.name
                                        multipass_dir = str(email_dir)
                                        hex_file_path = str(hex_file)
                                        logging.info(f"✅ MULTIPASS trouvé pour {hex_pubkey}: {email_dir}")
                                        break
                                except Exception as e:
                                    logging.warning(f"Erreur lors de la lecture de {hex_file}: {e}")
                                    continue
            except Exception as e:
                logging.warning(f"Erreur lors de la recherche du MULTIPASS: {e}")
        else:
            # Si NIP-42 n'est pas valide, on ne révèle PAS si le MULTIPASS existe
            # Pour éviter l'énumération (sniffing)
            logging.info(f"⚠️ NIP-42 non valide pour {hex_pubkey}, informations MULTIPASS non divulguées (sécurité)")
        
        # Préparer la réponse détaillée
        response_data = {
            "input_key": npub,
            "input_format": "hex" if is_hex_format else "npub",
            "hex_pubkey": hex_pubkey,
            "relay_url": relay_url,
            "relay_connected": relay_connected,
            "auth_verified": auth_result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {
                "key_format_valid": True,
                "hex_conversion_success": hex_pubkey is not None,
                "relay_connection": relay_connected,
                "nip42_events_found": auth_result
            }
        }
        
        # Ajouter les informations MULTIPASS SEULEMENT si NIP-42 est valide
        if auth_result:
            response_data["multipass_registered"] = multipass_registered
            response_data["checks"]["multipass_hex_file_exists"] = multipass_registered
            
            # Ajouter les détails MULTIPASS si trouvés
            if multipass_registered:
                response_data["multipass_email"] = multipass_email
                response_data["multipass_directory"] = multipass_dir
                response_data["hex_file_path"] = hex_file_path
        else:
            # Si NIP-42 n'est pas valide, on ne révèle PAS l'état du MULTIPASS
            # Pour éviter l'énumération
            response_data["multipass_registered"] = None
            response_data["checks"]["multipass_hex_file_exists"] = None
        
        # Déterminer le statut global
        # SÉCURITÉ: Les informations MULTIPASS ne sont divulguées que si NIP-42 est valide
        if auth_result and multipass_registered:
            response_data["message"] = "✅ Connexion complète - NIP42 vérifié et MULTIPASS inscrit sur le relai"
            response_data["status"] = "complete"
        elif auth_result:
            # NIP-42 valide mais MULTIPASS non trouvé (on peut le dire car NIP-42 est valide)
            response_data["message"] = "⚠️ Authentification NIP42 OK mais MULTIPASS non trouvé sur le relai"
            response_data["status"] = "partial"
            response_data["recommendations"] = [
                f"Le fichier HEX n'a pas été trouvé dans ~/.zen/game/nostr/*@*/HEX pour la clé {hex_pubkey}",
                "Vérifiez que votre MULTIPASS est bien inscrit sur le relai",
                "Le répertoire MULTIPASS doit contenir un fichier HEX avec votre clé publique"
            ]
        elif relay_connected:
            # NIP-42 non valide - on ne révèle PAS si MULTIPASS existe (sécurité)
            response_data["message"] = "⚠️ Connexion au relai OK mais aucun événement NIP42 récent trouvé"
            response_data["status"] = "partial"
            response_data["recommendations"] = [
                "Vérifiez que votre client NOSTR a bien envoyé un événement d'authentification",
                "L'événement doit être de kind 22242 (NIP42)",
                "L'événement doit dater de moins de 24 heures",
                f"Vérifiez que la clé publique {hex_pubkey} correspond bien à votre identité NOSTR",
                "Une fois NIP-42 validé, les informations MULTIPASS seront vérifiées"
            ]
        else:
            response_data["message"] = "❌ Impossible de se connecter au relai NOSTR"
            response_data["status"] = "error"
            response_data["recommendations"] = [
                f"Vérifiez que le relai NOSTR est démarré sur {relay_url}",
                "Vérifiez la configuration réseau",
                "Le relai doit accepter les connexions WebSocket"
            ]
        
        return response_data
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors du test NOSTR: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur lors du test: {str(e)}")

@app.get("/api/test-nostr")
async def test_nostr_auth_get(npub: str):
    """Test NOSTR authentication for a given npub (GET version for browser testing)"""
    return await test_nostr_auth(npub)

@app.get("/api/myGPS")
async def get_my_gps_coordinates(npub: str):
    """
    Get GPS coordinates for authenticated user (requires NIP-42 authentication)
    
    This endpoint returns the user's GPS coordinates stored in ~/.zen/game/nostr/{email}/GPS
    Only accessible by users who have sent a valid NIP-42 authentication event.
    
    Args:
        npub: User's NOSTR public key (hex or npub format) for authentication
    
    Returns:
        JSON with:
        - success: bool
        - coordinates: { lat: float, lon: float } (rounded to 0.01° for UMAP precision)
        - umap_key: str (formatted as "lat,lon" for UMAP DID lookup)
        - email: str (associated email)
        - message: str (status message)
    
    Raises:
        HTTPException 403: If user is not authenticated (no recent NIP-42 event)
        HTTPException 404: If GPS file not found for this user
        HTTPException 500: If error reading GPS file
    
    Security:
        - Requires valid NIP-42 authentication (checked via verify_nostr_auth)
        - Only returns coordinates for the authenticated user
        - Does not expose coordinates of other users
    
    Example:
        GET /api/myGPS?npub=npub1abc123...
        Response: {
            "success": true,
            "coordinates": { "lat": 48.20, "lon": -2.48 },
            "umap_key": "48.20,-2.48",
            "email": "user@example.com",
            "message": "GPS coordinates retrieved successfully"
        }
    """
    try:
        # 1. Verify NIP-42 authentication (force check to ensure fresh auth)
        logging.info(f"🔐 GPS request from npub: {npub[:16]}...")
        
        is_authenticated = await verify_nostr_auth(npub, force_check=True)
        
        if not is_authenticated:
            logging.warning(f"⚠️ GPS access denied - No valid NIP-42 authentication for {npub[:16]}...")
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "authentication_required",
                    "message": "NIP-42 authentication required to access GPS coordinates",
                    "hint": "Please connect your NOSTR wallet and send an authentication event (kind 22242)"
                }
            )
        
        # 2. Convert npub to hex if needed
        pubkey_hex = npub
        if npub.startswith('npub1'):
            try:
                from nostr_sdk import PublicKey
                pubkey_hex = PublicKey.from_bech32(npub).to_hex()
            except:
                # Fallback si nostr_sdk n'est pas disponible
                pass
        
        # 3. Find email associated with this pubkey
        # Search in ~/.zen/game/nostr/ directories
        game_nostr_path = Path.home() / ".zen" / "game" / "nostr"
        
        if not game_nostr_path.exists():
            logging.error(f"❌ NOSTR game directory not found: {game_nostr_path}")
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "directory_not_found",
                    "message": "NOSTR game directory not found",
                    "path": str(game_nostr_path)
                }
            )
        
        # Search for GPS file in user directories
        gps_file_path = None
        user_email = None
        
        for email_dir in game_nostr_path.iterdir():
            if not email_dir.is_dir():
                continue
            
            # Check if this directory has a _pub.key matching our pubkey
            pub_key_file = email_dir / "HEX"
            if pub_key_file.exists():
                try:
                    stored_pubkey = pub_key_file.read_text().strip()
                    if stored_pubkey == pubkey_hex or stored_pubkey == npub:
                        # Found the matching user
                        gps_file = email_dir / "GPS"
                        if gps_file.exists():
                            gps_file_path = gps_file
                            user_email = email_dir.name
                            logging.info(f"✅ Found GPS file for {user_email}")
                            break
                except Exception as e:
                    logging.debug(f"Error reading {pub_key_file}: {e}")
                    continue
        
        if not gps_file_path:
            logging.warning(f"❌ GPS file not found for pubkey {npub[:16]}...")
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "gps_not_found",
                    "message": "GPS coordinates not found for this user",
                    "hint": "GPS coordinates are set during MULTIPASS registration"
                }
            )
        
        # 4. Read and parse GPS file
        try:
            gps_content = gps_file_path.read_text().strip()
            logging.info(f"📍 GPS file content: {gps_content}")
            
            # Parse format: LAT=48.20; LON=-2.48;
            lat = None
            lon = None
            
            for part in gps_content.split(';'):
                part = part.strip()
                if part.startswith('LAT='):
                    lat = float(part.replace('LAT=', ''))
                elif part.startswith('LON='):
                    lon = float(part.replace('LON=', ''))
            
            if lat is None or lon is None:
                raise ValueError(f"Invalid GPS format: {gps_content}")
            
            # Round to UMAP precision (0.01°)
            lat_rounded = round(lat, 2)
            lon_rounded = round(lon, 2)
            umap_key = f"{lat_rounded:.2f},{lon_rounded:.2f}"
            
            logging.info(f"✅ GPS coordinates retrieved for {user_email}: {umap_key}")
            
            return {
                "success": True,
                "coordinates": {
                    "lat": lat_rounded,
                    "lon": lon_rounded
                },
                "umap_key": umap_key,
                "email": user_email,
                "message": "GPS coordinates retrieved successfully",
                "timestamp": datetime.now().isoformat()
            }
            
        except ValueError as e:
            logging.error(f"❌ Error parsing GPS file: {e}")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "gps_parse_error",
                    "message": f"Error parsing GPS file: {str(e)}"
                }
            )
        except Exception as e:
            logging.error(f"❌ Error reading GPS file: {e}")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "gps_read_error",
                    "message": f"Error reading GPS file: {str(e)}"
                }
            )
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"❌ Unexpected error in /api/myGPS: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": f"Unexpected error: {str(e)}"
            }
        )

async def get_umap_geolinks(lat: float, lon: float) -> Dict[str, Any]:
    """
    Récupérer les liens géographiques des UMAPs, SECTORs et REGIONs adjacentes
    en utilisant Umap_geonostr.sh v0.4+
    
    Args:
        lat: Latitude de l'UMAP centrale (format décimal, -90 à 90)
        lon: Longitude de l'UMAP centrale (format décimal, -180 à 180)
    
    Returns:
        Dictionnaire contenant:
        - umaps: Dict avec 9 clés hex UMAPs (0.01° = ~1.1 km rayon)
        - sectors: Dict avec 9 clés hex SECTORs (0.1° = ~11 km rayon)
        - regions: Dict avec 9 clés hex REGIONs (1° = ~111 km rayon)
        - metadata: coordonnées, timestamps, performance
    
    Raises:
        ValueError: Si format invalide ou coordonnées hors limites
        RuntimeError: Si le script Umap_geonostr.sh échoue
        FileNotFoundError: Si le script n'est pas trouvé
    """
    start_time = time.time()
    
    try:
        # Validation des coordonnées
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise ValueError("Latitude et longitude doivent être des nombres")
        
        if lat < -90 or lat > 90:
            raise ValueError("Latitude doit être entre -90 et 90")
        
        if lon < -180 or lon > 180:
            raise ValueError("Longitude doit être entre -180 et 180")
        
        # Chemin vers le script Umap_geonostr.sh
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/Umap_geonostr.sh")
        
        if not os.path.exists(script_path):
            raise FileNotFoundError(f"Script Umap_geonostr.sh non trouvé: {script_path}")
        
        # Vérifier que le script est exécutable
        if not os.access(script_path, os.X_OK):
            os.chmod(script_path, 0o755)
            logging.info(f"Rendu exécutable le script: {script_path}")
        
        # Exécuter le script avec les coordonnées
        process = await asyncio.create_subprocess_exec(
            script_path, str(lat), str(lon),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip() if stderr else "Erreur inconnue"
            logging.error(f"Erreur Umap_geonostr.sh: {error_msg}")
            raise RuntimeError(f"Script Umap_geonostr.sh a échoué: {error_msg}")
        
        # Parser la sortie JSON du script
        try:
            raw_data = json.loads(stdout.decode().strip())
        except json.JSONDecodeError as e:
            logging.error(f"Erreur parsing JSON de Umap_geonostr.sh: {e}")
            raise ValueError(f"Sortie JSON invalide du script: {e}")
        
        # Validation du nouveau format structuré (v0.4+)
        required_sections = ['umaps', 'sectors', 'regions']
        missing_sections = [section for section in required_sections if section not in raw_data]
        
        if missing_sections:
            raise ValueError(f"Format invalide - sections manquantes: {missing_sections}. Veuillez mettre à jour Umap_geonostr.sh v0.4+")
        
        # Extraire les données
        umaps_data = raw_data['umaps']
        sectors_data = raw_data['sectors']
        regions_data = raw_data['regions']
        
        # Validation des clés dans chaque section
        expected_keys = ['north', 'south', 'east', 'west', 'northeast', 'northwest', 'southeast', 'southwest', 'here']
        
        for section_name, section_data in [('umaps', umaps_data), ('sectors', sectors_data), ('regions', regions_data)]:
            missing_keys = [key for key in expected_keys if key not in section_data]
            if missing_keys:
                raise ValueError(f"Clés manquantes dans {section_name}: {missing_keys}")
        
        # Compter les UMAPs adjacentes (exclure 'here')
        adjacent_count = len([k for k in umaps_data.keys() if k != 'here'])
        
        processing_time = int((time.time() - start_time) * 1000)
        
        return {
            "success": True,
            "message": f"Liens géographiques récupérés pour UMAP ({lat}, {lon})",
            "umap_coordinates": {"lat": lat, "lon": lon},
            "umaps": umaps_data,
            "sectors": sectors_data,
            "regions": regions_data,
            "total_adjacentes": adjacent_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "processing_time_ms": processing_time
        }
        
    except Exception as e:
        processing_time = int((time.time() - start_time) * 1000)
        logging.error(f"Erreur lors de la récupération des liens UMAP: {str(e)}")
        
        return {
            "success": False,
            "message": f"Erreur: {str(e)}",
            "umap_coordinates": {"lat": lat, "lon": lon},
            "umaps": {},
            "sectors": {},
            "regions": {},
            "total_adjacentes": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "processing_time_ms": processing_time
        }

def convert_g1_to_zen(g1_balance: str) -> str:
    """Convertir une balance Ğ1 en ẐEN en utilisant la formule (balance - 1) * 10"""
    try:
        # Nettoyer la balance (enlever les unités et espaces)
        clean_balance = g1_balance.replace('Ğ1', '').replace('G1', '').strip()
        
        # Convertir en float
        balance_float = float(clean_balance)
        
        # Appliquer la formule: (balance - 1) * 10
        zen_amount = (balance_float - 1) * 10
        
        # Retourner en format entier
        return f"{int(zen_amount)} Ẑ"
        
    except (ValueError, TypeError):
        # Si la conversion échoue, retourner la valeur originale
        return g1_balance

def generate_society_html_page(request: Request, g1pub: str, society_data: Dict[str, Any]):
    """Generate HTML page to display SOCIETY wallet transaction history using template"""
    try:
        # Extract Nostr DID data if available
        nostr_did_data = society_data.get('nostr_did_data', [])
        has_nostr_data = len(nostr_did_data) > 0
        
        return templates.TemplateResponse("society.html", {
            "request": request,
            "g1pub": g1pub,
            "total_outgoing_zen": society_data['total_outgoing_zen'],
            "total_outgoing_g1": society_data['total_outgoing_g1'],
            "total_transfers": society_data['total_transfers'],
            "transfers": society_data['transfers'],
            "timestamp": society_data['timestamp'],
            "nostr_did_data": nostr_did_data,
            "has_nostr_data": has_nostr_data,
            "nostr_count": len(nostr_did_data)
        })
    except Exception as e:
        logging.error(f"Error generating society HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_revenue_html_page(request: Request, g1pub: str, revenue_data: Dict[str, Any]):
    """Generate HTML page to display revenue history (Chiffre d'Affaires) using template"""
    try:
        return templates.TemplateResponse("revenue.html", {
            "request": request,
            "g1pub": g1pub,
            "filter_year": revenue_data.get('filter_year', 'all'),
            "total_revenue_zen": revenue_data['total_revenue_zen'],
            "total_revenue_g1": revenue_data['total_revenue_g1'],
            "total_transactions": revenue_data['total_transactions'],
            "yearly_summary": revenue_data.get('yearly_summary', []),
            "transactions": revenue_data['transactions'],
            "timestamp": revenue_data['timestamp']
        })
    except Exception as e:
        logging.error(f"Error generating revenue HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_impots_html_page(request: Request, impots_data: Dict[str, Any]):
    """Generate HTML page to display tax provisions history (TVA + IS) using template"""
    try:
        return templates.TemplateResponse("impots.html", {
            "request": request,
            "g1pub": impots_data.get('wallet', 'N/A'),
            "total_provisions_zen": impots_data['total_provisions_zen'],
            "total_provisions_g1": impots_data['total_provisions_g1'],
            "total_transactions": impots_data['total_transactions'],
            "tva_total_zen": impots_data['breakdown']['tva']['total_zen'],
            "tva_total_g1": impots_data['breakdown']['tva']['total_g1'],
            "tva_transactions": impots_data['breakdown']['tva']['transactions'],
            "is_total_zen": impots_data['breakdown']['is']['total_zen'],
            "is_total_g1": impots_data['breakdown']['is']['total_g1'],
            "is_transactions": impots_data['breakdown']['is']['transactions'],
            "provisions": impots_data['provisions']
        })
    except Exception as e:
        logging.error(f"Error generating impots HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_zencard_html_page(request: Request, email: str, zencard_data: Dict[str, Any]):
    """Generate HTML page to display ZEN Card social shares history using template"""
    try:
        return templates.TemplateResponse("zencard_api.html", {
            "request": request,
            "zencard_email": zencard_data.get('zencard_email', email),
            "zencard_g1pub": zencard_data.get('zencard_g1pub', 'N/A'),
            "filter_years": zencard_data.get('filter_years', 3),
            "filter_period": zencard_data.get('filter_period', 'Dernières 3 années'),
            "total_received_g1": zencard_data.get('total_received_g1', 0),
            "total_received_zen": zencard_data.get('total_received_zen', 0),
            "valid_balance_g1": zencard_data.get('valid_balance_g1', 0),
            "valid_balance_zen": zencard_data.get('valid_balance_zen', 0),
            "total_transfers": zencard_data.get('total_transfers', 0),
            "valid_transfers": zencard_data.get('valid_transfers', 0),
            "transfers": zencard_data.get('transfers', []),
            "timestamp": zencard_data.get('timestamp', '')
        })
    except Exception as e:
        logging.error(f"Error generating ZEN Card HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_balance_html_page(identifier: str, balance_data: Dict[str, Any]) -> HTMLResponse:
    """Générer une page HTML pour afficher les balances en utilisant le template message.html"""
    try:
        # Lire le template message.html
        template_path = Path(__file__).parent / "templates" / "message.html"
        
        if not template_path.exists():
            logging.error(f"Template message.html non trouvé: {template_path}")
            raise HTTPException(status_code=500, detail="Template HTML non trouvé")
        
        with open(template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()
        
        # Préparer le titre avec formatage amélioré
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Titre plus court et plus lisible
        if "@" in identifier:
            # Pour les emails, afficher juste l'email et les balances
            title_parts = [f"{timestamp} - {identifier}"]
            if "balance" in balance_data:
                zen_balance = convert_g1_to_zen(balance_data['balance'])
                title_parts.append(f"👛 {zen_balance}")
            if "balance_zencard" in balance_data:
                title_parts.append(f"💳")
            title = " / ".join(title_parts)
        else:
            title = f"{timestamp} - {identifier}"
        
        # Préparer le message avec les balances en HTML (converties en ẐEN)
        message_parts = []
        
        # Détecter si c'est un email avec plusieurs balances ou une g1pub simple
        has_multiple_balances = "balance_zencard" in balance_data
        
        # Helper function to get NOSTR profile URL
        def get_nostr_profile_url(email_param):
            ipfs_gateway = get_myipfs_gateway()
            hex_pubkey = None
            if email_param and "@" in email_param:
                try:
                    script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/search_for_this_email_in_nostr.sh")
                    result = subprocess.run([script_path, email_param], capture_output=True, text=True, timeout=30)
                    if result.returncode == 0:
                        last_line = result.stdout.strip().split('\n')[-1]
                        hex_match = re.search(r'HEX=([a-fA-F0-9]+)', last_line)
                        if hex_match:
                            hex_pubkey = hex_match.group(1)
                except Exception as e:
                    logging.warning(f"Could not get HEX pubkey for {email_param}: {e}")
            
            if hex_pubkey:
                return f"{ipfs_gateway}/ipns/copylaradio.com/nostr_profile_viewer.html?hex={hex_pubkey}"
            else:
                return f"{ipfs_gateway}/ipns/copylaradio.com/nostr_profile_viewer.html"
        
        if not has_multiple_balances:
            # Cas d'une g1pub simple
            zen_balance = convert_g1_to_zen(balance_data['balance'])
            email_param = identifier if "@" in identifier else balance_data.get('email', identifier)
            nostr_url = get_nostr_profile_url(email_param)
            
            # Formatage amélioré pour une seule balance - taille réduite pour le rond blanc
            message_parts.append(f"""
            <div style="text-align: center; margin: 10px 0; padding: 15px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 12px; color: white; box-shadow: 0 4px 16px rgba(0,0,0,0.2); max-width: 300px; margin-left: auto; margin-right: auto;">
                <h2 style="margin: 0 0 8px 0; font-size: 1.2em;">👛 MULTIPASS</h2>
                <div style="font-size: 1.6em; font-weight: bold; margin: 8px 0;">{zen_balance}</div>
                <a href='{nostr_url}' target='_blank' style='color: #fff; text-decoration: none; background: rgba(255,255,255,0.2); padding: 6px 12px; border-radius: 15px; display: inline-block; margin-top: 8px; font-size: 0.85em;'>🔗 Profil MULTIPASS</a>
            </div>
            """)
        else:
            # Cas d'un email avec plusieurs balances - formatage en colonnes - taille réduite
            message_parts.append("""
            <div style="display: flex; gap: 15px; justify-content: center; flex-wrap: wrap; margin: 10px 0; max-width: 600px; margin-left: auto; margin-right: auto;">
            """)
            
            if "balance" in balance_data:
                zen_balance = convert_g1_to_zen(balance_data['balance'])
                email_param = identifier if "@" in identifier else balance_data.get('email', identifier)
                nostr_url = get_nostr_profile_url(email_param)
                
                message_parts.append(f"""
                <div style="flex: 1; min-width: 200px; text-align: center; padding: 15px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 12px; color: white; box-shadow: 0 6px 24px rgba(0,0,0,0.2);">
                    <h3 style="margin: 0 0 10px 0; font-size: 1.1em;">MULTIPASS 👛</h3>
                    <div style="font-size: 1.4em; font-weight: bold; margin: 6px 0;">{zen_balance}</div>
                    <a href='{nostr_url}' target='_blank' style='color: #fff; text-decoration: none; background: rgba(255,255,255,0.2); padding: 4px 8px; border-radius: 10px; display: inline-block; margin-top: 5px; font-size: 0.8em;'>🔗 Profil NOSTR</a>
                </div>
                """)
                
                # Le titre est déjà géré dans la section précédente
            
            if "balance_zencard" in balance_data:
                zen_balance_zencard = convert_g1_to_zen(balance_data['balance_zencard'])
                email_param = identifier if "@" in identifier else balance_data.get('email', identifier)
                
                message_parts.append(f"""
                <div style="flex: 1; min-width: 180px; text-align: center; padding: 15px; background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); border-radius: 12px; color: white; box-shadow: 0 4px 16px rgba(0,0,0,0.2);">
                    <h3 style="margin: 0 0 15px 0; font-size: 1.1em;">💳 ZEN Card</h3>
                    <a href='/check_zencard?email={email_param}&html=1' target='_blank' style='color: #fff; text-decoration: none; background: rgba(255,255,255,0.2); padding: 6px 12px; border-radius: 10px; display: inline-block; margin-top: 5px; font-size: 0.8em;'>📊 Historique</a>
                </div>
                """)
                
                # Le titre est déjà géré dans la section précédente
            
            message_parts.append("</div>")
        
        message = "".join(message_parts)
        
        # Remplacer les variables dans le template
        html_content = template_content.replace("_TITLE_", title).replace("_MESSAGE_", message)
        
        return HTMLResponse(content=html_content)
        
    except Exception as e:
        logging.error(f"Erreur lors de la génération de la page HTML: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur lors de la génération HTML: {str(e)}")

def get_env_from_mysh(var_name: str, default: str = "") -> str:
    """Récupérer une variable d'environnement depuis my.sh de façon fiable
    
    Args:
        var_name: Nom de la variable à récupérer (ex: "CAPTAINEMAIL", "UPLANETNAME_G1", "IPFSNODEID")
        default: Valeur par défaut si la variable n'est pas trouvée
    
    Returns:
        La valeur de la variable ou la valeur par défaut
    """
    try:
        # Exécuter le script my.sh pour obtenir la variable
        my_sh_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/my.sh")
        
        if not os.path.exists(my_sh_path):
            logging.debug(f"Script my.sh non trouvé: {my_sh_path}, using default for {var_name}")
            return default
        
        # Utiliser bash explicitement et sourcer my.sh pour récupérer la variable
        cmd = f"bash -c 'source {my_sh_path} && echo ${var_name}'"
        
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0 and result.stdout.strip():
            value = result.stdout.strip()
            logging.debug(f"Variable {var_name} obtenue depuis my.sh: {value}")
            return value
        else:
            logging.debug(f"Variable {var_name} non trouvée dans my.sh, using default: {default}")
            return default
            
    except subprocess.TimeoutExpired:
        logging.warning(f"Timeout lors de l'exécution de my.sh pour {var_name}")
        return default
    except Exception as e:
        logging.warning(f"Erreur lors de la récupération de {var_name} depuis my.sh: {e}")
        return default

def get_myipfs_gateway() -> str:
    """Récupérer l'adresse de la gateway IPFS en utilisant my.sh"""
    return get_env_from_mysh("myIPFS", "http://localhost:8080")

async def get_n1_follows(pubkey_hex: str) -> List[str]:
    """Récupérer la liste N1 (personnes suivies) d'une clé publique"""
    try:
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/nostr_get_N1.sh")
        
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
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/nostr_followers.sh")
        
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
        nostr_script_path = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "nostr_get_events.sh"
        
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

################################################################################
# ORACLE SYSTEM (PERMIT MANAGEMENT) API ROUTES
################################################################################

# Pydantic models for permit API
class PermitDefinitionRequest(BaseModel):
    id: str
    name: str
    description: str
    min_attestations: int = 5
    required_license: Optional[str] = None
    valid_duration_days: int = 0
    revocable: bool = True
    verification_method: str = "peer_attestation"
    metadata: Dict[str, Any] = {}

class PermitApplicationRequest(BaseModel):
    permit_definition_id: str
    applicant_npub: str
    statement: str
    evidence: List[str] = []

class PermitAttestationRequest(BaseModel):
    request_id: str
    attester_npub: str
    statement: str
    attester_license_id: Optional[str] = None

class PermitDefinitionCreateRequest(BaseModel):
    permit: PermitDefinitionRequest
    npub: str  # NOSTR public key for authentication
    bootstrap_emails: Optional[List[str]] = None  # Optional: emails for bootstrap initialization

@app.post("/api/permit/define")
async def create_permit_definition(request: PermitDefinitionCreateRequest):
    """Create a new permit definition (requires NIP-42 authentication)
    
    Any authenticated user can create a new permit definition.
    The permit will be signed by UPLANETNAME_G1 authority.
    Optionally, can trigger bootstrap initialization with provided emails.
    """
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        # Verify NIP-42 authentication
        if not request.npub:
            raise HTTPException(status_code=401, detail="NOSTR authentication required (npub parameter)")
        
        if not await verify_nostr_auth(request.npub, force_check=True):
            raise HTTPException(status_code=401, detail="NOSTR authentication failed (NIP-42)")
        
        permit_req = request.permit
        
        # Check if permit ID already exists
        if permit_req.id in oracle_system.definitions:
            existing_def = oracle_system.definitions[permit_req.id]
            raise HTTPException(
                status_code=400, 
                detail=f"Permit definition {permit_req.id} already exists. Please select it from the permit list instead of creating a new one."
            )
        
        uplanet_g1_key = get_env_from_mysh("UPLANETNAME_G1", "")
        if not uplanet_g1_key:
            # Fallback to environment variable
            uplanet_g1_key = os.getenv("UPLANETNAME_G1", "")
        issuer_did = f"did:nostr:{uplanet_g1_key[:16]}" if uplanet_g1_key else "did:nostr:unknown"
        
        # Calculate min_attestations based on number of competencies if not provided
        min_attestations = permit_req.min_attestations
        if min_attestations == 5:  # Default value, recalculate if competencies provided
            competencies = permit_req.metadata.get("competencies", [])
            if competencies and len(competencies) > 0:
                # Base: 2 + 1 per competency (minimum 2 for bootstrap)
                min_attestations = max(2, 2 + len(competencies))
        
        definition = PermitDefinition(
            id=permit_req.id,
            name=permit_req.name,
            description=permit_req.description,
            issuer_did=issuer_did,
            min_attestations=min_attestations,
            required_license=permit_req.required_license,
            valid_duration_days=permit_req.valid_duration_days,
            revocable=permit_req.revocable,
            verification_method=permit_req.verification_method,
            metadata=permit_req.metadata
        )
        
        # Pass creator npub to save event in their MULTIPASS directory
        success = oracle_system.create_permit_definition(definition, creator_npub=request.npub)
        
        if success:
            response_data = {
                "success": True,
                "message": f"Permit definition {permit_req.id} created",
                "definition_id": permit_req.id,
                "min_attestations": min_attestations
            }
            
            # Optionally trigger bootstrap if emails provided
            if request.bootstrap_emails and len(request.bootstrap_emails) >= 2:
                try:
                    # Launch bootstrap script in background
                    script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/oracle.WoT_PERMIT.init.sh")
                    if os.path.exists(script_path):
                        bootstrap_emails_str = " ".join(request.bootstrap_emails)
                        # Run bootstrap script asynchronously
                        asyncio.create_task(run_script(
                            script_path,
                            permit_req.id,
                            *request.bootstrap_emails,
                            log_file_path=os.path.expanduser(f"~/.zen/tmp/bootstrap_{permit_req.id}.log")
                        ))
                        response_data["bootstrap_initiated"] = True
                        response_data["bootstrap_emails"] = request.bootstrap_emails
                        logging.info(f"Bootstrap initiated for {permit_req.id} with {len(request.bootstrap_emails)} emails")
                except Exception as e:
                    logging.error(f"Failed to initiate bootstrap: {e}")
                    response_data["bootstrap_error"] = str(e)
            
            return JSONResponse(response_data)
        else:
            raise HTTPException(status_code=400, detail="Failed to create permit definition")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error creating permit definition: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# NOTE: /api/permit/request is REMOVED - Permit requests (30501) must be created directly by MULTIPASS
# via Nostr events in wotx2.html. The API only handles:
# - 30500: Permit definitions (created by UPLANETNAME_G1 via /api/permit/define)
# - 30503: Permit credentials (issued by UPLANETNAME_G1 via /api/permit/issue)

# NOTE: /api/permit/attest is REMOVED - Permit attestations (30502) must be created directly by MULTIPASS
# via Nostr events in wotx2.html. The API only handles:
# - 30500: Permit definitions (created by UPLANETNAME_G1 via /api/permit/define)
# - 30503: Permit credentials (issued by UPLANETNAME_G1 via /api/permit/issue)

# NOTE: /api/permit/status and /api/permit/list are REMOVED (v2.1)
# Permit requests (30501) are now stored in Nostr, not in oracle_system.requests
# Use /api/permit/nostr/fetch?kind=30501 to fetch requests from Nostr
# Use /api/permit/nostr/fetch?kind=30503 to fetch credentials from Nostr

@app.get("/api/permit/credential/{credential_id}")
async def get_permit_credential(credential_id: str):
    """Get a specific permit credential (Verifiable Credential)"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        credential = oracle_system.credentials.get(credential_id)
        
        if not credential:
            raise HTTPException(status_code=404, detail="Credential not found")
        
        definition = oracle_system.definitions.get(credential.permit_definition_id)
        
        # Build W3C Verifiable Credential format
        vc = {
            "@context": [
                "https://www.w3.org/2018/credentials/v1",
                "https://w3id.org/security/v2",
                "https://uplanet.copylaradio.com/credentials/v1"
            ],
            "id": f"urn:uuid:{credential.credential_id}",
            "type": ["VerifiableCredential", "UPlanetLicense"],
            "issuer": credential.issued_by,
            "issuanceDate": credential.issued_at.isoformat(),
            "expirationDate": credential.expires_at.isoformat() if credential.expires_at else None,
            "credentialSubject": {
                "id": credential.holder_did,
                "license": credential.permit_definition_id,
                "licenseName": definition.name if definition else "Unknown",
                "holderNpub": credential.holder_npub,
                "attestationsCount": len(credential.attestations),
                "status": credential.status.value
            },
            "proof": credential.proof
        }
        
        return JSONResponse(vc)
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error getting credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/permit/definitions")
async def list_permit_definitions():
    """List all available permit definitions (loaded from NOSTR)"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        # Load from NOSTR if definitions are empty
        if len(oracle_system.definitions) == 0:
            try:
                definitions_nostr = oracle_system.fetch_permit_definitions_from_nostr()
                for definition in definitions_nostr:
                    oracle_system.definitions[definition.id] = definition
                
                if definitions_nostr:
                    oracle_system.save_data()
                    logging.info(f"✅ Loaded {len(definitions_nostr)} permit definitions from NOSTR")
            except Exception as e:
                logging.warning(f"⚠️  Could not fetch definitions from NOSTR: {e}")
        
        definitions = [
            {
                "id": d.id,
                "name": d.name,
                "description": d.description,
                "min_attestations": d.min_attestations,
                "required_license": d.required_license,
                "valid_duration_days": d.valid_duration_days,
                "verification_method": d.verification_method
            }
            for d in oracle_system.definitions.values()
        ]
        
        return JSONResponse({
            "success": True,
            "count": len(definitions),
            "definitions": definitions
        })
    
    except Exception as e:
        logging.error(f"Error listing definitions: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/permit/stats")
async def get_permit_statistics():
    """Get public statistics for all permits (no authentication required)
    
    Returns counts of holders, pending requests, and permit details for public viewing.
    """
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        # Fetch definitions from Nostr if needed
        if len(oracle_system.definitions) == 0:
            try:
                definitions_nostr = oracle_system.fetch_permit_definitions_from_nostr()
                for definition in definitions_nostr:
                    oracle_system.definitions[definition.id] = definition
                if definitions_nostr:
                    oracle_system.save_data()
            except Exception as e:
                logging.warning(f"Could not fetch definitions from NOSTR: {e}")
        
        # Build statistics for each permit
        permit_stats = []
        for def_id, permit_def in oracle_system.definitions.items():
            # Count holders (credentials)
            holders_count = sum(1 for cred in oracle_system.credentials.values() 
                              if cred.permit_definition_id == def_id and not cred.revoked)
            
            # Count pending requests (from Nostr - approximate)
            # Note: Since v2.1, requests are in Nostr, not oracle_system.requests
            pending_count = 0  # Will be calculated from Nostr by frontend
            
            # Count total attestations (from Nostr - approximate)
            total_attestations = 0  # Will be calculated from Nostr by frontend
            
            # Get metadata
            metadata = permit_def.metadata or {}
            competencies = metadata.get("competencies", [])
            category = metadata.get("category", "general")
            
            # Determine level based on min_attestations
            level = "Beginner"
            if permit_def.min_attestations >= 10:
                level = "Expert"
            elif permit_def.min_attestations >= 6:
                level = "Advanced"
            elif permit_def.min_attestations >= 3:
                level = "Intermediate"
            
            permit_stats.append({
                "id": def_id,
                "name": permit_def.name,
                "description": permit_def.description,
                "min_attestations": permit_def.min_attestations,
                "required_license": permit_def.required_license,
                "valid_duration_days": permit_def.valid_duration_days,
                "revocable": permit_def.revocable,
                "verification_method": permit_def.verification_method,
                "category": category,
                "competencies": competencies,
                "competencies_count": len(competencies),
                "level": level,
                "holders_count": holders_count,
                "pending_requests_count": pending_count,
                "total_attestations": total_attestations,
                "metadata": metadata
            })
        
        # Global statistics
        total_permits = len(permit_stats)
        total_holders = sum(s["holders_count"] for s in permit_stats)
        total_pending = sum(s["pending_requests_count"] for s in permit_stats)
        total_attestations = sum(s["total_attestations"] for s in permit_stats)
        
        return JSONResponse({
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "global_stats": {
                "total_permits": total_permits,
                "total_holders": total_holders,
                "total_pending_requests": total_pending,
                "total_attestations": total_attestations
            },
            "permits": permit_stats
        })
    
    except Exception as e:
        logging.error(f"Error getting permit statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/permit/nostr/fetch")
async def fetch_permits_from_nostr(
    kind: Optional[int] = None, 
    type: Optional[str] = None,
    npub: Optional[str] = None
):
    """Fetch permit events from NOSTR relays
    
    Args:
        kind: Event kind (30500=definitions, 30501=requests, 30503=credentials)
        type: Alternative to kind - "definitions", "requests", or "credentials"
        npub: Optional filter by author/holder npub (hex format)
    """
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    # Convert type to kind if provided
    if type and not kind:
        type_map = {
            "definitions": 30500,
            "requests": 30501,
            "credentials": 30503
        }
        if type not in type_map:
            raise HTTPException(status_code=400, detail=f"Invalid type '{type}' (must be: definitions, requests, credentials)")
        kind = type_map[type]
    
    if not kind:
        raise HTTPException(status_code=400, detail="Either 'kind' or 'type' parameter is required")
    
    try:
        if kind == 30500:
            # Fetch permit definitions
            definitions = oracle_system.fetch_permit_definitions_from_nostr()
            return JSONResponse({
                "success": True,
                "kind": kind,
                "count": len(definitions),
                "events": [
                    {
                        "id": d.id,
                        "name": d.name,
                        "description": d.description,
                        "min_attestations": d.min_attestations
                    }
                    for d in definitions
                ]
            })
        
        elif kind == 30501:
            # Fetch permit requests
            requests = oracle_system.fetch_permit_requests_from_nostr()
            if npub:
                requests = [r for r in requests if r.applicant_npub == npub]
            
            return JSONResponse({
                "success": True,
                "kind": kind,
                "count": len(requests),
                "events": [
                    {
                        "request_id": r.request_id,
                        "permit_id": r.permit_definition_id,
                        "applicant_npub": r.applicant_npub,
                        "statement": r.statement,
                        "created_at": r.created_at.isoformat()
                    }
                    for r in requests
                ]
            })
        
        elif kind == 30503:
            # Fetch permit credentials
            credentials = oracle_system.fetch_permit_credentials_from_nostr(holder_npub=npub)
            
            return JSONResponse({
                "success": True,
                "kind": kind,
                "count": len(credentials),
                "events": [
                    {
                        "credential_id": c.credential_id,
                        "permit_id": c.permit_definition_id,
                        "holder_npub": c.holder_npub,
                        "issued_at": c.issued_at.isoformat(),
                        "expires_at": c.expires_at.isoformat() if c.expires_at else None
                    }
                    for c in credentials
                ]
            })
        
        else:
            raise HTTPException(status_code=400, detail="Invalid kind (must be 30500, 30501, or 30503)")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching from NOSTR: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/permit/issue/{request_id}")
async def issue_permit_credential(request_id: str):
    """Manually trigger credential issuance for a validated request
    
    This endpoint is idempotent and can be called by ORACLE.refresh.sh
    """
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        credential = oracle_system.issue_credential(request_id)
        
        if credential:
            return JSONResponse({
                "success": True,
                "message": "Credential issued",
                "credential_id": credential.credential_id,
                "holder_npub": credential.holder_npub,
                "permit_id": credential.permit_definition_id
            })
        else:
            # Check if already issued
            existing = None
            for cred in oracle_system.credentials.values():
                if cred.request_id == request_id:
                    existing = cred
                    break
            
            if existing:
                return JSONResponse({
                    "success": True,
                    "message": "Credential already issued",
                    "credential_id": existing.credential_id,
                    "holder_npub": existing.holder_npub,
                    "permit_id": existing.permit_definition_id
                })
            else:
                raise HTTPException(status_code=400, detail="Request not ready for issuance or not found")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error issuing credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# NOTE: /api/permit/expire is REMOVED (v2.1)
# Permit requests (30501) are now stored in Nostr, not in oracle_system.requests
# Expiration is handled by ORACLE.refresh.sh which reads from Nostr

@app.post("/api/permit/revoke/{credential_id}")
async def revoke_permit_credential(credential_id: str, reason: Optional[str] = None):
    """Revoke a permit credential"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        if credential_id not in oracle_system.credentials:
            raise HTTPException(status_code=404, detail="Credential not found")
        
        credential = oracle_system.credentials[credential_id]
        
        # Check if permit is revocable
        definition = oracle_system.definitions.get(credential.permit_definition_id)
        if definition and not definition.revocable:
            raise HTTPException(status_code=400, detail="This permit type cannot be revoked")
        
        credential.status = PermitStatus.REVOKED
        
        oracle_system.save_data()
        
        return JSONResponse({
            "success": True,
            "message": "Credential revoked",
            "credential_id": credential_id,
            "reason": reason or "No reason provided"
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error revoking credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

################################################################################
# END ORACLE SYSTEM API ROUTES
################################################################################

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
