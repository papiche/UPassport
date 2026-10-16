"""
services/videofun.py — Les vidéos FUN vivent dans l'ucloud : dossier DAV `/VideoFUN/<ami>/<date> <titre>.mp4`.

Chaque vidéo produite par le Studio (story.html) est COPIÉE, chiffrée comme n'importe quel fichier du cloud, dans `/VideoFUN/` :
visible par tout client DAV (Finder, Files, Nextcloud…), comptée dans l'espace du MULTIPASS, sauvegardée avec le reste, et
gérée depuis ucloud.html (onglet Vidéos). Les métadonnées (pour qui, titre, durée, personnages, scène d'origine) suivent le
fichier dans l'entrée d'index (`entry["videofun"]`) : un déplacement ou un renommage DAV ne les perd pas.

Fait le lien avec le CARNET D'ADRESSES : `to` (prénom) et `contact` (UID de la fiche) rangent les vidéos par ami.
Bibliothèque standard + cloud_storage (ffprobe seulement pour lire la durée).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services import cloud_storage, ucloud_db

ROOT = "/VideoFUN"
RECEIVED_DIR = "Reçues"          # /VideoFUN/Reçues/<expéditeur>/ : vidéos envoyées par des amis (cloud à cloud)
VIDEO_MIMES = ("video/mp4", "video/webm", "video/quicktime")


def ffmpeg_bin() -> Optional[str]:
    return os.environ.get("FFMPEG") or shutil.which("ffmpeg")


def ffprobe_bin() -> Optional[str]:
    return os.environ.get("FFPROBE") or shutil.which("ffprobe")


def duration_of(path: Path) -> float:
    probe = ffprobe_bin()
    if not probe:
        return 0.0
    try:
        out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def poster_frame(blob: Optional[bytes], path: Optional[Path], out: Path, at: float = 1.0) -> None:
    """Une image JPEG de la vidéo (à `at` s, ou au début si elle est plus courte) : l'aperçu du lien envoyé. `blob` (octets) OU `path`.
    Lève RuntimeError si ffmpeg manque ou échoue (jamais d'image vide silencieuse)."""
    ff = ffmpeg_bin()
    if not ff:
        raise RuntimeError("ffmpeg absent")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="poster_") as tmp:
        src = Path(path) if path else Path(tmp) / "in.mp4"
        if not path:
            src.write_bytes(blob or b"")
        tmp_out = Path(tmp) / "p.jpg"
        for t in (at, 0.0):
            proc = subprocess.run([ff, "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-vf", "scale='min(1200,iw)':-2",
                                   "-q:v", "4", str(tmp_out)], capture_output=True, text=True, timeout=60)
            if proc.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0:
                break
        else:
            raise RuntimeError(f"image d'aperçu impossible : {(proc.stderr or '').strip()[-200:]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(out.with_name(f".{out.name}.tmp")), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(tmp_out.read_bytes())
        os.replace(out.with_name(f".{out.name}.tmp"), out)


_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_segment(raw: str, fallback: str) -> str:
    s = _BAD.sub("", str(raw or "")).strip(" .")
    return re.sub(r"\s+", " ", s)[:60] or fallback


def target_path(to: str, title: str, ts: Optional[float] = None) -> str:
    """`/VideoFUN/<ami>/<AAAA-MM-JJ> <titre>.mp4` ; sans destinataire : `/VideoFUN/Divers/…`."""
    day = time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))
    return f"{ROOT}/{clean_segment(to, 'Divers')}/{day} {clean_segment(title, 'Vidéo FUN')}.mp4"


def ensure_root(email: str) -> str:
    """Crée `/VideoFUN/` s'il n'existe pas (visible dès maintenant par les clients DAV) ; rend son chemin."""
    cloud_storage.ensure_folder(email, ROOT)
    return ROOT


