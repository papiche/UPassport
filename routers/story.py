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
    GET  /api/story/people                              les personnes nommées de mon ucloud (FaceCloud) : candidates à devenir personnage
    POST /api/story/character-from-person               {name, point_ids, consent} → personnage PRIVÉ (portrait tiré de ses photos, look, voix)
    GET  /api/story/assistant/models                    modèles de texte installés sur Ollama
    POST /api/story/assistant/chat                       {messages, cast, model?} → un tour de discussion avec le scénariste IA
    POST /api/story/assistant/storyboard                 {messages, cast, model?} → storyboard JSON validé (3 tentatives de correction)
    POST /api/story/scene-from-storyboard                {name, storyboard} → nouvelle scène privée contenant ce storyboard
    POST /api/story/share                               {version_cid, render_id, title?, to?} → lien secret à envoyer à un ami (14 jours)
    GET  /api/story/shares · DELETE /api/story/share/{token}   mes liens · révoquer un lien
    GET  /api/story/watch/{token}[/video]                PUBLIC (le jeton fait foi) : la page et la vidéo, lecture sur téléphone (Range)
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
import json
import logging
import mimetypes
import subprocess

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from core.config import settings
from services import analysis, cloud_storage, story_assistant, story_person, story_render, story_share, videofun
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


# ─── Vidéos FUN pour des amis : personnages tirés de l'ucloud + assistant scénariste ───────────────
async def _assist(fn, *args, **kwargs):
    """Les erreurs de l'assistant sont des messages lisibles : 503 si l'IA est injoignable, 422 sinon."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except story_assistant.AssistantError as exc:
        down = "injoignable" in str(exc) or "aucun modèle" in str(exc)
        raise HTTPException(status_code=503 if down else 422, detail=str(exc))


_LOOKS: dict = {}   # cid d'un personnage → son « look » (une ouverture de paquet = IPFS + déchiffrement : une fois)


def _library_cast(names=None) -> list:
    """[{name, look}] des personnages de MA bibliothèque (ou seulement ceux de `names`), pour la consigne de l'assistant."""
    wanted = {str(n).lower() for n in names} if names else None
    out = []
    for a in story_asset.list_assets():
        if a.get("type") != "character" or not a.get("mine") or (wanted is not None and a["name"].lower() not in wanted):
            continue
        if a["cid"] not in _LOOKS:
            look = ""
            try:
                _e, _m, files = story_asset.open_bundle(a["cid"])
                path = next((p for p in files if p.endswith("character.json")), None)
                look = (json.loads(files[path]).get("look") or "") if path else ""
            except Exception as exc:  # un paquet illisible ne bloque pas l'assistant
                logger.warning("story: look de %s illisible : %s", a["name"], exc)
            _LOOKS[a["cid"]] = look
        out.append({"name": a["name"], "look": _LOOKS[a["cid"]]})
    return out


