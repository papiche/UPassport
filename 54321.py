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
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
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
import os
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

# Create global rate limiter instance
rate_limiter = RateLimiter()

# Cache pour les authentifications NOSTR (évite les requêtes répétées)
nostr_auth_cache = {}
NOSTR_CACHE_TTL = 300  # 5 minutes

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
    """Trouver le répertoire utilisateur correspondant à la clé publique hex"""
    if not hex_pubkey:
        raise HTTPException(status_code=400, detail="Clé publique hex manquante")
    
    # Normaliser la clé hex
    hex_pubkey = hex_pubkey.lower().strip()
    
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

def sanitize_filename(filename: str) -> str:
    """Nettoyer le nom de fichier pour qu'il soit sécurisé"""
    # Remplacer les caractères dangereux par des underscores
    dangerous_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '#', '|']
    clean_name = filename
    for char in dangerous_chars:
        clean_name = clean_name.replace(char, '_')
    
    # Éviter les noms commençant par un point
    if clean_name.startswith('.'):
        clean_name = 'file_' + clean_name[1:]
    
    return clean_name

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

# Backward-compatible alias used by Urbanivore endpoints --- WORK IN PROGRESS ---
async def run_Urbanivore_generation_script(source_dir: Path, enable_logging: bool = False) -> Dict[str, Any]:
    """Alias to generate IPFS structure for Urbanivore using the same uDRIVE generator."""
    return await run_uDRIVE_generation_script(source_dir, enable_logging=enable_logging)

# NOSTR and NIP42 Functions
def npub_to_hex(npub: str) -> Optional[str]:
    """Convertir une clé publique npub en format hexadécimal"""
    try:
        # Si c'est déjà du hex (64 caractères), le valider et le retourner
        if len(npub) == 64:
            try:
                int(npub, 16)  # Vérifier que c'est du hex valide
                logging.info(f"Clé publique déjà en format hex: {npub}")
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

async def verify_nostr_auth(npub: Optional[str]) -> bool:
    """Vérifier l'authentification NOSTR si une npub est fournie avec cache"""
    if not npub:
        logging.info("Aucune npub fournie, pas de vérification NOSTR")
        return False
    
    # Vérifier le cache d'abord
    current_time = time.time()
    if npub in nostr_auth_cache:
        cached_result, cached_time = nostr_auth_cache[npub]
        if current_time - cached_time < NOSTR_CACHE_TTL:
            logging.info(f"✅ Authentification NOSTR depuis le cache pour {npub}")
            return cached_result
    
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
            "audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4", "audio/webm",
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

@app.get("/scan")
async def get_root(request: Request):
    return templates.TemplateResponse("scan_new.html", {"request": request})

@app.get("/scan_multipass_payment.html")
async def get_scan_multipass_payment(request: Request):
    """MULTIPASS Payment Terminal - Internal route for authenticated payments between MULTIPASS wallets"""
    return templates.TemplateResponse("scan_multipass_payment.html", {"request": request})

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
    """Serve cookie export guide template"""
    logging.info("Serving cookie guide template")
    return templates.TemplateResponse("cookie.html", {"request": request})

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

# UPlanet G1 Registration
@app.get("/g1", response_class=HTMLResponse)
async def get_root(request: Request):
    return templates.TemplateResponse("g1nostr.html", {"request": request})

