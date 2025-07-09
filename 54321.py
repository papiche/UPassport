#!/usr/bin/env python3*
import uuid
import re
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
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
import subprocess
import magic
import time
import hashlib
import websockets
import shutil
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote, urlparse, parse_qs
from pathlib import Path
import mimetypes
import sys
import unicodedata
from collections import defaultdict, deque
import threading
import ipaddress

# Obtenir le timestamp Unix actuel
unix_timestamp = int(time.time())

# Configure le logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Charger les variables d'environnement depuis le fichier .env
load_dotenv()
# Récupérer la valeur de OBSkey depuis l'environnement
OBSkey = os.getenv("OBSkey")

# Configuration for H2G2 functionality
DEFAULT_PORT = 54321
DEFAULT_HOST = "127.0.0.1"
SCRIPT_DIR = Path(__file__).parent
DEFAULT_SOURCE_DIR = SCRIPT_DIR

# Rate Limiting Configuration
RATE_LIMIT_REQUESTS = 12  # Maximum requests per minute
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

# Create global rate limiter instance
rate_limiter = RateLimiter()

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

# ~ # Configurer CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins or restrict
    # ~ allow_origins=["https://ipfs.astroport.com", "https://u.astroport.com"],
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

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

# Nouveaux modèles pour H2G2 functionality
class UploadResponse(BaseModel):
    success: bool
    message: str
    file_path: str
    file_type: str
    target_directory: str
    new_cid: Optional[str] = None
    timestamp: str
    auth_verified: Optional[bool] = False

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

class UploadFromDriveRequest(BaseModel):
    ipfs_link: str # Format attendu : QmHASH/filename.ext
    npub: str

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

# H2G2 utility functions
def get_source_directory(source_dir: Optional[str] = None) -> Path:
    """Obtenir le répertoire source, avec validation"""
    if source_dir:
        source_path = Path(source_dir).resolve()
    else:
        source_path = DEFAULT_SOURCE_DIR.resolve()
    
    if not source_path.exists():
        raise HTTPException(status_code=404, detail=f"Répertoire source non trouvé: {source_path}")
    
    if not source_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Le chemin spécifié n'est pas un répertoire: {source_path}")
    
    return source_path

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
    app_dir = user_root_dir / "APP/uDRIVE"
    app_dir.mkdir(exist_ok=True)  # S'assurer que APP/ existe
    
    logging.info(f"Répertoire APP utilisateur (sécurisé): {app_dir}")
    return app_dir

def is_safe_filename(filename: str) -> bool:
    """Vérifier si le nom de fichier est sécurisé"""
    # Interdire les caractères dangereux et les chemins relatifs
    dangerous_chars = ['..', '/', '\\', ':', '*', '?', '"', '<', '>', '|']
    return not any(char in filename for char in dangerous_chars)

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