@router.get("/api/story/people", summary="Les personnes nommées de mon ucloud, candidates à devenir personnage")
async def story_people(npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    from routers import mailjet
    collection = mailjet._faces_collection(settings.CAPTAINEMAIL)
    if not collection:
        return {"people": [], "reason": "no_hex"}
    try:
        points = await mailjet._scroll_all_points(collection, with_vector=False)
    except Exception as exc:
        logger.warning("story: Qdrant injoignable pour %s : %s", collection, exc)
        return {"people": [], "reason": "qdrant_unavailable"}
    me_hex = await asyncio.to_thread(cloud_storage.hex_for_email, settings.CAPTAINEMAIL) or ""
    return {"people": story_person.group_people(points, me_hex=me_hex)}


async def _face_crops(ids, k):
    """[(point_id, png recadré tête et épaules, côté source)] pour au plus `k` visages de `ids` (un par photo, les plus grands d'abord)."""
    from routers import mailjet
    email = settings.CAPTAINEMAIL
    collection = mailjet._faces_collection(email)
    if not collection:
        raise HTTPException(status_code=404, detail="MULTIPASS sans HEX sur cette station")
    cands = story_person.candidate_faces(await mailjet._fetch_face_points(collection, ids), k)
    out = []
    for pid, pl in cands:
        try:
            plain, _mime = await mailjet._decrypt_source_photo(email, pl["source_path"])
            png, side = await asyncio.to_thread(story_person.crop_portrait, plain, pl.get("bbox") or {})
            out.append((pid, png, side))
        except HTTPException:
            continue                    # photo supprimée ou illisible : on essaie le visage suivant
        except Exception as exc:
            logger.warning("story: recadrage impossible pour %s : %s", pl.get("source_path"), exc)
    return out


def _preview(png: bytes, side: int = 240) -> str:
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


@router.post("/api/story/portrait-candidates", summary="Les photos de cette personne parmi lesquelles choisir le visage du personnage")
async def story_portrait_candidates(request: Request, npub: str = Depends(require_nostr_auth)):
    """{point_ids} → {candidates:[{id, preview (JPEG 240 px), score}], suggested:[id…]} : une photo par cliché source, la plus nette
    d'abord ; `suggested` = la sélection automatique (la meilleure puis jusqu'à 2 autres assez nettes). Aucune création."""
    _require_captain(npub)
    b = await _json(request)
    ids = [str(x) for x in (b.get("point_ids") or []) if x][:200]
    if not ids:
        raise HTTPException(status_code=400, detail="point_ids requis")
    crops = await _face_crops(ids, 12)
    if not crops:
        raise HTTPException(status_code=404, detail="aucun visage exploitable pour cette personne (photos source introuvables ?)")
    ranked = await asyncio.to_thread(story_person.rank_portraits, crops)
    picked = {r[0] for r in story_person.auto_pick(ranked)}
    return {"candidates": [{"id": pid, "preview": await asyncio.to_thread(_preview, png), "score": round(score, 1)} for pid, png, score in ranked],
            "suggested": [r[0] for r in ranked if r[0] in picked]}


@router.post("/api/story/character-from-person",
             summary="Personnage PRIVÉ tiré d'une personne de l'ucloud (portrait, apparence, voix)")
async def story_character_from_person(request: Request, npub: str = Depends(require_nostr_auth)):
    """Le visage du personnage vient de ses photos. Sans `photo_ids`, sélection automatique : la photo la plus nette (carré « tête et
    épaules » 1024 px) plus jusqu'à 2 autres assez nettes, passées toutes au modèle vidéo comme références du même visage. Avec
    `photo_ids` (ordre = priorité, 4 au plus) : exactement ces photos, la première est la principale. `portrait_only` (personnage
    déjà créé) ne remplace QUE les photos : fiche, apparence et voix restent. Le consentement de la personne est EXIGÉ et consigné ;
    le personnage reste privé. `render` (défaut vrai) lance aussitôt la création de sa voix (rendu du personnage)."""
    _require_captain(npub)
    b = await _json(request)
    if b.get("consent") is not True:
        raise HTTPException(status_code=400, detail="Le consentement de la personne est requis : ses photos ne servent "
                                                    "qu'à des vidéos privées, pour elle ou ses proches.")
    name = story_person.clean_name(b.get("name"))
    ids = [str(x) for x in (b.get("point_ids") or []) if x][:200]
    chosen = list(dict.fromkeys(str(x) for x in (b.get("photo_ids") or []) if x))[:story_person.MAX_PORTRAITS]      # sans doublon, ordre conservé
    if not name or not (ids or chosen):
        raise HTTPException(status_code=400, detail="nom et visages (point_ids) requis")
    if chosen:
        crops = {pid: (png, side) for pid, png, side in await _face_crops(chosen, len(chosen))}
        portraits = [crops[pid][0] for pid in chosen if pid in crops]
        if not portraits:
            raise HTTPException(status_code=404, detail="les photos choisies sont introuvables ou illisibles")
        score = (await asyncio.to_thread(story_person.rank_portraits, [(chosen[0], *crops[chosen[0]])]))[0][2] if chosen[0] in crops else 0.0
    else:
        crops = await _face_crops(ids, story_person.MAX_CANDIDATES)
        ranked = await asyncio.to_thread(story_person.rank_portraits, crops)
        picked = story_person.auto_pick(ranked)
        if not picked:
            raise HTTPException(status_code=404, detail="aucun visage exploitable pour cette personne (photos source introuvables ?)"
                                if not crops else "aucun portrait exploitable : les photos source sont illisibles")
        portraits, score = [r[1] for r in picked], picked[0][2]
    existing = next((a for a in await _run(story_asset.list_assets)
                     if a.get("type") == "character" and a.get("mine") and a["name"].lower() == name.lower()), None)
    look = voice = ""
    if existing and b.get("portrait_only"):
        # Photos seules : on garde fiche, apparence et voix ; les anciennes vues en trop sont retirées
        files = story_person.portrait_files(name, portraits)
        stale = {f"cast/{name}/portrait_{i}.png": None for i in range(2, 10) if f"cast/{name}/portrait_{i}.png" not in files}
        files.update(stale)
    else:
        look, voice = await asyncio.to_thread(story_person.describe_look, portraits[0], analysis.DESCRIBE_MODEL)
        look = (b.get("look") or look or "").strip()[:300]
        voice = (b.get("voice_design") or voice or "").strip()[:120]
        files = story_person.character_files(name, look, voice, portraits)
    cid = existing["cid"] if existing else (await _run(story_asset.create, "character", name, "private", "Personnage tiré de FaceCloud"))["cid"]
    entry = await _run(story_asset.new_version, cid, files, "Photos du personnage changées" if b.get("portrait_only") and existing else "Portrait tiré de FaceCloud",
                       False, False, [])
    _LOOKS.pop(cid, None)
    job, job_error = None, None
    if b.get("render", True):
        try:
            job = await _run(story_render.start_render, entry["cid"], [])
        except HTTPException as exc:
            job_error = exc.detail
    logger.info("story: personnage « %s » %s depuis FaceCloud (%s, %d photo(s))", name, "mis à jour" if existing else "créé", entry["cid"], len(portraits))
    return {"cid": entry["cid"], "name": name, "look": look, "voice_design": voice, "updated": bool(existing), "photos": len(portraits),
            "sharpness": round(score, 1), "job": job, "job_error": job_error}


@router.get("/api/story/assistant/models", summary="Modèles de texte installés sur Ollama")
async def story_assistant_models(npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    have = await _assist(story_assistant.installed_models)
    return {"models": have, "default": await _assist(story_assistant.pick_model, "")}


@router.post("/api/story/assistant/chat", summary="Un tour de discussion avec le scénariste IA")
async def story_assistant_chat(request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    b = await _json(request)
    cast = await asyncio.to_thread(_library_cast, b.get("cast") if isinstance(b.get("cast"), list) else None)
    return await _assist(story_assistant.converse, story_render.GEN_DIR, b.get("messages"), cast, str(b.get("model") or ""), b.get("shoot"))


@router.post("/api/story/assistant/storyboard", summary="Storyboard JSON validé, d'après la discussion")
async def story_assistant_storyboard(request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    b = await _json(request)
    cast = await asyncio.to_thread(_library_cast, b.get("cast") if isinstance(b.get("cast"), list) else None)
    return await _assist(story_assistant.generate, story_render.GEN_DIR, b.get("messages"), cast, str(b.get("model") or ""), None, b.get("shoot"))


MAX_DECOR_BYTES = 12 * 1024 * 1024


def _decor_jpeg(raw: bytes) -> tuple:
    """Décor normalisé : JPEG ≤ 1280 px (orientation EXIF appliquée), et son format (9:16, 16:9 ou 1:1)."""
    import io
    from PIL import Image, ImageOps
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception:
        raise HTTPException(status_code=400, detail="le décor n'est pas une image lisible")
    w, h = img.size
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    ratio = "9:16" if h > w * 1.2 else "16:9" if w > h * 1.2 else "1:1"
    return buf.getvalue(), ratio


@router.post("/api/story/scene-from-storyboard", summary="Nouvelle scène privée contenant ce storyboard (et son décor)")
async def story_scene_from_storyboard(request: Request, npub: str = Depends(require_nostr_auth)):
    """`decor` (facultatif) : `{ucloud_path}` — une photo de MON ucloud — ou `{b64}` ; elle devient `decor/decor.jpg` du paquet et
    chaque plan IA la reçoit comme décor de référence. `shoot` : durée maximale (3 à 12 s), consignes, destinataire : réappliqués ici."""
    _require_captain(npub)
    b = await _json(request)
    name = story_person.clean_name(b.get("name"))
    sb = b.get("storyboard")
    if not name or not isinstance(sb, dict):
        raise HTTPException(status_code=400, detail="nom et storyboard (objet JSON) requis")
    shoot = story_assistant.clean_shoot(b.get("shoot") or {})
    files = {}
    decor = b.get("decor")
    if decor:
        if not isinstance(decor, dict):
            raise HTTPException(status_code=400, detail="decor : {ucloud_path} ou {b64}")
        if decor.get("ucloud_path"):
            try:
                raw, mime = await asyncio.to_thread(cloud_storage.read_plaintext, settings.CAPTAINEMAIL, str(decor["ucloud_path"]))
            except (FileNotFoundError, RuntimeError, ValueError):
                raise HTTPException(status_code=404, detail="photo du décor introuvable dans votre ucloud")
            if not str(mime).startswith("image/"):
                raise HTTPException(status_code=400, detail="le décor doit être une image")
        else:
            try:
                raw = base64.b64decode(str(decor.get("b64") or ""), validate=True)
            except ValueError:
                raise HTTPException(status_code=400, detail="décor : base64 invalide")
        if not raw or len(raw) > MAX_DECOR_BYTES:
            raise HTTPException(status_code=400, detail=f"décor vide ou trop gros ({MAX_DECOR_BYTES // 1024 // 1024} Mo max)")
        jpeg, ratio = await asyncio.to_thread(_decor_jpeg, raw)
        files["decor/decor.jpg"] = jpeg
        shoot["decor"], shoot["decor_ratio"] = True, shoot["decor_ratio"] or ratio
    else:
        shoot["decor"] = False
    sb = story_assistant.apply_shoot(sb, shoot)
    err = await asyncio.to_thread(story_assistant.check_storyboard, story_render.GEN_DIR, sb)
    if err:
        raise HTTPException(status_code=422, detail=f"storyboard invalide : {err}")
    files["storyboard.json"] = json.dumps(sb, ensure_ascii=False, indent=1).encode("utf-8")
    cid = (await _run(story_asset.create, "scene", name, "private", str(b.get("description") or "")[:300]))["cid"]
    entry = await _run(story_asset.new_version, cid, files, None, False, False, [])
    await asyncio.to_thread(videofun.ensure_root, settings.CAPTAINEMAIL)       # /VideoFUN/ existe déjà pour les clients DAV
    return {"cid": entry["cid"], "name": name, "version": entry["version"], "decor": bool(decor), "storyboard": sb}


# ─── Voix d'un personnage : un enregistrement REMPLACE la voix générée ────────────────────────────────────
MAX_VOICE_BYTES = 15 * 1024 * 1024
VOICE_MIN_S, VOICE_MAX_S = 3.0, 30.0


def _voice_to_wav(raw: bytes) -> bytes:
    """Audio quelconque (mp3, m4a, ogg, webm du navigateur…) → wav mono 48 kHz, 20 s au plus, comme generate_character.sh."""
    import tempfile
    from pathlib import Path
    ff = videofun.ffmpeg_bin()
    if not ff:
        raise HTTPException(status_code=503, detail="ffmpeg est absent de la station : conversion de la voix impossible")
    with tempfile.TemporaryDirectory(prefix="voice-") as d:
        src, dst = Path(d) / "in.bin", Path(d) / "voice.wav"
        src.write_bytes(raw)
        real = videofun.duration_of(src)
        if real and not (VOICE_MIN_S <= real <= VOICE_MAX_S):
            raise HTTPException(status_code=400, detail=f"voix de {real:.0f} s : entre {VOICE_MIN_S:.0f} et {VOICE_MAX_S:.0f} secondes attendues (une phrase naturelle)")
        proc = subprocess.run([ff, "-v", "error", "-y", "-i", str(src), "-vn", "-t", "20", "-ac", "1", "-ar", "48000", str(dst)],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not dst.exists():
            raise HTTPException(status_code=400, detail="audio illisible : " + (proc.stderr or "").strip()[-160:])
        return dst.read_bytes()


@router.post("/api/story/asset/{cid}/voice", summary="Remplacer la voix d'un personnage par un enregistrement")
async def story_character_voice(cid: str, npub: str = Depends(require_nostr_auth), file: UploadFile = File(...),
                                consent: str = Form("")):
    _require_captain(npub)
    if consent.lower() not in ("1", "true", "oui", "yes", "on"):
        raise HTTPException(status_code=400, detail="Confirmez que la personne est d'accord pour que sa voix serve dans des vidéos "
                                                    "(privées, pour elle ou ses proches).")
    raw = await file.read()
    if not raw or len(raw) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=400, detail=f"fichier audio vide ou trop gros ({MAX_VOICE_BYTES // 1024 // 1024} Mo max)")
    entry, manifest, files = await _run(story_asset.open_bundle, cid)
    if manifest.get("type") != "character" or entry.get("foreign"):
        raise HTTPException(status_code=400, detail="seul un de VOS personnages peut recevoir une voix")
    meta_path = next((p for p in files if p.endswith("character.json")), f"cast/{manifest['name']}/character.json")
    wav = await asyncio.to_thread(_voice_to_wav, raw)
    try:
        meta = json.loads(files.get(meta_path) or b"{}")
    except ValueError:
        meta = {}
    meta["voice_source"] = "upload"
    meta["voice_consent"] = {"given": True, "at": int(__import__("time").time())}
    base = meta_path.rsplit("/", 1)[0]
    new = await _run(story_asset.new_version, cid, {f"{base}/voice.wav": wav, meta_path: json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8")},
                     "Voix remplacée par un enregistrement", False, False, [])
    _LOOKS.pop(cid, None)
    return {"cid": new["cid"], "version": new["version"], "bytes": len(wav)}


# ─── Envoyer la vidéo à un ami : lien secret, page publique, lecture avec Range (iOS en a besoin) ───────────
@router.post("/api/story/share", summary="Lien secret pour envoyer un rendu à un ami")
async def story_share_create(request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    b = await _json(request)
    vcid, rid = str(b.get("version_cid") or ""), str(b.get("render_id") or "")
    source = None
    if b.get("path"):                                                          # une vidéo de /VideoFUN/ dans l'ucloud
        try:
            path = videofun.in_root(str(b["path"]))
            await asyncio.to_thread(videofun.read_video, settings.CAPTAINEMAIL, path)
        except (ValueError, FileNotFoundError, RuntimeError):
            raise HTTPException(status_code=404, detail="vidéo introuvable dans /VideoFUN/")
        source = {"kind": "ucloud", "email": settings.CAPTAINEMAIL, "path": path}
    else:
        try:
            await _run(story_render.render_file, vcid, rid, "scene.mp4")        # le rendu existe bien, et c'est le mien
        except HTTPException:
            raise HTTPException(status_code=404, detail="rendu introuvable")
    from routers import mailjet
    rec = story_share.create(story_asset.LIBRARY, vcid, rid, str(b.get("title") or "Une vidéo pour toi"), str(b.get("to") or ""),
                             int(b.get("days") or story_share.TTL_DAYS), mailjet._owner_display_name(settings.CAPTAINEMAIL), source)
    return {"token": rec["token"], "url": f"{str(settings.uSPOT).rstrip('/')}/api/story/watch/{rec['token']}",
            "expires": rec["expires"], "title": rec["title"]}


@router.get("/api/story/shares", summary="Mes liens d'envoi encore valables")
async def story_shares(npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    return {"shares": story_share.listing(story_asset.LIBRARY), "base": f"{str(settings.uSPOT).rstrip('/')}/api/story/watch/"}


@router.delete("/api/story/share/{token}", summary="Révoquer un lien d'envoi")
async def story_share_revoke(token: str, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    if not story_share.revoke(story_asset.LIBRARY, token):
        raise HTTPException(status_code=404, detail="lien introuvable")
    return {"success": True}


def _watch_page(rec: dict, token: str) -> str:
    import html
    title = html.escape(rec.get("title") or "Une vidéo pour toi")
    who = html.escape(rec.get("sender") or "Un ami")
    to = html.escape(rec.get("to") or "")
    home = html.escape(str(settings.uSPOT).rstrip("/") + "/earth/GAFAM.html")
    video = f"/api/story/watch/{token}/video"
    base = str(settings.uSPOT).rstrip("/")
    poster = f"{base}/api/story/watch/{token}/poster.jpg"        # absolu : les aperçus WhatsApp/Telegram l'exigent
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{title}</title>
<meta property="og:type" content="video.other"><meta property="og:title" content="🎬 {title}"><meta property="og:description" content="{who} t'a fait une vidéo{(' ' + to) if to else ''}">
<meta property="og:image" content="{poster}"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="{poster}">
<style>:root{{color-scheme:dark}}body{{margin:0;background:#0b1118;color:#e6edf3;font-family:system-ui,sans-serif;display:grid;min-height:100vh;place-items:center;padding:16px;box-sizing:border-box}}
main{{width:min(520px,100%);text-align:center}}h1{{font-size:1.25rem;margin:0 0 4px}}p{{color:#8ea3b5;margin:4px 0 14px}}
video{{width:100%;max-height:78vh;background:#000;border-radius:12px}}a.cta{{display:inline-block;margin-top:16px;padding:10px 18px;border-radius:10px;background:#3ecf9a;color:#06210f;font-weight:700;text-decoration:none}}
small{{display:block;margin-top:10px;color:#5b6b7a}}</style></head><body><main>
<h1>🎬 {title}</h1><p>{who} t'a fait une vidéo{(' ' + to) if to else ''}.</p>
<video src="{video}" poster="{poster}" controls playsinline preload="metadata"></video>
<a class="cta" href="{home}">Fais la tienne, c'est gratuit</a><small>Vidéo privée, visible seulement avec ce lien.</small></main></body></html>"""


@router.get("/api/story/watch/{token}", response_class=HTMLResponse, summary="PUBLIC : page de lecture d'une vidéo envoyée")
async def story_watch(token: str):
    rec = await asyncio.to_thread(story_share.get, story_asset.LIBRARY, token, True)
    if not rec:
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Lien expiré</title><body style='font-family:sans-serif;padding:2rem'>"
                            "<h1>Ce lien n'est plus valable</h1><p>Demande-en un nouveau à la personne qui te l'a envoyé.</p>",
                            status_code=404, headers={"X-Robots-Tag": "noindex"})
    return HTMLResponse(_watch_page(rec, token), headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-store",
                                                          "Referrer-Policy": "no-referrer"})


def _share_video(rec: dict):
    """(octets, chemin, taille) de la vidéo d'un lien : octets déchiffrés en mémoire pour une vidéo de /VideoFUN/ (≤ 20 Mo), chemin d'un rendu sinon."""
    src = rec.get("source") or {"kind": "render"}
    if src.get("kind") == "ucloud":
        blob, _mime = videofun.read_video(src["email"], src["path"])
        return blob, None, len(blob)
    path = story_render.render_file(rec["version_cid"], rec["render_id"], "scene.mp4")
    return None, path, path.stat().st_size


@router.get("/api/story/watch/{token}/poster.jpg", summary="PUBLIC : image d'aperçu du lien (une image de la vidéo)")
async def story_watch_poster(token: str):
    rec = await asyncio.to_thread(story_share.get, story_asset.LIBRARY, token)
    if not rec:
        raise HTTPException(status_code=404, detail="lien expiré ou inconnu")
    cache = story_share.poster_path(story_asset.LIBRARY, token)
    if not cache.exists():
        try:
            blob, path, _size = await asyncio.to_thread(_share_video, rec)
            await asyncio.to_thread(videofun.poster_frame, blob, path, cache)
        except (story_asset.AssetError, ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=404, detail=f"aperçu indisponible : {exc}")
    return Response(content=cache.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400", "X-Robots-Tag": "noindex"})


@router.get("/api/story/watch/{token}/video", summary="PUBLIC : la vidéo (Range pris en charge)")
async def story_watch_video(token: str, request: Request):
    rec = await asyncio.to_thread(story_share.get, story_asset.LIBRARY, token)
    if not rec:
        raise HTTPException(status_code=404, detail="lien expiré ou inconnu")
    try:
        blob, path, size = await asyncio.to_thread(_share_video, rec)
    except (story_asset.AssetError, ValueError, FileNotFoundError, RuntimeError):
        raise HTTPException(status_code=404, detail="vidéo introuvable")
    rng = story_share.byte_range(request.headers.get("range", ""), size)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600", "X-Robots-Tag": "noindex"}
    if rng == (-1, -1):
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    start, end = rng if rng else (0, size - 1)

    def chunks():
        if blob is not None:
            yield blob[start:end + 1]
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                data = fh.read(min(256 * 1024, left))
                if not data:
                    break
                left -= len(data)
                yield data
    headers.update({"Content-Length": str(end - start + 1), "Content-Type": "video/mp4"})
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(chunks(), status_code=206 if rng else 200, headers=headers)


# ─── Ranger une vidéo dans /VideoFUN/ (ucloud) : automatique pour les scènes de l'assistant, manuel pour les autres ─────────────
def _fun_meta(sb: dict, title: str, scene: str, scene_cid: str) -> dict:
    fun = sb.get("fun") if isinstance(sb.get("fun"), dict) else {}
    return {"to": fun.get("to", ""), "contact": fun.get("contact", ""), "title": title, "scene": scene, "scene_cid": scene_cid,
            "cast": sorted((sb.get("cast") or {}).keys()), "brief": fun.get("brief", ""), "max_seconds": fun.get("max_seconds")}


def _archive_to_ucloud(job: dict, work) -> dict | None:
    """Crochet de fin de rendu (story_render.ON_SCENE_DONE) : copie la vidéo dans /VideoFUN/<ami>/ — seulement si la scène sort de
    l'assistant (bloc « fun » du storyboard) et m'appartient ; les autres rendus se rangent à la main (to-ucloud)."""
    from pathlib import Path
    if not job.get("mine"):
        return None
    try:
        sb = json.loads((Path(job["src"]) / "storyboard.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(sb.get("fun"), dict):
        return None
    return videofun.archive(settings.CAPTAINEMAIL, Path(work) / "scene.mp4", _fun_meta(sb, job["name"], job["name"], job["cid"]))


story_render.ON_SCENE_DONE = _archive_to_ucloud


@router.post("/api/story/render/{version_cid}/{render_id}/to-ucloud", summary="Ranger ce rendu dans /VideoFUN/ de mon ucloud")
async def story_render_to_ucloud(version_cid: str, render_id: str, request: Request, npub: str = Depends(require_nostr_auth)):
    _require_captain(npub)
    b = await _json(request)
    path = await _run(story_render.render_file, version_cid, render_id, "scene.mp4")
    try:
        _e, manifest, files = await _run(story_asset.open_bundle, version_cid)
        sb = json.loads(files.get("storyboard.json") or b"{}")
    except Exception:
        manifest, sb = {"name": "Vidéo"}, {}
    meta = _fun_meta(sb, str(b.get("title") or manifest.get("name") or "Vidéo"), manifest.get("name", ""), version_cid)
    if b.get("to"):
        meta["to"] = str(b["to"])[:60]
    try:
        return await asyncio.to_thread(videofun.archive, settings.CAPTAINEMAIL, path, meta)
    except Exception as exc:   # espace plein, fichier trop gros pour l'ucloud (20 Mo)…
        raise HTTPException(status_code=409, detail=f"rangement impossible : {exc}")
