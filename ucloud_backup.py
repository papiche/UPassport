#!/usr/bin/env python3
"""
ucloud_backup.py — Sauvegarde et restauration du cloud FaceCloud d'un MULTIPASS.

UNE seule implémentation pour trois usages :
  * sauvegarde quotidienne hors station (RUNTIME/NOSTRCARD.refresh.sh) ;
  * export dans l'archive de nostr_DESTROY_TW.sh (mêmes noms de fichiers
    qu'avant : ucloud_index.json, ucloud_keyring.json, qdrant_faces.json…) ;
  * restauration (FaceCloud, nostr_RESTORE_TW.sh).

Contenu du « paquet » (`export_bundle`) — tout ce qui permet de RETROUVER et
DÉCHIFFRER le cloud ; les fichiers eux-mêmes restent sur IPFS, déjà chiffrés :
    ucloud_index.json       chemins ↔ CID (photos ET fiches de contact .vcf)
    ucloud_keyring.json     clé AES-256 de chaque CID
    webdav_sources.json     sources photo externes (identifiants inclus)
    carddav_sources.json    carnets externes (identifiants inclus)
    webdav_import_state_*.json  ce qui a déjà été importé (évite tout réimport)
    face_invites/*.json     invitations « Envoyer ses photos » en attente
    qdrant_faces.json       catalogue de visages (vecteurs + noms + liens)
Non inclus, reconstruits seuls : résumé et clé du carnet (contacts.enc), cache
de vignettes, jeton WebDAV (à recréer : il donne accès au disque).

Sauvegarde hors station (`snapshot`) : le paquet est compressé, chiffré
AES-256-GCM avec une clé NEUVE, ajouté et épinglé sur IPFS ; UN SEUL event
NOSTR kind 30078 `d=facecloud:backup`, contenu chiffré NIP-44 pour soi-même
({cid, key, sha, at, counts}), est (re)publié — remplacé à chaque nouvelle
version, l'ancienne archive est désépinglée. Rien n'est republié si le paquet
n'a pas changé. Choix délibéré (2026-10-12) : pas un event par contact ni par
fichier — un seul pointeur, peu de métadonnées visibles, un seul event à
remplacer ou supprimer.

Usage :
    ucloud_backup.py snapshot <email>
    ucloud_backup.py export   <email> <dossier>
    ucloud_backup.py restore  <email> (--from-relay | --bundle <dossier>) [--qdrant]
    ucloud_backup.py pin      <email>          épingle tous les CID de l'index
    ucloud_backup.py status   <email>
"""

import sys as _sys
import os as _os

_venv_python = _os.path.expanduser("~/.astro/bin/python3")
if _os.path.exists(_venv_python) and _sys.executable != _venv_python:
    _os.execv(_venv_python, [_venv_python] + _sys.argv)

_UPASSPORT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _UPASSPORT_DIR not in _sys.path:
    _sys.path.insert(0, _UPASSPORT_DIR)
del _sys, _os

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from services import cloud_storage, ucloud_db  # noqa: E402

D_TAG = "facecloud:backup"
RELAYS = ["ws://127.0.0.1:7777", "wss://relay.copylaradio.com"]
QDRANT_URL = "http://localhost:6333"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _user_dir(email: str) -> Path:
    return cloud_storage.GAME_NOSTR_PATH / email


def _qdrant_headers() -> Dict[str, str]:
    try:
        for line in (Path.home() / ".zen" / "ai-company" / ".env").read_text().splitlines():
            if line.startswith("QDRANT_API_KEY="):
                return {"api-key": line.split("=", 1)[1].strip().strip('"')}
    except OSError:
        pass
    return {}


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


