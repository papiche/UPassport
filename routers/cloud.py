"""
routers/cloud.py — Cloud personnel chiffré du MULTIPASS (enrôlement WebDAV).

Le transfert de fichiers lui-même ne passe PAS par ce router : il se fait en
WebDAV standard sur `/dav/…` (montage WSGI déclaré dans 54321.py, logique dans
services/cloud_storage.py). Les clients DAV (Nautilus, Finder, davfs2,
Explorateur Windows) ne savent pas signer un event NOSTR par requête ; ce router
leur délivre donc UNE FOIS un token Basic Auth opaque, en échange d'une preuve
de possession de la clé MULTIPASS (NIP-98, ou NIP-42 en repli — les deux sont
gérés par `services.nostr.require_nostr_auth`).

    POST /api/cloud/enroll  → {"dav_url", "email", "token", "instructions"}
    GET  /api/cloud/status  → {"enrolled", "dav_url", ...}   (ne révèle pas le token)
    POST /api/cloud/reveal  → {"dav_url", "email", "token", "instructions"}
                               (récupère le token EXISTANT, sans le renouveler)

Il n'y a PAS d'endpoint d'upload JSON ici : l'ancien placeholder
`POST /api/cloud/upload` ne faisait rien de réel et est remplacé par le vrai
`PUT /dav/<chemin>` — un seul chemin d'écriture, donc un seul endroit où le
chiffrement AES-256-GCM est appliqué.
"""

import asyncio
import io
import json
import logging
import subprocess
import sys

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from core.config import settings
from services.nostr import require_nostr_auth
from services import cloud_storage, webdav_client
from utils.crypto import npub_to_hex

logger = logging.getLogger(__name__)

router = APIRouter()


def _hex_from_npub(npub: str) -> str:
    """npub bech32 (ou hex déjà brut) → hex 64. Lève 400 si inexploitable."""
    key = (npub or "").strip().lower()
    if len(key) == 64 and all(c in "0123456789abcdef" for c in key):
        return key
    hex_pub = npub_to_hex(key)
    if not hex_pub:
        raise HTTPException(status_code=400, detail=f"npub invalide: {npub!r}")
    return hex_pub


def _email_for_authenticated_npub(npub: str) -> str:
    """Résout le MULTIPASS (EMAIL) du npub authentifié.

    Même source de vérité que routers/identity.py::_find_email_by_npub :
    le scan de ~/.zen/game/nostr/*/.secret.nostr. On passe par le hex pour
    accepter indifféremment npub1… et hex brut.
    """
    hex_pub = _hex_from_npub(npub)
    email = cloud_storage.email_for_hex(hex_pub)
    if not email:
        raise HTTPException(
            status_code=404,
            detail="Aucun MULTIPASS sur cette station pour cette clé NOSTR. "
                   "Créez-le d'abord (/g1nostr), ou enrôlez-vous sur votre station d'origine.",
        )
    return email


def _mount_instructions(dav_url: str, email: str) -> dict:
    return {
        "login": email,
        "password": "le champ `token` de cette réponse",
        "linux": f"sudo mount -t davfs {dav_url} ~/UPlanet",
        "linux_gnome": f"dav{'s' if dav_url.startswith('https') else ''}://{dav_url.split('://', 1)[-1]}",
        "macos": f"Finder → Aller → Se connecter au serveur… → {dav_url}",
        "windows": f"Explorateur → Ce PC → Ajouter un emplacement réseau → {dav_url}",
        "note": "Les fichiers sont chiffrés (AES-256-GCM) avant d'être stockés sur IPFS. "
                "Les clés vivent uniquement sur cette station, dans votre keyring privé.",
    }


