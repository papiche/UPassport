import os
import re
import magic
import logging
import threading
import unicodedata
from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import UploadFile

def is_safe_email(email: str) -> bool:
    """Valider qu'un email est sûr et ne contient pas de caractères dangereux"""
    if not email or len(email) > 254:
        return False
    
    dangerous_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '..']
    for char in dangerous_chars:
        if char in email:
            return False
    
    if email.count('@') != 1 or email.startswith('@') or email.endswith('@'):
        return False
    
    local_part, domain_part = email.split('@', 1)
    if not local_part or not domain_part:
        return False
    
    if '.' not in domain_part:
        return False
    
    return True

def is_multipass_user(hex_pubkey: str) -> bool:
    """
    Verify if a user is recognized as MULTIPASS by checking if their account exists in ~/.zen/game/nostr/.
    Uses an in-memory cache (built once) for O(1) lookup instead of scanning all directories.
    """
    if not hex_pubkey:
        return False
    
    hex_pubkey = hex_pubkey.lower().strip()
    
    from core.state import app_state
    
    if hex_pubkey in app_state.hex_to_email_cache:
        email = app_state.hex_to_email_cache[hex_pubkey]
        logging.info(f"✅ User is recognized MULTIPASS (650MB quota) - found in {email}")
        return True
    
    logging.debug(f"ℹ️  User is not recognized MULTIPASS (100MB quota) - hex not in index")
    return False

def get_max_file_size_for_user(npub: str) -> int:
    """
    Get the maximum file size limit for a user according to UPlanet_FILE_CONTRACT.md.
    """
    from utils.crypto import npub_to_hex
    hex_pubkey = npub_to_hex(npub) if npub else None
    if hex_pubkey and is_multipass_user(hex_pubkey):
        return 681574400  # 650MB
    else:
        return 104857600  # 100MB

def is_safe_g1pub(g1pub: str) -> bool:
    """Valider qu'une g1pub est sûre et ne contient pas de caractères dangereux."""
    if not g1pub or len(g1pub) > 100:
        return False

    safe_pattern = re.compile(r'^[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]+(:ZEN)?$')
    return bool(safe_pattern.match(g1pub))

def get_safe_user_path(user_type: str, email: str, filename: str) -> Optional[str]:
    """Construire un chemin sûr pour un fichier utilisateur"""
    try:
        if not is_safe_email(email) or not filename or '/' in filename or '\\' in filename:
            return None
        
        from core.config import settings
        base_path = settings.GAME_PATH / user_type
        user_dir = os.path.join(base_path, email)
        
        final_path = os.path.join(user_dir, filename)
        resolved_path = os.path.realpath(final_path)
        base_resolved = os.path.realpath(base_path)
        
        if not resolved_path.startswith(base_resolved):
            logging.warning(f"Tentative d'accès hors répertoire autorisé: {resolved_path}")
            return None
        
        return final_path
        
    except Exception as e:
        logging.error(f"Erreur construction chemin sûr: {e}")
        return None

def is_safe_ssh_key(ssh_key: str) -> bool:
    """Valider qu'une clé SSH publique est sûre"""
    if not ssh_key or len(ssh_key) > 2000:
        return False
    
    ssh_pattern = re.compile(r'^ssh-ed25519 [A-Za-z0-9+/=]+(\s+[^@\s]+@[^@\s]+)?$')
    return bool(ssh_pattern.match(ssh_key))

def is_safe_node_id(node_id: str) -> bool:
    """Valider qu'un node ID est sûr"""
    if not node_id or len(node_id) > 100:
        return False
    
    node_pattern = re.compile(r'^[a-zA-Z0-9_-]+$')
    return bool(node_pattern.match(node_id))

def get_safe_swarm_path(node_id: str, filename: str) -> Optional[str]:
    """Construire un chemin sûr pour un fichier swarm"""
    try:
        if not is_safe_node_id(node_id) or not filename or '/' in filename or '\\' in filename:
            return None
        
        from core.config import settings
        base_path = settings.ZEN_PATH / "tmp" / "swarm"
        node_dir = os.path.join(base_path, node_id)
        
        final_path = os.path.join(node_dir, filename)
        resolved_path = os.path.realpath(final_path)
        base_resolved = os.path.realpath(base_path)
        
        if not resolved_path.startswith(base_resolved):
            logging.warning(f"Tentative d'accès hors répertoire swarm autorisé: {resolved_path}")
            return None
        
        return final_path
        
    except Exception as e:
        logging.error(f"Erreur construction chemin swarm sûr: {e}")
        return None

def detect_file_type(file_content: bytes, filename: str) -> str:
    """
    Détecte le type de fichier basé sur le contenu ou l'extension.
    """
    ext = filename.split('.')[-1].lower()

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
    """Sanitizes a filename to prevent directory traversal and invalid characters."""
    filename = filename.replace('\0', '')
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('utf-8')
    filename = str(Path(filename).name)
    invalid_chars_re = re.compile(r'[<>:"/\\|?*\x00]')
    filename = invalid_chars_re.sub('_', filename)
    if not filename:
        return "unnamed_file"
    filename = filename[:250]
    return filename

