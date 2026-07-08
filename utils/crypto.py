import hashlib
import json
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


# ─── secp256k1 / BIP-340 Schnorr (pur Python, sans dépendance) ────────────────
# Vérification directe d'une signature d'event NOSTR, sans passer par le relay
# ni par un fichier marqueur (contrairement à verify_nostr_auth/check_nip42_auth
# dans services/nostr.py, qui ne font que constater qu'un marker récent existe).
# Originellement dans routers/mailjet.py — factorisé ici pour être réutilisé par
# tout endpoint qui doit prouver que l'appelant possède une clé NOSTR précise.

_SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _pt_add(P, Q):
    if P is None: return Q
    if Q is None: return P
    x1, y1 = P; x2, y2 = Q
    if x1 == x2:
        if y1 != y2: return None
        lam = 3 * x1 * x1 * pow(2 * y1, _SECP256K1_P - 2, _SECP256K1_P) % _SECP256K1_P
    else:
        lam = (y2 - y1) * pow(x2 - x1, _SECP256K1_P - 2, _SECP256K1_P) % _SECP256K1_P
    x3 = (lam * lam - x1 - x2) % _SECP256K1_P
    return x3, (lam * (x1 - x3) - y1) % _SECP256K1_P


def _pt_mul(P, n):
    R = None
    for i in range(256):
        if (n >> i) & 1: R = _pt_add(R, P)
        P = _pt_add(P, P)
    return R


def _lift_x(x: int):
    if x >= _SECP256K1_P: return None
    y_sq = (pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    y = pow(y_sq, (_SECP256K1_P + 1) // 4, _SECP256K1_P)
    if pow(y, 2, _SECP256K1_P) != y_sq: return None
    return x, (y if y % 2 == 0 else _SECP256K1_P - y)


def _tagged_hash(tag: str, data: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + data).digest()


def schnorr_verify(msg: bytes, pk32: bytes, sig64: bytes) -> bool:
    """Vérifie une signature Schnorr BIP-340."""
    if len(pk32) != 32 or len(sig64) != 64: return False
    P = _lift_x(int.from_bytes(pk32, "big"))
    if P is None: return False
    r = int.from_bytes(sig64[:32], "big")
    s = int.from_bytes(sig64[32:], "big")
    if r >= _SECP256K1_P or s >= _SECP256K1_N: return False
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", sig64[:32] + pk32 + msg), "big"
    ) % _SECP256K1_N
    R = _pt_add(_pt_mul(_SECP256K1_G, s), _pt_mul(P, _SECP256K1_N - e))
    return R is not None and R[1] % 2 == 0 and R[0] == r


def verify_nostr_event(ev: dict) -> bool:
    """Vérifie l'ID (SHA-256 NIP-01) et la signature Schnorr d'un événement NOSTR."""
    try:
        serial = json.dumps(
            [0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
            separators=(",", ":"), ensure_ascii=False,
        )
        if ev.get("id") != hashlib.sha256(serial.encode()).hexdigest():
            return False
        return schnorr_verify(
            bytes.fromhex(ev["id"]),
            bytes.fromhex(ev["pubkey"]),
            bytes.fromhex(ev["sig"]),
        )
    except Exception:
        return False
