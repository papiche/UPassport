"""
services/node_share.py — Envoi de photos ENTRE STATIONS par le canal inter-NODE.

Quand la personne reconnue (ou désignée par « Envoyer ses photos ») a son
MULTIPASS sur une AUTRE station de la constellation, la photo ne part plus en
message NIP-17 adressé à la personne (un message par photo, dans sa
messagerie) : elle part de station à station, sur le canal chiffré NIP-44
déjà utilisé par les jobs FaceID (Astroport.ONE/tools/nostr_node_intercom.py,
canal `ucloud_share`), et la station du destinataire la range directement
dans SON cloud, `/Reçues/<expéditeur>/…` — exactement comme une copie entre
comptes d'une même station. Aucun humain ne reçoit de message : seules les
stations se parlent ; le destinataire reçoit au plus un email récapitulatif.

    expéditeur                                  destinataire
    ─────────                                   ────────────
    MÊME bloc chiffré, MÊME clé (aucun rechiffrement)
    1 DM inter-NODE / 100 photos ──────────────▶ bro_dm_daemon.sh (_is_swarm_node)
      {to_email, to_hex, from_hex, from_name,    → ucloud_share.py --receive
       items:[{cid, key, sha, name, mime}]}        vérifie to_email ET to_hex hébergés ici
                                                   ipfs cat → déchiffre → contrôle sha
                                                   → cloud_storage.adopt_blob (même CID,
                                                     épinglé ICI : une copie de plus)
    (accusé `ucloud_share_ack` : ne sert plus qu'aux envois d'anciennes stations)

Annuaire : services/identity_links.py (MULTIPASS → station) ; adresse de la
station (NODEHEX) : ~/.zen/tmp/swarm/<IPFSNODEID>/12345.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from services import cloud_storage

logger = logging.getLogger(__name__)

ZEN = Path.home() / ".zen"
INTERCOM = ZEN / "Astroport.ONE" / "tools" / "nostr_node_intercom.py"
RELAYS = ["wss://relay.copylaradio.com"]
ITEMS_PER_MESSAGE = 100
SHARE_TTL = 7 * 24 * 3600         # une station éteinte quelques jours reçoit quand même
MAX_ITEM_BYTES = 60 * 1024 * 1024
PENDING_MAX_AGE = 30 * 24 * 3600  # copies temporaires désépinglées au plus tard après 30 j


def _node_secret() -> Dict[str, str]:
    try:
        raw = (ZEN / "game" / "secret.nostr").read_text()
    except OSError:
        return {}
    out = {}
    for key in ("NSEC", "HEX"):
        m = re.search(key + r"=([^;\s]+)", raw)
        if m:
            out[key] = m.group(1)
    return out


def node_hex_for(ipfs_node_id: str) -> Optional[str]:
    """NODEHEX (clé NOSTR de la station) publié dans son 12345.json."""
    try:
        data = json.loads((ZEN / "tmp" / "swarm" / ipfs_node_id / "12345.json").read_text())
    except (OSError, ValueError):
        return None
    h = str(data.get("NODEHEX") or "").lower()
    return h if re.fullmatch(r"[0-9a-f]{64}", h) else None


def _intercom_send(to_node_hex: str, channel: str, payload: Dict[str, Any], ttl: int) -> bool:
    secret = _node_secret()
    if not secret.get("NSEC") or not INTERCOM.exists():
        logger.warning("node_share: identité NODE ou %s introuvable", INTERCOM)
        return False
    try:
        proc = subprocess.run(
            [sys.executable, str(INTERCOM), "send", "--nsec-stdin", "--to", to_node_hex,
             "--channel", channel, "--payload", json.dumps(payload, separators=(",", ":")),
             "--ttl", str(ttl), "--relays", *RELAYS],
            input=secret["NSEC"] + "\n", text=True, capture_output=True, timeout=120)
    except Exception as exc:
        logger.warning("node_share: envoi %s échoué (%s)", channel, exc)
        return False
    if proc.returncode != 0:
        logger.warning("node_share: envoi %s refusé : %s", channel, proc.stderr.strip()[:300])
    return proc.returncode == 0


# ── copies temporaires en attente d'accusé de réception ─────────────────────
def _pending_path(email: str) -> Path:
    return cloud_storage.ensure_ucloud_dir(email) / "node_shares.json"


def _load_pending(email: str) -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_pending_path(email).read_text())
    except (OSError, ValueError):
        return {}


def _save_pending(email: str, data: Dict[str, Dict[str, Any]]) -> None:
    path = _pending_path(email)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _unpin(cids: List[str]) -> None:
    for cid in cids:
        try:
            subprocess.run(["ipfs", "pin", "rm", cid], capture_output=True, timeout=60)
        except Exception:
            pass


def expire_pending(email: str) -> int:
    """Désépingle les copies jamais acquittées depuis PENDING_MAX_AGE."""
    pending = _load_pending(email)
    old = [cid for cid, p in pending.items() if time.time() - p.get("at", 0) > PENDING_MAX_AGE]
    if old:
        _unpin(old)
        for cid in old:
            pending.pop(cid, None)
        _save_pending(email, pending)
    return len(old)


# ── envoi ───────────────────────────────────────────────────────────────────
def send(owner_email: str, from_name: str, account: Dict[str, str], paths: List[str]) -> Dict[str, Any]:
    """Envoie `paths` (chemins du cloud de `owner_email`) au MULTIPASS
    `account` = {email, hex, node} hébergé sur une autre station.
    Retourne {"status": "sent"|"no_node"|"error", "photos": n}."""
    node_hex = node_hex_for(account.get("node", ""))
    if not node_hex:
        return {"status": "no_node", "photos": 0}
    from_hex = cloud_storage.hex_for_email(owner_email) or ""
    items: List[Dict[str, str]] = []
    for path in paths:
        entry = cloud_storage.db_entry(owner_email, path)
        if not entry or not entry.get("cid"):
            logger.warning("node_share: %s introuvable", path)
            continue
        key_hex = cloud_storage.get_key_hex(owner_email, entry["cid"])
        if not key_hex:
            logger.warning("node_share: clé absente pour %s", path)
            continue
        # MÊME bloc, MÊME clé : rien n'est rechiffré ni réuploadé. La station du destinataire
        # récupère le bloc auprès du swarm et l'épingle chez elle (copie supplémentaire = vraie
        # redondance) ; la nôtre reste épinglée tant qu'un compte le référence.
        items.append({"cid": entry["cid"], "key": key_hex, "sha": entry.get("sha256_plain") or "",
                      "name": cloud_storage.base_name(cloud_storage.normalize_path(path)),
                      "mime": entry.get("mime") or "image/jpeg"})
    if not items:
        return {"status": "error", "photos": 0}
    from services import face_ref
    return send_items(owner_email, from_hex, from_name, account, items,
                      face_ref=face_ref.payload_for(owner_email, account.get("hex", "")))


def send_items(owner_email: str, from_hex: str, from_name: str, account: Dict[str, str],
               items: List[Dict[str, str]], face_ref: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Envoie des `items` {cid, key, sha, name, mime} déjà prêts (cloud d'un expéditeur OU invitation
    portable, cf. services/face_invites.py) au MULTIPASS `account` = {email, hex, node}."""
    node_hex = node_hex_for(account.get("node", ""))
    if not node_hex:
        return {"status": "no_node", "photos": 0}
    sent = 0
    for i in range(0, len(items), ITEMS_PER_MESSAGE):
        chunk = items[i:i + ITEMS_PER_MESSAGE]
        message = {"to_email": account["email"], "to_hex": account["hex"], "from_hex": from_hex,
                   "from_name": from_name, "reply_node_hex": _node_secret().get("HEX", ""),
                   "owner_email": owner_email, "items": chunk}
        if face_ref and i == 0:       # empreinte de visage de l'expéditeur : dans le 1er message seulement
            message["face_ref"] = {"vectors": face_ref["vectors"]}
        if _intercom_send(node_hex, "ucloud_share", message, SHARE_TTL):
            sent += len(chunk)
            if face_ref and i == 0:
                from services import face_ref as _fr
                if cloud_storage.hex_for_email(owner_email):          # expéditeur hébergé ici : registre à jour
                    _fr.mark_given(owner_email, account["hex"], face_ref, account.get("email", ""))
    # Plus de copies temporaires à suivre (pending) : le bloc partagé est celui du cloud lui-même.
    logger.info("node_share: %s → %s@%s : %d/%d photo(s)", owner_email, account["email"],
                account.get("node", "?")[:12], sent, len(items))
    return {"status": "sent" if sent else "error", "photos": sent}


