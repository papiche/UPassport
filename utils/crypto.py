import logging
from typing import Optional
import os

def convert_nostr_key(key: str, to_format: str) -> Optional[str]:
    """Convertit une clé NOSTR entre bech32 (npub) et hexadécimal."""
    if not key: return None
    key = key.lower().strip()
    
    try:
        if to_format == "hex":
            if len(key) == 64: return key # Déjà en hex
            if not key.startswith('npub1'): return None
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
