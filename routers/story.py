"""
routers/story.py — Studio vidéo IA : personnages et scènes (Kind 30510), versions, rendus.

Les paquets sont ceux de `Astroport.ONE/tools/story_asset.py` : tar.gz chiffré AES-256-GCM sur IPFS.
  scope private : clé aléatoire dans `~/.zen/workspace/scenes/library/keyring.json` (jamais renvoyée)
  scope coop    : clé coopérative dérivée de $UPLANETNAME — tous les Capitaines lisent le paquet
Une modification crée une NOUVELLE version (anciens CID conservés) ; le rendu (« Générer ») est un job
suivi par `services/story_render.py`.

    GET  /api/story/assets                              mes paquets + paquets coopératifs des autres
    POST /api/story/assets                              {type,name,scope,description} → nouveau paquet
    GET  /api/story/asset/{cid}                         manifest, fichiers (texte inclus), rendus
    PUT  /api/story/asset/{cid}                         {changes, description, attach, reshare} → nouvelle version
    GET  /api/story/asset/{cid}/file?path=              octets d'un fichier du paquet (aperçu)
    GET  /api/story/asset/{cid}/versions                historique + rendus de chaque version
    POST /api/story/asset/{cid}/restore                 nouvelle version = copie d'une ancienne
    POST /api/story/asset/{cid}/fork                    copie à mon nom (paquet d'un autre Capitaine)
    GET  /api/story/asset/{cid}/export                  tar.gz EN CLAIR (sauvegarde / transfert manuel)
    POST /api/story/import                               {file} → installe un paquet exporté dans ma bibliothèque
    DELETE /api/story/asset/{cid}                         retire le paquet (toute sa lignée) de ma bibliothèque
    POST /api/story/asset/{cid}/render                  {regen?} → job (scène : vidéo ; personnage : portrait + voix)
    POST /api/story/asset/{cid}/render-shot              {index, storyboard?} → job (un seul plan ; storyboard = définition courante non enregistrée)
    GET  /api/story/asset/{cid}/shot/{index}/takes       prises archivées de ce plan (hors la courante)
    GET  …/shot/{index}/takes/{take}/file                 fichier d'une prise archivée
    GET  /api/story/jobs · /api/story/jobs/{id}         suivi de progression
    POST /api/story/jobs/{id}/cancel
    GET  /api/story/jobs/{id}/file?name=                plan / vidéo / voix en cours de rendu
    GET  /api/story/render/{version_cid}/{rid}/file     fichier d'un rendu archivé

Auth : NIP-98 (require_nostr_auth) ET clé = MULTIPASS du Capitaine de cette station.
"""

import asyncio
import base64
import logging
import mimetypes

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from core.config import settings
from services import cloud_storage, story_render
from services.nostr import require_nostr_auth
from utils.crypto import npub_to_hex

story_asset = story_render.story_asset

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY = 25 * 1024 * 1024
MAX_IMPORT = 200 * 1024 * 1024  # paquet exporté (tar.gz en clair, vidéo incluse pour une scène)
TEXT_EXT = (".json", ".txt", ".md")
TEXT_PREVIEW_MAX = 512 * 1024


def _require_captain(npub: str) -> None:
    hex_pub = (npub_to_hex(npub) if npub.startswith("npub") else npub).lower()
    captain = cloud_storage.hex_for_email(settings.CAPTAINEMAIL) if settings.CAPTAINEMAIL else None
    if not captain or hex_pub != captain.lower():
        raise HTTPException(status_code=403, detail="Réservé au Capitaine de la station (signataire du Kind 30510)")


