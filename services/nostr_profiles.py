"""
services/nostr_profiles.py — Lire les PROFILS (kind 0) NOSTR sur plusieurs relais, et en garder les vignettes.

Pourquoi : `fetch_nostr_profiles` (services/nostr.py) n'interroge que le relais local ; tout ami dont le
profil n'y est pas arrivé s'affichait sans nom ni vignette. Ici on parcourt les RELAIS PRÉFÉRÉS de
l'utilisateur, dans cet ordre :
    1. le relais de cette station (settings.NOSTR_RELAYS) ;
    2. ses relais déclarés (kind 10002, NIP-65, lus sur ces mêmes relais) ;
    3. les relais publics des autres stations de la constellation (~/.zen/tmp/swarm/*/12345.json → myRELAY).
Chaque relais est interrogé en parallèle (REQ kind 0, jusqu'à EOSE ou délai) ; on garde, par clé, le profil
le PLUS RÉCENT. Résultat mis en cache sur disque (24 h ; 6 h si introuvable) pour ne pas relancer.

AVATARS : la page ne charge JAMAIS d'image extérieure (pas de fuite d'adresse IP vers un hébergeur inconnu). La
station télécharge l'image du profil UNE fois, contrôle l'adresse (pas d'accès au réseau interne), la réduit à
96 px en JPEG et la sert elle-même : `avatar_jpeg(hex)`. Les échecs sont mémorisés (6 h).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
import websockets

from core.config import settings

logger = logging.getLogger(__name__)

CACHE_DIR = settings.GAME_PATH / "profiles"
PROFILE_TTL = 24 * 3600
MISS_TTL = 6 * 3600
RELAY_TIMEOUT = 8.0
MAX_RELAYS = 8
MAX_AVATAR_BYTES = 2 * 1024 * 1024
AVATAR_SIZE = 96
_HEX = re.compile(r"^[0-9a-f]{64}$")


# ═════════════════════════════════════════════════════════════════════════════
# Relais préférés
# ═════════════════════════════════════════════════════════════════════════════
def _norm_relay(url: str) -> Optional[str]:
    url = (url or "").strip().rstrip("/")
    return url if re.match(r"^wss?://[^\s]+$", url) else None


def station_relays() -> List[str]:
    out = [_norm_relay(u) for u in str(settings.NOSTR_RELAYS or "").split()]
    return [u for u in out if u]


def swarm_relays() -> List[str]:
    """Relais PUBLICS (wss) annoncés par les autres stations de la constellation."""
    out: List[str] = []
    for f in sorted((Path.home() / ".zen" / "tmp" / "swarm").glob("*/12345.json")):
        try:
            u = _norm_relay(json.loads(f.read_text()).get("myRELAY", ""))
        except (OSError, ValueError):
            continue
        if u and u.startswith("wss://") and u not in out:
            out.append(u)
    return out


async def _query(relay: str, flt: Dict[str, Any], timeout: float = RELAY_TIMEOUT) -> List[Dict[str, Any]]:
    """REQ unique → événements reçus avant EOSE / délai. Jamais d'exception : un relais mort rend []."""
    events: List[Dict[str, Any]] = []
    sub = "p" + uuid.uuid4().hex[:8]
    try:
        async with websockets.connect(relay, open_timeout=min(timeout, 6), max_size=2 ** 22) as ws:
            await ws.send(json.dumps(["REQ", sub, flt]))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, list) or len(msg) < 2 or msg[1] != sub:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3 and isinstance(msg[2], dict):
                    events.append(msg[2])
                elif msg[0] in ("EOSE", "CLOSED"):
                    break
            try:
                await ws.send(json.dumps(["CLOSE", sub]))
            except Exception:
                pass
    except Exception as exc:
        logger.debug("profiles: %s injoignable (%s)", relay, type(exc).__name__)
    return events


async def preferred_relays(me_hex: str) -> List[str]:
    """Relais de la station, puis ceux que l'utilisateur déclare (NIP-65), puis ceux de la constellation."""
    relays = station_relays()
    declared: List[str] = []
    if _HEX.match(me_hex or ""):
        for ev in await _query(relays[0], {"kinds": [10002], "authors": [me_hex], "limit": 1}, 5.0) if relays else []:
            for tag in ev.get("tags", []):
                if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "r":
                    u = _norm_relay(tag[1])
                    if u and u not in declared:
                        declared.append(u)
    out: List[str] = []
    for u in relays + declared + swarm_relays():
        if u not in out:
            out.append(u)
    return out[:MAX_RELAYS]


# ═════════════════════════════════════════════════════════════════════════════
# Profils
# ═════════════════════════════════════════════════════════════════════════════
def _cache_file(pubkey: str) -> Path:
    return CACHE_DIR / f"{pubkey}.json"


