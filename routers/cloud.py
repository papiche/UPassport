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
import base64
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from core.config import settings
from services.nostr import require_nostr_auth
from services import analysis, cloud_storage, ucloud_embed, webdav_client
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


def _sync_urls(dav_url: str) -> dict:
    """Adresses de synchro téléphone/Thunderbird (même hôte et mêmes identifiants que /dav)."""
    base = re.sub(r"/dav/?$", "", dav_url)
    return {"caldav_url": base + "/caldav/", "carddav_url": base + "/carddav/"}


def _mount_instructions(dav_url: str, email: str) -> dict:
    urls = _sync_urls(dav_url)
    return {
        **urls,
        "android": f"DAVx⁵ → compte « URL et identifiant » → {urls['caldav_url']} (agenda) puis {urls['carddav_url']} (contacts)",
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
            **_sync_urls(dav_url),
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
        # Agrégats SQL (un placeholder sans CID n'est pas un fichier : le compter
        # laissait « N photos en attente » à vie) — aucune entrée n'est chargée.
        st = await asyncio.to_thread(cloud_storage.ucloud_db.stats, cloud_storage.ucloud_dir(email))
        files, bytes_plain, faceid, received = st["files"], st["bytes"], st["faceid"], st["received"]
    except Exception as exc:
        logger.error("ucloud: index illisible pour %s: %s", email, exc)
        files, bytes_plain, faceid, received = 0, 0, {}, 0

    return JSONResponse(
        {
            "enrolled": bool(desc),
            "dav_url": dav_url,
            **_sync_urls(dav_url),
            "email": email,
            "enrolled_at": (desc or {}).get("created_at"),
            "files": files,
            "bytes": bytes_plain,
            "max_file_size": cloud_storage.MAX_FILE_SIZE,
            "faceid": faceid,
            "received": received,
            "reprocess": _reprocess_progress(email),
            "blocked_senders": cloud_storage.blocked_senders(email),
        }
    )


@router.post("/api/cloud/senders", summary="Bloquer / débloquer un expéditeur de photos")
async def set_sender(request: Request, hex: str = Form(...), blocked: str = Form(default="1"),
                     npub: str = Depends(require_nostr_auth)):
    """« Ne plus recevoir de photos de X » : appliqué à tous les canaux
    (cloud_storage.ingest_received). Les photos déjà reçues ne sont pas
    supprimées ici (action séparée, /api/cloud/files/delete)."""
    email = _email_for_authenticated_npub(npub)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", hex or ""):
        raise HTTPException(status_code=400, detail="clé invalide")
    current = await asyncio.to_thread(cloud_storage.set_sender_blocked, email, hex,
                                      blocked not in ("0", "false", ""))
    return JSONResponse({"ok": True, "blocked_senders": current})


@router.get("/api/cloud/face-ref", summary="Mon visage de référence confié aux amis")
async def face_ref_status(npub: str = Depends(require_nostr_auth)):
    """`{enabled, has_reference, given:[{hex, name, at}]}` : le réglage, si « Votre visage » est désigné,
    et à qui l'empreinte a été confiée (donc qui peut me reconnaître dans ses photos)."""
    from services import face_ref
    email = _email_for_authenticated_npub(npub)
    vectors = await asyncio.to_thread(face_ref.reference_vectors, email)
    return JSONResponse({"enabled": face_ref.sharing_enabled(email), "has_reference": bool(vectors),
                         "given": [{"hex": h, "name": g.get("name") or "", "at": g.get("at")}
                                   for h, g in face_ref.given(email).items()]})


@router.post("/api/cloud/face-ref", summary="Activer / couper l'envoi de mon empreinte avec mes photos")
async def face_ref_set(enabled: str = Form(...), npub: str = Depends(require_nostr_auth)):
    from services import face_ref
    email = _email_for_authenticated_npub(npub)
    face_ref.set_sharing(email, enabled not in ("0", "false", ""))
    return JSONResponse({"ok": True, "enabled": face_ref.sharing_enabled(email)})


@router.post("/api/cloud/face-ref/revoke", summary="Retirer mon empreinte chez mes amis (tous, ou un seul)")
async def face_ref_revoke(hex: str = Form(default=""), npub: str = Depends(require_nostr_auth)):
    """Les amis hébergés ici l'effacent tout de suite ; ceux d'une autre station reçoivent une demande de retrait
    (canal inter-NODE). Les photos déjà reçues ne bougent pas. Rend `{revoked, pending}`."""
    from services import face_ref, identity_links, node_share
    email = _email_for_authenticated_npub(npub)
    my_hex = cloud_storage.hex_for_email(email) or ""

    def run() -> dict:
        revoked, pending = [], []
        for to_hex in ([hex.lower()] if hex else list(face_ref.given(email))):
            acct = identity_links.email_for_hex(to_hex)
            if acct and acct.get("where") == "local":
                ok = face_ref.forget(acct["email"], my_hex)
            elif acct and acct.get("node"):
                ok = node_share.revoke_remote(email, acct)
            else:
                ok = False
            (revoked if ok else pending).append(to_hex)
        data = face_ref.given(email)
        for h in revoked:
            data.pop(h, None)
        face_ref._write_json(face_ref._given_path(email), data)
        return {"revoked": revoked, "pending": pending}

    return JSONResponse(await asyncio.to_thread(run))


def _reprocess_progress(email: str) -> Optional[dict]:
    """État du dernier rattrapage FaceID (faceid_reprocess.py), ou None.
    `running` est recoupé avec l'existence du PID : un script tué laisse
    sinon un « en cours » éternel dans le fichier de progression."""
    path = cloud_storage.ucloud_dir(email) / "faceid_reprocess.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("running"):
        try:
            os.kill(int(data.get("pid") or 0), 0)
        except (OSError, ValueError):
            data["running"] = False
    return data


_PIN_TASKS: set = set()


async def _run_backup(*args: str, timeout: int = 600) -> dict:
    """UPassport/ucloud_backup.py en sous-processus (jamais importé ici :
    son amorce se ré-exécute dans le venv, à garder hors du serveur)."""
    script = settings.BASE_DIR / "ucloud_backup.py"
    proc = await asyncio.create_subprocess_exec(sys.executable, str(script), *args,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="Opération trop longue")
    try:
        return json.loads(out.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        logger.warning("ucloud_backup %s : %s", args[:1], err.decode()[-400:])
        raise HTTPException(status_code=500, detail="Sauvegarde indisponible")


@router.get("/api/cloud/backup", summary="État de la sauvegarde hors station du cloud")
async def backup_status(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    return JSONResponse(await _run_backup("status", email, timeout=30))


@router.post("/api/cloud/backup", summary="Sauvegarder le cloud maintenant")
async def backup_now(request: Request, npub: str = Depends(require_nostr_auth)):
    """Archive chiffrée (index, trousseau, sources, visages) sur IPFS + UN
    event kind 30078 d=facecloud:backup chiffré NIP-44 pour soi-même."""
    email = _email_for_authenticated_npub(npub)
    return JSONResponse(await _run_backup("snapshot", email))


@router.post("/api/cloud/backup/restore", summary="Restaurer le cloud depuis la sauvegarde")
async def backup_restore(request: Request, npub: str = Depends(require_nostr_auth)):
    """FUSION : ajoute ce qui manque ici (fichiers, clés, sources, visages),
    n'écrase rien. Utile après une perte locale, ou en arrivant sur une
    nouvelle station avec le même MULTIPASS. Épingle ensuite les fichiers."""
    email = _email_for_authenticated_npub(npub)
    res = await _run_backup("restore", email, "--from-relay")
    if res.get("status") == "restored" and res.get("files"):
        task = asyncio.create_task(_run_backup("pin", email, timeout=24 * 3600))
        _PIN_TASKS.add(task)                      # référence forte jusqu'à la fin
        task.add_done_callback(_PIN_TASKS.discard)
    return JSONResponse(res)


@router.post(
    "/api/cloud/faceid-reprocess",
    summary="Relancer l'analyse des visages (rattrapage silencieux)",
    description="Lance UPassport/faceid_reprocess.py --quick en arrière-plan : "
                "images jamais analysées ou sans visage, ComfyUI en direct "
                "(local/tunnel/P2P IPFS, repli relais NOSTR), sans notification. Progression dans GET /api/cloud/status "
                "(`reprocess`). Refusé si un rattrapage tourne déjà.",
)
async def faceid_reprocess(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    current = _reprocess_progress(email)
    if current and current.get("running"):
        return JSONResponse({"success": False, "error": "Une analyse est déjà en cours",
                             "reprocess": current}, status_code=409)
    script = settings.BASE_DIR / "faceid_reprocess.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="faceid_reprocess.py introuvable")
    try:
        subprocess.Popen(
            [sys.executable, str(script), email, "--quick"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("ucloud: rattrapage FaceID impossible pour %s: %s", email, exc)
        raise HTTPException(status_code=500, detail="Lancement impossible")
    logger.info("ucloud: rattrapage FaceID lancé pour %s", email)
    return JSONResponse({"success": True})


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
            **_sync_urls(dav_url),
            "email": email,
            "token": desc["token"],
            "instructions": _mount_instructions(dav_url, email),
        }
    )


def _list_files(email: str) -> list:
    """Fichiers avec contenu, depuis SQLite (les colonnes dérivées évitent de parser
    chaque entrée ; `data` n'est lu que pour les champs rares)."""
    conn = cloud_storage.db(email)
    files = []
    for path, mime, size, mtime, taken, data in conn.execute(
            "SELECT path,mime,size,mtime,taken_at,data FROM entries WHERE type='file' AND cid IS NOT NULL"):
        entry = json.loads(data)
        shared = entry.get("origin") == "facecloud-share"
        faceid = entry.get("faceid") or {}
        files.append({
            "path": path,
            "mime": mime or "",
            "size": size or 0,
            "mtime": mtime or 0,
            # Date de prise de vue (EXIF) quand on l'a : l'upload date de l'envoi,
            # pas de la photo (un client DAV n'envoie pas la date d'origine).
            "taken_at": taken or 0,
            "tags": entry.get("tags") or [],
            "readonly": bool(entry.get("readonly")),
            # "no_face" : analysé, aucun visage trouvé — conservé quand même,
            # distinct de "pas encore analysé" (absent).
            "faceid_status": faceid.get("status"),
            "faces": faceid.get("count") or 0,
            # Copie reçue d'un ami (cloud_storage.share_copy) : d'où elle vient.
            "from_name": entry.get("from_name") if shared else None,
            "from_hex": entry.get("from_hex") if shared else None,
        })
    return files


@router.get(
    "/api/cloud/files",
    summary="Lister les fichiers du cloud chiffré",
    description="TOUS les fichiers présents sur /dav/ — pas seulement ceux "
                "catalogués par FaceID (visages). Sert de navigateur générique "
                "dans FaceCloud, section « Mes fichiers ».",
)
async def list_cloud_files(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    files = await asyncio.to_thread(_list_files, email)
    files.sort(key=lambda f: f["taken_at"] or f["mtime"], reverse=True)
    return JSONResponse({"files": files})


@router.get(
    "/api/cloud/files/page",
    summary="Une page de la galerie (images), la plus récente d'abord",
    description="Pagination et filtres EN SQL (all|faces|no_face|waiting|received), `q` = "
                "sous-chaîne du chemin. Rend aussi le nombre d'images par filtre : l'interface "
                "affiche les premières photos sans attendre tout le catalogue.",
)
async def list_cloud_files_page(
    request: Request,
    flt: str = Query(default="all", alias="filter"),
    q: str = Query(default="", max_length=120),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=120, ge=1, le=500),
    counts: int = Query(default=0),
    since: Optional[int] = Query(default=None),
    until: Optional[int] = Query(default=None),
    lat: Optional[float] = Query(default=None),
    lon: Optional[float] = Query(default=None),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)

    def work():
        udir = cloud_storage.ensure_ucloud_dir(email)
        rows, total = cloud_storage.ucloud_db.page(udir, flt, q, offset, limit, want_total=offset == 0,
                                                   since=since, until=until, lat=lat, lon=lon)
        out = {"files": [_file_row(p, e) for p, e in rows], "total": total, "offset": offset}
        if counts or offset == 0:
            out["counts"] = cloud_storage.ucloud_db.gallery_counts(udir)
        return out
    return JSONResponse(await asyncio.to_thread(work))


def _file_row(path: str, entry: dict) -> dict:
    shared = entry.get("origin") == "facecloud-share"
    faceid = entry.get("faceid") or {}
    return {
        "path": path, "mime": entry.get("mime") or "", "size": entry.get("size_plain") or 0,
        "mtime": entry.get("mtime") or 0, "taken_at": entry.get("taken_at") or 0,
        "tags": entry.get("tags") or [], "readonly": bool(entry.get("readonly")),
        "faceid_status": faceid.get("status"), "faces": faceid.get("count") or 0,
        "from_name": entry.get("from_name") if shared else None,
        "from_hex": entry.get("from_hex") if shared else None,
        "desc": (entry.get("desc") or {}).get("text") or "", "desc_src": (entry.get("desc") or {}).get("src") or "",
    }


@router.get(
    "/api/cloud/quota",
    summary="Espace utilisé / offre du MULTIPASS (10 Go, 128 Go avec une ZenCard)",
    description="`used` = `.ucloud` (cloud chiffré) + `udrive` (dossier public) ; `quota`, `pct`, `full`, `zen_week` "
                "(coût hebdomadaire de l'offre) et `upgrade` (offre supérieure, absente pour une ZenCard). "
                "Au-delà du quota les NOUVEAUX fichiers sont refusés (DAV : 507) ; rien n'est jamais supprimé.",
)
async def cloud_quota(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    return JSONResponse(await asyncio.to_thread(
        lambda: cloud_storage.storage_quota.status(cloud_storage.nostr_dir(email), fresh=True)))


def _kick_embed(email: str, udir) -> None:
    """Lance en arrière-plan la vectorisation des descriptions qui n'ont pas encore de vecteur (une seule à la fois)."""
    from services import ucloud_embed
    try:
        if ucloud_embed.counts(udir)["pending"] <= 0 or _json_progress(udir / "embed_scan.json").get("running"):
            return
        script = settings.BASE_DIR / "ucloud_embed.py"
        if script.exists():
            subprocess.Popen([sys.executable, str(script), "scan", email], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        logger.warning("ucloud: vectorisation non lancée pour %s: %s", email, exc)


@router.get(
    "/api/cloud/search",
    summary="Recherche PAR LE SENS dans les photos (descriptions vectorisées + mots)",
    description="Classement hybride : proximité sémantique de la requête avec la description de chaque photo "
                "(`paraphrase-multilingual`, Ollama) + bonus quand tous les mots y sont. Même forme de réponse que "
                "`files/page` (`files`, `total`, `offset`, `counts`) plus `mode` (`semantic` | `keyword` si Ollama ne répond pas), "
                "`note` et `index` (photos décrites / vectorisées). Les photos sans vecteur restent trouvables par mots ; "
                "la vectorisation des manquantes est lancée en tâche de fond.",
)
async def cloud_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    flt: str = Query(default="all", alias="filter"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=120, ge=1, le=500),
    since: Optional[int] = Query(default=None),
    until: Optional[int] = Query(default=None),
    lat: Optional[float] = Query(default=None),
    lon: Optional[float] = Query(default=None),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    from services import ucloud_embed

    def work():
        udir = cloud_storage.ensure_ucloud_dir(email)
        qvec, note = None, ""
        try:
            qvec = ucloud_embed.query_vec(q)
        except ucloud_embed.EmbedError as exc:
            note = f"Recherche par mots seulement : {exc}"
        ranked = ucloud_embed.search(udir, q, qvec, flt, since, until, lat, lon)
        page = ranked[offset:offset + limit]
        rows = ucloud_embed.entries_for(udir, [p for p, _s in page])
        score = dict(page)
        files = []
        for p, e in rows:
            r = _file_row(p, e)
            r["score"] = round(score.get(p, 0.0), 3)
            files.append(r)
        out = {"files": files, "total": len(ranked), "offset": offset, "mode": "semantic" if qvec is not None else "keyword",
               "note": note, "index": ucloud_embed.counts(udir)}
        if offset == 0:
            out["counts"] = cloud_storage.ucloud_db.gallery_counts(udir)
            if qvec is not None:
                _kick_embed(email, udir)
        return out
    return JSONResponse(await asyncio.to_thread(work))


@router.get(
    "/api/cloud/videofun",
    summary="Les vidéos FUN de /VideoFUN/ (dossier DAV), par ami",
    description="Rend `videos` (récentes d'abord : path, name, to, contact, title, duration, cast, size) et `root`. Le dossier est créé "
                "s'il n'existe pas. Les vidéos viennent du Studio (story.html) ou ont été déposées par un client DAV.",
)
async def cloud_videofun(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    from services import videofun

    def work():
        videofun.ensure_root(email)
        return {"root": videofun.ROOT, "videos": videofun.list_videos(email)}
    return JSONResponse(await asyncio.to_thread(work))


@router.get(
    "/api/cloud/friend",
    summary="Fiche d'un ami : photos reçues de lui, vidéos FUN pour lui, liens d'envoi vers lui",
    description="`name` (obligatoire), `hex` (clé NOSTR) et `contact` (UID de la fiche du carnet) précisent la personne. Rend `received` {count, last, items}, "
                "`videos` (comme /api/cloud/videofun) et `shares` (liens valables, vues comprises : Capitaine seulement). Les photos OÙ elle apparaît "
                "et celles qu'on lui a envoyées viennent déjà de /mailjet/faces (visages, `sent`).",
)
async def cloud_friend(request: Request, name: str = Query(..., min_length=1, max_length=80), hex: str = Query("", max_length=64),
                       contact: str = Query("", max_length=64), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    from services import friend_card, story_render

    def work():
        lib = story_render.story_asset.LIBRARY if email == settings.CAPTAINEMAIL else None
        return friend_card.card(email, name.strip(), hex.strip().lower(), contact.strip(), lib)
    return JSONResponse(await asyncio.to_thread(work))


@router.post(
    "/api/cloud/videofun/send",
    summary="Envoyer une vidéo de /VideoFUN/ dans le cloud d'un ami (cloud à cloud, sans lien)",
    description="`path`, et `pubkey` (64 hex) ou `to_email` de l'ami. Même station : copie directe ; autre station : canal inter-NODE. "
                "404 `no_account` si l'ami n'a pas de MULTIPASS connu : proposer alors le lien secret (`POST /api/story/share`).",
)
async def cloud_videofun_send(request: Request, path: str = Form(...), pubkey: str = Form(""), to_email: str = Form(""),
                              npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    from routers import mailjet
    from services import videofun
    if pubkey and not re.fullmatch(r"[0-9a-fA-F]{64}", pubkey.strip()):
        raise HTTPException(status_code=400, detail="clé publique invalide (64 caractères hexadécimaux)")
    try:
        res = await asyncio.to_thread(videofun.send_to_friend, email, path, mailjet._owner_display_name(email), pubkey, to_email)
    except LookupError:
        return JSONResponse({"error": "no_account", "detail": "Cette personne n'a pas de MULTIPASS connu : envoyez-lui un lien."}, status_code=404)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True, **res})


@router.get("/api/cloud/videofun/file", summary="Une vidéo de /VideoFUN/ (déchiffrée en mémoire, 20 Mo au plus)")
async def cloud_videofun_file(request: Request, path: str = Query(...), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    from services import videofun
    try:
        data, mime = await asyncio.to_thread(videofun.read_video, email, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (FileNotFoundError, RuntimeError):
        raise HTTPException(status_code=404, detail="vidéo introuvable")
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store", "Content-Length": str(len(data))})


@router.delete("/api/cloud/videofun", summary="Supprimer une vidéo de /VideoFUN/")
async def cloud_videofun_delete(request: Request, path: str = Query(...), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    from services import videofun
    try:
        ok = await asyncio.to_thread(videofun.delete_video, email, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="vidéo introuvable")
    return JSONResponse({"success": True})


@router.post(
    "/api/cloud/photos/info",
    summary="Fiche (date, description…) de photos données — frise d'une personne",
    description="`paths` = tableau JSON de chemins (1000 max) ; rend `files` (même forme que `files/page`) dans l'ordre "
                "chronologique inverse, sans les chemins inconnus ou qui ne sont pas des images.",
)
async def cloud_photos_info(request: Request, paths: str = Form(...), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    try:
        wanted = json.loads(paths)
    except ValueError:
        return JSONResponse({"error": "paths : tableau JSON attendu"}, status_code=400)
    if not isinstance(wanted, list) or len(wanted) > 1000 or not all(isinstance(x, str) for x in wanted):
        return JSONResponse({"error": "paths : 1000 chemins de texte au plus"}, status_code=400)

    def work():
        udir = cloud_storage.ensure_ucloud_dir(email)
        ud = cloud_storage.ucloud_db
        conn = ud.connect(udir)
        rows = []
        for raw in dict.fromkeys(wanted):
            path = cloud_storage.normalize_path(raw)
            e = ud.get_entry(conn, path)
            if e and e.get("type") == "file" and e.get("cid") and str(e.get("mime") or "").startswith("image/"):
                rows.append(_file_row(path, e))
        rows.sort(key=lambda r: r["taken_at"] or r["mtime"], reverse=True)
        return {"files": rows}
    return JSONResponse(await asyncio.to_thread(work))


@router.get(
    "/api/cloud/senders",
    summary="Expéditeurs des photos reçues (nombre de photos chacun)",
)
async def cloud_senders(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)

    def work():
        conn = cloud_storage.db(email)
        rows = conn.execute(
            "SELECT json_extract(data,'$.from_hex'), json_extract(data,'$.from_name'), COUNT(*) FROM entries "
            "WHERE type='file' AND cid IS NOT NULL AND origin='facecloud-share' "
            "GROUP BY json_extract(data,'$.from_hex'), json_extract(data,'$.from_name') ORDER BY COUNT(*) DESC")
        return [{"hex": h, "name": n, "count": c} for h, n, c in rows]
    return JSONResponse({"senders": await asyncio.to_thread(work),
                         "blocked": cloud_storage.blocked_senders(email)})


@router.post(
    "/api/cloud/senders/purge",
    summary="Supprimer TOUTES les photos reçues d'un expéditeur",
)
async def cloud_senders_purge(request: Request, hex: str = Form(default=""), name: str = Form(default=""),
                              npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    if not hex and not name:
        raise HTTPException(status_code=400, detail="hex ou name requis")

    def work():
        conn = cloud_storage.db(email)
        rows = conn.execute(
            "SELECT path FROM entries WHERE type='file' AND origin='facecloud-share' AND "
            "(json_extract(data,'$.from_hex')=? OR (? <> '' AND json_extract(data,'$.from_name')=?))",
            (hex or "-", name, name)).fetchall()
        paths = [r[0] for r in rows]
        if not paths:
            return 0
        deleted, _ = cloud_storage.delete_paths(email, paths, skip_readonly=True)
        return len(deleted)
    return JSONResponse({"success": True, "deleted": await asyncio.to_thread(work)})


def _json_progress(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("running"):
        try:
            os.kill(int(data.get("pid") or 0), 0)
        except (OSError, ValueError):
            data["running"] = False          # un script tué laisse « en cours » éternel dans son fichier
    return data


def _analysis_status(email: str, pubkey_hex: str) -> dict:
    udir = cloud_storage.ensure_ucloud_dir(email)
    c = cloud_storage.ucloud_db.analysis_counts(udir)
    faces_run = _reprocess_progress(email) or {}
    enrich_run = _json_progress(udir / "dedupe_scan.json")
    desc_run = _json_progress(udir / "describe_scan.json")
    now = time.time()

    def rate(run: dict) -> Optional[float]:
        """Photos/minute mesurées sur le passage en cours (None tant que rien n'est fini)."""
        if not run.get("running") or not run.get("done") or not run.get("started_at"):
            return None
        return round(60.0 * run["done"] / max(1.0, now - run["started_at"]), 1)

    from services.admin_auth import get_node_and_captain_hex
    try:
        captain = bool(pubkey_hex) and pubkey_hex.lower() == get_node_and_captain_hex()[1].lower()
    except Exception:
        captain = False
    images = c["images"]
    return {
        "images": images,
        "mode": {"configured": analysis.get_mode(), "can_set": captain,
                 "faces": analysis.resolve("faces"), "describe": analysis.resolve("describe"),
                 "enrich": analysis.resolve("enrich")},
        "faces": {"done": c["faces_done"], "waiting": c["faces_waiting"], "pending": c["faces_pending"],
                  "timeout": c["faces_timeout"], "unreadable": c["unreadable"], "run": faces_run, "per_min": rate(faces_run)},
        "enrich": {"hashed": c["hashed"], "located": c["located"], "dated": c["dated"], "pending": max(0, images - c["hashed"]),
                   "run": enrich_run, "per_min": rate(enrich_run)},
        "describe": {"done": c["described"], "pending": max(0, images - c["described"] - c["unreadable"]),
                     "run": desc_run, "per_min": rate(desc_run), "model": analysis.DESCRIBE_MODEL,
                     "stale": cloud_storage.ucloud_db.describe_stale_count(udir, analysis.DESCRIBE_MODEL)},
        "embed": {**ucloud_embed.counts(udir), "model": ucloud_embed.MODEL, "run": _json_progress(udir / "embed_scan.json"),
                  "per_min": rate(_json_progress(udir / "embed_scan.json"))},
        "refresh": analysis.refresh_info(cloud_storage.nostr_dir(email)),
    }


@router.get(
    "/api/cloud/analysis",
    summary="Où en sont les analyses (visages, dates/lieux, descriptions) et par quel chemin elles tournent",
    description="Compteurs par tâche, passage en cours (avancement, photos/min), mode choisi par le capitaine "
                "(auto|direct|relay) et chemin effectivement utilisé, heure du rafraîchissement quotidien du MULTIPASS.",
)
async def cloud_analysis(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    return JSONResponse(await asyncio.to_thread(_analysis_status, email, _hex_from_npub(npub)))


@router.post(
    "/api/cloud/analysis/run",
    summary="Lancer une analyse : faces | enrich | describe",
    description="`task=faces` : rattrapage FaceID silencieux (faceid_reprocess.py) ; `task=enrich` : dates, lieux, empreintes "
                "(ucloud_dedupe.py) ; `task=describe` : descriptions IA (ucloud_describe.py), `paths` = tableau JSON pour "
                "décrire des photos précises (sinon toutes celles sans description). Le CHEMIN (direct ou relais NOSTR) est "
                "celui choisi par le capitaine. 409 si cette tâche tourne déjà.",
)
async def cloud_analysis_run(request: Request, task: str = Form(...), paths: str = Form(default=""),
                             force: str = Form(default="0"), redo: str = Form(default="0"),
                             npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    udir = cloud_storage.ensure_ucloud_dir(email)
    if task == "faces":
        cur = _reprocess_progress(email)
        script, extra, running = "faceid_reprocess.py", ["--quick"], bool(cur and cur.get("running"))
    elif task == "enrich":
        script, extra, running = "ucloud_dedupe.py", ["scan"], bool(_dedupe_progress(email).get("running"))
    elif task == "describe":
        script, extra = "ucloud_describe.py", ["scan"]
        running = bool(_json_progress(udir / "describe_scan.json").get("running"))
        if paths:
            try:
                wanted = [x for x in json.loads(paths) if isinstance(x, str)][:200]
            except ValueError:
                raise HTTPException(status_code=400, detail="paths doit être un tableau JSON")
            if wanted:
                extra += ["--paths", *wanted]
        if force in ("1", "true"):
            extra.append("--force")
        if redo in ("1", "true"):
            extra.append("--redo")
    elif task == "embed":
        script, extra = "ucloud_embed.py", ["scan"]
        running = bool(_json_progress(udir / "embed_scan.json").get("running"))
    else:
        raise HTTPException(status_code=400, detail="task inconnue (faces|enrich|describe|embed)")
    if running:
        return JSONResponse({"success": False, "error": "Cette analyse est déjà en cours"}, status_code=409)
    route = analysis.resolve("describe" if task in ("describe", "embed") else "faces") if task != "enrich" else analysis.resolve("enrich")
    if task == "embed" and not route["direct_ready"]:
        return JSONResponse({"success": False, "error": "La recherche par le sens a besoin d'Ollama en accès direct (la machine d'IA ne répond pas)."}, status_code=503)
    if route["effective"] == "none":
        return JSONResponse({"success": False, "error": route["why"]}, status_code=503)
    script_path = settings.BASE_DIR / script
    if not script_path.exists():
        raise HTTPException(status_code=500, detail=f"{script} introuvable")
    cmd = [sys.executable, str(script_path)] + ([email] + extra if script == "faceid_reprocess.py"
                                                else [extra[0], email] + extra[1:])
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        logger.warning("ucloud: analyse %s impossible pour %s: %s", task, email, exc)
        raise HTTPException(status_code=500, detail="Lancement impossible")
    logger.info("ucloud: analyse %s lancée pour %s (chemin : %s)", task, email, route["effective"])
    return JSONResponse({"success": True, "task": task, "route": route})


@router.get("/api/cloud/photo", summary="Une photo : métadonnées, description, état de la description en cours")
async def cloud_photo(request: Request, path: str = Query(...), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    try:
        entry = await asyncio.to_thread(cloud_storage.db_entry, email, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="chemin invalide")
    if not entry or entry.get("type") != "file":
        raise HTTPException(status_code=404, detail="fichier introuvable")
    row = _file_row(cloud_storage.normalize_path(path), entry)
    # `desc_job` : tâche envoyée au Brain (mode relais), résultat pas encore revenu.
    row["desc_pending"] = bool(entry.get("desc_job") and time.time() - entry["desc_job"] < 40 * 60)
    return JSONResponse(row)


@router.put(
    "/api/cloud/photo/desc",
    summary="Écrire ou corriger la description d'une photo (texte vide = la retirer)",
    description="Une description saisie ici (`src: user`) n'est jamais remplacée par une description IA.",
)
async def cloud_photo_desc(request: Request, path: str = Form(...), text: str = Form(default=""),
                           npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    try:
        entry = await asyncio.to_thread(cloud_storage.db_entry, email, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="chemin invalide")
    if not entry or entry.get("type") != "file":
        raise HTTPException(status_code=404, detail="fichier introuvable")
    r = await asyncio.to_thread(cloud_storage.set_photo_description, email, path, text, "user")
    return JSONResponse({"success": True, "result": r, "text": " ".join(text.split())[:cloud_storage.MAX_DESC]})


@router.get(
    "/api/cloud/geo",
    summary="Carte : lieux des photos entre deux dates",
    description="`points` : un lieu par case de 0,01° (≈ 1 km) avec le nombre de photos entre `since` et "
                "`until` (secondes), la plus récente en couverture ; `months` : histogramme AAAA-MM des "
                "photos géolocalisées (toutes dates). Regroupé en SQL : jamais une ligne par photo. Les "
                "coordonnées sont celles de l'EXIF, arrondies à 0,01° à l'envoi — le lieu exact n'est pas gardé.",
)
async def cloud_geo(request: Request, since: Optional[int] = Query(default=None), until: Optional[int] = Query(default=None),
                    npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)

    def work():
        udir = cloud_storage.ensure_ucloud_dir(email)
        ud = cloud_storage.ucloud_db
        points = ud.geo_points(udir, since, until)
        months = [{"m": m, "n": n} for m, n in ud.geo_months(udir)]
        return {"points": points, "months": months, "shown": sum(p["n"] for p in points),
                "total_geo": sum(x["n"] for x in months), "scan": _dedupe_progress(email)}
    return JSONResponse(await asyncio.to_thread(work))


@router.get(
    "/api/cloud/memories",
    summary="Souvenirs du jour : photos prises à la même date les années passées",
    description="Une ligne par année passée (nombre, couverture, intervalle `since`/`until` pour ouvrir la galerie). "
                "Le jour est celui de l'appelant (`month`/`day`/`year`, sinon la date du serveur).",
)
async def cloud_memories(request: Request, month: Optional[int] = Query(default=None, ge=1, le=12),
                         day: Optional[int] = Query(default=None, ge=1, le=31),
                         year: Optional[int] = Query(default=None, ge=1970, le=2200),
                         npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    import time as _time
    now = _time.localtime()
    m, d, y = month or now.tm_mon, day or now.tm_mday, year or now.tm_year

    def work():
        return {"memories": cloud_storage.ucloud_db.memories(cloud_storage.ensure_ucloud_dir(email), m, d, y)}
    return JSONResponse(await asyncio.to_thread(work))


@router.get(
    "/api/cloud/events",
    summary="Événements : photos regroupées par moment (écart de plus de 6 h = nouvel événement)",
    description="Du plus récent au plus ancien, avec intervalle `since`/`until`, nombre de photos, couverture et lieu "
                "le plus fréquent. L'interface ouvre la galerie sur l'intervalle et propose d'envoyer l'événement "
                "aux personnes reconnues dessus.",
)
async def cloud_events(request: Request, offset: int = Query(default=0, ge=0), limit: int = Query(default=40, ge=1, le=200),
                       min_photos: int = Query(default=6, ge=2, le=200), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)

    def work():
        evs, total = cloud_storage.ucloud_db.events(cloud_storage.ensure_ucloud_dir(email), min_photos=min_photos,
                                                    limit=limit, offset=offset)
        return {"events": evs, "total": total, "offset": offset}
    return JSONResponse(await asyncio.to_thread(work))


@router.post(
    "/api/cloud/have",
    summary="Quelles de ces empreintes SHA-256 sont déjà dans le cloud ?",
    description="Avant d'envoyer, l'interface calcule le SHA-256 de chaque photo et demande ce "
                "qui existe déjà : une photo déjà présente n'est pas renvoyée (ni stockée, ni "
                "analysée deux fois). `hashes` = tableau JSON (500 max).",
)
async def cloud_have(request: Request, hashes: str = Form(...), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    try:
        wanted = [h for h in json.loads(hashes) if isinstance(h, str)][:500]
    except ValueError:
        raise HTTPException(status_code=400, detail="hashes doit être un tableau JSON")
    found = await asyncio.to_thread(cloud_storage.db_present_hashes, email, wanted)
    return JSONResponse({"present": found})


def _dedupe_progress(email: str) -> dict:
    """Avancement du calcul d'empreintes (ucloud_dedupe.py), `running` recoupé avec le PID."""
    udir = cloud_storage.ucloud_dir(email)
    try:
        data = json.loads((udir / "dedupe_scan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if data.get("running"):
        try:
            os.kill(int(data.get("pid") or 0), 0)
        except (OSError, ValueError):
            data["running"] = False
    done, total = cloud_storage.ucloud_db.phash_count(udir)
    data["hashed"], data["images"] = done, total
    return data


def _best_first(files: list) -> list:
    """Meilleure copie d'abord : la sienne (pas une copie reçue), puis la plus lourde (la
    plus fidèle), puis la plus ancienne, puis le chemin le plus court."""
    return sorted(files, key=lambda f: (f["from_name"] is not None, -(f["size"] or 0),
                                         f["taken_at"] or f["mtime"] or 0, len(f["path"]), f["path"]))


def _dup_groups(email: str) -> dict:
    udir = cloud_storage.ensure_ucloud_dir(email)
    ud = cloud_storage.ucloud_db
    exact = [_best_first([_file_row(p, e) for p, e in g]) for g in ud.exact_duplicates(udir)]
    # Chaque groupe d'exacts est représenté par SA MEILLEURE copie dans la recherche de
    # quasi-doublons : une version réduite se rattache ainsi à cette copie (et non à
    # chacune), sans qu'une copie exacte soit comptée deux fois.
    extra_copies = {f["path"] for g in exact for f in g[1:]}
    near = [[_file_row(p, e) for p, e in g] for g in ud.near_duplicates(udir, extra_copies)]

    def pack(groups: list, kind: str) -> list:
        out = []
        for g in groups:
            g = _best_first(g)
            out.append({"kind": kind, "keep": g[0]["path"], "files": g,
                        "saved": sum(f["size"] or 0 for f in g[1:])})
        return out
    exact_g, near_g = pack(exact, "exact"), pack(near, "near")
    return {"exact": exact_g, "near": near_g,
            "saved_exact": sum(g["saved"] for g in exact_g), "saved_near": sum(g["saved"] for g in near_g),
            "scan": _dedupe_progress(email)}


@router.get(
    "/api/cloud/preview",
    summary="Aperçu d'une photo (≤ 2400 px) pour la visionneuse",
    description="Beaucoup plus léger que l'original (le GET /dav/ rend les 5 à 20 Mo d'origine) : "
                "JPEG redimensionné, déchiffré à la volée puis gardé dans le cache disque CHIFFRÉ "
                "des miniatures — la photo suivante s'ouvre instantanément. L'original reste "
                "disponible sur /dav/.",
)
async def cloud_preview(request: Request, path: str = Query(...), size: int = Query(default=1600, ge=400, le=2400),
                        npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    try:
        entry = await asyncio.to_thread(cloud_storage.db_entry, email, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="chemin invalide")
    if not entry or not entry.get("cid"):
        raise HTTPException(status_code=404, detail="fichier introuvable")
    if not (entry.get("mime") or "").startswith("image/"):
        raise HTTPException(status_code=415, detail="pas une image")
    jpeg = await asyncio.to_thread(cloud_storage.thumb_cache_get, email, entry["cid"], size)
    if jpeg is None:
        key_hex = await asyncio.to_thread(cloud_storage.get_key_hex, email, entry["cid"])
        if not key_hex:
            raise HTTPException(status_code=404, detail="clé de déchiffrement introuvable")
        try:
            payload = await asyncio.to_thread(cloud_storage.ipfs_cat, entry["cid"], cloud_storage.cipher_limit(entry))
            plaintext = cloud_storage.uenc_codec.decrypt_aes256gcm(payload, key_hex)
            jpeg = await asyncio.to_thread(_resized_jpeg, plaintext, size, 82)
        except Exception as exc:
            logger.warning("ucloud: aperçu %s impossible (%s)", path, exc)
            raise HTTPException(status_code=502, detail=f"aperçu impossible: {type(exc).__name__}")
        await asyncio.to_thread(cloud_storage.thumb_cache_put, email, entry["cid"], size, jpeg)
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get(
    "/api/cloud/duplicates",
    summary="Photos en double (identiques, et quasi identiques)",
    description="`exact` : même contenu (SHA-256), trouvé instantanément en SQL. `near` : même "
                "image redimensionnée / recompressée (empreinte perceptuelle dHash) — seulement "
                "pour les photos dont l'empreinte est calculée (`scan`). Chaque groupe propose "
                "`keep`, la meilleure copie ; rien n'est supprimé ici (POST /api/cloud/files/delete).",
)
async def cloud_duplicates(request: Request, summary: int = Query(default=0), npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    res = await asyncio.to_thread(_dup_groups, email)
    if summary:      # juste de quoi afficher le bouton « Doublons (n) » sans renvoyer les groupes
        res = {"exact": len(res["exact"]), "near": len(res["near"]),
               "photos": sum(len(g["files"]) - 1 for g in res["exact"] + res["near"]),
               "saved_exact": res["saved_exact"], "saved_near": res["saved_near"], "scan": res["scan"]}
    return JSONResponse(res)


@router.post(
    "/api/cloud/duplicates/scan",
    summary="Calculer les empreintes des photos existantes (quasi-doublons)",
)
async def cloud_duplicates_scan(request: Request, npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    if _dedupe_progress(email).get("running"):
        return JSONResponse({"success": False, "error": "Un calcul est déjà en cours"}, status_code=409)
    script = settings.BASE_DIR / "ucloud_dedupe.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="ucloud_dedupe.py introuvable")
    try:
        subprocess.Popen([sys.executable, str(script), "scan", email],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as exc:
        logger.warning("ucloud: scan doublons impossible pour %s: %s", email, exc)
        raise HTTPException(status_code=500, detail="Lancement impossible")
    return JSONResponse({"success": True})


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

    # Normalisation et garde-fous AVANT de toucher quoi que ce soit : "/" (ou "")
    # désigne la racine, dont remove_subtree() emporterait TOUT le cloud ; un
    # chemin avec ".." est une erreur de l'appelant (400), pas un 500.
    clean = []
    for p in requested:
        try:
            norm = cloud_storage.normalize_path(p)
        except ValueError:
            return JSONResponse({"error": f"Chemin invalide : {p!r}"}, status_code=400)
        if norm == "/":
            return JSONResponse({"error": "La racine du cloud ne peut pas être supprimée."}, status_code=400)
        clean.append(norm)

    # index_lock est BLOQUANT (poll + sleep) et unpin = jusqu'à 200 requêtes
    # IPFS synchrones : tout dans un thread, pas dans la boucle uvicorn.
    deleted, skipped_readonly = await asyncio.to_thread(cloud_storage.delete_paths, email, clean, True)

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
    try:
        entry = await asyncio.to_thread(cloud_storage.db_entry, email, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="chemin invalide")
    if not entry or not entry.get("cid"):
        raise HTTPException(status_code=404, detail="fichier introuvable")
    if not (entry.get("mime") or "").startswith("image/"):
        raise HTTPException(status_code=415, detail="pas une image")

    key_hex = await asyncio.to_thread(cloud_storage.get_key_hex, email, entry["cid"])
    if not key_hex:
        raise HTTPException(status_code=404, detail="clé de déchiffrement introuvable")

    try:
        # ipfs_cat() est synchrone (streaming httpx borné) : hors thread, elle
        # gèlerait la boucle uvicorn le temps du téléchargement.
        # Borne = taille réelle de l'entrée (un import peut dépasser 20 Mo).
        payload = await asyncio.to_thread(cloud_storage.ipfs_cat, entry["cid"],
                                          cloud_storage.cipher_limit(entry))
        plaintext = cloud_storage.uenc_codec.decrypt_aes256gcm(payload, key_hex)
    except Exception as exc:
        logger.error("ucloud: miniature — déchiffrement échoué pour %s: %s", path, exc)
        raise HTTPException(status_code=502, detail=f"déchiffrement impossible: {type(exc).__name__}")

    try:
        data_url = await asyncio.to_thread(_photo_thumb_data_url, plaintext, 300)
        buf = io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"miniature impossible: {type(exc).__name__}")

    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=1800"},
    )


_PHOTO_THUMB_CACHE: "OrderedDict[str, str]" = OrderedDict()   # RAM seulement
_PHOTO_THUMB_CACHE_MAX = 3000


def _resized_jpeg(plaintext: bytes, size: int, quality: int = 75) -> bytes:
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(plaintext))
    img.draft("RGB", (size * 2, size * 2))   # décodage JPEG réduit : bien plus rapide
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=size <= 400)
    return buf.getvalue()


def _photo_thumb_data_url(plaintext: bytes, size: int) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(_resized_jpeg(plaintext, size)).decode()


@router.post(
    "/api/cloud/thumbnails",
    summary="Miniatures de plusieurs images en un appel",
    description="`paths` = tableau JSON (40 max), `size` ≤ 400 → "
                "{thumbs: {path: data URL JPEG}}. Une seule signature NIP-98 "
                "pour toute une page de galerie ; cache mémoire uniquement "
                "(jamais de clair sur disque). Les chemins illisibles sont omis.",
)
async def cloud_thumbnails(
    request: Request,
    paths: str = Form(...),
    size: int = Form(default=240),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    try:
        wanted = [p for p in json.loads(paths) if isinstance(p, str)][:40]
    except ValueError:
        raise HTTPException(status_code=400, detail="paths doit être un tableau JSON")
    size = max(64, min(int(size or 240), 400))
    thumbs: Dict[str, str] = {}
    sem = asyncio.Semaphore(4)

    def lookup(path: str):
        """(entrée, clé) d'UN chemin — deux requêtes SQLite indexées (plus de
        chargement de l'index ni du keyring entiers)."""
        try:
            entry = cloud_storage.db_entry(email, path)
        except ValueError:
            return None, None
        if not entry or not entry.get("cid") or not str(entry.get("mime", "")).startswith("image/"):
            return None, None
        return entry, cloud_storage.get_key_hex(email, entry["cid"])

    async def one(path: str) -> None:
        entry, key_hex = await asyncio.to_thread(lookup, path)
        if not entry:
            return
        key = f"{entry['cid']}:{size}"
        if key in _PHOTO_THUMB_CACHE:
            _PHOTO_THUMB_CACHE.move_to_end(key)
            thumbs[path] = _PHOTO_THUMB_CACHE[key]
            return
        if not key_hex:
            return
        async with sem:
            try:
                # Cache DISQUE chiffré d'abord : instantané après un redémarrage.
                jpeg = await asyncio.to_thread(cloud_storage.thumb_cache_get, email, entry["cid"], size)
                if jpeg is not None:
                    data = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
                else:
                    payload = await asyncio.to_thread(cloud_storage.ipfs_cat, entry["cid"],
                                                      cloud_storage.cipher_limit(entry))
                    plaintext = cloud_storage.uenc_codec.decrypt_aes256gcm(payload, key_hex)
                    data = await asyncio.to_thread(_photo_thumb_data_url, plaintext, size)
                    await asyncio.to_thread(cloud_storage.thumb_cache_put, email, entry["cid"], size,
                                            base64.b64decode(data.split(",", 1)[1]))
            except Exception as exc:
                logger.warning("ucloud: miniature %s impossible (%s)", path, exc)
                return
        _PHOTO_THUMB_CACHE[key] = data
        thumbs[path] = data

    await asyncio.gather(*(one(p) for p in wanted))
    while len(_PHOTO_THUMB_CACHE) > _PHOTO_THUMB_CACHE_MAX:
        _PHOTO_THUMB_CACHE.popitem(last=False)
    return JSONResponse({"thumbs": thumbs})


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
    return JSONResponse({
        "sources": [_public_source(s) for s in sources],
        "refresh": analysis.refresh_info(cloud_storage.nostr_dir(email)),
    })


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
    username: str = Form(default=""),
    password: str = Form(default=""),
    path: str = Form(default=""),
    source_id: str = Form(default=""),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    if source_id and not password:
        # Modification d'une source existante : on réutilise son mot de passe
        # enregistré (jamais renvoyé au navigateur) plutôt que de le retaper.
        stored = cloud_storage.get_webdav_source(email, source_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Source introuvable")
        password = stored.get("password", "")
        username = username or stored.get("username", "")
    if not username or not password:
        return JSONResponse({"error": "Identifiant et mot de passe requis."}, status_code=400)
    try:
        await webdav_client.validate_remote_url(url)
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
        await webdav_client.validate_remote_url(url)
        await webdav_client.check_connection(url, username, password)
    except webdav_client.WebdavError as exc:
        return JSONResponse({"error": f"Connexion refusée : {exc}"}, status_code=400)

    entry = cloud_storage.add_webdav_source(
        email, label=label.strip() or url, url=url.strip(), username=username.strip(),
        password=password, remote_path=remote_path.strip("/"),
        dest_prefix=dest_prefix.strip(),     # normalisé/unique côté cloud_storage
    )
    return JSONResponse({"success": True, "source": _public_source(entry)})


@router.put(
    "/api/cloud/webdav-sources/{source_id}",
    summary="Modifier une source WebDAV externe",
    description="Libellé, adresse, identifiant, mot de passe (vide = inchangé) et "
                "dossier distant. Si la connexion change (adresse, identifiant, mot "
                "de passe, dossier), elle est revalidée AVANT d'enregistrer. Le "
                "dossier de destination et les photos déjà importées ne bougent pas.",
)
async def edit_webdav_source(
    request: Request,
    source_id: str,
    label: Optional[str] = Form(default=None),
    url: Optional[str] = Form(default=None),
    username: Optional[str] = Form(default=None),
    password: Optional[str] = Form(default=None),
    remote_path: Optional[str] = Form(default=None),
    npub: str = Depends(require_nostr_auth),
):
    email = _email_for_authenticated_npub(npub)
    current = cloud_storage.get_webdav_source(email, source_id)
    if not current:
        raise HTTPException(status_code=404, detail="Source introuvable")
    changes = {
        "label": label.strip() if label is not None else None,
        "url": url.strip() if url is not None else None,
        "username": username.strip() if username is not None else None,
        "password": password,
        "remote_path": remote_path.strip("/") if remote_path is not None else None,
    }
    if changes["label"] == "":
        changes["label"] = current.get("label")
    effective = {k: (changes.get(k) if changes.get(k) not in (None, "") else current.get(k))
                 for k in ("url", "username", "password", "remote_path")}
    if any(changes.get(k) not in (None, "") and changes[k] != current.get(k)
           for k in ("url", "username", "password", "remote_path")):
        try:
            await webdav_client.validate_remote_url(effective["url"])
            # Revalide la connexion ET l'existence du dossier choisi.
            await webdav_client.list_folder(effective["url"], effective["username"],
                                            effective["password"], effective["remote_path"] or "")
        except webdav_client.WebdavError as exc:
            return JSONResponse({"error": f"Connexion refusée : {exc}"}, status_code=400)
    updated = cloud_storage.update_webdav_source(email, source_id, changes)
    if not updated:
        raise HTTPException(status_code=404, detail="Source introuvable")
    return JSONResponse({"success": True, "source": _public_source(updated)})


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

    # Un import par source à la fois (cron + clics répétés lançaient N copies
    # parallèles qui importaient les mêmes fichiers). Le script reprend le même
    # verrou ; ici on ne fait que tester s'il est pris.
    probe = cloud_storage.try_lock_nowait(cloud_storage.webdav_import_lock_path(email, source_id))
    if probe is None:
        return JSONResponse({"success": False, "error": "Un import de cette source est déjà en cours"},
                            status_code=409)
    os.close(probe)

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
    local_count = await asyncio.to_thread(cloud_storage.ucloud_db.count_under,
                                          cloud_storage.ucloud_dir(email), dest_prefix)
    return JSONResponse({
        "remote_count": len(remote_files),
        "remote_capped": len(remote_files) >= webdav_client.WALK_MAX_FILES,
        "local_count": local_count,
    })