def archive(email: str, mp4: Path, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Range une vidéo dans `/VideoFUN/…`. `meta` : to, contact, title, max_seconds, cast, scene, scene_cid, brief.
    Rend {path, size, duration}. Lève cloud_storage / storage_quota si l'espace est plein ou le fichier trop gros."""
    src = Path(mp4)
    data = src.read_bytes()
    duration = duration_of(src)
    now = int(time.time())
    path = target_path(meta.get("to", ""), meta.get("title", ""), now)
    ensure_root(email)
    entry = cloud_storage.ingest_plaintext(
        email, path, data, content_type="video/mp4", trigger_faceid=False, unique=True,
        extra={"videofun": {**{k: meta.get(k) for k in ("to", "contact", "title", "cast", "scene", "scene_cid", "brief", "max_seconds")},
                            "created": now, "duration": round(duration, 1), "source": "studio"}})
    return {"path": entry["path"], "size": len(data), "duration": round(duration, 1)}


def send_to_friend(owner_email: str, path: str, from_name: str, pubkey: str = "", to_email: str = "") -> Dict[str, Any]:
    """Envoie une vidéo de /VideoFUN/ DIRECTEMENT dans le cloud d'un ami qui a un MULTIPASS : même bloc chiffré, même clé (aucun rechiffrement,
    aucun lien à ouvrir) — il la retrouve dans /VideoFUN/Reçues/<toi>/. Même station → `share_copy` ; autre station de la constellation →
    canal inter-NODE `ucloud_share` (`node_share.send`). Rend {mode: "local"|"node", status, path?}.
    Lève LookupError si la personne n'a pas de MULTIPASS connu (l'appelant propose alors le lien secret) ; ValueError/FileNotFoundError si
    la vidéo n'est pas une vidéo de /VideoFUN/."""
    from services import identity_links, node_share
    p = in_root(path)
    mime = (cloud_storage.db_entry(owner_email, p) or {}).get("mime") or ""
    if not str(mime).startswith("video/"):
        raise ValueError("ce fichier n'est pas une vidéo")
    from_hex = cloud_storage.hex_for_email(owner_email) or ""
    pubkey, to_email = (pubkey or "").strip().lower(), (to_email or "").strip().lower()
    recipient = cloud_storage.email_for_hex(pubkey) if pubkey else (to_email if to_email and cloud_storage.hex_for_email(to_email) else None)
    if recipient:
        if recipient.lower() == owner_email.lower():
            raise ValueError("c'est votre propre compte")
        res = cloud_storage.share_copy(owner_email, p, recipient, from_hex, from_name)
        return {"mode": "local", "status": res["status"], "path": res.get("path", "")}
    account = identity_links.email_for_hex(pubkey) if pubkey else None
    if not account and to_email:
        exact = [a for a in identity_links.accounts_for_emails([to_email]) if a.get("match") == "exact"]
        account = exact[0] if exact else None
    if not account or not node_share.node_hex_for(account.get("node", "")):
        raise LookupError("aucun MULTIPASS connu pour cette personne")
    res = node_share.send(owner_email, from_name, account, [p])
    return {"mode": "node", "status": res.get("status", "error"), "path": ""}


def list_videos(email: str) -> List[Dict[str, Any]]:
    """Les vidéos de `/VideoFUN/` (récentes d'abord), avec leurs métadonnées ; une vidéo déposée à la main (DAV) est listée aussi."""
    udir = cloud_storage.ensure_ucloud_dir(email)
    lo, hi = ucloud_db._range(ROOT)
    conn = ucloud_db.connect(udir)
    out = []
    for path, data in conn.execute("SELECT path, data FROM entries WHERE type='file' AND cid IS NOT NULL AND path>=? AND path<? ORDER BY mtime DESC", (lo, hi)):
        import json as _json
        e = _json.loads(data)
        if not str(e.get("mime") or "").startswith("video/"):
            continue
        v = e.get("videofun") or {}
        parts = path[len(ROOT) + 1:].split("/")
        received = len(parts) > 2 and parts[0] == RECEIVED_DIR                  # /VideoFUN/Reçues/<expéditeur>/… : envoyée PAR un ami
        out.append({"path": path, "name": parts[-1], "folder": (f"{RECEIVED_DIR} de {parts[1]}" if received else parts[0] if len(parts) > 1 else ""),
                    "received": received, "from_name": e.get("from_name") or (parts[1] if received else ""), "size": e.get("size_plain") or 0,
                    "mtime": e.get("mtime") or 0, "mime": e.get("mime"), "to": "" if received else (v.get("to") or (parts[0] if len(parts) > 1 else "")),
                    "contact": v.get("contact"), "title": v.get("title") or Path(path).stem, "duration": v.get("duration"),
                    "cast": v.get("cast") or [], "created": v.get("created") or e.get("mtime"), "source": v.get("source") or "dav"})
    return out


def in_root(path: str) -> str:
    """Chemin normalisé, ou ValueError s'il sort de /VideoFUN/ (jamais d'accès au reste du cloud par ces routes)."""
    p = cloud_storage.normalize_path(path)
    if not (p == ROOT or p.startswith(ROOT + "/")):
        raise ValueError("chemin hors de /VideoFUN/")
    return p


def read_video(email: str, path: str) -> Tuple[bytes, str]:
    p = in_root(path)
    data, mime = cloud_storage.read_plaintext(email, p)
    if not str(mime).startswith("video/"):
        raise ValueError("ce fichier n'est pas une vidéo")
    return data, mime


def delete_video(email: str, path: str) -> bool:
    p = in_root(path)
    deleted, _skipped = cloud_storage.delete_paths(email, [p], True)
    return bool(deleted)