# UPlanet Oracle - Permit Management Interface
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
        
        # Get permit requests
        requests_list = []
        for req_id, req in oracle_system.requests.items():
            # Filter by npub if specified
            if npub and req.applicant_npub != npub:
                continue
            
            requests_list.append({
                "id": req_id,
                "permit_definition_id": req.permit_definition_id,
                "applicant_npub": req.applicant_npub,
                "statement": req.statement,
                "evidence": req.evidence,
                "status": req.status,
                "attestations": [
                    {
                        "attester_npub": att.attester_npub,
                        "statement": att.statement,
                        "timestamp": att.timestamp.isoformat() if att.timestamp else None,
                        "attester_license_id": att.attester_license_id
                    }
                    for att in req.attestations
                ],
                "created_at": req.created_at.isoformat() if req.created_at else None,
                "issued_credential_id": req.issued_credential_id
            })
        
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
        subscription_dir = os.path.expanduser(f"~/.zen/tmp/{os.environ.get('IPFSNODEID', 'unknown')}")
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
    """Check revenue history from RENTAL transactions (Chiffre d'Affaires)
    
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
                    "tva": {"total_g1": 0, "total_zen": 0, "transactions": 0, "description": "TVA collectée sur locations RENTAL (20%)"},
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

@app.get("/theater", response_class=HTMLResponse)
async def theater_modal_route(request: Request):
    """Theater mode modal for immersive video viewing"""
    return templates.TemplateResponse("theater-modal.html", {
        "request": request,
        "myIPFS": get_myipfs_gateway()
    })

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
    try:
        # Import the video channel functions
        import sys
        sys.path.append(os.path.expanduser("~/.zen/Astroport.ONE/IA"))
        from create_video_channel import fetch_and_process_nostr_events, create_channel_playlist
        
        # Fetch NOSTR events
        video_messages = await fetch_and_process_nostr_events("ws://127.0.0.1:7777", 200)
        
        # Validate and normalize video data
        validated_videos = []
        for video in video_messages:
            # Ensure required fields exist
            if not video.get('title') or not video.get('ipfs_url'):
                continue
            
            # Normalize field names for consistency
            # IPFS URLs are kept as CID pur for client-side gateway detection
            
            normalized_video = {
                'title': video.get('title', ''),
                'uploader': video.get('uploader', ''),
                'content': video.get('content', ''),  # Comment/description from NOSTR event (NIP-71)
                'duration': int(video.get('duration', 0)) if str(video.get('duration', 0)).isdigit() else 0,
                'ipfs_url': video.get('ipfs_url', ''),
                'youtube_url': video.get('youtube_url', '') or video.get('original_url', ''),
                'thumbnail_ipfs': video.get('thumbnail_ipfs', ''),
                'gifanim_ipfs': video.get('gifanim_ipfs', ''),  # Animated GIF CID from upload2ipfs.sh
                'metadata_ipfs': video.get('metadata_ipfs', ''),
                'subtitles': video.get('subtitles', []),
                'channel_name': video.get('channel_name', ''),
                'topic_keywords': video.get('topic_keywords', ''),
                'created_at': video.get('created_at', ''),
                'download_date': video.get('download_date', '') or video.get('created_at', ''),
                'file_size': int(video.get('file_size', 0)) if str(video.get('file_size', 0)).isdigit() else 0,
                'message_id': video.get('message_id', ''),
                'author_id': video.get('author_id', ''),
                'latitude': video.get('latitude'),  # GPS coordinates
                'longitude': video.get('longitude')  # GPS coordinates
            }
            validated_videos.append(normalized_video)
        
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
        
        # Create channel playlists
        channel_playlists = {}
        for channel_name, videos in channels.items():
            channel_playlists[channel_name] = create_channel_playlist(videos, channel_name)
        
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
            
            return templates.TemplateResponse("youtube.html", {
                "request": request,
                "youtube_data": response_data,
                "myIPFS": ipfs_gateway,
                "auto_open_video": auto_open_video
            })
        
        # Return JSON response
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logging.error(f"Error in youtube_route: {e}", exc_info=True)
        if html is not None:
            return HTMLResponse(content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", status_code=500)
        raise HTTPException(status_code=500, detail=str(e))

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

###################################################
######### REC / STOP - NODE OBS STUDIO -
# Store the OBS Studio recording process object
recording_process = None
current_player = None # Pour stocker l'email
@app.get("/rec", response_class=HTMLResponse)
async def rec_form(request: Request):
    return templates.TemplateResponse("rec_form.html", {"request": request, "recording": False})

@app.get("/webcam", response_class=HTMLResponse)
async def rec_form(request: Request):
    return templates.TemplateResponse("webcam.html", {
        "request": request, 
        "recording": False,
        "myIPFS": get_myipfs_gateway()
    })

@app.post("/webcam", response_class=HTMLResponse)
async def process_webcam_video(
    request: Request,
    player: str = Form(...),
    ipfs_cid: str = Form(...),  # IPFS CID from /api/fileupload (required)
    thumbnail_ipfs: str = Form(default=""),  # Thumbnail CID from upload2ipfs.sh (optional, centralized generation)
    gifanim_ipfs: str = Form(default=""),  # Animated GIF CID from upload2ipfs.sh (optional, centralized generation)
    info_cid: str = Form(default=""),  # Info.json CID from upload2ipfs.sh (optional, contains metadata)
    file_hash: str = Form(default=""),  # SHA256 hash from upload2ipfs.sh (required for provenance)
    mime_type: str = Form(default="video/webm"),  # MIME type from upload2ipfs.sh (default: video/webm)
    upload_chain: str = Form(default=""),  # Upload chain from upload2ipfs.sh provenance (for re-uploads)
    duration: str = Form(default="0"),  # Duration from upload2ipfs.sh (optional, for video kind determination)
    video_dimensions: str = Form(default="640x480"),  # Video dimensions from upload2ipfs.sh (optional, for imeta tag)
    title: str = Form(default=""),
    description: str = Form(default=""),
    npub: str = Form(default=""),
    publish_nostr: str = Form(default="false"),
    latitude: str = Form(default=""),
    longitude: str = Form(default="")
):
    """
    Process webcam video and publish to NOSTR as NIP-71 video event
    
    Video must be uploaded via /api/fileupload first to obtain the IPFS CID.
    This route only handles NOSTR event creation and publishing.
    """
    global recording_process, current_player
    
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
        file_size = 0
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
        
        if info_cid:
            try:
                import httpx
                gateway = get_myipfs_gateway()
                info_url = f"{gateway}/ipfs/{info_cid}/info.json"
                logging.info(f"📋 Loading metadata from info.json: {info_url}")
                
                async with httpx.AsyncClient(timeout=10.0) as client:
                    info_response = await client.get(info_url)
                    if info_response.status_code == 200:
                        info_data = info_response.json()
                        # Extract metadata from info.json
                        if info_data.get("media"):
                            media = info_data["media"]
                            if media.get("dimensions"):
                                video_dimensions = media["dimensions"]
                                logging.info(f"📐 Video dimensions from info.json: {video_dimensions}")
                            if media.get("duration"):
                                duration = int(float(media["duration"]))
                                logging.info(f"⏱️  Video duration from info.json: {duration}s")
                            # Load thumbnail and gifanim from info.json if not provided
                            if not thumbnail_ipfs and media.get("thumbnail_ipfs"):
                                thumbnail_ipfs_from_info = media["thumbnail_ipfs"]
                                logging.info(f"🖼️  Thumbnail CID from info.json: {thumbnail_ipfs_from_info}")
                            if not gifanim_ipfs and media.get("gifanim_ipfs"):
                                gifanim_ipfs_from_info = media["gifanim_ipfs"]
                                logging.info(f"🎬 Animated GIF CID from info.json: {gifanim_ipfs_from_info}")
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
                        file_size = video_files[0].stat().st_size
                        logging.info(f"✅ Selected video file: {filename} ({file_size} bytes)")
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
        
        ipfs_url = f"/ipfs/{ipfs_cid}/{filename}"
        logging.info(f"🔗 IPFS URL: {ipfs_url}")
        
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
                    publish_cmd.extend(["--upload-chain", upload_chain])
                
                publish_cmd.extend([
                    "--duration", str(duration),
                    "--dimensions", video_dimensions,
                    "--latitude", str(lat),
                    "--longitude", str(lon),
                    "--channel", player
                ])
                
                logging.info(f"🚀 Executing unified NOSTR publish script...")
                logging.info(f"📝 Title: {title}")
                logging.info(f"⏱️  Duration: {duration}s")
                logging.info(f"📍 Location: {lat:.2f}, {lon:.2f}")
                logging.info(f"🔐 File hash: {file_hash[:16] if file_hash else 'N/A'}...")
                logging.info(f"🔗 Upload chain: {upload_chain[:50] if upload_chain else 'N/A'}...")
                
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
                        # Try to extract event ID from output
                        nostr_event_id = publish_result.stdout.strip().split('\n')[-1] if publish_result.stdout else ""
                        logging.info(f"✅ NOSTR video event published: {nostr_event_id}")
                else:
                    logging.error(f"❌ Failed to publish NOSTR event (return code: {publish_result.returncode})")
                    logging.error(f"❌ stderr: {publish_result.stderr}")
                    logging.error(f"❌ stdout: {publish_result.stdout}")
                    
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

@app.post("/rec", response_class=HTMLResponse)
async def start_recording(request: Request, player: str = Form(...), link: str = Form(default=""), file: UploadFile = File(None)):
    global recording_process, current_player

    if not player:
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "No player provided. What is your email?", "recording": False})

    if not re.match(r"[^@]+@[^@]+\.[^@]+", player):
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "Invalid email address provided.", "recording": False})

    script_path = "./startrec.sh"

    # Cas 2: Upload de fichier
    if file and file.filename:
        file_size = len(await file.read())
        await file.seek(0)  # reset file pointer
        if file_size > 1024 * 1024 * 1024:
            return templates.TemplateResponse("rec_form.html", {"request": request, "error": "File size exceeds the limit of 1GB.", "recording": False})

        file_location = f"tmp/{file.filename}"
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        return_code, last_line = await run_script(script_path, player, f"upload={file_location}")

    # Cas 3: Lien YouTube
    elif link:
        return_code, last_line = await run_script(script_path, player, f"link={link}")

    # Cas 4: Enregistrement OBS
    else:
        if recording_process:
            return templates.TemplateResponse("rec_form.html", {"request": request, "error": "Recording is already in progress.", "recording": True, "current_player": current_player})

        return_code, last_line = await run_script(script_path, player)

        if return_code == 0:
            obsws_url = f"obsws://127.0.0.1:4455/{OBSkey}"
            getlog = subprocess.run(
                ["obs-cmd", "--websocket", obsws_url, "recording", "start"],
                capture_output=True, text=True
            )

            if getlog.returncode == 0:
                recording_process = True
                current_player = player
                return templates.TemplateResponse("rec_form.html", {"request": request, "message": "Recording started successfully.", "player_info": last_line.strip(), "obs_output": getlog.stdout.strip(), "recording": True, "current_player": current_player})
            else:
                return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Failed to start OBS recording. Error: {getlog.stderr.strip()}", "recording": False})

    if return_code == 0:
        return templates.TemplateResponse("rec_form.html", {"request": request, "message": f"Operation completed successfully {last_line.strip()}", "recording": False})
    else:
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": f"Script execution failed: {last_line.strip()}", "recording": False})

@app.get("/stop")
async def stop_recording(request: Request, player: Optional[str] = None):
    global recording_process
    if not recording_process:
        raise HTTPException(status_code=400, detail="No recording in progress to stop.")

    if not player:
        return {"message": "No player provided. Recording not stopped."}

    if not re.match(r"[^@]+@[^@]+\.[^@]+", player):
        raise HTTPException(status_code=400, detail="Invalid email address provided.")

    script_path = "./stoprec.sh"
    return_code, last_line = await run_script(script_path, player)

    if return_code == 0:
        recording_process = None
        return templates.TemplateResponse(
            "rec_form.html",
            {"request": request, "message": f"Operation completed successfully {last_line.strip()}", "recording": False}
        )
    else:
        return templates.TemplateResponse(
            "rec_form.html",
            {"request": request, "error": f"Script execution failed: {last_line.strip()}", "recording": False}
        )

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

@app.get("/index", response_class=HTMLResponse)
async def welcomeuplanet(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post('/ping')
async def get_webhook(request: Request):
    if request.method == 'POST':
        try:
            # Générer un nom de fichier avec un timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_filename = f"/tmp/ping_{timestamp}.log"

            # Récupérer les données de la requête
            data = await request.json()  # Récupérer le corps de la requête en JSON
            referer = request.headers.get("referer")  # Récupérer l'en-tête Referer

            # Écrire les données dans le fichier
            with open(log_filename, "w") as log_file:
                log_file.write(f"Received PING: {data}, Referer: {referer}\n")

            # Appeler le script mailjet.sh avec les arguments appropriés
            subprocess.run([
                os.path.expanduser("~/.zen/Astroport.ONE/tools/mailjet.sh"),
                "sagittarius@g1sms.fr",
                log_filename,
                "PING RECEIVED"
            ])

            # Supprimer le fichier après l'appel
            os.remove(log_filename)

            return {"received": data, "referer": referer}
        except Exception as e:
            # Supprimer le fichier en cas d'erreur (s'il existe)
            if os.path.exists(log_filename):
                os.remove(log_filename)
            raise HTTPException(status_code=400, detail=f"Invalid request data: {e}")

    else:
        raise HTTPException(status_code=400, detail="Invalid method.")

### GENERIC UPLOAD - Free & Anonymous 
@app.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request):
    return templates.TemplateResponse("upload2ipfs.html", {"request": request})

# Old NIP96 method, still used by coracle.copylaradio.com
@app.post("/upload2ipfs")
async def upload_to_ipfs(request: Request, file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    file_location = f"tmp/{file.filename}"
    try:
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        # Generate a unique temporary file path
        temp_file_path = f"tmp/temp_{uuid.uuid4()}.json"

        script_path = "./upload2ipfs.sh"
        
        # Get user pubkey for provenance tracking from NIP-98 Authorization header
        user_pubkey_hex = ""
        try:
            # Extract Authorization header (NIP-98)
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Nostr "):
                # Decode the base64-encoded NIP-98 event
                auth_base64 = auth_header.replace("Nostr ", "").strip()
                auth_json = base64.b64decode(auth_base64).decode('utf-8')
                auth_event = json.loads(auth_json)
                
                # Extract pubkey from the NIP-98 event (kind 27235)
                if auth_event.get("kind") == 27235 and "pubkey" in auth_event:
                    user_pubkey_hex = auth_event["pubkey"]
                    logging.info(f"🔑 NIP-98 Auth: Provenance tracking enabled for user: {user_pubkey_hex[:16]}...")
                else:
                    logging.warning(f"⚠️ Invalid NIP-98 event: kind={auth_event.get('kind')}")
            else:
                logging.info(f"ℹ️ No NIP-98 Authorization header, uploading without provenance tracking")
        except Exception as e:
            logging.warning(f"⚠️ Could not extract pubkey from NIP-98 Authorization header: {e}")
        
        # Pass user pubkey as 3rd parameter to upload2ipfs.sh
        return_code, last_line = await run_script(script_path, file_location, temp_file_path, user_pubkey_hex)

        if return_code == 0:
          try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip()) # Remove extra spaces/newlines

                # Delete the temporary files
                os.remove(temp_file_path)
                os.remove(file_location)
                return JSONResponse(content=json_output)
          except (json.JSONDecodeError, FileNotFoundError) as e:
                logging.error(f"Failed to decode JSON from temp file: {temp_file_path}, Error: {e}")
                return JSONResponse(
                  content={
                      "error": "Failed to process script output, JSON decode error.",
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
                    "error": f"Script execution failed.",
                    "raw_output": last_line.strip()
                  },
                  status_code=500
               )
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        return JSONResponse(
            content={
                "error": "An unexpected error occurred.",
                "exception": str(e)
                },
            status_code=500
        )

# Upload after NIP-42 NOSTR authentication
@app.post("/api/fileupload", response_model=UploadResponse)
async def upload_file_to_ipfs(
    file: UploadFile = File(...),
    npub: str = Form(...)  # Seule npub ou hex est acceptée
):
    """
    Upload file to IPFS with NIP-42 authentication.
    Places file in appropriate IPFS structure based on file type.
    For images, generates AI description and renames file accordingly.
    """
    # Verify NIP-42 authentication
    auth_verified = await verify_nostr_auth(npub)
    if not auth_verified:
        raise HTTPException(status_code=403, detail="Nostr authentication failed or not provided.")

    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    try:
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
                    # Get the user's root directory (parent of APP)
                    hex_pubkey = npub_to_hex(npub)
                    user_root_dir = find_user_directory_by_hex(hex_pubkey)
                    
                    # Save cookie file to user's root directory as .cookie.txt
                    cookie_path = user_root_dir / ".cookie.txt"
                    
                    async with aiofiles.open(cookie_path, 'wb') as cookie_file:
                        await cookie_file.write(file_content)
                    
                    logging.info(f"✅ Cookie file saved to: {cookie_path}")
                    
                    # Return success response without generating IPFS structure
                    return UploadResponse(
                        success=True,
                        message="Cookie file uploaded successfully. YouTube downloads will now use your authentication.",
                        file_path=str(cookie_path.relative_to(user_root_dir.parent)),
                        file_type="netscape_cookies",
                        target_directory=str(user_root_dir),
                        new_cid=None,  # No IPFS generation for sensitive cookie files
                        timestamp=datetime.now().isoformat(),
                        auth_verified=True
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
        temp_file_path = f"tmp/temp_{uuid.uuid4()}.json"
        script_path = "./upload2ipfs.sh"
        
        # Get user pubkey for provenance tracking (if authenticated)
        user_pubkey_hex = ""
        try:
            if npub and npub != "anonymous":
                user_pubkey_hex = npub_to_hex(npub)
                logging.info(f"🔑 Provenance tracking enabled for user: {user_pubkey_hex[:16]}...")
        except Exception as e:
            logging.warning(f"⚠️ Could not convert npub to hex for provenance: {e}")
        
        # Pass user pubkey as 3rd parameter to upload2ipfs.sh
        return_code, last_line = await run_script(script_path, str(file_path), temp_file_path, user_pubkey_hex)
        
        if return_code == 0:
            try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip())
                
                # Clean up temporary files
                os.remove(temp_file_path)
                
                # Get fileName from json_output (from upload2ipfs.sh) or use original filename
                response_fileName = json_output.get('fileName') or sanitized_filename
                # Get info CID from json_output (info.json metadata file)
                info_cid = json_output.get('info')
                # Get thumbnail CID from json_output (generated by upload2ipfs.sh for videos)
                thumbnail_cid = json_output.get('thumbnail_ipfs') or ''
                # Get animated GIF CID from json_output (generated by upload2ipfs.sh for videos)
                gifanim_cid = json_output.get('gifanim_ipfs') or ''
                
                # Publish NOSTR event using unified publish_nostr_file.sh
                # This handles all file types: NIP-94 (kind 1063) for general files, delegates to video script for videos
                file_mime = json_output.get('mimeType', '')
                provenance_info = json_output.get('provenance', {})
                is_reupload = provenance_info.get('is_reupload', False)
                
                # Only publish for non-video first uploads (videos are published by /webcam endpoint)
                # Re-uploads are skipped (provenance already established)
                if not file_mime.startswith('video/') and not is_reupload and user_pubkey_hex:
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
                                
                                # Use unified script with --auto mode (reads upload2ipfs.sh JSON output)
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
                                    logging.info(f"✅ Published NOSTR event (kind {kind}): {event_id} (to {relays_success} relays)")
                                else:
                                    logging.warning(f"⚠️ Failed to publish NOSTR event: {result.stderr}")
                            else:
                                logging.debug(f"⚠️ publish_nostr_file.sh not found, skipping NOSTR publication")
                        else:
                            logging.debug(f"⚠️ No secret file found, skipping NOSTR publication")
                    except Exception as e:
                        logging.warning(f"⚠️ Could not publish NOSTR event: {e}")
                else:
                    if file_mime.startswith('video/'):
                        logging.info(f"📹 Video file - kind 21/22 will be published by /webcam endpoint")
                    elif is_reupload:
                        logging.info(f"🔗 Re-upload detected - NOSTR event already exists, skipping publication")
                    elif not user_pubkey_hex:
                        logging.info(f"👤 No user pubkey - skipping NOSTR publication")
                
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
                    gifanim_ipfs=gifanim_cid if gifanim_cid else None  # CID of animated GIF (for videos)
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
    npub: str = Form(...)  # Seule npub ou hex est acceptée
):
    auth_verified = await verify_nostr_auth(npub)
    if not auth_verified:
        raise HTTPException(status_code=403, detail="Nostr authentication failed or not provided.")

    try:
        user_NOSTR_path = get_authenticated_user_directory(npub)
        user_drive_path = user_NOSTR_path / "APP" / "uDRIVE"
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error determining user directory: {e}")

    # Validation sécurisée du fichier uploadé
    validation_result = await validate_uploaded_file(file, max_size_mb=720)
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
            auth_verified=auth_verified
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
            auth_verified=auth_verified
        )
    except Exception as e:
        logging.error(f"Error saving file or running IPFS script: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process file: {e}")


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

@app.post("/api/upload_from_drive", response_model=UploadFromDriveResponse)
async def upload_from_drive(request: UploadFromDriveRequest):
    # Log les données du propriétaire du drive source si fournies
    if request.owner_hex_pubkey or request.owner_email:
        logging.info(f"Sync from drive - Source owner: {request.owner_email} (hex: {request.owner_hex_pubkey[:12] if request.owner_hex_pubkey else 'N/A'}...)")
    
    auth_verified = await verify_nostr_auth(request.npub)
    if not auth_verified:
        raise HTTPException(status_code=403, detail="Nostr authentication failed or not provided.")

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
            auth_verified=auth_verified
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
        auth_verified = await verify_nostr_auth(request.npub)
        
        if not auth_verified:
            logging.warning(f"❌ Authentification NOSTR échouée pour suppression - npub: {request.npub}")
            raise HTTPException(
                status_code=401,
                detail="❌ Authentification NOSTR échouée. "
                       "Vérifiez que vous êtes connecté au relai NOSTR et que votre "
                       "événement d'authentification NIP42 est récent (moins de 24h). "
                       f"Clé publique: {request.npub}"
            )
        else:
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

# Nouveaux modèles pour l'analyse des réseaux NOSTR N2
class N2NetworkNode(BaseModel):
    pubkey: str
    level: int  # 0 = center, 1 = N1, 2 = N2
    is_follower: bool = False  # True si cette clé suit la clé centrale
    is_followed: bool = False  # True si la clé centrale suit cette clé
    mutual: bool = False  # True si c'est un suivi mutuel
    connections: List[str] = []  # Liste des pubkeys auxquels ce nœud est connecté

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

# Nouveaux modèles pour les ressources Urbanivore
class UrbanivoreResource(BaseModel):
    type: str  # 'tree' ou 'recipe'
    title: str
    description: str
    latitude: float
    longitude: float
    species: Optional[str] = None  # Pour les arbres
    season: Optional[str] = None   # Pour les arbres
    difficulty: Optional[str] = None  # Pour les recettes
    time: Optional[str] = None     # Pour les recettes
    images: List[str] = []
    npub: str  # Authentification NOSTR

class UrbanivoreResourceResponse(BaseModel):
    success: bool
    message: str
    resource_id: str
    cid: str
    ipfs_url: str
    nostr_event_id: Optional[str] = None
    timestamp: str
    auth_verified: bool

class CopyProjectRequest(BaseModel):
    project_url: str  # URL IPFS du projet (ex: /ipfs/QmHASH/ ou QmHASH)
    npub: str  # Authentification NOSTR obligatoire
    project_name: Optional[str] = None  # Nom personnalisé pour le projet

class CopyProjectResponse(BaseModel):
    success: bool
    message: str
    project_name: str
    project_path: str
    files_copied: int
    new_cid: Optional[str] = None
    timestamp: str
    auth_verified: bool

@app.post("/api/urbanivore/resource", response_model=UrbanivoreResourceResponse)
async def create_urbanivore_resource(resource: UrbanivoreResource):
    """Créer une ressource Urbanivore et la publier sur IPFS + NOSTR"""
    try:
        # Vérifier l'authentification NOSTR
        auth_verified = await verify_nostr_auth(resource.npub)
        if not auth_verified:
            raise HTTPException(status_code=403, detail="Authentification NOSTR requise")
        
        # Créer le répertoire utilisateur
        user_drive_path = get_authenticated_user_directory(resource.npub)
        urbanivore_dir = user_drive_path / "APP" / "Urbanivore"
        urbanivore_dir.mkdir(parents=True, exist_ok=True)
        
        # Générer un ID unique
        resource_id = f"{resource.type}_{int(time.time())}_{resource.npub[:8]}"
        
        # Créer le fichier JSON de la ressource
        resource_data = {
            "id": resource_id,
            "type": resource.type,
            "title": resource.title,
            "description": resource.description,
            "latitude": resource.latitude,
            "longitude": resource.longitude,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "npub": resource.npub,
            "images": resource.images
        }
        
        # Ajouter les champs spécifiques
        if resource.type == "tree":
            resource_data.update({
                "species": resource.species,
                "season": resource.season
            })
        elif resource.type == "recipe":
            resource_data.update({
                "difficulty": resource.difficulty,
                "time": resource.time
            })
        
        # Sauvegarder le fichier JSON
        resource_file = urbanivore_dir / f"{resource_id}.json"
        with open(resource_file, 'w', encoding='utf-8') as f:
            json.dump(resource_data, f, indent=2, ensure_ascii=False)
        
        # Régénérer la structure IPFS
        ipfs_result = await run_Urbanivore_generation_script(user_drive_path)
        new_cid = ipfs_result.get("final_cid") if ipfs_result["success"] else None
        
        if not new_cid:
            raise HTTPException(status_code=500, detail="Erreur lors de la génération IPFS")
        
        # Construire l'URL IPFS
        ipfs_url = f"http://127.0.0.1:8080/ipfs/{new_cid}/Urbanivore/{resource_id}.json"
        
        # Publier l'événement NOSTR (optionnel)
        nostr_event_id = await publish_nostr_event(resource_data, resource.npub)
        
        return UrbanivoreResourceResponse(
            success=True,
            message=f"Ressource {resource.type} créée avec succès",
            resource_id=resource_id,
            cid=new_cid,
            ipfs_url=ipfs_url,
            nostr_event_id=nostr_event_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            auth_verified=True
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur création ressource Urbanivore: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur: {str(e)}")

async def publish_nostr_event(resource_data: dict, npub: str) -> Optional[str]:
    """Publier un événement NOSTR pour la ressource Urbanivore"""
    try:
        # Créer l'événement NOSTR
        event = {
            "kind": 1,
            "pubkey": npub,
            "created_at": int(time.time()),
            "content": f"{resource_data['type']}: {resource_data['title']}\n\n{resource_data['description']}",
            "tags": [
                ["application", "Urbanivore"],
                ["type", resource_data["type"]],
                ["latitude", str(resource_data["latitude"])],
                ["longitude", str(resource_data["longitude"])],
                ["g", f"{resource_data['latitude']};{resource_data['longitude']}"],
                ["resource_id", resource_data["id"]]
            ]
        }
        
        # Ajouter les tags spécifiques
        if resource_data["type"] == "tree":
            event["tags"].extend([
                ["species", resource_data.get("species", "")],
                ["season", resource_data.get("season", "")]
            ])
        elif resource_data["type"] == "recipe":
            event["tags"].extend([
                ["title", resource_data.get("title", "")],
                ["difficulty", resource_data.get("difficulty", "")]
            ])
        
        # Publier sur le relai local
        relay_url = get_nostr_relay_url()
        async with websockets.connect(relay_url) as websocket:
            await websocket.send(json.dumps(["EVENT", event]))
            response = await websocket.recv()
            response_data = json.loads(response)
            
            if response_data[0] == "OK" and response_data[2]:
                return event.get("id")
        
        return None
        
    except Exception as e:
        logging.warning(f"Erreur publication NOSTR: {e}")
        return None

@app.post("/api/copy_project", response_model=CopyProjectResponse)
async def copy_project_to_udrive(request: CopyProjectRequest):
    """Copier un projet IPFS complet dans l'uDRIVE de l'utilisateur comme une App"""
    try:
        # Vérifier l'authentification NOSTR
        auth_verified = await verify_nostr_auth(request.npub)
        if not auth_verified:
            raise HTTPException(status_code=403, detail="Authentification NOSTR requise")
        
        # Obtenir le répertoire utilisateur
        user_NOSTR_path = get_authenticated_user_directory(request.npub)
        user_drive_path = user_NOSTR_path / "APP" / "uDRIVE"
        apps_dir = user_drive_path / "Apps"
        apps_dir.mkdir(parents=True, exist_ok=True)
        
        # Nettoyer l'URL du projet
        project_url = request.project_url.strip()
        if project_url.startswith('/ipfs/'):
            project_url = project_url[6:]  # Enlever '/ipfs/'
        elif project_url.startswith('ipfs/'):
            project_url = project_url[5:]   # Enlever 'ipfs/'
        
        # Extraire le CID (première partie avant le slash)
        project_cid = project_url.split('/')[0]
        
        # Déterminer le nom du projet
        if request.project_name:
            project_name = sanitize_filename_python(request.project_name)
        else:
            # Utiliser le CID tronqué comme nom par défaut
            project_name = f"Project_{project_cid[:12]}"
        
        # Créer le répertoire de destination
        project_dir = apps_dir / project_name
        
        # Vérifier si le projet existe déjà
        if project_dir.exists():
            # Ajouter un suffixe numérique
            counter = 1
            while (apps_dir / f"{project_name}_{counter}").exists():
                counter += 1
            project_name = f"{project_name}_{counter}"
            project_dir = apps_dir / project_name
        
        project_dir.mkdir(parents=True, exist_ok=True)
        
        logging.info(f"Copie du projet IPFS {project_cid} vers {project_dir}")
        
        # Télécharger le projet via IPFS
        ipfs_get_command = ["ipfs", "get", f"/ipfs/{project_url}", "-o", str(project_dir)]
        
        process = await asyncio.create_subprocess_exec(
            *ipfs_get_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_message = stderr.decode().strip()
            logging.error(f"Erreur téléchargement IPFS {project_url}: {error_message}")
            
            # Nettoyer le répertoire créé en cas d'erreur
            if project_dir.exists():
                shutil.rmtree(project_dir)
            
            raise HTTPException(
                status_code=500, 
                detail=f"Erreur lors du téléchargement IPFS: {error_message}"
            )
        
        # Compter les fichiers copiés
        files_copied = 0
        for root, dirs, files in os.walk(project_dir):
            files_copied += len(files)
        
        logging.info(f"Projet copié avec succès: {files_copied} fichiers dans {project_dir}")
        
        # Régénérer la structure IPFS
        try:
            ipfs_result = await run_uDRIVE_generation_script(user_drive_path)
            new_cid = ipfs_result.get("final_cid") if ipfs_result["success"] else None
        except Exception as e:
            logging.warning(f"Erreur lors de la régénération IPFS: {e}")
            new_cid = None
        
        return CopyProjectResponse(
            success=True,
            message=f"Projet '{project_name}' copié avec succès dans vos Apps",
            project_name=project_name,
            project_path=f"Apps/{project_name}",
            files_copied=files_copied,
            new_cid=new_cid,
            timestamp=datetime.now(timezone.utc).isoformat(),
            auth_verified=True
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors de la copie du projet: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur: {str(e)}")

@app.get("/api/urbanivore/resources")
async def list_urbanivore_resources(npub: str, limit: int = 50):
    """Lister les ressources Urbanivore d'un utilisateur"""
    try:
        # Vérifier l'authentification NOSTR
        auth_verified = await verify_nostr_auth(npub)
        if not auth_verified:
            raise HTTPException(status_code=403, detail="Authentification NOSTR requise")
        
        # Obtenir le répertoire utilisateur
        user_drive_path = get_authenticated_user_directory(npub)
        urbanivore_dir = user_drive_path / "APP" / "Urbanivore"
        
        if not urbanivore_dir.exists():
            return {"resources": [], "total": 0}
        
        # Lister les fichiers JSON
        resources = []
        for json_file in urbanivore_dir.glob("*.json"):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    resource_data = json.load(f)
                    resources.append(resource_data)
            except Exception as e:
                logging.warning(f"Erreur lecture {json_file}: {e}")
        
        # Trier par date de création (plus récent en premier)
        resources.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        return {
            "resources": resources[:limit],
            "total": len(resources)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur liste ressources: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur: {str(e)}")

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
    
    Paramètres:
    - hex: Clé publique en format hexadécimal (64 caractères)
    - range: "default" (seulement les connexions mutuelles) ou "full" (toutes les connexions N1)
    - output: "json" (réponse JSON) ou "html" (visualisation avec p5.js)
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
    """Tester l'authentification NOSTR pour une npub donnée"""
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
        
        if auth_result:
            response_data["message"] = "✅ Authentification NOSTR réussie - Événements NIP42 récents trouvés"
            response_data["status"] = "success"
        elif relay_connected:
            response_data["message"] = "⚠️ Connexion au relai OK mais aucun événement NIP42 récent trouvé"
            response_data["status"] = "partial"
            response_data["recommendations"] = [
                "Vérifiez que votre client NOSTR a bien envoyé un événement d'authentification",
                "L'événement doit être de kind 22242 (NIP42)",
                "L'événement doit dater de moins de 24 heures",
                f"Vérifiez que la clé publique {hex_pubkey} correspond bien à votre identité NOSTR"
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

def get_myipfs_gateway() -> str:
    """Récupérer l'adresse de la gateway IPFS en utilisant my.sh"""
    try:
        # Exécuter le script my.sh pour obtenir la variable myIPFS
        my_sh_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/my.sh")
        
        if not os.path.exists(my_sh_path):
            logging.warning(f"Script my.sh non trouvé: {my_sh_path}")
            return "http://localhost:8080"  # Fallback
        
        # Utiliser bash explicitement et sourcer my.sh pour récupérer myIPFS
        cmd = f"bash -c 'source {my_sh_path} && echo $myIPFS'"
        
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0 and result.stdout.strip():
            myipfs = result.stdout.strip()
            logging.info(f"Gateway IPFS obtenue depuis my.sh: {myipfs}")
            return myipfs
        else:
            logging.warning(f"Erreur lors de l'exécution de my.sh: {result.stderr}")
            return "http://localhost:8080"  # Fallback
            
    except subprocess.TimeoutExpired:
        logging.error("Timeout lors de l'exécution de my.sh")
        return "http://localhost:8080"  # Fallback
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de myIPFS: {e}")
        return "http://localhost:8080"  # Fallback

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
    
    processing_time_ms = int((time.time() - start_time) * 1000)
    
    return {
        "center_pubkey": center_pubkey,
        "total_n1": len(n1_follows),
        "total_n2": len(n2_keys),
        "total_nodes": len(nodes),
        "range_mode": range_mode,
        "nodes": list(nodes.values()),
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

@app.post("/api/permit/define")
async def create_permit_definition(request: PermitDefinitionRequest):
    """Create a new permit definition (admin/UPlanet authority only)"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        uplanet_g1_key = os.getenv("UPLANETNAME_G1", "")
        issuer_did = f"did:nostr:{uplanet_g1_key[:16]}"
        
        definition = PermitDefinition(
            id=request.id,
            name=request.name,
            description=request.description,
            issuer_did=issuer_did,
            min_attestations=request.min_attestations,
            required_license=request.required_license,
            valid_duration_days=request.valid_duration_days,
            revocable=request.revocable,
            verification_method=request.verification_method,
            metadata=request.metadata
        )
        
        success = oracle_system.create_permit_definition(definition)
        
        if success:
            return JSONResponse({
                "success": True,
                "message": f"Permit definition {request.id} created",
                "definition_id": request.id
            })
        else:
            raise HTTPException(status_code=400, detail="Failed to create permit definition")
    
    except Exception as e:
        logging.error(f"Error creating permit definition: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/permit/request")
async def request_permit(request: PermitApplicationRequest):
    """Submit a permit request"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        # Verify NOSTR authentication
        if not await verify_nostr_auth(request.applicant_npub):
            raise HTTPException(status_code=401, detail="NOSTR authentication failed")
        
        # Generate request ID
        request_id = hashlib.sha256(
            f"{request.applicant_npub}:{request.permit_definition_id}:{time.time()}".encode()
        ).hexdigest()[:16]
        
        # Create permit request
        permit_request = PermitRequest(
            request_id=request_id,
            permit_definition_id=request.permit_definition_id,
            applicant_did=f"did:nostr:{request.applicant_npub}",
            applicant_npub=request.applicant_npub,
            statement=request.statement,
            evidence=request.evidence,
            status=PermitStatus.PENDING,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            attestations=[],
            nostr_event_id=None
        )
        
        success = oracle_system.request_permit(permit_request)
        
        if success:
            return JSONResponse({
                "success": True,
                "message": "Permit request submitted",
                "request_id": request_id,
                "status": "pending",
                "permit_type": request.permit_definition_id
            })
        else:
            raise HTTPException(status_code=400, detail="Failed to submit permit request")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error requesting permit: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/permit/attest")
async def attest_permit(request: PermitAttestationRequest):
    """Add an attestation to a permit request"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        # Verify NOSTR authentication
        if not await verify_nostr_auth(request.attester_npub):
            raise HTTPException(status_code=401, detail="NOSTR authentication failed")
        
        # Generate attestation ID
        attestation_id = hashlib.sha256(
            f"{request.attester_npub}:{request.request_id}:{time.time()}".encode()
        ).hexdigest()[:16]
        
        # Create attestation signature
        signature = hashlib.sha256(
            f"{request.statement}:{request.attester_npub}:{time.time()}".encode()
        ).hexdigest()
        
        # Create permit attestation
        attestation = PermitAttestation(
            attestation_id=attestation_id,
            request_id=request.request_id,
            attester_did=f"did:nostr:{request.attester_npub}",
            attester_npub=request.attester_npub,
            attester_license_id=request.attester_license_id,
            statement=request.statement,
            signature=signature,
            created_at=datetime.now(),
            nostr_event_id=None
        )
        
        success = oracle_system.attest_permit(attestation)
        
        if success:
            # Check if permit was validated and credential issued
            permit_request = oracle_system.requests.get(request.request_id)
            
            return JSONResponse({
                "success": True,
                "message": "Attestation added",
                "attestation_id": attestation_id,
                "request_id": request.request_id,
                "status": permit_request.status.value if permit_request else "unknown",
                "attestations_count": len(permit_request.attestations) if permit_request else 0
            })
        else:
            raise HTTPException(status_code=400, detail="Failed to add attestation")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error attesting permit: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/permit/status/{request_id}")
async def get_permit_status(request_id: str):
    """Get the status of a permit request"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        status = oracle_system.get_request_status(request_id)
        
        if status:
            return JSONResponse(status)
        else:
            raise HTTPException(status_code=404, detail="Permit request not found")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error getting permit status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/permit/list")
async def list_permits(type: str = "requests", npub: Optional[str] = None):
    """List permit requests or credentials"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        if type == "requests":
            results = oracle_system.list_requests(applicant_npub=npub)
        elif type == "credentials":
            results = oracle_system.list_credentials(holder_npub=npub)
        else:
            raise HTTPException(status_code=400, detail="Invalid type (must be 'requests' or 'credentials')")
        
        return JSONResponse({
            "success": True,
            "type": type,
            "count": len(results),
            "results": results
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error listing permits: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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

@app.post("/api/permit/expire/{request_id}")
async def expire_permit_request(request_id: str):
    """Mark a permit request as expired (for old requests)"""
    if not ORACLE_ENABLED or oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        if request_id not in oracle_system.requests:
            raise HTTPException(status_code=404, detail="Request not found")
        
        request = oracle_system.requests[request_id]
        request.status = PermitStatus.REJECTED
        request.updated_at = datetime.now()
        
        oracle_system.save_data()
        
        return JSONResponse({
            "success": True,
            "message": "Request marked as expired",
            "request_id": request_id
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error expiring request: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