def is_safe_file_content(content_sample: bytes, mime_type: str) -> bool:
    """Vérifier que le contenu du fichier est sûr"""
    try:
        if mime_type.startswith("image/"):
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
            return False
        elif mime_type == "application/pdf":
            return content_sample.startswith(b'%PDF')
        elif mime_type.startswith("text/"):
            try:
                content_sample.decode('utf-8')
                return True
            except UnicodeDecodeError:
                return False
        return True
    except Exception:
        return False

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
        if not file.size or file.size > max_size_mb * 1024 * 1024:
            validation_result["error"] = f"File size exceeds maximum allowed size of {max_size_mb}MB"
            return validation_result
        
        if not file.filename or len(file.filename) > 255:
            validation_result["error"] = "Invalid filename"
            return validation_result
        
        allowed_mime_types = {
            "image/jpeg", "image/png", "image/gif", "image/webp",
            "application/pdf", "text/plain", "text/markdown", "text/html",
            "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4", "audio/webm", "audio/flac",
            "video/mp4", "video/webm", "video/ogg", "video/avi", "video/mov",
            "application/zip", "application/x-7z-compressed",
            "text/javascript", "application/json", "text/css", "text/xml",
            "application/x-python-code", "text/x-python", "text/markdown"
        }
        
        content_sample = await file.read(1024)
        await file.seek(0)
        
        detected_mime = magic.from_buffer(content_sample, mime=True)
        
        if detected_mime == "application/octet-stream" and file.filename:
            extension_mime_map = {
                ".mp3": "audio/mpeg", ".mpeg": "audio/mpeg", ".wav": "audio/wav",
                ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/mp4", ".m4a": "audio/mp4",
                ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/mov",
                ".avi": "video/avi", ".mkv": "video/webm",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp",
            }
            
            file_ext = os.path.splitext(file.filename.lower())[1]
            if file_ext in extension_mime_map:
                detected_mime = extension_mime_map[file_ext]
            else:
                validation_result["error"] = f"File type 'application/octet-stream' with extension '{file_ext}' is not allowed"
                return validation_result
        
        if detected_mime not in allowed_mime_types:
            validation_result["error"] = f"File type '{detected_mime}' is not allowed"
            return validation_result
        
        if not is_safe_file_content(content_sample, detected_mime):
            validation_result["error"] = "File content validation failed"
            return validation_result
        
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

def check_secret_file_permissions(filepath: str) -> bool:
    """Vérifier que le fichier secret a les permissions 0600"""
    try:
        if not os.path.exists(filepath):
            return False
        
        stat = os.stat(filepath)
        # Check if permissions are exactly 0600 (owner read/write only)
        # stat.st_mode & 0o777 gets the permission bits
        return (stat.st_mode & 0o777) == 0o600
    except Exception as e:
        logging.error(f"Erreur lors de la vérification des permissions pour {filepath}: {e}")
        return False

def extract_nsec_from_keyfile(keyfile_path: str) -> str:
    """Extract NSEC key from .secret.nostr file with permission check"""
    if not os.path.exists(keyfile_path):
        raise FileNotFoundError(f"Keyfile not found: {keyfile_path}")
    
    if not check_secret_file_permissions(keyfile_path):
        logging.warning(f"Insecure permissions on keyfile {keyfile_path}. Should be 0600.")
        # In a strict environment, we might raise an exception here
        # raise PermissionError(f"Insecure permissions on keyfile {keyfile_path}. Must be 0600.")
    
    with open(keyfile_path, 'r') as f:
        content = f.read().strip()
    
    for part in content.split(';'):
        part = part.strip()
        if part.startswith('NSEC='):
            nsec = part[5:].strip()
            if nsec.startswith('nsec1'):
                return nsec
            raise ValueError(f"Invalid NSEC format in keyfile: {nsec[:15]}...")
    
    raise ValueError("No NSEC key found in keyfile")

# Cache pour le mapping hex -> email (MULTIPASS detection)
# Maps hex_pubkey (lowercase) -> email directory name
hex_to_email_cache = {}
# Cache pour les répertoires utilisateur (évite les scans répétés)
# Maps hex_pubkey (lowercase) -> Path to user directory
hex_to_directory_cache = {}
hex_cache_lock = threading.Lock()
hex_cache_built = False

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
        from core.config import settings
        nostr_base_path = settings.GAME_PATH / "nostr"
        
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

def find_user_directory_by_hex(hex_pubkey: str) -> Path:
    """Trouver le répertoire utilisateur correspondant à la clé publique hex (with caching)"""
    from fastapi import HTTPException
    if not hex_pubkey:
        raise HTTPException(status_code=400, detail="Clé publique hex manquante")
    
    # Normaliser la clé hex
    hex_pubkey = hex_pubkey.lower().strip()
    
    from core.state import app_state
    from core.config import settings
    
    # Check cache first
    if hex_pubkey in app_state.hex_to_directory_cache:
        cached_dir = app_state.hex_to_directory_cache[hex_pubkey]
        # Verify cache is still valid (directory still exists)
        if cached_dir.exists():
            logging.info(f"✅ Répertoire trouvé dans le cache pour {hex_pubkey}: {cached_dir}")
            return cached_dir
        else:
            # Cache invalid, remove it
            del app_state.hex_to_directory_cache[hex_pubkey]
            logging.warning(f"Cache invalide pour {hex_pubkey}, répertoire n'existe plus")
    
    # Chemin de base pour les utilisateurs NOSTR
    nostr_base_path = settings.GAME_PATH / "nostr"
    
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
                        app_dir.mkdir(parents=True, exist_ok=True)
                        
                        # Vérifier la présence du script IPFS et le copier si nécessaire
                        user_script = app_dir / "generate_ipfs_structure.sh"
                        if not user_script.exists():
                            generic_script = settings.TOOLS_PATH / "generate_ipfs_structure.sh"
                            if generic_script.exists():
                                # Créer un lien symbolique
                                user_script.symlink_to(generic_script)
                                logging.info(f"Lien symbolique créé vers {user_script}")
                            else:
                                logging.warning(f"Script générique non trouvé dans {generic_script}")
                        
                        # Cache the result
                        app_state.hex_to_directory_cache[hex_pubkey] = email_dir
                        
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
    from utils.crypto import npub_to_hex
    
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
