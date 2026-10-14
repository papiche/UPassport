"""
services/face_ref.py — « Mon visage de référence » offert aux amis à qui l'on donne ses photos.

Quand on partage ses photos avec quelqu'un, on lui confie aussi son empreinte de visage (au plus
REF_MAX vecteurs 512D — ceux que l'on a soi-même désignés par « Votre visage », jamais un visage
d'un tiers). Sa station les range dans SA collection Qdrant `faces_<hex>` avec `ref: "shared"` :
le matcher (satellite_face_matcher.py) reconnaît alors l'expéditeur dans les photos de l'ami dès
l'import — sans que celui-ci ait rien nommé — et les lui renvoie par le chemin habituel, qui exige
toujours le suivi RÉCIPROQUE (_is_reciprocal).

Garde-fous :
  * opt-out par compte (`.ucloud/face_ref_sharing.json`, activé par défaut) ; sans « Votre visage »
    désigné, rien n'est envoyé ;
  * un expéditeur bloqué par le destinataire (blocked_senders) n'est pas enregistré ;
  * chaque envoi est consigné (`.ucloud/face_ref_given.json`) : on sait à qui on l'a confiée, on ne la
    renvoie pas deux fois, et on peut la RETIRER (`revoke`) — le destinataire efface ses points ;
  * le vecteur ne voyage jamais en clair : copie locale (même station), canal inter-NODE chiffré NIP-44,
    ou invitation chiffrée avec le jeton du lien (services/face_invites.py).
Fonctions synchrones, qui ne lèvent pas vers l'appelant : un échec ne bloque jamais un partage de photos.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from services import cloud_storage

logger = logging.getLogger(__name__)

QDRANT_URL = "http://localhost:6333"
VECTOR_SIZE = 512                 # InsightFace buffalo_l (satellite_face_matcher.FACE_VECTOR_SIZE)
REF_MAX = 3
_NS = uuid.UUID("6f1f6f3e-3b1c-4a39-9d57-0c2f7a8e5b11")


def _headers() -> Dict[str, str]:
    try:
        for line in (Path.home() / ".zen" / "ai-company" / ".env").read_text().splitlines():
            if line.startswith("QDRANT_API_KEY="):
                return {"api-key": line.split("=", 1)[1].strip().strip('"')}
    except OSError:
        pass
    return {}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


# ── réglage du compte ───────────────────────────────────────────────────────
def _setting_path(email: str) -> Path:
    return cloud_storage.ucloud_dir(email) / "face_ref_sharing.json"


def sharing_enabled(email: str) -> bool:
    return bool(_read_json(_setting_path(email), {}).get("enabled", True))


def set_sharing(email: str, enabled: bool) -> None:
    _write_json(_setting_path(email), {"enabled": bool(enabled), "at": int(time.time())})


# ── registre des envois (à qui l'ai-je confiée ?) ───────────────────────────
def _given_path(email: str) -> Path:
    return cloud_storage.ucloud_dir(email) / "face_ref_given.json"


def given(email: str) -> Dict[str, Dict[str, Any]]:
    """{hex du destinataire: {at, sha, name}}."""
    return _read_json(_given_path(email), {})


def _remember(email: str, to_hex: str, sha: str, name: str = "") -> None:
    data = given(email)
    data[to_hex] = {"at": int(time.time()), "sha": sha, "name": name}
    _write_json(_given_path(email), data)


def _sha(vectors: List[List[float]]) -> str:
    return hashlib.sha256(json.dumps([[round(v, 5) for v in vec] for vec in vectors]).encode()).hexdigest()[:16]


# ── côté expéditeur ─────────────────────────────────────────────────────────
def reference_vectors(email: str) -> List[List[float]]:
    """Mes visages de référence : les points de MA collection portant MA clé (« Votre visage »)."""
    my_hex = cloud_storage.hex_for_email(email)
    if not my_hex:
        return []
    body = {"limit": 50, "with_payload": True, "with_vector": True,
            "filter": {"must": [{"key": "pubkey", "match": {"value": my_hex}}]}}
    try:
        r = httpx.post(f"{QDRANT_URL}/collections/faces_{my_hex}/points/scroll", headers=_headers(), json=body, timeout=15)
        if r.status_code != 200:
            return []
        points = r.json().get("result", {}).get("points", [])
    except Exception as exc:
        logger.warning("face_ref: Qdrant injoignable (%s)", exc)
        return []
    points = [p for p in points if (p.get("payload") or {}).get("ref") != "shared" and _valid(p.get("vector"))]
    points.sort(key=lambda p: str((p.get("payload") or {}).get("timestamp") or ""), reverse=True)
    return [p["vector"] for p in points[:REF_MAX]]


def payload_for(email: str, to_hex: Optional[str]) -> Optional[Dict[str, Any]]:
    """Ce qu'il faut joindre à un envoi de photos vers `to_hex` : {"vectors": [...], "sha"} ou None (réglage
    coupé, pas de « Votre visage », ou déjà confiée à l'identique). `to_hex=None` : destinataire pas encore
    connu (invitation) — pas de contrôle « déjà confiée »."""
    if not sharing_enabled(email):
        return None
    vectors = reference_vectors(email)
    if not vectors:
        return None
    if to_hex and given(email).get(to_hex, {}).get("sha") == _sha(vectors):
        return None
    return {"vectors": vectors, "sha": _sha(vectors)}


def mark_given(email: str, to_hex: str, payload: Dict[str, Any], name: str = "") -> None:
    try:
        _remember(email, to_hex, payload["sha"], name)
    except Exception as exc:
        logger.warning("face_ref: registre non écrit (%s)", exc)


def give_local(owner_email: str, recipient_email: str, from_name: str = "") -> int:
    """Destinataire hébergé sur CETTE station : l'empreinte est rangée directement. Rend le nombre de points."""
    try:
        to_hex = cloud_storage.hex_for_email(recipient_email) or ""
        from_hex = cloud_storage.hex_for_email(owner_email) or ""
        payload = payload_for(owner_email, to_hex)
        if not payload or not from_hex:
            return 0
        n = store(recipient_email, from_hex, from_name or owner_email.split("@", 1)[0], payload["vectors"])
        if n:
            mark_given(owner_email, to_hex, payload, from_name)
        return n
    except Exception as exc:
        logger.warning("face_ref: remise locale impossible (%s)", exc)
        return 0