def _read_cache(pubkey: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(_cache_file(pubkey).read_text())
    except (OSError, ValueError):
        return None
    ttl = PROFILE_TTL if data.get("profile") else MISS_TTL
    return data if time.time() - float(data.get("at", 0)) < ttl else None


def _write_cache(pubkey: str, profile: Optional[Dict[str, Any]], relays: List[str]) -> None:
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = _cache_file(pubkey).with_name(f".{pubkey}.{uuid.uuid4().hex[:6]}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"at": time.time(), "profile": profile, "relays": relays}, fh)
    os.replace(tmp, _cache_file(pubkey))


def _clean_profile(content: str) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(content or "{}")
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    def s(k: str, n: int = 300) -> str:
        v = d.get(k)
        return str(v).strip()[:n] if isinstance(v, (str, int, float)) else ""
    return {"name": s("name", 80), "display_name": s("display_name", 80) or s("displayName", 80), "about": s("about", 400),
            "picture": s("picture", 500), "nip05": s("nip05", 120), "email": s("email", 120) or s("lud16", 120)}


def cached_profile(pubkey: str) -> Optional[Dict[str, Any]]:
    """Profil déjà lu (cache disque, sans réseau) ou None."""
    c = _read_cache(str(pubkey).lower())
    return (c or {}).get("profile") or None


async def read_profiles(pubkeys: Iterable[str], me_hex: str = "", force: bool = False) -> Dict[str, Any]:
    """{profiles: {hex: profil}, missing: [hex], relays: [relais interrogés], cached: n}.
    Seules les clés ABSENTES du cache (ou périmées) sont demandées aux relais."""
    keys = [k for k in dict.fromkeys(str(k).lower() for k in pubkeys) if _HEX.match(k)]
    out: Dict[str, Any] = {}
    todo: List[str] = []
    for k in keys:
        c = None if force else _read_cache(k)
        if c is not None:
            if c.get("profile"):
                out[k] = c["profile"]
        else:
            todo.append(k)
    cached = len(keys) - len(todo)
    relays: List[str] = []
    if todo:
        relays = await preferred_relays(me_hex)
        newest: Dict[str, Dict[str, Any]] = {}
        sem = asyncio.Semaphore(4)

        async def one(relay: str, chunk: List[str]) -> None:
            async with sem:
                for ev in await _query(relay, {"kinds": [0], "authors": chunk, "limit": len(chunk) * 2}):
                    pk = str(ev.get("pubkey", "")).lower()
                    if pk in chunk and (pk not in newest or int(ev.get("created_at", 0)) > int(newest[pk].get("created_at", 0))):
                        newest[pk] = ev
        await asyncio.gather(*(one(r, todo[i:i + 100]) for r in relays for i in range(0, len(todo), 100)))
        for k in todo:
            prof = _clean_profile(newest[k].get("content", "")) if k in newest else None
            _write_cache(k, prof, relays)
            if prof:
                out[k] = prof
    return {"profiles": out, "missing": [k for k in keys if k not in out], "relays": relays, "cached": cached}


# ═════════════════════════════════════════════════════════════════════════════
# Avatars servis par la station
# ═════════════════════════════════════════════════════════════════════════════
def _avatar_file(pubkey: str) -> Path:
    return CACHE_DIR / f"{pubkey}.jpg"


def _avatar_miss(pubkey: str) -> Path:
    return CACHE_DIR / f"{pubkey}.nopic"


async def _download(url: str) -> Optional[bytes]:
    from services.webdav_client import WebdavError, validate_remote_url
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            for _ in range(4):
                await validate_remote_url(url)          # http(s) vers une adresse non interne, à CHAQUE saut
                async with client.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 307, 308) and resp.headers.get("location"):
                        url = str(httpx.URL(url).join(resp.headers["location"]))
                        continue
                    if resp.status_code != 200 or not str(resp.headers.get("content-type", "")).startswith("image/"):
                        return None
                    buf = b""
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) > MAX_AVATAR_BYTES:
                            return None
                    return buf
    except (WebdavError, httpx.HTTPError):
        return None
    return None


def _to_jpeg(data: bytes) -> Optional[bytes]:
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (AVATAR_SIZE * 3, AVATAR_SIZE * 3))
        img = ImageOps.fit(ImageOps.exif_transpose(img).convert("RGB"), (AVATAR_SIZE, AVATAR_SIZE))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=82)
        return out.getvalue()
    except Exception:
        return None


async def avatar_jpeg(pubkey: str, picture_url: str = "") -> Optional[bytes]:
    """Vignette 96 px du profil `pubkey`, depuis le cache de la station ; sinon téléchargée une fois."""
    pubkey = pubkey.lower()
    if not _HEX.match(pubkey):
        return None
    f = _avatar_file(pubkey)
    try:
        return f.read_bytes()
    except OSError:
        pass
    miss = _avatar_miss(pubkey)
    try:
        if time.time() - miss.stat().st_mtime < MISS_TTL:
            return None
    except OSError:
        pass
    url = picture_url
    if not url:
        c = _read_cache(pubkey)
        url = ((c or {}).get("profile") or {}).get("picture", "")
    jpeg = None
    if url:
        raw = await _download(url)
        jpeg = _to_jpeg(raw) if raw else None
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if jpeg:
        f.write_bytes(jpeg)
        os.chmod(f, 0o600)
    else:
        miss.touch()
    return jpeg