async def _run(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except story_asset.AssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _json(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(status_code=413, detail="corps trop gros (25 Mo max)")
    try:
        body = await request.json() if raw else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="JSON invalide")
    return body if isinstance(body, dict) else {}


def _kind(path: str) -> str:
    low = path.lower()
    if low.endswith(TEXT_EXT):
        return "text"
    if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if low.endswith((".wav", ".mp3", ".ogg", ".flac")):
        return "audio"
    if low.endswith((".mp4", ".webm")):
        return "video"
    return "other"


# ─── Bibliothèque ───────────────────────────────────────────────────────────
@router.get("/api/story/assets", summary="Mes paquets et ceux de la coopérative")
async def list_story_assets(npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    return {"assets": await _run(story_asset.list_assets), "ipfs_gateway": settings.IPFS_GATEWAY,
            "me": await _run(story_asset.captain_hex)}


@router.post("/api/story/assets", summary="Créer un personnage ou une scène")
async def create_story_asset(request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    b = await _json(request)
    typ, name = b.get("type"), (b.get("name") or "").strip()
    if typ not in ("character", "scene") or not (1 <= len(name) <= 60):
        raise HTTPException(status_code=400, detail="type (character|scene) et nom (1 à 60 caractères) requis")
    if not all(c.isalnum() or c in " -_'" for c in name):
        raise HTTPException(status_code=400, detail="nom : lettres, chiffres, espace, - _ ' seulement")
    scope = b.get("scope", "private")
    if scope not in story_asset.SCOPES:
        raise HTTPException(status_code=400, detail="scope : private ou coop")
    entry = await _run(story_asset.create, typ, name, scope, b.get("description") or "")
    return {"cid": entry["cid"], "version": entry["version"]}


# ─── Un paquet ──────────────────────────────────────────────────────────────
@router.get("/api/story/asset/{cid}", summary="Contenu déchiffré d'un paquet")
async def get_story_asset(cid: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    entry, manifest, files = await _run(story_asset.open_bundle, cid)
    out = []
    for path, data in sorted(files.items()):
        item = {"path": path, "size": len(data), "kind": _kind(path)}
        if item["kind"] == "text" and len(data) <= TEXT_PREVIEW_MAX:
            item["text"] = data.decode("utf-8", "replace")
        out.append(item)
    latest = story_asset.latest_of(entry) if not entry.get("foreign") else entry
    return {"cid": entry["cid"], "version": entry.get("version", 1), "scope": entry.get("scope", "private"),
            "mine": not entry.get("foreign"), "is_latest": latest["cid"] == entry["cid"], "latest_cid": latest["cid"],
            "manifest": manifest, "files": out, "shared": len(entry.get("shared_with", []))}


@router.get("/api/story/asset/{cid}/file", summary="Un fichier du paquet (aperçu)")
async def get_story_file(cid: str, path: str = Query(...), npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    _e, _m, files = await _run(story_asset.open_bundle, cid)
    if path not in files:
        raise HTTPException(status_code=404, detail="fichier absent du paquet")
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(content=files[path], media_type=mime, headers={"Cache-Control": "no-store"})


@router.put("/api/story/asset/{cid}", summary="Publier une nouvelle version d'un paquet")
async def update_story_asset(cid: str, request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    body = await _json(request)
    changes = {}
    for path, spec in (body.get("changes") or {}).items():
        if not isinstance(spec, dict):
            raise HTTPException(status_code=400, detail=f"modification invalide pour {path}")
        if spec.get("delete"):
            changes[path] = None
        elif "text" in spec:
            changes[path] = str(spec["text"]).encode("utf-8")
        elif "b64" in spec:
            try:
                changes[path] = base64.b64decode(spec["b64"], validate=True)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"base64 invalide pour {path}")
        else:
            raise HTTPException(status_code=400, detail=f"modification invalide pour {path}")
    attach = [a for a in (body.get("attach") or []) if isinstance(a, dict) and a.get("cid")]
    if not changes and not attach and body.get("description") is None:
        raise HTTPException(status_code=400, detail="rien à modifier")
    entry = await _run(story_asset.new_version, cid, changes, body.get("description"),
                       bool(body.get("reshare", True)), False, attach)
    logger.info("story: %s « %s » v%s republié (%s)", entry["type"], entry["name"], entry["version"], entry["cid"])
    return JSONResponse({"success": True, "cid": entry["cid"], "event_id": entry["event_id"],
                         "version": entry["version"], "reshared": len(entry.get("shared_with", []))})


@router.get("/api/story/asset/{cid}/versions", summary="Historique et rendus par version")
async def story_versions(cid: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    return {"versions": await _run(story_asset.versions, cid)}


@router.post("/api/story/asset/{cid}/restore", summary="Restaurer une ancienne version (nouvelle version)")
async def story_restore(cid: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    entry = await _run(story_asset.restore, cid)
    return {"cid": entry["cid"], "version": entry["version"]}


@router.post("/api/story/asset/{cid}/fork", summary="Copier un paquet dans ma bibliothèque")
async def story_fork(cid: str, request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    b = await _json(request)
    scope = b.get("scope", "private")
    if scope not in story_asset.SCOPES:
        raise HTTPException(status_code=400, detail="scope : private ou coop")
    entry = await _run(story_asset.fork, cid, (b.get("name") or "").strip() or None, scope)
    return {"cid": entry["cid"], "version": entry["version"]}


@router.get("/api/story/asset/{cid}/export", summary="Télécharger le paquet en clair (sauvegarde / transfert manuel)")
async def story_export(cid: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    entry = await _run(story_asset.find_entry, cid)
    blob = await _run(story_asset.export_bytes, cid)
    filename = f"{entry['type']}-{story_asset.slug(entry['name'])}.tar.gz"
    return Response(content=blob, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.delete("/api/story/asset/{cid}", summary="Retirer un paquet (toute sa lignée) de ma bibliothèque")
async def story_delete(cid: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    result = await _run(story_asset.delete, cid)
    logger.info("story: %s « %s » supprimé (%d version(s))", result["type"], result["name"], len(result["removed"]))
    return {"removed": len(result["removed"])}


@router.post("/api/story/import", summary="Installer un paquet exporté dans ma bibliothèque")
async def story_import(npub: str = Depends(require_nostr_auth), file: UploadFile = File(...),
                       scope: str = Form("private"), name: str = Form(None)):
    _require_captain(npub)
    if scope not in story_asset.SCOPES:
        raise HTTPException(status_code=400, detail="scope : private ou coop")
    blob = await file.read()
    if len(blob) > MAX_IMPORT:
        raise HTTPException(status_code=413, detail=f"paquet trop gros ({MAX_IMPORT // 1024 // 1024} Mo max)")
    entry = await _run(story_asset.import_file, blob, scope, (name or "").strip() or None)
    logger.info("story: %s « %s » importé (%s)", entry["type"], entry["name"], entry["cid"])
    return {"cid": entry["cid"], "name": entry["name"], "type": entry["type"], "version": entry["version"]}


# ─── Rendu et suivi ─────────────────────────────────────────────────────────
@router.post("/api/story/asset/{cid}/render", summary="Générer (scène : vidéo ; personnage : portrait et voix)")
async def story_render_start(cid: str, request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    b = await _json(request)
    regen = [r for r in (b.get("regen") or []) if r in ("portrait", "voice")]
    return await _run(story_render.start_render, cid, regen)


@router.post("/api/story/asset/{cid}/render-shot", summary="Générer un seul plan (scène uniquement)")
async def story_render_shot(cid: str, request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    b = await _json(request)
    index = b.get("index")
    if not isinstance(index, int) or index < 0:
        raise HTTPException(status_code=400, detail="index de plan invalide")
    sb = b.get("storyboard")
    if sb is not None and not isinstance(sb, dict):
        raise HTTPException(status_code=400, detail="storyboard invalide")
    return await _run(story_render.start_shot_render, cid, index, sb)


@router.get("/api/story/asset/{cid}/shot/{index}/takes", summary="Prises archivées d'un plan")
async def story_shot_takes(cid: str, index: int, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    return {"takes": await _run(story_render.shot_takes, cid, index)}


@router.get("/api/story/asset/{cid}/shot/{index}/takes/{take}/file", summary="Fichier d'une prise archivée")
async def story_shot_take_file(cid: str, index: int, take: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    path = await _run(story_render.shot_take_file, cid, index, take)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@router.get("/api/story/jobs", summary="Rendus en cours et récents")
async def story_jobs(npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    return {"jobs": await _run(story_render.list_jobs)}


@router.get("/api/story/jobs/{job_id}", summary="Progression d'un rendu")
async def story_job(job_id: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    return await _run(story_render.job_status, job_id)


@router.post("/api/story/jobs/{job_id}/cancel", summary="Annuler un rendu")
async def story_job_cancel(job_id: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    return await _run(story_render.cancel, job_id)


@router.get("/api/story/jobs/{job_id}/file", summary="Plan, voix ou vidéo d'un rendu")
async def story_job_file(job_id: str, name: str = Query(...), npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    path = await _run(story_render.job_file, job_id, name)
    return FileResponse(path, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/story/render/{version_cid}/{render_id}/file", summary="Fichier d'un rendu archivé")
async def story_render_file(version_cid: str, render_id: str, name: str = Query(...), npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    path = await _run(story_render.render_file, version_cid, render_id, name)
    return FileResponse(path, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
