"""
routers/facecloud.py — Entrée dans FaceCloud : invitation et création du MULTIPASS.

Le parcours guidé (UPlanet/earth/ucloud.html#demarrer, page d'entrée
GAFAM.html) commence ici :

  GET  /api/facecloud/invitation/{token}   PUBLIC — ce qu'une invitation
       « Envoyer ses photos » contient : expéditeur, nombre de photos,
       4 aperçus recadrés, email masqué. Le jeton (reçu par email, 24 octets
       aléatoires) prouve la possession de l'adresse.
  POST /api/facecloud/start                crée le MULTIPASS et renvoie de quoi
       se connecter tout de suite (nsec, gardé EN MÉMOIRE par la page) :
         - avec `claim` : pour l'email de l'invitation (pas de saisie), puis
           copie ses photos dans son cloud AVANT de répondre — elles sont là
           quand la personne arrive ; si le MULTIPASS existe déjà, les photos
           sont copiées et la personne se connecte avec son code PASS ;
         - sans `claim` : exactement /g1/onboard (email + position, secrets
           générés par la station).
       Position : celle du navigateur si fournie, sinon celle de la station
       (~/.zen/GPS) — la création d'un MULTIPASS exige une case UMAP.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from core.config import settings
from services import analysis, facecloud_funnel, nostr_profiles
from services.admin_auth import require_captain_signature
from services.nostr import fetch_nostr_profiles, get_followers, get_n1_follows, require_nostr_auth
from utils.crypto import npub_to_hex
from utils.observability import log_node_event, log_user_event
from utils.security import is_safe_email

logger = logging.getLogger(__name__)
router = APIRouter()

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _find_invite(token: str) -> Optional[tuple]:
    """(chemin, données) de l'invitation `token`, si elle existe et n'a pas expiré."""
    if not _TOKEN_RE.match(token or ""):
        return None
    for path in (settings.GAME_PATH / "nostr").glob(f"*/.face_invites/{token}.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if int(data.get("expires_at", 0)) < time.time():
            return None
        return path, data
    return None


def _local_account_name(email: str) -> str:
    """Nom RÉEL du dossier MULTIPASS hébergé ici pour `email` (casse ignorée), '' s'il n'y en a pas."""
    root = settings.GAME_PATH / "nostr"
    if (root / email / "HEX").exists():
        return email
    try:
        for d in root.iterdir():
            if d.name.lower() == email and (d / "HEX").exists():
                return d.name
    except OSError:
        pass
    return ""


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return (local[:1] + "•" * max(1, min(6, len(local) - 1))) + "@" + domain


def _station_gps() -> tuple:
    try:
        raw = (Path.home() / ".zen" / "GPS").read_text()
        lat = re.search(r"LAT=([-0-9.]+)", raw).group(1)
        lon = re.search(r"LON=([-0-9.]+)", raw).group(1)
        return lat, lon
    except Exception:
        return "0.00", "0.00"


def _coords(lat: str, lon: str) -> tuple:
    try:
        return f"{round(float(lat), 2):.2f}", f"{round(float(lon), 2):.2f}"
    except (TypeError, ValueError):
        return _station_gps()


async def _load_invite(token: str) -> Optional[dict]:
    """L'invitation `token`, où qu'elle ait été créée : fichier local (cette station) puis relais
    commun (autre station). `{"local": (chemin, données) | None, "record": dict | None}`, ou None si
    absente, expirée ou déjà réclamée (usage unique, quelle que soit la station qui l'a honorée)."""
    from services import face_invites
    found = _find_invite(token)
    if found:
        if await asyncio.to_thread(face_invites.is_claimed, token):
            found[0].unlink(missing_ok=True)
            return None
        return {"local": found, "record": None}
    record = await asyncio.to_thread(face_invites.fetch, token)
    return {"local": None, "record": record} if record else None


def _relay_previews(items: list) -> list:
    """Aperçus recadrés des 4 premiers visages d'une invitation du relais : le bloc est lu sur IPFS
    et déchiffré avec la clé portée par l'invitation (aucune dépendance au cloud de l'expéditeur)."""
    from routers import mailjet
    from services import cloud_storage
    previews = []
    for it in [i for i in items if i.get("bbox")][:4]:
        try:
            plaintext = cloud_storage.uenc_codec.decrypt_aes256gcm(cloud_storage.ipfs_cat(it["cid"]), it["key"])
            crops = mailjet._crop_faces(plaintext, [("x", it["bbox"])], 200)
            if "x" in crops:
                previews.append("data:image/jpeg;base64," + base64.b64encode(crops["x"]).decode())
        except Exception:
            continue
    return previews


async def _invite_payload(loaded: dict) -> dict:
    """Ce qu'une invitation montre à son destinataire (jamais d'email en clair ni de clé de photo)."""
    if loaded["record"]:
        rec = loaded["record"]
        to_email = rec.get("to_email") or ""
        return {
            "from_name": rec.get("from_name") or (rec.get("owner_email") or "").split("@", 1)[0],
            "photos": len(rec["items"]),
            "previews": await asyncio.to_thread(_relay_previews, rec["items"]),
            "email": _mask(to_email) if to_email else "",
            "open": bool(rec.get("open")),
            "expires_at": rec.get("expires_at"),
            "has_account": bool(to_email) and (settings.GAME_PATH / "nostr" / to_email / "HEX").exists(),
        }
    _path, data = loaded["local"]
    owner = data.get("owner_email") or ""
    photos = data.get("photos") or []
    previews = []
    from routers import mailjet
    for ref in photos[:4]:
        try:
            plaintext, _mime = await mailjet._decrypt_source_photo(owner, ref["source_path"])
            crops = mailjet._crop_faces(plaintext, [("x", ref.get("bbox") or {})], 200)
            if "x" in crops:
                previews.append("data:image/jpeg;base64," + base64.b64encode(crops["x"]).decode())
        except Exception:
            continue
    to_email = data.get("to_email") or ""
    return {
        "from_name": owner.split("@", 1)[0],
        "photos": len(photos),
        "previews": previews,
        "email": _mask(to_email) if to_email else "",
        "open": bool(data.get("open")),       # invitation par lien : l'invité saisit SON email
        "expires_at": data.get("expires_at"),
        "has_account": bool(to_email) and (settings.GAME_PATH / "nostr" / to_email / "HEX").exists(),
    }


def _invite_meta(loaded: dict) -> dict:
    """Expéditeur, mode et génération de l'invitation, pour l'entonnoir."""
    if loaded["record"]:
        rec = loaded["record"]
        return {"owner": rec.get("owner_email") or "", "gen": int(rec.get("gen") or 0),
                "mode": "link" if rec.get("open") else "email"}
    data = loaded["local"][1]
    owner = data.get("owner_email") or ""
    return {"owner": owner, "gen": int(data.get("gen") if data.get("gen") is not None else facecloud_funnel.generation_of(owner)),
            "mode": "link" if data.get("open") else "email"}


@router.get("/api/facecloud/invitation/{token}")
async def invitation(token: str):
    loaded = await _load_invite(token)
    if not loaded:
        return JSONResponse({"error": "Cette invitation n'existe pas ou a expiré."}, status_code=404)
    meta = _invite_meta(loaded)
    facecloud_funnel.record("opened", token, gen=meta["gen"], mode=meta["mode"])
    return JSONResponse(await _invite_payload(loaded))


# ═════════════════════════════════════════════════════════════════════════════
# /i/<jeton> — la page que voient les robots de WhatsApp, Telegram, Signal… (aperçu du lien)
# ═════════════════════════════════════════════════════════════════════════════
# Le lien partagé n'était qu'une page statique qui redirigeait en JavaScript : les robots n'exécutent pas
# de JavaScript et ne voient pas le fragment (#), donc le destinataire recevait un lien nu. Ici le serveur
# répond avec les balises Open Graph de CETTE invitation ; l'humain est renvoyé aussitôt vers l'accueil.
# L'image est une MOSAÏQUE FLOUTÉE (jamais un visage lisible) : les serveurs de messagerie la gardent.
# Même jeton, mêmes droits qu'avant : lire cette page ne réclame rien (usage unique à la création du compte).
_OG_W, _OG_H = 1200, 630
_og_cache: dict = {}
_OG_TTL = 3600.0
_OG_CACHE_MAX = 64


def _og_image(previews: list) -> bytes:
    """Mosaïque 1200×630 : jusqu'à 4 recadrages réduits à 10 px puis flous — rien de reconnaissable."""
    import io
    from PIL import Image, ImageDraw, ImageFilter
    canvas = Image.new("RGB", (_OG_W, _OG_H), (246, 243, 236))
    draw = ImageDraw.Draw(canvas)
    tiles = []
    for url in previews[:4]:
        try:
            raw = base64.b64decode(url.split(",", 1)[1])
            tiles.append(Image.open(io.BytesIO(raw)).convert("RGB"))
        except Exception:
            continue
    palette = [(220, 193, 150), (31, 95, 209), (43, 138, 87), (196, 160, 106)]
    size, gap = 250, 40
    n = 4
    x0 = (_OG_W - (n * size + (n - 1) * gap)) // 2
    y0 = (_OG_H - size) // 2
    for i in range(n):
        if i < len(tiles):
            t = tiles[i].resize((size, size))
            t = t.resize((8, 8), Image.BOX).resize((size, size), Image.BICUBIC).filter(ImageFilter.GaussianBlur(14))
        else:                                       # pas assez d'aperçus : des pastilles de la couleur du site
            t = Image.new("RGB", (size, size), palette[i % len(palette)]).filter(ImageFilter.GaussianBlur(10))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), 28, fill=255)
        x = x0 + i * (size + gap)
        y = y0 + (14 if i % 2 else -14)
        draw.rounded_rectangle((x - 10, y - 10, x + size + 10, y + size + 34), 22, fill=(255, 255, 255))
        canvas.paste(t, (x, y), mask)
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=82)
    return out.getvalue()