# ── côté destinataire ───────────────────────────────────────────────────────
def _valid(vec: Any) -> bool:
    return (isinstance(vec, list) and len(vec) == VECTOR_SIZE
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in vec))


def _point_id(from_hex: str, vec: List[float]) -> str:
    return str(uuid.uuid5(_NS, from_hex + _sha([vec])))


def _ensure_collection(client: httpx.Client, collection: str) -> None:
    if client.get(f"{QDRANT_URL}/collections/{collection}", headers=_headers()).status_code == 200:
        return
    client.put(f"{QDRANT_URL}/collections/{collection}", headers=_headers(),
               json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}})


def store(recipient_email: str, from_hex: str, from_name: str, vectors: List[List[float]]) -> int:
    """Range l'empreinte de `from_hex` dans la collection du destinataire (`ref: "shared"`, ni photo ni
    visage affiché). Refusé si l'expéditeur est bloqué. Rend le nombre de points écrits."""
    try:
        if len(from_hex) != 64 or from_hex.lower() in cloud_storage.blocked_senders(recipient_email):
            return 0
        to_hex = cloud_storage.hex_for_email(recipient_email) or ""
        good = [v for v in (vectors or [])[:REF_MAX] if _valid(v)]
        if len(to_hex) != 64 or not good or from_hex == to_hex:
            return 0
        collection = f"faces_{to_hex}"
        with httpx.Client(timeout=20) as client:
            _ensure_collection(client, collection)
            # une seule génération par expéditeur : l'ancienne est remplacée, jamais cumulée
            client.post(f"{QDRANT_URL}/collections/{collection}/points/delete?wait=true", headers=_headers(),
                        json={"filter": {"must": [{"key": "ref", "match": {"value": "shared"}},
                                                  {"key": "pubkey", "match": {"value": from_hex}}]}})
            points = [{"id": _point_id(from_hex, v), "vector": v,
                       "payload": {"name": from_name or "?", "pubkey": from_hex, "ref": "shared",
                                   "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")}} for v in good]
            r = client.put(f"{QDRANT_URL}/collections/{collection}/points?wait=true", headers=_headers(),
                           json={"points": points})
            if r.status_code != 200:
                logger.warning("face_ref: Qdrant a refusé l'empreinte (%s)", r.status_code)
                return 0
        logger.info("face_ref: empreinte de %s… rangée chez %s (%d)", from_hex[:8], recipient_email, len(points))
        return len(points)
    except Exception as exc:
        logger.warning("face_ref: stockage impossible (%s)", exc)
        return 0


def forget(recipient_email: str, from_hex: str) -> bool:
    """Le destinataire efface l'empreinte de `from_hex` (retrait demandé par l'expéditeur, ou blocage)."""
    try:
        to_hex = cloud_storage.hex_for_email(recipient_email) or ""
        if len(to_hex) != 64 or len(from_hex) != 64:
            return False
        r = httpx.post(f"{QDRANT_URL}/collections/faces_{to_hex}/points/delete?wait=true", headers=_headers(), timeout=20,
                       json={"filter": {"must": [{"key": "ref", "match": {"value": "shared"}},
                                                 {"key": "pubkey", "match": {"value": from_hex}}]}})
        return r.status_code in (200, 404)
    except Exception as exc:
        logger.warning("face_ref: effacement impossible (%s)", exc)
        return False
