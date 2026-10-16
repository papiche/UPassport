"""
services/friend_card.py — La « fiche d'un ami » : tout ce que le cloud sait d'UNE personne du carnet d'adresses, hors visages.

Le carnet d'adresses est le point de rassemblement : une personne = un nom (+ UID de contact, clé NOSTR). Les photos où elle
apparaît viennent des visages (Qdrant, déjà dans l'interface) ; ici on réunit ce qui est rangé dans le cloud chiffré :
  - `received`  : les photos qu'ELLE m'a partagées (`/Reçues/<nom>/…`)
  - `videos_received` : les vidéos FUN qu'ELLE m'a envoyées (`/VideoFUN/Reçues/<nom>/`)
  - `videos`    : les vidéos FUN faites pour elle (`/VideoFUN/<nom>/…` ou `entry.videofun.contact`)
  - `shares`    : les liens d'envoi encore valables vers elle (vues comprises — « elle l'a regardée »), Capitaine seulement
Lecture seule, une requête SQL par facette, jamais de clair.
"""

from __future__ import annotations

from typing import Any, Dict, List

from services import cloud_storage, story_share, ucloud_db, videofun

RECEIVED_LIMIT = 24


def received_from(email: str, name: str, hex_key: str = "") -> Dict[str, Any]:
    """Photos reçues de cette personne : segments `/Reçues/<nom>/` (nom affiché) et `/Reçues/<8 premiers hex>/` (nom inconnu à l'envoi)."""
    udir = cloud_storage.ensure_ucloud_dir(email)
    conn = ucloud_db.connect(udir)
    segs = {cloud_storage._safe_segment(name, "")} if name else set()
    if hex_key:
        segs.add(hex_key[:8])
    segs.discard("")
    count, last, items = 0, 0, []
    for seg in sorted(segs):
        lo, hi = ucloud_db._range(f"/Reçues/{seg}")
        n, m = conn.execute("SELECT COUNT(*), COALESCE(MAX(mtime),0) FROM entries WHERE type='file' AND cid IS NOT NULL AND path>=? AND path<?", (lo, hi)).fetchone()
        count, last = count + n, max(last, m)
        for path, mtime in conn.execute("SELECT path, mtime FROM entries WHERE type='file' AND cid IS NOT NULL AND mime LIKE 'image/%' AND path>=? AND path<? "
                                        "ORDER BY mtime DESC LIMIT ?", (lo, hi, RECEIVED_LIMIT)):
            items.append({"path": path, "mtime": mtime})
    items.sort(key=lambda i: -i["mtime"])
    return {"count": count, "last": last, "items": items[:RECEIVED_LIMIT]}


def videos_for(email: str, name: str, contact: str = "") -> List[Dict[str, Any]]:
    n = (name or "").strip().casefold()
    out = []
    for v in videofun.list_videos(email):
        if v.get("received"):
            continue
        if (contact and v.get("contact") == contact) or (n and str(v.get("to") or "").strip().casefold() == n):
            out.append(v)
    return out


def shares_to(library, name: str) -> List[Dict[str, Any]]:
    n = (name or "").strip().casefold()
    if not n:
        return []
    return [{k: s.get(k) for k in ("token", "title", "created", "expires", "views")} for s in story_share.listing(library)
            if str(s.get("to") or "").strip().casefold() == n]


def videos_from(email: str, name: str) -> List[Dict[str, Any]]:
    """Vidéos que CETTE personne m'a envoyées (`/VideoFUN/Reçues/<nom>/`)."""
    n = (name or "").strip().casefold()
    return [v for v in videofun.list_videos(email) if v.get("received") and str(v.get("from_name") or "").strip().casefold() == n]


def card(email: str, name: str, hex_key: str = "", contact: str = "", library=None) -> Dict[str, Any]:
    return {"name": name, "received": received_from(email, name, hex_key), "videos": videos_for(email, name, contact),
            "videos_received": videos_from(email, name),
            "shares": shares_to(library, name) if library is not None else []}