@router.post(
    "/api/cloud/enroll",
    summary="Enrôler un client WebDAV",
    description="Délivre (ou renouvelle) le token Basic Auth du cloud chiffré "
                "personnel. Authentification NIP-98 (ou NIP-42 en repli).",
)
async def enroll_cloud(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    hex_pub = _hex_from_npub(npub)

    try:
        token = cloud_storage.create_dav_token(email, hex_pub)
    except OSError as exc:
        logger.error("ucloud: création du token DAV impossible pour %s: %s", email, exc)
        raise HTTPException(status_code=500, detail=f"ERROR:ucloud:{exc}")

    dav_url = await cloud_storage.dav_public_url()
    logger.info("ucloud: enrôlement DAV pour %s", email)
    return JSONResponse(
        {
            "success": True,
            "dav_url": dav_url,
            "email": email,
            "token": token,
            "instructions": _mount_instructions(dav_url, email),
        }
    )


@router.get(
    "/api/cloud/status",
    summary="État du cloud chiffré personnel",
    description="Indique si un token WebDAV existe déjà pour ce MULTIPASS. "
                "Ne révèle jamais le token.",
)
async def cloud_status(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    desc = cloud_storage.load_dav_token(email)
    dav_url = await cloud_storage.dav_public_url()

    try:
        index = cloud_storage.load_index(email)
        entries = index.get("entries", {})
        files = sum(1 for e in entries.values() if isinstance(e, dict) and e.get("type") == "file")
        bytes_plain = sum(
            e.get("size_plain") or 0
            for e in entries.values()
            if isinstance(e, dict) and e.get("type") == "file"
        )
    except RuntimeError as exc:
        logger.error("ucloud: index illisible pour %s: %s", email, exc)
        files, bytes_plain = 0, 0

    return JSONResponse(
        {
            "enrolled": bool(desc),
            "dav_url": dav_url,
            "email": email,
            "enrolled_at": (desc or {}).get("created_at"),
            "files": files,
            "bytes": bytes_plain,
            "max_file_size": cloud_storage.MAX_FILE_SIZE,
        }
    )


@router.post(
    "/api/cloud/reveal",
    summary="Récupérer le mot de passe WebDAV déjà délivré",
    description="Retourne le token Basic Auth EXISTANT, sans le renouveler "
                "(contrairement à /api/cloud/enroll, qui en génère un nouveau "
                "et déconnecte les clients déjà montés). Authentification "
                "NIP-98 (ou NIP-42) : la même preuve de possession de la clé "
                "MULTIPASS donne de toute façon un accès complet à /dav/ en "
                "direct — révéler ce token n'élargit donc aucun accès pour "
                "cet appelant, ça lui évite juste de devoir le régénérer.",
)
async def reveal_cloud_token(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    desc = cloud_storage.load_dav_token(email)
    if not desc or not desc.get("token"):
        raise HTTPException(
            status_code=404,
            detail="Aucun cloud activé pour ce MULTIPASS — utilisez d'abord /api/cloud/enroll.",
        )

    dav_url = await cloud_storage.dav_public_url()
    logger.info("ucloud: mot de passe DAV récupéré (reveal) pour %s", email)
    return JSONResponse(
        {
            "success": True,
            "dav_url": dav_url,
            "email": email,
            "token": desc["token"],
            "instructions": _mount_instructions(dav_url, email),
        }
    )


@router.get(
    "/api/cloud/files",
    summary="Lister les fichiers du cloud chiffré",
    description="TOUS les fichiers présents sur /dav/ — pas seulement ceux "
                "catalogués par FaceID (visages). Sert de navigateur générique "
                "dans FaceCloud, section « Mes fichiers ».",
)
async def list_cloud_files(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    index = cloud_storage.load_index(email)
    files = []
    for path, entry in index.get("entries", {}).items():
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        files.append({
            "path": path,
            "mime": entry.get("mime") or "",
            "size": entry.get("size_plain") or 0,
            "mtime": entry.get("mtime") or 0,
            "tags": entry.get("tags") or [],
            "readonly": bool(entry.get("readonly")),
            # "no_face" (satellite_face_matcher.py::_tag_ucloud_no_face) :
            # analysé, aucun visage trouvé — conservé quand même (post-
            # traitement futur), distinct de "pas encore analysé" (absent).
            "faceid_status": (entry.get("faceid") or {}).get("status"),
        })
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return JSONResponse({"files": files})


@router.post(
    "/api/cloud/files/delete",
    summary="Supprimer plusieurs fichiers du cloud chiffré",
    description="Suppression définitive (index + clé de keyring + dépin IPFS "
                "des CID devenus orphelins) d'un lot de chemins — même "
                "discipline que UCloudFileResource.delete() (DAV). Les "
                "entrées en lecture seule (partagées par un autre compte) "
                "sont ignorées, jamais supprimées d'ici.",
)
async def delete_cloud_files(
    request: Request,
    paths: str = Form(..., description="Tableau JSON de chemins, ex. '[\"/Photos/a.jpg\"]'"),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    try:
        requested = json.loads(paths)
    except json.JSONDecodeError:
        return JSONResponse({"error": "paths doit être un tableau JSON de chemins."}, status_code=400)
    if not isinstance(requested, list) or not requested or not all(isinstance(p, str) for p in requested):
        return JSONResponse({"error": "paths doit être un tableau JSON non vide de chaînes."}, status_code=400)
    if len(requested) > 200:
        return JSONResponse({"error": "Trop de fichiers sélectionnés (200 max)."}, status_code=400)

    deleted = []
    skipped_readonly = []
    with cloud_storage.index_lock(email):
        index = cloud_storage.load_index(email)
        keyring = cloud_storage.load_keyring(email)
        old_keyring = dict(keyring)
        for path in requested:
            entry = cloud_storage.get_entry(index, path)
            if entry is None:
                continue
            if entry.get("readonly"):
                skipped_readonly.append(path)
                continue
            cloud_storage.remove_subtree(index, path)
            deleted.append(path)
        keyring = cloud_storage.prune_keyring(index, keyring)
        cloud_storage.save_keyring(email, keyring)
        cloud_storage.save_index(email, index)
    # Hors du verrou (I/O réseau IPFS) — best-effort, cf. ipfs_unpin().
    cloud_storage.unpin_orphaned_cids(old_keyring, keyring)

    logger.info("ucloud: suppression groupée pour %s — %d supprimé(s), %d lecture seule ignoré(s)",
                email, len(deleted), len(skipped_readonly))
    return JSONResponse({"success": True, "deleted": deleted, "skipped_readonly": skipped_readonly})


@router.get(
    "/api/cloud/thumbnail",
    summary="Miniature d'un fichier du cloud chiffré",
    description="Miniature JPEG (300×300 max) de N'IMPORTE QUEL fichier image "
                "présent sur /dav/, déchiffrée à la volée et jamais persistée "
                "en clair — même discipline que le GET /dav/ lui-même. "
                "Contrairement à /mailjet/faces|inventory/thumbnail, ne requiert "
                "aucun catalogage FaceID/inventaire préalable.",
)
async def cloud_file_thumbnail(
    request: Request,
    path: str = Query(...),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    index = cloud_storage.load_index(email)
    entry = cloud_storage.get_entry(index, path)
    if not entry or not entry.get("cid"):
        raise HTTPException(status_code=404, detail="fichier introuvable")
    if not (entry.get("mime") or "").startswith("image/"):
        raise HTTPException(status_code=415, detail="pas une image")

    key_hex = cloud_storage.get_key_hex(email, entry["cid"])
    if not key_hex:
        raise HTTPException(status_code=404, detail="clé de déchiffrement introuvable")

    try:
        # ipfs_cat() est synchrone (streaming httpx borné) : hors thread, elle
        # gèlerait la boucle uvicorn le temps du téléchargement.
        payload = await asyncio.to_thread(cloud_storage.ipfs_cat, entry["cid"])
        plaintext = cloud_storage.uenc_codec.decrypt_aes256gcm(payload, key_hex)
    except Exception as exc:
        logger.error("ucloud: miniature — déchiffrement échoué pour %s: %s", path, exc)
        raise HTTPException(status_code=502, detail=f"déchiffrement impossible: {type(exc).__name__}")

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(plaintext)).convert("RGB")
        img.thumbnail((300, 300))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"miniature impossible: {type(exc).__name__}")

    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=1800"},
    )


@router.post(
    "/api/cloud/revoke",
    summary="Révoquer le token WebDAV",
    description="Supprime le token Basic Auth : tous les clients DAV montés "
                "sont déconnectés. Les fichiers et les clés ne sont pas touchés.",
)
async def revoke_cloud(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    revoked = cloud_storage.revoke_dav_token(email)
    logger.info("ucloud: révocation DAV pour %s (existait=%s)", email, revoked)
    return JSONResponse({"success": True, "revoked": revoked, "email": email})


# ═════════════════════════════════════════════════════════════════════════════
# Sources WebDAV externes — import quotidien vers FaceCloud
# ═════════════════════════════════════════════════════════════════════════════
# Permet d'alimenter régulièrement son propre .ucloud depuis un dossier d'un
# AUTRE serveur WebDAV (cf. services/webdav_client.py, services/cloud_storage.py
# ::load_webdav_sources, webdav_import.py). Le mot de passe du serveur tiers
# n'est JAMAIS renvoyé par GET — seul `POST` le reçoit, à l'ajout.

def _public_source(source: dict) -> dict:
    """Vue sans mot de passe — seule forme jamais renvoyée au client."""
    return {k: v for k, v in source.items() if k != "password"}


@router.get(
    "/api/cloud/webdav-sources",
    summary="Lister les sources WebDAV externes configurées",
)
async def list_webdav_sources(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    sources = cloud_storage.load_webdav_sources(email)
    return JSONResponse({"sources": [_public_source(s) for s in sources]})


@router.post(
    "/api/cloud/webdav-browse",
    summary="Parcourir un dossier d'un serveur WebDAV tiers",
    description="PROPFIND Depth:1 sur `url`+`path` — alimente le sélecteur de "
                "dossier de ucloud.html AVANT d'enregistrer une source (les "
                "identifiants ne sont pas persistés par cet appel).",
)
async def browse_webdav(
    request: Request,
    url: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    path: str = Form(default=""),
    npub: str = Depends(require_nostr_auth),
):
    _email_for_authenticated_npub(npub)   # auth seule : pas besoin de l'email ici
    try:
        items = await webdav_client.list_folder(url, username, password, path)
    except webdav_client.WebdavError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"path": path, "items": items})


@router.post(
    "/api/cloud/webdav-sources",
    summary="Ajouter une source WebDAV externe",
    description="Vérifie la connexion (PROPFIND) avant d'enregistrer — un "
                "identifiant invalide échoue ici plutôt qu'en silence le "
                "lendemain, dans le cron quotidien.",
)
async def add_webdav_source(
    request: Request,
    label: str = Form(...),
    url: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    remote_path: str = Form(default=""),
    dest_prefix: str = Form(default="/Photos/Import"),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    try:
        await webdav_client.check_connection(url, username, password)
    except webdav_client.WebdavError as exc:
        return JSONResponse({"error": f"Connexion refusée : {exc}"}, status_code=400)

    entry = cloud_storage.add_webdav_source(
        email, label=label.strip() or url, url=url.strip(), username=username.strip(),
        password=password, remote_path=remote_path.strip("/"),
        dest_prefix=dest_prefix.strip() or "/Photos/Import",
    )
    return JSONResponse({"success": True, "source": _public_source(entry)})


@router.delete(
    "/api/cloud/webdav-sources/{source_id}",
    summary="Supprimer une source WebDAV externe",
)
async def delete_webdav_source(
    request: Request, source_id: str, npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    removed = cloud_storage.remove_webdav_source(email, source_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Source introuvable")
    return JSONResponse({"success": True})


@router.post(
    "/api/cloud/webdav-sources/{source_id}/sync",
    summary="Importer maintenant (hors attente du cron quotidien)",
    description="Lance webdav_import.py en arrière-plan pour CETTE seule "
                "source — répond immédiatement, le résultat apparaît dans "
                "`last_sync` au prochain GET /api/cloud/webdav-sources "
                "(l'UI propose un bouton Actualiser, même discipline que "
                "l'analyse FaceID asynchrone).",
)
async def sync_webdav_source_now(
    request: Request, source_id: str, npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    sources = cloud_storage.load_webdav_sources(email)
    if not any(s.get("id") == source_id for s in sources):
        raise HTTPException(status_code=404, detail="Source introuvable")

    script = settings.BASE_DIR / "webdav_import.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="webdav_import.py introuvable")
    try:
        subprocess.Popen(
            [sys.executable, str(script), email, "--source-id", source_id],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("ucloud: déclenchement webdav_import impossible pour %s: %s", email, exc)
        raise HTTPException(status_code=500, detail="Lancement de l'import impossible")
    logger.info("ucloud: import WebDAV manuel déclenché pour %s (source %s)", email, source_id)
    return JSONResponse({"success": True, "message": "Import lancé en arrière-plan"})


@router.get(
    "/api/cloud/webdav-sources/{source_id}/status",
    summary="Niveau de synchro d'une source WebDAV",
    description="Compte les images présentes côté serveur DISTANT (PROPFIND "
                "récursif plafonné, cf. webdav_client.walk_files) et celles "
                "déjà cataloguées LOCALEMENT sous dest_prefix — un aperçu, "
                "PAS un import. Appel potentiellement lent (parcours réseau "
                "complet du dossier distant) : à la demande (bouton dédié), "
                "jamais automatique au chargement de la page.",
)
async def webdav_source_status(
    request: Request, source_id: str, npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    sources = cloud_storage.load_webdav_sources(email)
    source = next((s for s in sources if s.get("id") == source_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="Source introuvable")

    try:
        remote_files = await webdav_client.walk_files(
            source["url"], source["username"], source["password"],
            source.get("remote_path") or "")
    except webdav_client.WebdavError as exc:
        return JSONResponse({"error": f"Connexion refusée : {exc}"}, status_code=502)

    dest_prefix = (source.get("dest_prefix") or "/Photos/Import").rstrip("/")
    index = cloud_storage.load_index(email)
    local_count = sum(
        1 for path, e in (index.get("entries") or {}).items()
        if isinstance(e, dict) and e.get("type") == "file" and path.startswith(dest_prefix + "/")
    )
    return JSONResponse({
        "remote_count": len(remote_files),
        "remote_capped": len(remote_files) >= 2000,
        "local_count": local_count,
    })