# ── réception (station du destinataire) ─────────────────────────────────────
def receive(message: Dict[str, Any]) -> Dict[str, Any]:
    """Range les photos reçues d'une autre station. Refuse si le destinataire
    n'est pas hébergé ICI avec CETTE clé (jamais de photo déposée chez la
    mauvaise personne). Retourne les compteurs + les CID effectivement reçus
    (pour l'accusé de réception)."""
    email = str(message.get("to_email") or "")
    if "@" not in email or "/" in email or ".." in email:
        return {"status": "refused", "reason": "email invalide"}
    if (cloud_storage.hex_for_email(email) or "") != str(message.get("to_hex") or "").lower():
        return {"status": "refused", "reason": "destinataire non hébergé ici"}
    from services import face_ref
    from_hex = str(message.get("from_hex") or "").lower()
    if message.get("face_ref_revoke"):                      # l'expéditeur retire son empreinte
        face_ref.forget(email, from_hex)
        return {"status": "ok", "email": email, "received": [], "copied": 0, "duplicate": 0, "error": 0}
    if isinstance(message.get("face_ref"), dict):
        face_ref.store(email, from_hex, str(message.get("from_name") or ""), message["face_ref"].get("vectors") or [])
    counts = {"copied": 0, "duplicate": 0, "error": 0}
    received: List[str] = []
    for it in (message.get("items") or [])[:ITEMS_PER_MESSAGE]:
        cid, key, sha = str(it.get("cid") or ""), str(it.get("key") or ""), str(it.get("sha") or "")
        if not cid or len(key) != 64:
            counts["error"] += 1
            continue
        try:
            # Le bloc est récupéré, déchiffré, contrôlé puis ÉPINGLÉ ici : aucun rechiffrement,
            # le même CID est désormais détenu par cette station aussi (cf. adopt_blob).
            res = cloud_storage.adopt_blob(email, {"cid": cid, "sha256_plain": sha, "mime": str(it.get("mime") or "image/jpeg")},
                                           key, str(it.get("name") or "photo.jpg"),
                                           str(message.get("from_hex") or ""), str(message.get("from_name") or ""),
                                           verify=True)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            received.append(cid)
        except Exception as exc:
            counts["error"] += 1
            logger.warning("node_share: réception %s pour %s échouée (%s)", cid[:16], email, exc)
    return {"status": "ok", "email": email, "received": received, **counts}