async def _og_for(token: str) -> bytes:
    key = facecloud_funnel.invite_ref(token)
    hit = _og_cache.get(key)
    if hit and time.time() - hit[0] < _OG_TTL:
        return hit[1]
    loaded = await _load_invite(token)
    previews = (await _invite_payload(loaded))["previews"] if loaded else []
    data = await asyncio.to_thread(_og_image, previews)
    if len(_og_cache) >= _OG_CACHE_MAX:
        for k in sorted(_og_cache, key=lambda k: _og_cache[k][0])[:16]:
            _og_cache.pop(k, None)
    _og_cache[key] = (time.time(), data)
    return data


def _public_base(request: Request) -> str:
    return str(settings.uSPOT).rstrip("/")


@router.get("/i/{token}", response_class=HTMLResponse, include_in_schema=False)
async def invitation_page(token: str, request: Request):
    import html as _html
    base = _public_base(request)
    ua = request.headers.get("user-agent", "")
    title = "Des photos de vous vous attendent"
    desc = "Un proche vous a reconnu·e sur des photos et vous les offre — chiffrées, gratuites, rien à installer."
    # Jeton mal formé : image générique (pastilles), jamais d'erreur visible dans un aperçu.
    image = f"{base}/i/{token if _TOKEN_RE.match(token or '') else 'x' * 24}/og.jpg"
    loaded = await _load_invite(token) if _TOKEN_RE.match(token or "") else None
    if loaded:
        meta = _invite_meta(loaded)
        n = len(loaded["record"]["items"]) if loaded["record"] else len(loaded["local"][1].get("photos") or [])
        who = (loaded["record"].get("from_name") if loaded["record"] else "") or meta["owner"].split("@", 1)[0] or "Un proche"
        title = f"{who} vous a reconnu·e sur {n} photo{'s' if n > 1 else ''}"
        facecloud_funnel.record("preview" if facecloud_funnel.is_bot(ua) else "visit", token, gen=meta["gen"], mode=meta["mode"])
    target = f"{base}/earth/ucloud.html#demarrer&claim={token}"
    esc = _html.escape

    def meta_tag(key: str, name: str, value: str) -> str:
        return '<meta %s="%s" content="%s">' % (key, name, esc(str(value)))

    page = "".join([
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>%s</title>" % esc(title),
        meta_tag("name", "robots", "noindex,nofollow"),
        meta_tag("name", "description", desc),
        meta_tag("property", "og:type", "website"),
        meta_tag("property", "og:site_name", "FaceCloud"),
        meta_tag("property", "og:title", title),
        meta_tag("property", "og:description", desc),
        meta_tag("property", "og:image", image),
        meta_tag("property", "og:image:width", _OG_W),
        meta_tag("property", "og:image:height", _OG_H),
        meta_tag("name", "twitter:card", "summary_large_image"),
        meta_tag("name", "twitter:title", title),
        meta_tag("name", "twitter:image", image),
        "<style>body{font:18px/1.5 system-ui,sans-serif;background:#f6f3ec;color:#1d2a32;display:grid;"
        "place-items:center;min-height:100vh;margin:0;text-align:center;padding:24px}"
        "a{color:#1f5fd1;font-weight:700}</style>",
        "<script>location.replace(%s);</script>" % json.dumps(target).replace("</", "<\\/"),
        "</head><body><main>",
        "<h1>%s</h1>" % esc(title),
        '<p><a href="%s">Voir mes photos</a></p>' % esc(target),
        "</main></body></html>"])
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
                                       "Referrer-Policy": "no-referrer"})


