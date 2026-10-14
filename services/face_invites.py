"""
services/face_invites.py — Invitations FaceCloud portables entre stations (via le relais NOSTR commun).

Problème : un jeton « claim » écrit dans ~/.zen/game/nostr/<expéditeur>/.face_invites/ n'existe que sur
la station de l'expéditeur. Si le destinataire a déjà un MULTIPASS ailleurs dans la constellation, cette
station ne le sait pas (elle ne regarde que ses fichiers locaux) et en crée un SECOND avec le même email.

Ici, l'invitation vit sur le relais commun (et le relais local), indépendamment de toute station :

  invitation   kind 30078, d=facecloud:invite:<h>:<n>, t=<h>   signé par le MULTIPASS de l'expéditeur
               <h> = sha256(jeton)[:32] ; contenu = AES-256-GCM (clé dérivée du JETON) en base64 de
               {owner_email, from_hex, from_name, to_email, open, expires_at,
                items:[{cid, key, sha, name, mime, point_id, bbox}]}
               → seul le porteur du lien peut lire les clés des photos ; le relais ne voit rien.
               Découpée en lots de CHUNK_ITEMS photos (limite de taille d'un événement).
  réclamée     kind 30078, d=facecloud:claimed:<h>, t=<h>   signé par le MULTIPASS du destinataire
               → l'invitation est à usage unique, quelle que soit la station qui l'a honorée.
  existence    kind 0 portant le tag ["i", "email:<email>"] (publié par nostr_setup_profile.py pour
               TOUT MULTIPASS) → « cet email a-t-il déjà un MULTIPASS quelque part ? » se demande au
               relais, pas aux fichiers de la station.

Les photos elles-mêmes ne bougent pas : même bloc IPFS chiffré, même clé (cloud_storage.adopt_blob,
verify=True : récupéré, déchiffré, empreinte contrôlée, épinglé sur la station du destinataire).
Toutes les fonctions sont SYNCHRONES (à appeler via asyncio.to_thread) et ne lèvent pas vers l'appelant
pour les pannes de relais : elles rendent None / [] / False et journalisent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from services import cloud_storage

logger = logging.getLogger(__name__)

RELAYS = ["ws://127.0.0.1:7777", "wss://relay.copylaradio.com"]
CHUNK_ITEMS = 60
MAX_ITEMS = 2000
D_INVITE = "facecloud:invite:"
D_CLAIMED = "facecloud:claimed:"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def token_hash(token: str) -> str:
    return hashlib.sha256(("facecloud-invite-id\0" + token).encode()).hexdigest()[:32]


def _key_hex(token: str) -> str:
    return hashlib.sha256(("facecloud-invite-key\0" + token).encode()).hexdigest()


def _keys(email: str):
    import nostr_sdk
    raw = (cloud_storage.GAME_NOSTR_PATH / email / ".secret.nostr").read_text()
    m = re.search(r"NSEC=([^;\s]+)", raw)
    if not m:
        raise RuntimeError("NSEC introuvable")
    return nostr_sdk.Keys.parse(m.group(1))


# ── relais ──────────────────────────────────────────────────────────────────
class RelayUnavailable(RuntimeError):
    """Aucun relais n'a répondu : « pas de résultat » ne veut alors RIEN dire."""


def _query(filt: Dict[str, Any], timeout: float = 8.0, strict: bool = False) -> List[Dict[str, Any]]:
    """Événements correspondant à `filt`, dédoublonnés par id, sur tous les relais joignables.
    `strict` : lève RelayUnavailable si aucun relais n'a répondu (sinon rend simplement [])."""
    from websockets.sync.client import connect
    found: Dict[str, Dict[str, Any]] = {}
    answered = 0
    for relay in RELAYS:
        try:
            with connect(relay, open_timeout=timeout, close_timeout=3) as ws:
                ws.send(json.dumps(["REQ", "fci", filt]))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=timeout))
                    if msg[0] == "EVENT":
                        found[msg[2]["id"]] = msg[2]
                    elif msg[0] in ("EOSE", "CLOSED"):
                        answered += 1
                        break
        except Exception as exc:
            logger.warning("face_invites: %s injoignable (%s)", relay, exc)
    if strict and not answered:
        raise RelayUnavailable("aucun relais NOSTR ne répond")
    return list(found.values())


