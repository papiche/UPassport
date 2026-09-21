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

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from services.nostr import require_nostr_auth
from services import cloud_storage
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