@router.get("/i/{token}/og.jpg", include_in_schema=False)
async def invitation_og_image(token: str):
    if not _TOKEN_RE.match(token or ""):
        return Response(status_code=404)
    return Response(await _og_for(token), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=600", "X-Robots-Tag": "noindex"})


@router.get("/api/facecloud/funnel")
async def funnel(days: int = Query(30, ge=1, le=365), _captain: str = Depends(require_captain_signature)):
    """Entonnoir viral des invitations FaceCloud sur cette station (NIP-98 Capitaine)."""
    return JSONResponse(await asyncio.to_thread(facecloud_funnel.stats, days))


@router.post("/api/facecloud/start")
async def start(
    request: Request,
    email: str = Form(default=""),
    claim: str = Form(default=""),
    lang: str = Form(default="fr"),
    lat: str = Form(default=""),
    lon: str = Form(default=""),
):
    invite = None
    if claim:
        invite = await _load_invite(claim)
        if not invite:
            return JSONResponse({"error": "Cette invitation n'existe pas ou a expiré."}, status_code=404)
        # Invitation par email : l'adresse est celle de l'invitation. Par lien (« open ») : celle que saisit l'invité.
        inv_to = (invite["local"][1] if invite["local"] else invite["record"]).get("to_email")
        if inv_to:
            email = inv_to
    email = (email or "").strip().lower()
    if not is_safe_email(email):
        raise HTTPException(status_code=400, detail="Adresse email invalide.")
    lat, lon = _coords(lat, lon) if (lat and lon) else _station_gps()
    imeta = _invite_meta(invite) if invite else None
    if imeta:
        facecloud_funnel.record("started", claim, gen=imeta["gen"], mode=imeta["mode"])

    from routers.identity import G1NostrForm, _scan_qr_impl
    from routers.mailjet import process_pending_face_claims

    # Insensible à la casse : un ancien MULTIPASS créé « Prenom@Exemple.org » (le dossier garde la casse
    # soumise, cf. _scan_qr_impl) est le même compte que « prenom@exemple.org ».
    real = _local_account_name(email)
    exists = bool(real)
    if exists:
        email = real
        # Compte déjà là : les photos de l'invitation sont copiées tout de
        # suite ; la connexion se fait avec le code PASS (jamais de clé
        # renvoyée pour un compte existant sans preuve).
        if invite:
            await process_pending_face_claims(email, claim)
            facecloud_funnel.record("delivered", claim, gen=imeta["gen"], mode=imeta["mode"], sender=imeta["owner"])
        return JSONResponse({"status": "exists", "email": email})

    # Jamais de doublon : « cet email a-t-il un MULTIPASS ? » se demande au relais commun, pas aux
    # fichiers de CETTE station — le compte peut être hébergé n'importe où dans la constellation.
    from services import face_invites
    try:
        account = await asyncio.to_thread(face_invites.account_for_email, email)
    except face_invites.RelayUnavailable:
        return JSONResponse({"error": "Le relais NOSTR ne répond pas : impossible de vérifier que cette adresse "
                                      "n'a pas déjà un MULTIPASS. Réessayez dans un instant."}, status_code=503)
    if not account:
        # Filet : les profils kind 0 se filtrent à l'identique (« email:x » exact) ; un compte créé avec une
        # autre casse n'y serait pas trouvé. L'annuaire (comptes locaux + swarm, clés en minuscules) l'est.
        from services import identity_links
        known = identity_links.multipass_directory().get(email)
        account = {"hex": known["hex"]} if known else None
    if account:
        delivered = await _deliver_elsewhere(claim, invite, email, account["hex"])
        logger.info("FaceCloud : %s a déjà un MULTIPASS ailleurs (%s…) — pas de création, %d photo(s) transmises",
                    email, account["hex"][:8], delivered)
        log_user_event(email, "multipass", "facecloud_start", False, extra={"status": "elsewhere", "delivered": delivered})
        if imeta and delivered:
            facecloud_funnel.record("delivered", claim, gen=imeta["gen"], mode=imeta["mode"], sender=imeta["owner"])
        return JSONResponse({"status": "elsewhere", "email": email, "delivered": delivered})

    result = await _scan_qr_impl(request, G1NostrForm(email=email, lang=lang or "fr", lat=lat, lon=lon,
                                                      format="json"))
    if getattr(result, "status_code", 200) != 200:
        return result
    data = json.loads(bytes(result.body))
    copied = 0
    if invite:
        before = _received_count(email)
        await process_pending_face_claims(email, claim)
        copied = _received_count(email) - before
    if imeta:
        gen = facecloud_funnel.remember_origin(email, imeta["gen"], imeta["owner"].split("@", 1)[0])
        facecloud_funnel.record("claimed", claim, gen=gen, mode=imeta["mode"], sender=imeta["owner"], account=email)
    logger.info("FaceCloud : MULTIPASS créé pour %s (invitation=%s, %d photo(s) reçues)", email, bool(invite), copied)
    log_node_event("facecloud_start", True, category="multipass", extra={"invitation": bool(invite), "photos": copied})
    log_user_event(email, "multipass", "facecloud_start", True, extra={"invitation": bool(invite), "photos": copied})
    return JSONResponse({"status": "created", "email": email, "nsec": data.get("nsec", ""),
                         "npub": data.get("npub", ""), "photos": copied})


async def _deliver_elsewhere(claim: str, invite: Optional[dict], email: str, hex_pubkey: str) -> int:
    """Le MULTIPASS `email` existe sur une AUTRE station : au lieu d'en créer un second, les photos de
    l'invitation lui sont envoyées de station à station (canal inter-NODE, même bloc, même clé).
    Rend le nombre de photos transmises ; 0 = station inconnue ou envoi refusé — l'invitation reste
    alors valable (le destinataire peut ouvrir le lien depuis sa propre station)."""
    if not invite:
        return 0
    from services import cloud_storage, face_invites, identity_links, node_share
    info = identity_links.email_for_hex(hex_pubkey)
    if not info or not info.get("node") or not node_share.node_hex_for(info["node"]):
        return 0
    if invite["record"]:
        rec = invite["record"]
        owner, items = rec.get("owner_email") or "", rec["items"]
        from_hex, from_name = rec.get("from_hex") or "", rec.get("from_name") or ""
    else:
        data = invite["local"][1]
        owner = data.get("owner_email") or ""
        items = await asyncio.to_thread(face_invites.items_from_local, owner, data.get("photos") or [])
        from_hex, from_name = cloud_storage.hex_for_email(owner) or "", owner.split("@", 1)[0]
    items = [{k: it[k] for k in ("cid", "key", "sha", "name", "mime")} for it in items]
    ref = (invite["record"] or {}).get("face_ref") if invite["record"] else None
    if invite["local"]:
        from services import face_ref
        ref = await asyncio.to_thread(face_ref.payload_for, owner, hex_pubkey)
    res = await asyncio.to_thread(node_share.send_items, owner, from_hex, from_name,
                                  {"email": email, "hex": hex_pubkey, "node": info["node"]}, items, ref)
    if res.get("status") != "sent":
        return 0
    if invite["local"]:
        invite["local"][0].unlink(missing_ok=True)
    await asyncio.to_thread(face_invites.mark_claimed, claim, owner)
    return int(res.get("photos", 0))


def _received_count(email: str) -> int:
    from services import cloud_storage
    try:
        return cloud_storage.ucloud_db.stats(cloud_storage.ucloud_dir(email))["received"]
    except Exception:
        return 0


# ═════════════════════════════════════════════════════════════════════════════
# Liens NOSTR — les listes de suivis (kind 3) FONT FOI pour les relations
# ═════════════════════════════════════════════════════════════════════════════
# Qui suit qui. Plus de « cercles » placés à la main (l'onglet a été retiré le 2026-10-19 : il apportait de la confusion) :
# on lit seulement ce que NOSTR dit déjà. Borné par MES liens (jamais le réseau entier : 100 amis × 100 amis = 10 000
# personnes) :
#   * `/api/facecloud/links`   ceux que je suis ET ceux qui me suivent, avec la nature du lien.
# Nature du lien (`rel`) : « mutual » = ils se suivent (p2p) ; « out » = je le suis sans réciproque (1→p) ;
# « in » = il me suit sans réciproque (p→1).
_LINKS_TTL = 600.0
_LINKS_MAX = 600          # plafond des liens renvoyés (profils inclus)
_links_cache: dict = {}
_links_locks: dict = {}


def _profile_fields(prof: dict, pubkey: str = "") -> dict:
    """Seulement ce qui sert à reconnaître quelqu'un — jamais l'URL d'avatar (la page ne charge aucune image
    extérieure : la station la sert elle-même, cf. /api/facecloud/avatars) ni la bio. Complète avec les profils déjà
    lus sur les relais préférés (cache disque, sans réseau)."""
    prof = dict(prof or {})
    cached = nostr_profiles.cached_profile(pubkey) if pubkey else None
    for k in ("name", "display_name", "email"):
        if not prof.get(k) and cached and cached.get(k):
            prof[k] = cached[k]
    if not prof.get("npub") and pubkey:
        from utils.crypto import hex_to_npub
        prof["npub"] = hex_to_npub(pubkey) or ""
    return {"name": prof.get("name") or "", "display_name": prof.get("display_name") or "",
            "email": prof.get("email") or "", "npub": prof.get("npub") or "", "has_pic": bool(cached and cached.get("picture")),
            "avatar": bool(pubkey and nostr_profiles._avatar_file(pubkey).exists())}


async def _cached(key: str, loader):
    """Résultat mémoïsé 10 min ; un seul calcul à la fois par clé (pas de rafale
    de `nostr_get_N1.sh` quand l'interface se rafraîchit)."""
    now = time.time()
    hit = _links_cache.get(key)
    if hit and now - hit[0] < _LINKS_TTL:
        return hit[1]
    lock = _links_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _links_cache.get(key)
        if hit and time.time() - hit[0] < _LINKS_TTL:
            return hit[1]
        value = await loader()
        _links_cache[key] = (time.time(), value)
        if len(_links_cache) > 400:                      # mémoire bornée
            for k in sorted(_links_cache, key=lambda k: _links_cache[k][0])[:100]:
                _links_cache.pop(k, None)
        return value


def _hex_of(npub: str) -> str:
    h = (npub or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        h = npub_to_hex(h) or ""
    if not h:
        raise HTTPException(status_code=400, detail="clé invalide")
    return h


def _relation(pubkey: str, follows: set, followers: set) -> str:
    if pubkey in follows and pubkey in followers:
        return "mutual"
    return "out" if pubkey in follows else "in"


async def _my_graph(me: str) -> tuple:
    async def load():
        follows, followers = await asyncio.gather(get_n1_follows(me), get_followers(me))
        return set(follows) - {me}, set(followers) - {me}
    return await _cached(f"graph:{me}", load)


@router.get("/api/facecloud/links", summary="Mes liens NOSTR (kind 3) : qui je suis, qui me suit, et la nature du lien (p2p, 1→p, p→1)")
async def links(request: Request, refresh: int = Query(default=0), npub: str = Depends(require_nostr_auth)):
    me = _hex_of(npub)
    if refresh:
        _links_cache.pop(f"graph:{me}", None)
    follows, followers = await _my_graph(me)
    keys = sorted(follows | followers)
    truncated = len(keys) > _LINKS_MAX
    # Les liens mutuels d'abord : si l'on tronque, on garde les plus proches.
    keys.sort(key=lambda k: (_relation(k, follows, followers) != "mutual", k))
    keys = keys[:_LINKS_MAX]
    profiles = await fetch_nostr_profiles(keys)
    links = [{"pubkey": k, "rel": _relation(k, follows, followers), **_profile_fields(profiles.get(k, {}), k)}
             for k in keys]
    return JSONResponse({
        "me": me, "truncated": truncated, "links": links,
        "counts": {"out": len(follows - followers), "in": len(followers - follows),
                   "mutual": len(follows & followers)},
    })


# ═════════════════════════════════════════════════════════════════════════════
# Mode d'analyse (choix du capitaine)
# ═════════════════════════════════════════════════════════════════════════════
@router.post("/api/facecloud/analysis/mode",
             summary="Capitaine : choisir par quel chemin tournent les analyses (auto | direct | relay)")
async def set_analysis_mode(request: Request, mode: str = Form(...)):
    """auto : accès direct si la machine d'IA répond, sinon relais NOSTR ; direct : ComfyUI/Ollama de cette
    station seulement (pas de repli) ; relay : messages chiffrés NIP-44 vers un Brain. NIP-98 du CAPITAINE
    exclusivement (la clé coopérative partagée n'est pas une preuve d'autorité suffisante ici)."""
    await require_captain_signature(request)
    try:
        analysis.set_mode(mode)
    except ValueError:
        raise HTTPException(status_code=400, detail="mode inconnu (auto|direct|relay)")
    logger.info("FaceCloud : mode d'analyse → %s (capitaine)", mode)
    return JSONResponse({"success": True, "mode": mode,
                         "faces": analysis.resolve("faces", mode), "describe": analysis.resolve("describe", mode)})


# ═════════════════════════════════════════════════════════════════════════════
# Profils lus sur les relais préférés + vignettes servies par la station
# ═════════════════════════════════════════════════════════════════════════════
def _hex_list(raw: str, limit: int) -> list:
    try:
        items = [h.lower() for h in json.loads(raw) if isinstance(h, str)]
    except ValueError:
        raise HTTPException(status_code=400, detail="hexes doit être un tableau JSON")
    return [h for h in dict.fromkeys(items) if re.fullmatch(r"[0-9a-f]{64}", h)][:limit]


@router.post("/api/facecloud/profiles",
             summary="Lire les profils (kind 0) manquants sur les relais préférés de l'utilisateur")
async def read_network_profiles(request: Request, hexes: str = Form(...), refresh: str = Form(default="0"),
                                npub: str = Depends(require_nostr_auth)):
    """Relais interrogés, dans l'ordre : celui de la station, ceux que l'utilisateur déclare (kind 10002) puis ceux de
    la constellation ; le profil le plus RÉCENT gagne. Mis en cache 24 h (6 h si introuvable). Ne rend ni URL
    d'avatar ni bio : les vignettes passent par /api/facecloud/avatars."""
    me = _hex_of(npub)
    res = await nostr_profiles.read_profiles(_hex_list(hexes, 100), me, force=refresh in ("1", "true"))
    return JSONResponse({
        "profiles": {k: {"name": p.get("name", ""), "display_name": p.get("display_name", ""), "email": p.get("email", ""),
                         "nip05": p.get("nip05", ""), "has_pic": bool(p.get("picture"))} for k, p in res["profiles"].items()},
        "missing": res["missing"], "relays": res["relays"], "cached": res["cached"]})


@router.post("/api/facecloud/avatars", summary="Vignettes (96 px) des profils NOSTR, servies par la station")
async def network_avatars(request: Request, hexes: str = Form(...), npub: str = Depends(require_nostr_auth)):
    """La station télécharge l'image du profil UNE fois (adresse contrôlée, 2 Mo max), la réduit et la garde :
    le navigateur ne contacte jamais l'hébergeur d'origine. Clés sans image : absentes de la réponse."""
    import base64
    keys = _hex_list(hexes, 40)
    sem = asyncio.Semaphore(6)

    async def one(k: str):
        async with sem:
            return k, await nostr_profiles.avatar_jpeg(k)
    out = {}
    for k, jpeg in await asyncio.gather(*(one(k) for k in keys)):
        if jpeg:
            out[k] = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    return JSONResponse({"avatars": out})