def _publish(keys, events_spec: List[tuple]) -> List[str]:
    """Publie [(d, t, content, expires_at)] (kind 30078). Rend les relais qui ont TOUT accepté."""
    import nostr_sdk
    from websockets.sync.client import connect
    raws = []
    for d, t, content, expires in events_spec:
        tags = [nostr_sdk.Tag.parse(["d", d]), nostr_sdk.Tag.parse(["t", t])]
        if expires:
            tags.append(nostr_sdk.Tag.parse(["expiration", str(int(expires))]))
        raws.append(json.loads(nostr_sdk.EventBuilder(nostr_sdk.Kind(30078), content).tags(tags).finalize(keys).as_json()))
    ok = []
    for relay in RELAYS:
        try:
            with connect(relay, open_timeout=10, close_timeout=5) as ws:
                accepted = 0
                for ev in raws:
                    ws.send(json.dumps(["EVENT", ev]))
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        msg = json.loads(ws.recv(timeout=10))
                        if msg[0] == "OK":
                            if msg[2]:
                                accepted += 1
                            else:
                                logger.warning("face_invites: %s refuse : %s", relay, msg[3] if len(msg) > 3 else "")
                            break
                if accepted == len(raws):
                    ok.append(relay)
        except Exception as exc:
            logger.warning("face_invites: %s injoignable (%s)", relay, exc)
    return ok


