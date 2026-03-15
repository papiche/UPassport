import os
import re
import magic
import logging
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
        
        base_path = os.path.expanduser(f"~/.zen/game/{user_type}")
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
        
        base_path = os.path.expanduser("~/.zen/tmp/swarm")
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
