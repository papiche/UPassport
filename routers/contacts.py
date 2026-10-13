"""
routers/contacts.py — Carnet d'adresses FaceCloud (API JSON, NIP-98).

Onglet Contacts de UPlanet/earth/ucloud.html. Le stockage est
services/contacts_store.py (vCards chiffrées dans le cloud) ; le même carnet
est synchronisé par téléphone via routers/carddav.py.

  GET    /api/contacts                     liste (+ adresse de synchro CardDAV, sources)
  GET    /api/contacts/item?name=          fiche complète (+ photo)
  POST   /api/contacts/item                créer / modifier (form : name?, data JSON,
                                           photo_point_id?, photo (fichier)?, drop_photo?, if_match?)
  DELETE /api/contacts/item?name=
  POST   /api/contacts/import-vcf          fichier .vcf (une ou plusieurs fiches)
  GET    /api/contacts/export.vcf          tout le carnet
  POST   /api/contacts/discover            carnets d'un serveur CardDAV (url, username, password)
  GET    /api/contacts/sources · POST · DELETE /{id} · POST /{id}/sync
  POST   /api/contacts/reconcile           personnes FaceCloud ↔ fiches ↔ MULTIPASS
                                           (services/identity_links.py)

Interopérabilité FaceCloud : la photo de chaque fiche devient un visage de
RÉFÉRENCE dans la collection Qdrant du compte (payload `ref: "contact"`) —
jamais affiché sur la table de tri, jamais rangé automatiquement, ignoré par
le matcher : il ne sert qu'à proposer « est-ce Marie ? » (GET /mailjet/faces,
champ `maybe`). Analyse par le mode rapide (faceid.sh --batch), en arrière-plan.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from core.config import settings
from routers.cloud import _email_for_authenticated_npub
from services import carddav_client, cloud_storage, contacts_store, identity_links
from services.nostr import require_nostr_auth

logger = logging.getLogger(__name__)
router = APIRouter()


def _public(card: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("name", "uid", "fn", "given", "family", "org", "emails", "tels", "nostr", "has_photo",
            "avatar", "etag", "mtime", "source", "face")
    out = {k: card.get(k) for k in keep}
    out["face"] = (card.get("face") or {}).get("status")
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Lecture / écriture
# ═════════════════════════════════════════════════════════════════════════════
@router.get("/api/contacts")
async def list_contacts(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    data = await asyncio.to_thread(contacts_store.load, email)
    cards = sorted((_public(c) for c in data["cards"].values()), key=lambda c: (c["fn"] or "").lower())
    dav = await cloud_storage.dav_public_url()
    base = dav.rsplit("/dav", 1)[0] if "/dav" in dav else dav
    return JSONResponse({
        "contacts": cards,
        "sync": {"url": base.rstrip("/") + "/carddav/", "username": email,
                 "enrolled": bool(cloud_storage.load_dav_token(email))},
        "sources": [_public_source(s) for s in _load_sources(email)],
    })


@router.get("/api/contacts/item")
async def get_contact(request: Request, name: str = Query(...), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    try:
        text = await asyncio.to_thread(contacts_store.get_text, email, name)
    except (KeyError, contacts_store.ContactError):
        raise HTTPException(status_code=404, detail="fiche introuvable")
    info = contacts_store.summarize(text)
    photo = contacts_store.photo_bytes(contacts_store.parse(text))
    data = await asyncio.to_thread(contacts_store.load, email)
    card = data["cards"].get(name) or {}
    info.update(name=name, etag=card.get("etag"), source=card.get("source"),
                face=(card.get("face") or {}).get("status"), photo=_photo_data_url(photo, 512))
    info.pop("avatar", None)
    return JSONResponse(info)


def _photo_data_url(photo: Optional[bytes], side: int) -> str:
    if not photo:
        return ""
    try:
        from PIL import Image, ImageOps
        import io
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
        img.thumbnail((side, side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def _square_jpeg(raw: bytes, side: int = 512) -> bytes:
    from PIL import Image, ImageOps
    import io
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


async def _face_photo(email: str, point_id: str) -> bytes:
    """Photo de contact depuis un visage de la table (recadrage carré 512)."""
    from routers import mailjet
    collection = mailjet._faces_collection(email)
    payload = await mailjet._fetch_face_point(collection, point_id) if collection else None
    if not payload or not payload.get("source_path"):
        raise HTTPException(status_code=404, detail="visage introuvable")
    plaintext, _mime = await mailjet._decrypt_source_photo(email, payload["source_path"])
    crops = await asyncio.to_thread(mailjet._crop_faces, plaintext, [(point_id, payload.get("bbox") or {})], 512)
    if point_id not in crops:
        raise HTTPException(status_code=500, detail="recadrage impossible")
    return await asyncio.to_thread(_square_jpeg, crops[point_id], 512)


@router.post("/api/contacts/item")
async def save_contact(
    request: Request,
    data: str = Form(...),
    name: str = Form(default=""),
    if_match: str = Form(default=""),
    photo_point_id: str = Form(default=""),
    drop_photo: str = Form(default=""),
    photo: Optional[UploadFile] = File(default=None),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    try:
        fields = json.loads(data)
    except ValueError:
        raise HTTPException(status_code=400, detail="données illisibles")
    new_photo = None
    if photo_point_id:
        new_photo = await _face_photo(email, photo_point_id)
    elif photo is not None:
        raw = await photo.read()
        if len(raw) > 15 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="photo trop volumineuse")
        try:
            new_photo = await asyncio.to_thread(_square_jpeg, raw, 512)
        except Exception:
            raise HTTPException(status_code=400, detail="image illisible")
    base_text = None
    if name:
        try:
            base_text = await asyncio.to_thread(contacts_store.get_text, email, name)
        except KeyError:
            base_text = None
    else:
        name = contacts_store.new_name()
    try:
        text = contacts_store.build(fields, base_text, photo=new_photo, drop_photo=bool(drop_photo))
        card = await asyncio.to_thread(contacts_store.put, email, name, text, if_match=if_match or None)
    except contacts_store.PreconditionFailed:
        return JSONResponse({"error": "Cette fiche a été modifiée ailleurs entre-temps (téléphone ?). "
                                      "Rechargez-la avant d'enregistrer."}, status_code=409)
    except contacts_store.ContactError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    schedule_face_refs(email)
    return JSONResponse({"ok": True, "contact": _public(card)})


@router.delete("/api/contacts/item")
async def delete_contact(request: Request, name: str = Query(...), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    try:
        ok = await asyncio.to_thread(contacts_store.delete, email, name)
    except contacts_store.ContactError:
        raise HTTPException(status_code=400, detail="nom refusé")
    schedule_face_refs(email)
    return JSONResponse({"ok": ok})


# ═════════════════════════════════════════════════════════════════════════════
# Import / export de fichiers
# ═════════════════════════════════════════════════════════════════════════════
@router.post("/api/contacts/import-vcf")
async def import_vcf(request: Request, file: UploadFile = File(...), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    raw = await file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="fichier trop volumineux")
    text = raw.decode("utf-8", "replace")
    result = await asyncio.to_thread(_import_cards, email, contacts_store.split_vcf(text), None)
    schedule_face_refs(email)
    return JSONResponse(result)


def _import_cards(email: str, cards: List[str], source: Optional[str]) -> Dict[str, int]:
    """Fiches d'un fichier : même UID qu'une fiche existante → mise à jour ;
    sinon nouvelle fiche. Les fiches illisibles sont comptées, jamais
    bloquantes pour les autres."""
    counts = {"created": 0, "updated": 0, "errors": 0}
    data = contacts_store.load(email)
    for text in cards:
        try:
            info = contacts_store.summarize(text)
            name = contacts_store.find_by_uid(data, info["uid"]) if info["uid"] else None
            existed = bool(name)
            name = name or contacts_store.new_name()
            contacts_store.put(email, name, text, source=source)
            data["cards"][name] = {"uid": info["uid"]}
            counts["updated" if existed else "created"] += 1
        except Exception as exc:
            counts["errors"] += 1
            logger.warning("contacts: fiche importée illisible (%s)", exc)
    return counts


@router.get("/api/contacts/export.vcf")
async def export_vcf(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    data = await asyncio.to_thread(contacts_store.load, email)
    parts = []
    for name in data["cards"]:
        try:
            parts.append((await asyncio.to_thread(contacts_store.get_text, email, name)).strip())
        except Exception:
            continue
    return Response(content="\r\n".join(parts) + "\r\n", media_type="text/vcard; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="contacts-facecloud.vcf"'})


# ═════════════════════════════════════════════════════════════════════════════
# Sources CardDAV externes
# ═════════════════════════════════════════════════════════════════════════════
def _sources_path(email: str) -> Path:
    return cloud_storage.ensure_ucloud_dir(email) / "carddav_sources.json"


def _load_sources(email: str) -> List[Dict[str, Any]]:
    try:
        return json.loads(_sources_path(email).read_text())
    except (OSError, ValueError):
        return []


def _save_sources(email: str, sources: List[Dict[str, Any]]) -> None:
    path = _sources_path(email)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(sources, fh)
    os.replace(tmp, path)


def _public_source(s: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in s.items() if k != "password"}


@router.post("/api/contacts/discover")
async def discover(request: Request, url: str = Form(...), username: str = Form(...),
                   password: str = Form(...), npub: str = Depends(require_nostr_auth)):
    _email_for_authenticated_npub(npub)
    try:
        books = await carddav_client.discover(url, username, password)
    except carddav_client.CardDAVError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"books": books})


@router.get("/api/contacts/sources")
async def list_sources(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    return JSONResponse({"sources": [_public_source(s) for s in _load_sources(email)]})


@router.post("/api/contacts/sources")
async def add_source(request: Request, label: str = Form(...), url: str = Form(...),
                     username: str = Form(...), password: str = Form(...),
                     npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    sources = _load_sources(email)
    source = {"id": secrets.token_hex(8), "label": label.strip() or "Carnet", "url": url.strip(),
              "username": username.strip(), "password": password, "created_at": int(time.time()),
              "last_sync": None}
    sources.append(source)
    _save_sources(email, sources)
    _start_sync(email, source["id"])
    return JSONResponse({"ok": True, "source": _public_source(source)})


@router.delete("/api/contacts/sources/{source_id}")
async def delete_source(request: Request, source_id: str, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    sources = [s for s in _load_sources(email) if s["id"] != source_id]
    _save_sources(email, sources)
    return JSONResponse({"ok": True})


@router.post("/api/contacts/sources/{source_id}/sync")
async def sync_source_now(request: Request, source_id: str, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    if not any(s["id"] == source_id for s in _load_sources(email)):
        raise HTTPException(status_code=404, detail="source introuvable")
    _start_sync(email, source_id)
    return JSONResponse({"ok": True})


_SYNC_TASKS: set = set()


def _start_sync(email: str, source_id: str) -> None:
    task = asyncio.create_task(sync_source(email, source_id))
    _SYNC_TASKS.add(task)
    task.add_done_callback(_SYNC_TASKS.discard)


async def sync_source(email: str, source_id: str) -> Dict[str, Any]:
    """Import UNIDIRECTIONNEL d'un carnet externe :
      * fiche nouvelle → créée (rattachée par UID à une fiche existante si
        elle y est déjà, ex. synchronisée depuis le téléphone) ;
      * fiche changée là-bas → mise à jour ici, SAUF si elle a été modifiée
        ici depuis le dernier import (la version locale l'emporte, comptée
        dans `kept_local`) ;
      * fiche disparue là-bas → supprimée ici seulement si elle n'a pas été
        modifiée ici."""
    source = next((s for s in _load_sources(email) if s["id"] == source_id), None)
    if not source:
        return {}
    result = {"at": int(time.time()), "created": 0, "updated": 0, "deleted": 0,
              "kept_local": 0, "errors": 0}
    try:
        remote = await carddav_client.fetch_all(source["url"], source["username"], source["password"])
    except carddav_client.CardDAVError as exc:
        result["error"] = str(exc)
        _record_sync(email, source_id, result)
        return result

    def apply() -> None:
        data = contacts_store.load(email)
        by_href = {(c.get("remote") or {}).get("href"): n for n, c in data["cards"].items()
                   if c.get("source") == source_id}
        seen = set()
        for href, etag, text in remote:
            seen.add(href)
            name = by_href.get(href)
            try:
                info = contacts_store.summarize(text)
            except contacts_store.ContactError:
                result["errors"] += 1
                continue
            if not name and info["uid"]:
                name = contacts_store.find_by_uid(data, info["uid"])
            if name:
                card = data["cards"].get(name) or {}
                rem = card.get("remote") or {}
                if rem.get("etag") == etag and etag:
                    continue
                if rem.get("sha") and card.get("sha") != rem.get("sha"):
                    result["kept_local"] += 1          # modifiée ici depuis l'import
                    continue
                new = False
            else:
                name = f"{source_id[:6]}-{hashlib.sha1(href.encode()).hexdigest()[:16]}.vcf"
                new = True
            try:
                card = contacts_store.put(email, name, text, source=source_id,
                                          remote={"href": href, "etag": etag})
                contacts_store.set_meta(email, name, remote={"href": href, "etag": etag, "sha": card["sha"]})
                result["created" if new else "updated"] += 1
            except contacts_store.ContactError:
                result["errors"] += 1
        for href, name in by_href.items():
            if href in seen:
                continue
            card = data["cards"].get(name) or {}
            rem = card.get("remote") or {}
            if rem.get("sha") and card.get("sha") != rem.get("sha"):
                result["kept_local"] += 1
                continue
            if contacts_store.delete(email, name):
                result["deleted"] += 1

    await asyncio.to_thread(apply)
    _record_sync(email, source_id, result)
    schedule_face_refs(email)
    logger.info("contacts: synchro %s pour %s : %s", source_id, email, result)
    return result


def _record_sync(email: str, source_id: str, result: Dict[str, Any]) -> None:
    sources = _load_sources(email)
    for s in sources:
        if s["id"] == source_id:
            s["last_sync"] = result
    _save_sources(email, sources)


# ═════════════════════════════════════════════════════════════════════════════
# Réconciliation personnes ↔ fiches ↔ MULTIPASS
# ═════════════════════════════════════════════════════════════════════════════
@router.post("/api/contacts/reconcile")
async def reconcile(request: Request, persons: str = Form(default="[]"),
                    npub: str = Depends(require_nostr_auth)):
    """`persons` = [{key, pubkey, email, contact}] (personnes de la table).
    Réponse : services/identity_links.reconcile — rapprochements certains
    (`exact`, `linked`) ou à confirmer (`alias`, sous-adresse « + »)."""
    email = _email_for_authenticated_npub(npub)
    try:
        plist = [p for p in json.loads(persons) if isinstance(p, dict) and p.get("key")]
    except ValueError:
        plist = []
    data = await asyncio.to_thread(contacts_store.load, email)
    result = await asyncio.to_thread(identity_links.reconcile, data["cards"], plist[:5000])
    return JSONResponse(result)


# ═════════════════════════════════════════════════════════════════════════════
# Visages de référence (photos des fiches)
# ═════════════════════════════════════════════════════════════════════════════
_REF_TIMERS: Dict[str, threading.Timer] = {}
_REF_LOCK = threading.Lock()
FACEID_SH = Path.home() / ".zen" / "Astroport.ONE" / "IA" / "generators" / "faceid.sh"


def schedule_face_refs(email: str, delay: float = 8.0) -> None:
    """Met à jour les visages de référence quelques secondes après la
    DERNIÈRE modification (un import de 500 fiches = un seul passage)."""
    with _REF_LOCK:
        old = _REF_TIMERS.pop(email, None)
        if old:
            old.cancel()
        timer = threading.Timer(delay, _safe_refs, args=(email,))
        timer.daemon = True
        _REF_TIMERS[email] = timer
        timer.start()


def _safe_refs(email: str) -> None:
    try:
        update_face_refs(email)
    except Exception as exc:
        logger.warning("contacts: visages de référence pour %s : %s", email, exc)


def _qdrant():
    from routers import mailjet
    return mailjet._QDRANT_URL, mailjet._qdrant_headers(), mailjet._faces_collection


def update_face_refs(email: str) -> Dict[str, int]:
    """Réconcilie les points Qdrant `ref: "contact"` avec les fiches : analyse
    les photos nouvelles ou changées (faceid.sh --batch, ComfyUI direct),
    retire les références des fiches supprimées ou sans photo."""
    url, headers, faces_collection = _qdrant()
    collection = faces_collection(email)
    if not collection:
        return {}
    data = contacts_store.load(email)
    counts = {"added": 0, "removed": 0, "no_face": 0, "pending": 0}

    with httpx.Client(timeout=30) as client:
        client.put(f"{url}/collections/{collection}", headers=headers,
                   json={"vectors": {"size": 512, "distance": "Cosine"}})      # 409 si déjà là : sans effet
        resp = client.post(f"{url}/collections/{collection}/points/scroll", headers=headers, json={
            "limit": 10000, "with_payload": True, "with_vector": False,
            "filter": {"must": [{"key": "ref", "match": {"value": "contact"}}]}})
        existing = resp.json().get("result", {}).get("points", []) if resp.status_code == 200 else []
        stale = [p["id"] for p in existing
                 if not (data["cards"].get((p.get("payload") or {}).get("contact")) or {}).get("has_photo")]
        if stale:
            client.post(f"{url}/collections/{collection}/points/delete?wait=true", headers=headers,
                        json={"points": stale})
            counts["removed"] = len(stale)

    todo = [(n, c) for n, c in data["cards"].items()
            if c.get("has_photo") and (c.get("face") or {}).get("sha") != c.get("sha")]
    if not todo or not FACEID_SH.exists():
        return counts

    tmp = tempfile.mkdtemp(prefix="contact_faces_")
    files: Dict[str, tuple] = {}
    try:
        for name, card in todo:
            photo = contacts_store.photo_bytes(contacts_store.parse(contacts_store.get_text(email, name)))
            if not photo:
                continue
            path = os.path.join(tmp, hashlib.sha1(name.encode()).hexdigest() + ".jpg")
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(photo)
            files[path] = (name, card)
        proc = subprocess.run(["bash", str(FACEID_SH), "--batch"], input="\n".join(files) + "\n",
                              capture_output=True, text=True, timeout=60 + 20 * len(files))
        results = {}
        for line in proc.stdout.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("fatal"):
                break
            results[row.get("file")] = row
        with httpx.Client(timeout=30) as client:
            for path, (name, card) in files.items():
                row = results.get(path)
                if not row or not row.get("ok"):
                    counts["pending"] += 1
                    contacts_store.set_meta(email, name, face={"status": "pending"})
                    continue
                faces = [f for f in row.get("faces") or [] if float(f.get("det_score") or 0) >= 0.6
                         and len(f.get("embedding") or []) == 512]
                if not faces:
                    counts["no_face"] += 1
                    contacts_store.set_meta(email, name, face={"status": "none", "sha": card["sha"]})
                    continue
                # Le plus grand visage de la photo = la personne de la fiche
                best = max(faces, key=lambda f: (f["bbox"]["x2"] - f["bbox"]["x1"]) * (f["bbox"]["y2"] - f["bbox"]["y1"]))
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"facecloud-contact:{email}:{name}"))
                emails = card.get("emails") or []
                client.put(f"{url}/collections/{collection}/points?wait=true", headers=headers, json={
                    "points": [{"id": point_id, "vector": best["embedding"], "payload": {
                        "ref": "contact", "contact": name, "name": card.get("fn") or "",
                        "email": emails[0]["value"] if emails else "", "pubkey": None,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")}}]})
                counts["added"] += 1
                contacts_store.set_meta(email, name, face={"status": "ok", "sha": card["sha"]})
    finally:
        for path in files:
            Path(path).unlink(missing_ok=True)
        os.rmdir(tmp) if os.path.isdir(tmp) and not os.listdir(tmp) else None
    logger.info("contacts: visages de référence %s : %s", email, counts)
    return counts
