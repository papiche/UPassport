"""
services/story_share.py — Envoyer une vidéo du Studio à un ami par un LIEN (WhatsApp, Telegram, SMS, mail…).

Un lien = un jeton secret aléatoire (capacité) vers UN rendu précis, valable `TTL_DAYS` jours, révocable. L'ami n'a besoin
ni de compte ni d'application : la page `/api/story/watch/<jeton>` lit la vidéo directement depuis la station. Les jetons sont
rangés dans `<bibliothèque du Studio>/shares/` (0700) ; la vidéo n'est jamais copiée ailleurs. Aucun contenu public : sans le
jeton, rien n'est accessible, et le jeton ne donne accès qu'à ce rendu (jamais au reste de la bibliothèque).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

TTL_DAYS = 14
MAX_TTL_DAYS = 60
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


def shares_dir(library: Path) -> Path:
    d = Path(library) / "shares"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _path(library: Path, token: str) -> Path:
    if not TOKEN_RE.match(token or ""):
        raise ValueError("jeton invalide")
    return shares_dir(library) / f"{token}.json"


def create(library: Path, version_cid: str, render_id: str, title: str, to_name: str = "", ttl_days: int = TTL_DAYS,
           sender: str = "", source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`source` : {"kind": "render", version_cid, render_id} (défaut, rendu du Studio) ou {"kind": "ucloud", email, path} (vidéo de
    /VideoFUN/ : même lien, mêmes règles — une seule mécanique d'envoi quel que soit l'endroit où vit la vidéo)."""
    token = secrets.token_urlsafe(24)
    now = int(time.time())
    src = source or {"kind": "render", "version_cid": version_cid, "render_id": render_id}
    rec = {"token": token, "source": src, "version_cid": version_cid, "render_id": render_id, "title": title[:80], "to": to_name[:60],
           "sender": sender[:60], "created": now, "expires": now + max(1, min(int(ttl_days), MAX_TTL_DAYS)) * 86400, "views": 0}
    p = _path(library, token)
    tmp = p.with_name(f".{p.name}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(rec, fh, ensure_ascii=False)
    os.replace(tmp, p)
    return rec


def get(library: Path, token: str, count_view: bool = False) -> Optional[Dict[str, Any]]:
    """Le lien s'il existe et n'a pas expiré (un lien expiré est supprimé), sinon None."""
    try:
        p = _path(library, token)
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if int(rec.get("expires", 0)) < time.time():
        p.unlink(missing_ok=True)
        poster_path(library, token).unlink(missing_ok=True)
        return None
    if count_view:
        rec["views"] = int(rec.get("views", 0)) + 1
        rec["last_view"] = int(time.time())
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    return rec


def poster_path(library: Path, token: str) -> Path:
    """Image d'aperçu du lien (une image de la vidéo, pour l'aperçu WhatsApp/Telegram), calculée une fois."""
    return shares_dir(library) / f"{token}.jpg"


def revoke(library: Path, token: str) -> bool:
    try:
        p = _path(library, token)
    except ValueError:
        return False
    if not p.exists():
        return False
    p.unlink()
    poster_path(library, token).unlink(missing_ok=True)
    return True


def listing(library: Path) -> List[Dict[str, Any]]:
    out = []
    for p in sorted(shares_dir(library).glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if int(rec.get("expires", 0)) >= time.time():
            out.append({k: rec.get(k) for k in ("token", "title", "to", "created", "expires", "views", "version_cid", "render_id")})
    return sorted(out, key=lambda r: -int(r["created"] or 0))


def byte_range(header: str, size: int) -> Optional[tuple]:
    """(début, fin incluse) d'un en-tête `Range: bytes=a-b`, None si absent/illisible (réponse complète), (-1, -1) si non satisfaisable."""
    m = re.match(r"^bytes=(\d*)-(\d*)$", (header or "").strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    if m.group(1) == "":                                  # « -N » : les N derniers octets
        n = int(m.group(2))
        return (max(0, size - n), size - 1) if n > 0 else (-1, -1)
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or start > end:
        return (-1, -1)
    return start, min(end, size - 1)