# ═════════════════════════════════════════════════════════════════════════════
# Paquet
# ═════════════════════════════════════════════════════════════════════════════
def export_bundle(email: str, out_dir: Path) -> Dict[str, int]:
    """Écrit le paquet dans `out_dir` (noms compatibles nostr_RESTORE_TW.sh)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    udir = cloud_storage.ucloud_dir(email)
    counts: Dict[str, int] = {}
    # L'index et le keyring vivent dans SQLite (ucloud.db) : exportés au format JSON
    # historique, aux noms attendus par nostr_RESTORE_TW.sh.
    if ucloud_db.exists(udir):
        ucloud_db.export_json(udir, out_dir, "ucloud_index.json", "ucloud_keyring.json")
    pairs = [("webdav_sources.json", "webdav_sources.json"), ("carddav_sources.json", "carddav_sources.json")]
    for src, dst in pairs:
        if (udir / src).is_file():
            shutil.copyfile(udir / src, out_dir / dst)
            os.chmod(out_dir / dst, 0o600)
    for state in udir.glob("webdav_import_state_*.json"):
        shutil.copyfile(state, out_dir / state.name)
    invites = _user_dir(email) / ".face_invites"
    if invites.is_dir():
        (out_dir / "face_invites").mkdir(exist_ok=True)
        for f in invites.glob("*.json"):
            shutil.copyfile(f, out_dir / "face_invites" / f.name)
    try:
        counts["files"] = ucloud_db.stats(udir)["files"]
    except (OSError, ValueError, KeyError, RuntimeError):
        counts["files"] = 0

    hex_pub = cloud_storage.hex_for_email(email)
    if hex_pub:
        points = _qdrant_export(f"faces_{hex_pub}")
        if points is not None:
            (out_dir / "qdrant_faces.json").write_text(json.dumps(points))
            counts["faces"] = len(points)
    # Tout le paquet en 0600 / 0700 : noms, identifiants de sources et clés
    # (copyfile ne recopie pas les droits, le umask donnerait 0664).
    for f in out_dir.rglob("*"):
        os.chmod(f, 0o700 if f.is_dir() else 0o600)
    return counts


def _qdrant_export(collection: str) -> Optional[List[Dict[str, Any]]]:
    points: List[Dict[str, Any]] = []
    offset = None
    try:
        with httpx.Client(timeout=60) as client:
            while True:
                body = {"limit": 1000, "with_payload": True, "with_vector": True}
                if offset is not None:
                    body["offset"] = offset
                r = client.post(f"{QDRANT_URL}/collections/{collection}/points/scroll",
                                headers=_qdrant_headers(), json=body)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                res = r.json()["result"]
                points.extend(res.get("points", []))
                offset = res.get("next_page_offset")
                if offset is None:
                    return points
    except Exception as exc:
        _log(f"WARN: export Qdrant {collection} impossible ({exc})")
        return None


def apply_bundle(email: str, bundle: Path, with_qdrant: bool = True) -> Dict[str, int]:
    """Restaure un paquet en FUSION : ce qui manque ici est ajouté, rien de
    local n'est écrasé (une restauration ne détruit jamais une photo ajoutée
    depuis). Retourne les compteurs."""
    udir = cloud_storage.ensure_ucloud_dir(email)
    counts = {"files": 0, "keys": 0, "sources": 0, "faces": 0}
    idx_file, key_file = bundle / "ucloud_index.json", bundle / "ucloud_keyring.json"
    if idx_file.is_file():
        remote_idx = json.loads(idx_file.read_text())
        remote_keys = json.loads(key_file.read_text()) if key_file.is_file() else {}
        with cloud_storage.index_lock(email):
            idx = cloud_storage.load_index(email)
            keyring = cloud_storage.load_keyring(email)
            for path, entry in remote_idx.get("entries", {}).items():
                if path not in idx["entries"]:
                    idx["entries"][path] = entry
                    if isinstance(entry, dict) and entry.get("type") == "file":
                        counts["files"] += 1
            for cid, k in remote_keys.items():
                if cid not in keyring:
                    keyring[cid] = k
                    counts["keys"] += 1
            cloud_storage.save_keyring(email, keyring)
            cloud_storage.save_index(email, idx)
    for name in ("webdav_sources.json", "carddav_sources.json"):
        src = bundle / name
        if src.is_file() and not (udir / name).exists():
            shutil.copyfile(src, udir / name)
            os.chmod(udir / name, 0o600)
            counts["sources"] += 1
    for state in bundle.glob("webdav_import_state_*.json"):
        if not (udir / state.name).exists():
            shutil.copyfile(state, udir / state.name)
            os.chmod(udir / state.name, 0o600)
    inv = bundle / "face_invites"
    if inv.is_dir():
        dest = _user_dir(email) / ".face_invites"
        dest.mkdir(parents=True, exist_ok=True)
        for f in inv.glob("*.json"):
            if not (dest / f.name).exists():
                shutil.copyfile(f, dest / f.name)
    hex_pub = cloud_storage.hex_for_email(email)
    if with_qdrant and hex_pub and (bundle / "qdrant_faces.json").is_file():
        counts["faces"] = _qdrant_restore(f"faces_{hex_pub}", json.loads((bundle / "qdrant_faces.json").read_text()))
    return counts


def _qdrant_restore(collection: str, points: List[Dict[str, Any]]) -> int:
    """Ré-insère les points ABSENTS de la collection (fusion)."""
    if not points:
        return 0
    headers = _qdrant_headers()
    with httpx.Client(timeout=60) as client:
        client.put(f"{QDRANT_URL}/collections/{collection}", headers=headers,
                   json={"vectors": {"size": 512, "distance": "Cosine"}})
        existing = set()
        offset = None
        while True:
            body = {"limit": 1000, "with_payload": False, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            r = client.post(f"{QDRANT_URL}/collections/{collection}/points/scroll", headers=headers, json=body)
            if r.status_code != 200:
                break
            res = r.json()["result"]
            existing.update(str(p["id"]) for p in res.get("points", []))
            offset = res.get("next_page_offset")
            if offset is None:
                break
        todo = [{"id": p["id"], "vector": p["vector"], "payload": p.get("payload") or {}}
                for p in points if str(p.get("id")) not in existing and isinstance(p.get("vector"), list)]
        for i in range(0, len(todo), 256):
            client.put(f"{QDRANT_URL}/collections/{collection}/points?wait=true", headers=headers,
                       json={"points": todo[i:i + 256]})
    return len(todo)


def pin_all(email: str) -> Dict[str, int]:
    """Épingle tous les CID de l'index sur CETTE station (prise de garde après
    restauration : sans ça, les fichiers ne vivent que tant que l'ancienne
    station les garde)."""
    idx = cloud_storage.load_index(email)
    cids = sorted({e["cid"] for e in idx["entries"].values() if isinstance(e, dict) and e.get("cid")})
    ok = failed = 0
    for cid in cids:
        try:
            r = subprocess.run(["ipfs", "pin", "add", "--progress=false", cid], capture_output=True, timeout=600)
            ok += r.returncode == 0
            failed += r.returncode != 0
        except Exception:
            failed += 1
    return {"pinned": ok, "failed": failed, "total": len(cids)}


# ═════════════════════════════════════════════════════════════════════════════
# Sauvegarde hors station : archive chiffrée sur IPFS + 1 event NOSTR
# ═════════════════════════════════════════════════════════════════════════════
def _keys(email: str):
    import nostr_sdk
    raw = (_user_dir(email) / ".secret.nostr").read_text()
    m = re.search(r"NSEC=([^;\s]+)", raw)
    if not m:
        raise RuntimeError("NSEC introuvable")
    return nostr_sdk.Keys.parse(m.group(1))


def _state_path(email: str) -> Path:
    return cloud_storage.ucloud_dir(email) / "backup.json"


def status(email: str) -> Dict[str, Any]:
    try:
        return json.loads(_state_path(email).read_text())
    except (OSError, ValueError):
        return {}


def _tar_bundle(email: str) -> tuple:
    tmp = Path(tempfile.mkdtemp(prefix="ucloud_bundle_"))
    try:
        counts = export_bundle(email, tmp)
        # Empreinte STABLE du contenu (sans dates d'archive) : rien à republier si rien n'a changé
        h = hashlib.sha256()
        for f in sorted(tmp.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(tmp)).encode())
                h.update(f.read_bytes())
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(str(tmp), arcname="bundle")
        return buf.getvalue(), h.hexdigest(), counts
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _publish(keys, content: str) -> List[str]:
    import nostr_sdk
    from websockets.sync.client import connect
    ev = nostr_sdk.EventBuilder(nostr_sdk.Kind(30078), content).tags(
        [nostr_sdk.Tag.parse(["d", D_TAG])]).finalize(keys)
    raw = ev.as_json()
    ok = []
    for relay in RELAYS:
        try:
            with connect(relay, open_timeout=10, close_timeout=5) as ws:
                ws.send(json.dumps(["EVENT", json.loads(raw)]))
                deadline = time.time() + 10
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=10))
                    if msg[0] == "OK":
                        if msg[2]:
                            ok.append(relay)
                        else:
                            _log(f"WARN: {relay} a refusé la sauvegarde : {msg[3] if len(msg) > 3 else ''}")
                        break
        except Exception as exc:
            _log(f"WARN: {relay} injoignable ({exc})")
    return ok


def snapshot(email: str, force: bool = False) -> Dict[str, Any]:
    udir = cloud_storage.ucloud_dir(email)
    if not (ucloud_db.exists(udir) or (udir / "index.json").exists()):
        return {"status": "empty"}
    data, sha, counts = _tar_bundle(email)
    prev = status(email)
    if not force and prev.get("sha") == sha and prev.get("relays"):
        return {"status": "unchanged", **prev}
    import nostr_sdk
    key_hex = os.urandom(32).hex()
    payload, _iv = cloud_storage.uenc_codec.encrypt_aes256gcm(data, key_hex)
    cid = cloud_storage.ipfs_add_bytes(payload, filename="facecloud-backup.uenc", pin=True)
    keys = _keys(email)
    content = nostr_sdk.nip44_encrypt(keys.secret_key(), keys.public_key(), json.dumps({
        "v": 1, "cid": cid, "key": key_hex, "sha": sha, "at": int(time.time()), "counts": counts,
    }), nostr_sdk.Nip44Version.V2)
    relays = _publish(keys, content)
    if not relays:
        subprocess.run(["ipfs", "pin", "rm", cid], capture_output=True, timeout=60)
        return {"status": "error", "reason": "aucun relais n'a accepté la sauvegarde"}
    if prev.get("cid") and prev["cid"] != cid:
        subprocess.run(["ipfs", "pin", "rm", prev["cid"]], capture_output=True, timeout=60)
    state = {"cid": cid, "sha": sha, "at": int(time.time()), "counts": counts, "relays": relays,
             "size": len(payload)}
    _write_private(_state_path(email), json.dumps(state).encode())
    return {"status": "saved", **state}


def fetch_pointer(email: str) -> Optional[Dict[str, Any]]:
    """Dernier pointeur de sauvegarde publié (déchiffré), depuis les relais."""
    import nostr_sdk
    from websockets.sync.client import connect
    keys = _keys(email)
    best = None
    for relay in RELAYS:
        try:
            with connect(relay, open_timeout=10, close_timeout=5) as ws:
                ws.send(json.dumps(["REQ", "fcb", {"kinds": [30078], "authors": [keys.public_key().to_hex()],
                                                    "#d": [D_TAG], "limit": 1}]))
                deadline = time.time() + 10
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=10))
                    if msg[0] == "EVENT":
                        ev = msg[2]
                        if not best or ev["created_at"] > best["created_at"]:
                            best = ev
                    elif msg[0] == "EOSE":
                        break
        except Exception as exc:
            _log(f"WARN: {relay} : {exc}")
    if not best:
        return None
    clear = nostr_sdk.nip44_decrypt(keys.secret_key(), keys.public_key(), best["content"])
    return json.loads(clear)


def restore_from_relay(email: str, with_qdrant: bool = True) -> Dict[str, Any]:
    pointer = fetch_pointer(email)
    if not pointer:
        return {"status": "none"}
    blob = cloud_storage.ipfs_cat(pointer["cid"], max_bytes=500 * 1024 * 1024)
    data = cloud_storage.uenc_codec.decrypt_aes256gcm(blob, pointer["key"])
    tmp = Path(tempfile.mkdtemp(prefix="ucloud_restore_"))
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")
        counts = apply_bundle(email, tmp / "bundle", with_qdrant)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"status": "restored", "from": pointer.get("at"), **counts}


def main() -> int:
    ap = argparse.ArgumentParser(description="Sauvegarde / restauration du cloud FaceCloud")
    ap.add_argument("action", choices=["snapshot", "export", "restore", "pin", "status"])
    ap.add_argument("email")
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--from-relay", action="store_true")
    ap.add_argument("--bundle")
    ap.add_argument("--no-qdrant", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not (cloud_storage.GAME_NOSTR_PATH / args.email).is_dir():
        print(f"ERROR:ucloud_backup:{args.email}: MULTIPASS absent de cette station", file=sys.stderr)
        return 1
    if args.action == "snapshot":
        res = snapshot(args.email, force=args.force)
    elif args.action == "export":
        if not args.dir:
            ap.error("export : dossier de destination requis")
        res = export_bundle(args.email, Path(args.dir))
    elif args.action == "restore":
        if args.bundle:
            res = apply_bundle(args.email, Path(args.bundle), not args.no_qdrant)
        elif args.from_relay:
            res = restore_from_relay(args.email, not args.no_qdrant)
        else:
            ap.error("restore : --from-relay ou --bundle requis")
    elif args.action == "pin":
        res = pin_all(args.email)
    else:
        res = status(args.email)
    print(json.dumps(res))
    return 0 if not (isinstance(res, dict) and res.get("status") == "error") else 1


if __name__ == "__main__":
    sys.exit(main())