def acknowledge(message: Dict[str, Any], result: Dict[str, Any]) -> bool:
    reply = str(message.get("reply_node_hex") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", reply) or not result.get("received"):
        return False
    return _intercom_send(reply, "ucloud_share_ack",
                          {"owner_email": message.get("owner_email"), "cids": result["received"]},
                          SHARE_TTL)


def on_ack(message: Dict[str, Any]) -> int:
    """Station de l'expéditeur : désépingle les copies temporaires reçues."""
    email = str(message.get("owner_email") or "")
    if not cloud_storage.hex_for_email(email):
        return 0
    pending = _load_pending(email)
    done = [c for c in (message.get("cids") or []) if c in pending]
    _unpin(done)
    for c in done:
        pending.pop(c, None)
    _save_pending(email, pending)
    return len(done)


def revoke_remote(owner_email: str, account: Dict[str, str]) -> bool:
    """Demande à la station de `account` ({email, hex, node}) d'effacer mon empreinte de visage."""
    node_hex = node_hex_for(account.get("node", ""))
    if not node_hex:
        return False
    message = {"to_email": account["email"], "to_hex": account["hex"],
               "from_hex": cloud_storage.hex_for_email(owner_email) or "", "from_name": "",
               "owner_email": owner_email, "face_ref_revoke": True, "items": []}
    return _intercom_send(node_hex, "ucloud_share", message, SHARE_TTL)