async def run_ipfs_generation_script(source_dir: Path, enable_logging: bool = False) -> Dict[str, Any]:
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
        return False
    
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
    """Vérifier l'authentification NOSTR si une npub est fournie"""
    if not npub:
        logging.info("Aucune npub fournie, pas de vérification NOSTR")
        return False
    
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
def check_balance(identifier):
    # Vérifier si l'identifiant est un email
    if '@' in identifier:
        email = identifier
        # Essayer de trouver la g1pub dans les différents emplacements
        g1pub = None
        
        # Vérifier dans le dossier nostr
        nostr_g1pub_path = os.path.expanduser(f"~/.zen/game/nostr/{email}/G1PUBNOSTR")
        if os.path.exists(nostr_g1pub_path):
            with open(nostr_g1pub_path, 'r') as f:
                g1pub = f.read().strip()
        
        # Si pas trouvé, vérifier dans le dossier players
        if not g1pub:
            players_g1pub_path = os.path.expanduser(f"~/.zen/game/players/{email}/.g1pub")
            if os.path.exists(players_g1pub_path):
                with open(players_g1pub_path, 'r') as f:
                    g1pub = f.read().strip()
        
        if not g1pub:
            raise ValueError(f"Impossible de trouver la g1pub pour l'email {email}")
    else:
        g1pub = identifier

    # Vérifier le solde avec la g1pub
    result = subprocess.run(["tools/COINScheck.sh", g1pub], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("Erreur dans COINScheck.sh: " + result.stderr)
    balance_line = result.stdout.strip().splitlines()[-1]
    return balance_line

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

@app.get("/nostr")
async def get_root(request: Request):
    return templates.TemplateResponse("nostr.html", {"request": request})

@app.get("/blog")
async def get_root(request: Request):
    return templates.TemplateResponse("nostr_blog.html", {"request": request})

@app.get("/g1", response_class=HTMLResponse)
async def get_root(request: Request):
    return templates.TemplateResponse("g1nostr.html", {"request": request})

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
            node_json_path = os.path.expanduser(f"~/.zen/tmp/swarm/{node_id}/12345.json")
            
            if os.path.exists(node_json_path):
                try:
                    with open(node_json_path, 'r') as f:
                        node_data = json.load(f)
                    
                    ssh_pub_key = node_data.get('SSHPUB', '').strip()
                    actual_node_id = node_data.get('ipfsnodeid', '').strip()
                    captain_email = node_data.get('captain', '').strip()
                    
                    if ssh_pub_key and actual_node_id:
                        logging.info(f"   Found SSH key: {ssh_pub_key[:50]}...")
                        logging.info(f"   Node ID from JSON: {actual_node_id}")
                        logging.info(f"   Captain: {captain_email}")
                        
                        # Vérifier que le node ID correspond
                        if actual_node_id == node_id:
                            # Vérifier la clé SSH avec ssh_to_g1ipfs.py
                            try:
                                ssh_to_g1_script = os.path.expanduser("~/.zen/Astroport.ONE/tools/ssh_to_g1ipfs.py")
                                if os.path.exists(ssh_to_g1_script):
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
                                                    new_notification["ssh_key"] = ssh_pub_key[:50] + "..."
                                                    
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
async def check_balance_route(g1pub: str):
    try:
        balance = check_balance(g1pub)
        return {"balance": balance, "g1pub": g1pub}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/upassport")
async def scan_qr(
    parametre: str = Form(...),
    imageData: str = Form(None),
    zlat: str = Form(None),
    zlon: str = Form(None)
):
    image_dir = "./tmp"

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
    logging.info(f"Received SSSS key: {ssss}")
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
    form_data = await request.form()
    zen = form_data.get("zen")
    g1source = form_data.get("g1source")
    g1dest = form_data.get("g1dest")

    logging.info(f"Zen Amount : {zen}")
    logging.info(f"Source : {g1source}")
    logging.info(f"Destination : {g1dest}")

    script_path = "./zen_send.sh"
    return_code, last_line = await run_script(script_path, zen, g1source, g1dest)

    if return_code == 0:
        returned_file_path = last_line.strip()
        return FileResponse(returned_file_path)
    else:
        return JSONResponse({"error": f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans ~/.zen/tmp/54321.log."})

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
    return templates.TemplateResponse("webcam.html", {"request": request, "recording": False})

@app.post("/rec", response_class=HTMLResponse)
async def start_recording(request: Request, player: str = Form(...), link: str = Form(default=""), file: UploadFile = File(None), video_blob: str = Form(default="")):
    global recording_process, current_player

    if not player:
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "No player provided. What is your email?", "recording": False})

    if not re.match(r"[^@]+@[^@]+\.[^@]+", player):
        return templates.TemplateResponse("rec_form.html", {"request": request, "error": "Invalid email address provided.", "recording": False})

    script_path = "./startrec.sh"

    # Cas 1: Enregistrement webcam
    if video_blob:
        try:
            # Vérifier si le blob contient une virgule (format data URL)
            if ',' in video_blob:
                # Extraire la partie après la virgule
                _, video_data_base64 = video_blob.split(',', 1)
                video_data = base64.b64decode(video_data_base64)
            else:
                # Si pas de virgule, supposer que c'est directement en base64
                video_data = base64.b64decode(video_blob)

            file_location = f"tmp/{player}_{int(time.time())}.webm"
            with open(file_location, 'wb') as f:
                f.write(video_data)

            return_code, last_line = await run_script(script_path, player, f"blob={file_location}")

            if return_code == 0:
                return templates.TemplateResponse("webcam.html", {"request": request, "message": f"Operation completed successfully {last_line.strip()}", "recording": False})
            else:
                return templates.TemplateResponse("webcam.html", {"request": request, "error": f"Script execution failed: {last_line.strip()}", "recording": False})

        except Exception as e:
            # Gérer toute exception qui pourrait se produire lors du traitement du blob
            return templates.TemplateResponse("webcam.html", {"request": request, "error": f"Error processing video data: {str(e)}", "recording": False})

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
        return_code, last_line = await run_script(script_path, file_location, temp_file_path)

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

@app.post("/register/{stall_id}")
async def register_stall(stall_id: str, stall_data: dict):
    try:
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/diagonalley.sh")
        lat = stall_data.get("lat")
        lon = stall_data.get("lon")
        if not lat or not lon:
            raise HTTPException(status_code=400, detail="Latitude and longitude are required")
        return_code, last_line = await run_script(script_path, "register", stall_id, stall_data["stall_url"], lat, lon)
        
        if return_code == 0:
            return JSONResponse(content=json.loads(last_line))
        else:
            raise HTTPException(status_code=500, detail="Failed to register stall")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/products/{stall_id}")
async def get_products(stall_id: str, indexer_id: str, lat: float, lon: float):
    try:
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/diagonalley.sh")
        return_code, last_line = await run_script(script_path, "products", stall_id, indexer_id, str(lat), str(lon))
        
        if return_code == 0:
            return JSONResponse(content=json.loads(last_line))
        else:
            raise HTTPException(status_code=500, detail="Failed to get products")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/order/{stall_id}")
async def place_order(stall_id: str, order_data: dict):
    try:
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/diagonalley.sh")
        lat = order_data.get("lat")
        lon = order_data.get("lon")
        if not lat or not lon:
            raise HTTPException(status_code=400, detail="Latitude and longitude are required")
        return_code, last_line = await run_script(script_path, "order", stall_id, json.dumps(order_data), str(lat), str(lon))
        
        if return_code == 0:
            return JSONResponse(content=json.loads(last_line))
        else:
            raise HTTPException(status_code=500, detail="Failed to place order")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status/{checking_id}")
async def check_order_status(checking_id: str, lat: float, lon: float):
    try:
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/diagonalley.sh")
        return_code, last_line = await run_script(script_path, "status", checking_id, str(lat), str(lon))
        
        if return_code == 0:
            return JSONResponse(content=json.loads(last_line))
        else:
            raise HTTPException(status_code=500, detail="Failed to check order status")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/verify_signature")
async def verify_signature(data: dict):
    try:
        message = data["message"]
        signature = data["signature"]
        stall_id = data["stall_id"]
        
        # Get stall's public key
        stall_dir = os.path.expanduser(f"~/.zen/game/diagonalley/stalls/{stall_id}")
        if not os.path.exists(stall_dir):
            raise HTTPException(status_code=404, detail="Stall not found")
            
        # Verify signature
        result = subprocess.run(
            ["openssl", "dgst", "-verify", f"{stall_dir}/public.pem", "-signature", "/dev/stdin"],
            input=base64.b64decode(signature),
            capture_output=True,
            text=True
        )
        
        return {"valid": result.returncode == 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/check_umap")
async def check_umap(g1pub: str, lat: float, lon: float):
    try:
        script_path = os.path.expanduser("~/.zen/Astroport.ONE/tools/diagonalley.sh")
        
        # Get UMAP directory for coordinates
        return_code, umap_dir = await run_script(script_path, "get_umap_dir", str(lat), str(lon))
        
        if return_code != 0:
            raise HTTPException(status_code=404, detail="UMAP directory not found")
            
        # Read cache content
        return_code, cache_content = await run_script(script_path, "read_cache", umap_dir.strip())
        
        if return_code != 0:
            raise HTTPException(status_code=500, detail="Failed to read cache")
            
        try:
            cache_data = json.loads(cache_content)
        except json.JSONDecodeError:
            cache_data = {}
            
        # Add registration status and UMAP ID
        response_data = {
            "registered": True,
            "umap_id": g1pub,
            "stalls": cache_data.get("stalls", []),
            "cache_timestamp": cache_data.get("timestamp", None)
        }
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logging.error(f"Error checking UMAP: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# H2G2 Endpoints - Upload and Delete with NOSTR authentication
@app.post("/api/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    npub: str = Form(...)  # Seule npub ou hex est acceptée
):
    auth_verified = await verify_nostr_auth(npub)
    if not auth_verified:
        raise HTTPException(status_code=403, detail="Nostr authentication failed or not provided.")

    try:
        user_drive_path = get_authenticated_user_directory(npub)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error determining user directory: {e}")

    # Sanitize the original filename provided by the client
    original_filename = file.filename if file.filename else "untitled_file"
    sanitized_filename = sanitize_filename_python(original_filename)

    # Determine target directory based on file type
    # Read a small chunk to detect type, then reset stream position
    file_content_sample = await file.read(1024)
    await file.seek(0) # Reset stream for full file saving

    file_type = detect_file_type(file_content_sample, sanitized_filename)

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

    try:
        with open(target_file_path, "wb") as buffer:
            # Read the file in chunks to handle large files efficiently
            while True:
                chunk = await file.read(1024 * 1024)  # Read 1MB chunks
                if not chunk:
                    break
                buffer.write(chunk)
        file_size = target_file_path.stat().st_size
        logging.info(f"File '{sanitized_filename}' saved to '{target_file_path}' (Size: {file_size} bytes)")

        # CORRECTION : Appeler la fonction spécialisée run_ipfs_generation_script
        # qui gère le changement de répertoire de travail (cwd) pour le script.
        ipfs_result = await run_ipfs_generation_script(user_drive_path)
        new_cid_info = ipfs_result.get("final_cid") # Accéder à "final_cid" depuis le dictionnaire de résultat
        logging.info(f"New IPFS CID generated: {new_cid_info}")

        return UploadResponse(
            success=True,
            message="File uploaded successfully",
            file_path=str(target_file_path.relative_to(user_drive_path)),
            file_type=file_type,
            target_directory=target_directory_name,
            new_cid=new_cid_info,
            timestamp=datetime.now().isoformat(),
            auth_verified=auth_verified
        )
    except Exception as e:
        logging.error(f"Error saving file or running IPFS script: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process file: {e}")

@app.post("/api/upload_from_drive", response_model=UploadFromDriveResponse)
async def upload_from_drive(request: UploadFromDriveRequest):
    auth_verified = await verify_nostr_auth(request.npub)
    if not auth_verified:
        raise HTTPException(status_code=403, detail="Nostr authentication failed or not provided.")

    try:
        user_drive_path = get_authenticated_user_directory(request.npub)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error determining user directory: {e}")

    # Extract filename from ipfs_link (e.g., "QmHASH/filename.ext" or just "filename.ext")
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

        # CORRECTION : Appeler la fonction spécialisée run_ipfs_generation_script
        # qui gère le changement de répertoire de travail (cwd) pour le script.
        ipfs_result = await run_ipfs_generation_script(user_drive_path)
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
            ipfs_result = await run_ipfs_generation_script(base_dir, enable_logging=False)
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

@app.post("/api/test-nostr")
async def test_nostr_auth(npub: str):
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

## FOR DOCUMENTATION ONLY DO NOT USE THIS FUNCTION
def get_user_nostr_private_key(hex_pubkey: str) -> Optional[str]:
    """Récupérer la clé privée NOSTR de l'utilisateur depuis son répertoire"""
    try:
        # Trouver le répertoire utilisateur correspondant à la clé publique
        user_dir = find_user_directory_by_hex(hex_pubkey)
        
        # Chercher le fichier contenant les clés NOSTR
        secret_file = user_dir / ".secret.nostr"
        
        if secret_file.exists():
            with open(secret_file, 'r') as f:
                content = f.read().strip()
            
            # Parser le contenu pour extraire NSEC
            # Format: NSEC=nsec1...; NPUB=npub1...; HEX=...;
            for line in content.split(';'):
                line = line.strip()
                if line.startswith('NSEC='):
                    nsec = line.replace('NSEC=', '').strip()
                    logging.info(f"Clé privée NOSTR trouvée pour {hex_pubkey}")
                    return nsec
            
            logging.warning(f"NSEC non trouvé dans {secret_file}")
            return None
        else:
            logging.warning(f"Fichier .secret.nostr non trouvé: {secret_file}")
            return None
            
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de la clé privée NOSTR: {e}")
        return None

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=54321)