# ── invitation ──────────────────────────────────────────────────────────────
def items_from_local(owner_email: str, photos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Photos du cloud de `owner_email` → items portables {cid, key, sha, name, mime, point_id, bbox}."""
    items = []
    for ref in photos[:MAX_ITEMS]:
        path = ref.get("source_path") or ""
        entry = cloud_storage.db_entry(owner_email, path)
        if not entry or not entry.get("cid"):
            continue
        key_hex = cloud_storage.get_key_hex(owner_email, entry["cid"])
        if not key_hex:
            continue
        items.append({"cid": entry["cid"], "key": key_hex, "sha": entry.get("sha256_plain") or "",
                      "name": cloud_storage.base_name(cloud_storage.normalize_path(path)),
                      "mime": entry.get("mime") or "image/jpeg",
                      "point_id": str(ref.get("point_id") or ""), "bbox": ref.get("bbox") or {}})
    return items


def publish(owner_email: str, token: str, data: Dict[str, Any], from_name: str) -> List[str]:
    """Publie l'invitation locale `data` (celle de .face_invites/<jeton>.json) sur les relais.
    Rend la liste des relais l'ayant acceptée ([] = non portable)."""
    try:
        items = items_from_local(owner_email, data.get("photos") or [])
        if not items:
            return []
        h, key = token_hash(token), _key_hex(token)
        from services import face_ref
        ref = face_ref.payload_for(owner_email, None)         # empreinte offerte avec les photos (chiffrée par le jeton)
        meta = {"owner_email": owner_email, "from_hex": cloud_storage.hex_for_email(owner_email) or "",
                "face_ref": {"vectors": ref["vectors"]} if ref else None,
                "from_name": from_name, "to_email": data.get("to_email") or "", "open": bool(data.get("open")),
                "expires_at": int(data.get("expires_at") or 0)}
        chunks = [items[i:i + CHUNK_ITEMS] for i in range(0, len(items), CHUNK_ITEMS)]
        spec = []
        for n, chunk in enumerate(chunks):
            payload, _iv = cloud_storage.uenc_codec.encrypt_aes256gcm(
                json.dumps(dict(meta, n=n, total=len(chunks), items=chunk), separators=(",", ":")).encode(), key)
            spec.append((f"{D_INVITE}{h}:{n}", h, base64.b64encode(payload).decode(), meta["expires_at"]))
        return _publish(_keys(owner_email), spec)
    except Exception as exc:
        logger.warning("face_invites: publication de l'invitation impossible (%s)", exc)
        return []


def is_claimed(token: str) -> bool:
    h = token_hash(token)
    return bool(_query({"kinds": [30078], "#d": [D_CLAIMED + h], "limit": 1}, timeout=4))


def fetch(token: str) -> Optional[Dict[str, Any]]:
    """L'invitation `token` lue sur les relais : {owner_email, from_hex, from_name, to_email, open,
    expires_at, items}. None si absente, expirée, illisible, incomplète ou déjà réclamée."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token or ""):
        return None
    h, key = token_hash(token), _key_hex(token)
    events = _query({"kinds": [30078], "#t": [h], "limit": 200})
    if any((ev.get("tags") and any(t[:2] == ["d", D_CLAIMED + h] for t in ev["tags"])) for ev in events):
        return None
    chunks: Dict[int, Dict[str, Any]] = {}
    for ev in sorted(events, key=lambda e: e["created_at"]):
        d = next((t[1] for t in ev["tags"] if t[0] == "d"), "")
        if not d.startswith(D_INVITE + h + ":"):
            continue
        try:
            part = json.loads(cloud_storage.uenc_codec.decrypt_aes256gcm(base64.b64decode(ev["content"]), key))
            chunks[int(part["n"])] = part
        except Exception:
            continue                                    # contenu non chiffré avec ce jeton : ignoré
    if not chunks:
        return None
    first = chunks[min(chunks)]
    if len(chunks) != int(first.get("total") or 0) or int(first.get("expires_at") or 0) < time.time():
        return None
    record = {k: first.get(k) for k in ("owner_email", "from_hex", "from_name", "to_email", "open", "expires_at", "face_ref")}
    record["items"] = [it for n in sorted(chunks) for it in chunks[n]["items"]]
    return record


def mark_claimed(token: str, claimant_email: str) -> bool:
    """Marque l'invitation consommée (événement signé par le destinataire, local à cette station)."""
    try:
        h = token_hash(token)
        spec = [(D_CLAIMED + h, h, base64.b64encode(os.urandom(8)).decode(), int(time.time()) + 30 * 24 * 3600)]
        return bool(_publish(_keys(claimant_email), spec))
    except Exception as exc:
        logger.warning("face_invites: marquage « réclamée » impossible (%s)", exc)
        return False


def adopt_items(email: str, record: Dict[str, Any]) -> Dict[str, int]:
    """Range les photos de l'invitation dans le cloud de `email` (hébergé ICI) : même bloc, même clé."""
    counts = {"copied": 0, "duplicate": 0, "error": 0}
    for it in record.get("items") or []:
        try:
            res = cloud_storage.adopt_blob(
                email, {"cid": it["cid"], "sha256_plain": it.get("sha") or "", "mime": it.get("mime") or "image/jpeg"},
                it["key"], it.get("name") or "photo.jpg", record.get("from_hex") or "", record.get("from_name") or "",
                verify=True)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
        except Exception as exc:
            counts["error"] += 1
            logger.warning("face_invites: adoption de %s pour %s échouée (%s)", str(it.get("cid"))[:16], email, exc)
    ref = record.get("face_ref")
    if isinstance(ref, dict) and (counts["copied"] or counts["duplicate"]):
        from services import face_ref
        if face_ref.store(email, record.get("from_hex") or "", record.get("from_name") or "", ref.get("vectors") or []):
            owner = record.get("owner_email") or ""
            if owner and cloud_storage.hex_for_email(owner):          # expéditeur hébergé ici : registre à jour
                face_ref.mark_given(owner, cloud_storage.hex_for_email(email) or "",
                                    {"sha": face_ref._sha(ref["vectors"])}, email)
    return counts


# ── existence d'un MULTIPASS, où qu'il soit ─────────────────────────────────
def account_for_email(email: str) -> Optional[Dict[str, str]]:
    """{hex} du MULTIPASS portant cet email (profil kind 0 avec ["i","email:<email>"]) sur le relais
    commun, ou None. N'utilise AUCUN fichier de la station : valable pour toute la constellation.
    Lève RelayUnavailable si aucun relais ne répond (créer un MULTIPASS « à l'aveugle » ferait un doublon)."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return None
    best = None
    for ev in _query({"kinds": [0], "#i": [f"email:{email}"], "limit": 5}, strict=True):
        if any(t[:2] == ["i", f"email:{email}"] for t in ev.get("tags", [])):
            if not best or ev["created_at"] > best["created_at"]:
                best = ev
    return {"hex": best["pubkey"]} if best else None
