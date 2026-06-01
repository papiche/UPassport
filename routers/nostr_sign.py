"""
POST /api/nostr/sign_and_publish
Signe un événement NOSTR avec BIP-340 Schnorr et le publie sur le relay local.
Fallback Android pour Cabine-33 (Godot ne peut pas signer Schnorr nativement).
"""
import asyncio
import hashlib
import json
import logging
from typing import Optional

from bech32 import bech32_decode, convertbits
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Constantes secp256k1 BIP-340 (partagées avec mailjet.py) ────────────────
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _pt_add(P, Q):
    if P is None: return Q
    if Q is None: return P
    x1, y1 = P; x2, y2 = Q
    if x1 == x2:
        if y1 != y2: return None
        lam = 3 * x1 * x1 * pow(2 * y1, _P - 2, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    return x3, (lam * (x1 - x3) - y1) % _P


def _pt_mul(P, n):
    R = None
    for i in range(256):
        if (n >> i) & 1: R = _pt_add(R, P)
        P = _pt_add(P, P)
    return R


def _tagged_hash(tag: str, data: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + data).digest()


def _schnorr_sign(msg: bytes, seckey_bytes: bytes) -> bytes:
    """Signature Schnorr BIP-340 déterministe."""
    d = int.from_bytes(seckey_bytes, "big") % _N
    if d == 0:
        raise ValueError("Clé secrète nulle")
    P = _pt_mul(_G, d)
    if P is None:
        raise ValueError("Point public invalide")
    # Normaliser : si y est impair, négativer d
    if P[1] % 2 != 0:
        d = _N - d
        P = _pt_mul(_G, d)
    Px = P[0].to_bytes(32, "big")
    # Nonce déterministe BIP-340
    t = bytes(a ^ b for a, b in zip(seckey_bytes, _tagged_hash("BIP0340/aux", b"\x00" * 32)))
    rand = _tagged_hash("BIP0340/nonce", t + Px + msg)
    k = int.from_bytes(rand, "big") % _N
    if k == 0:
        raise ValueError("Nonce nul")
    R = _pt_mul(_G, k)
    if R is None:
        raise ValueError("Point R invalide")
    if R[1] % 2 != 0:
        k = _N - k
        R = _pt_mul(_G, k)
    Rx = R[0].to_bytes(32, "big")
    e = int.from_bytes(_tagged_hash("BIP0340/challenge", Rx + Px + msg), "big") % _N
    s = (k + e * d) % _N
    return Rx + s.to_bytes(32, "big")


def _decode_nsec(nsec: str) -> bytes:
    """Décode nsec1... bech32 → 32 octets de clé privée."""
    try:
        hrp, data = bech32_decode(nsec)
        if hrp != "nsec" or data is None:
            raise ValueError(f"HRP invalide : {hrp}")
        decoded = convertbits(data, 5, 8, False)
        if decoded is None or len(decoded) != 32:
            raise ValueError(f"Longueur invalide : {len(decoded) if decoded else 'None'}")
        return bytes(decoded)
    except Exception as e:
        raise ValueError(f"Décodage nsec impossible : {e}")


def _compute_event_id(ev: dict) -> str:
    """SHA-256 canonique d'un événement NOSTR."""
    serial = json.dumps(
        [0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(serial.encode()).hexdigest()


async def _publish_event(relay_url: str, event: dict) -> bool:
    """Publie un événement NOSTR signé sur un relay WebSocket."""
    try:
        import websockets
        msg = json.dumps(["EVENT", event])
        async with websockets.connect(relay_url, open_timeout=5, close_timeout=3) as ws:
            await ws.send(msg)
            try:
                resp = await asyncio.wait_for(ws.recv(), timeout=5)
                parsed = json.loads(resp)
                # ["OK", id, true/false, message]
                ok = parsed[0] == "OK" and len(parsed) >= 3 and parsed[2] is True
                if not ok:
                    logger.warning(f"Relay {relay_url} a refusé l'événement : {parsed}")
                return ok
            except asyncio.TimeoutError:
                logger.warning(f"Relay {relay_url} pas de réponse OK dans les 5s")
                return True  # optimiste : l'événement a peut-être été accepté
    except Exception as e:
        logger.error(f"Échec publication sur {relay_url} : {e}")
        return False


# ── Schéma de requête ────────────────────────────────────────────────────────

class SignPublishRequest(BaseModel):
    event: dict          # Événement NOSTR non signé (avec id calculé)
    nsec: str            # Clé privée bech32 (nsec1...)
    relays: Optional[list[str]] = None  # Relais cibles (défaut : local + copylaradio)


# ── Endpoint ─────────────────────────────────────────────────────────────────

@router.post("/api/nostr/sign_and_publish", summary="Signe et publie un événement NOSTR (fallback Android)")
async def sign_and_publish(req: SignPublishRequest):
    """
    Fallback de signature Schnorr BIP-340 pour Godot sur Android.
    Sur Web, Cabine-33 signe directement en JS via nostr.bundle.js.
    Sur Android (pas de JavaScriptBridge), ce endpoint fait la signature côté serveur.

    Reçoit : {"event": {..., "id": "..."}, "nsec": "nsec1..."}
    Retourne : 200 {"published": true, "event_id": "..."}
    """
    ev = dict(req.event)
    nsec = req.nsec.strip()

    # ── Validation de base ──────────────────────────────────────────────────
    if not nsec.startswith("nsec1"):
        raise HTTPException(400, "Format nsec invalide — attendu nsec1...")
    for field in ("pubkey", "created_at", "kind", "tags", "content"):
        if field not in ev:
            raise HTTPException(400, f"Champ manquant dans l'événement : {field}")

    # ── Vérification / recalcul de l'ID ────────────────────────────────────
    computed_id = _compute_event_id(ev)
    if "id" in ev and ev["id"] != computed_id:
        logger.warning(f"ID de l'événement corrigé : {ev['id'][:12]}… → {computed_id[:12]}…")
    ev["id"] = computed_id

    # ── Décodage et signature ───────────────────────────────────────────────
    try:
        seckey = _decode_nsec(nsec)
    except ValueError as e:
        raise HTTPException(400, f"nsec invalide : {e}")

    try:
        sig = _schnorr_sign(bytes.fromhex(computed_id), seckey)
        ev["sig"] = sig.hex()
    except Exception as e:
        logger.error(f"Échec signature BIP-340 : {e}")
        raise HTTPException(500, f"Erreur signature Schnorr : {e}")

    # ── Publication sur les relays ──────────────────────────────────────────
    targets = req.relays if req.relays else settings.NOSTR_RELAYS.split()
    if not targets:
        targets = ["ws://127.0.0.1:7777"]

    results = await asyncio.gather(*[_publish_event(r, ev) for r in targets], return_exceptions=True)
    published_count = sum(1 for r in results if r is True)

    logger.info(f"Événement kind={ev['kind']} {computed_id[:12]}… publié sur {published_count}/{len(targets)} relays")

    if published_count == 0:
        raise HTTPException(502, "Aucun relay n'a accepté l'événement")

    return JSONResponse({"published": True, "event_id": computed_id, "relays": published_count})
