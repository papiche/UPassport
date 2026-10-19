"""
routers/account.py — « Mon compte » du MULTIPASS : un seul point d'entrée API,
authentifié par NIP-98 (preuve de possession de la clé NOSTR du membre).

Remplace, pour l'essentiel, les actions de /mailjet qui reposaient sur un
couple email + jeton statique placé dans les URL et les e-mails. Les helpers de
stockage sont ceux de routers/mailjet.py (même fichier `.mailjet`) : les deux
écrans restent cohérents pendant la transition.

- GET  /api/account/overview     état complet : offre, quota, solde (g1pub), sécurité, notifications, mémoire
- POST /api/account/prefs        canaux e-mail/NOSTR par flux + préférences KIN
- POST /api/account/pass         bloque / réactive la récupération de compte par email+PASS
- POST /api/account/pass/reveal  affiche le code PASS (POST, jamais mis en cache)
- GET  /api/account/links        services liés : cookies de session + scrapers BRO + clés d'API officielles
- POST /api/account/scraper(+/config, /channels)  activer / configurer un scraper BRO
- GET/POST /api/account/lifeos   profil LifeOS (5 fichiers identity/*.md) ; POST …/regenerate : depuis Mastodon
- POST /api/account/memory/reset réinitialiser un périmètre de mémoire (conversations, BRO, persona, LOVE…)
- GET  /api/account/memory/items            « Ce que BRO sait de moi » : souvenirs actifs (filtres kind/source/visibility/q)
- POST /api/account/memory/items            ajouter un souvenir à la main ; …/update (texte, type, visibilité) ; …/delete
- GET/POST /api/account/memory/sources      sources d'apprentissage cochées (consentement) ; …/forget : tout oublier d'une source
- POST /api/account/memory/learn-now        lance l'apprentissage des sources cochées (process détaché)
Dépôt / suppression de cookies : POST /api/fileupload et DELETE /cookie/{domain} (déjà NIP-98) ;
clés d'API : /api_access/{provider} (déjà NIP-98) — l'écran les appelle directement.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from core.config import settings
from routers import mailjet as _mj
from routers.cloud import _email_for_authenticated_npub
from routers.identity import _is_pass_restore_disabled, _pass_disabled_flag, _resolve_pass_file
from services import api_access_store, cloud_storage, cookie_store
from services.admin_auth import get_node_and_captain_hex
from services.memory_status import (
    IDENTITY_FILENAMES, IDENTITY_LABELS, IDENTITY_PLACEHOLDERS, RESET_SCOPES, get_identity_content,
    MEMORY_KINDS, MEMORY_VISIBILITIES, add_memory_item, delete_memory_items, forget_memory_source,
    get_memory_status, learn_now, list_memory_items, memory_sources, regenerate_lifeos_from_mastodon,
    reset_memory, save_identity_file, set_memory_source, update_memory_item,
)
from services.nostr import require_nostr_auth
from utils.security import safe_json_body

router = APIRouter()
logger = logging.getLogger(__name__)

# Flux d'information proposés au membre : clé stockée → (titre, description).
# `alerts` et `usociety` portent des messages utiles (solde bas, stockage, renouvellement) :
# l'interface avertit avant de les couper.
ACCOUNT_FLUX = {
    "alerts":     ("Alertes du compte", "Solde bas, paiement échoué, sécurité"),
    "usociety":   ("Parrainage et stockage", "Renouvellement, dépassement d'espace, rappels"),
    "milestones": ("Étapes", "100 Ẑ atteints, anniversaires"),
    "zine":       ("Lettre de la coopérative", "Nouvelles et invitations"),
    "kin_daily":  ("KIN du jour", "Synchronicité quotidienne"),
    "kin_weekly": ("KIN de la semaine", "Bilan hebdomadaire"),
    "n2_journal": ("Journal du réseau", "Publication mensuelle / annuelle du réseau N²"),
}
# `flux` (bool d'activation globale) n'existe que pour ces quatre catégories dans le stockage historique
_FLUX_SWITCH = ("zine", "usociety", "alerts", "milestones")


def _g1pub(email: str) -> str:
    try:
        return (settings.GAME_PATH / "nostr" / email / "G1PUBNOSTR").read_text().strip()
    except Exception:
        return ""


def _overview(email: str) -> dict:
    udir = settings.GAME_PATH / "nostr" / email
    cur = _mj._read_optout(email)
    kin = cur.get("kin", {})
    flux = cur.get("flux", {})
    saved = cur.get("flux_channels", {})
    channels = {}
    for k, d in _mj._FLUX_CHANNEL_DEFAULTS.items():
        c = {**d, **saved.get(k, {})}
        channels[k] = {"email": bool(c.get("email")), "nostr": bool(c.get("nostr")),
                       "enabled": bool(flux.get(k, True)) if k in _FLUX_SWITCH else True}
    mem = {}
    try:
        s = get_memory_status(email)
        mem = {
            "conversations": s["conversations"]["message_count"], "bro_memory": s["bro_memory"]["message_count"],
            "persona": s["persona"]["message_count"], "love": s["love"]["memories_count"],
            "qdrant_points": sum(s[k].get("qdrant_points", 0) or 0 for k in ("conversations", "bro_memory", "persona", "love")),
            "memories": (s.get("memories") or {}).get("total"),     # souvenirs actifs (schéma v2)
        }
    except Exception as e:
        mem = {"error": str(e)[:120]}
    quota = {}
    try:
        quota = cloud_storage.storage_quota.status(cloud_storage.nostr_dir(email))
    except Exception as e:
        quota = {"error": str(e)[:120]}
    pass_file = _resolve_pass_file(email)
    _, captain_hex = get_node_and_captain_hex()
    hex_pub = ""
    try:
        hex_pub = (udir / "HEX").read_text().strip()
    except Exception:
        pass
    return {
        "email": email, "hex": hex_pub, "g1pub": _g1pub(email),
        "is_captain": bool(captain_hex) and hex_pub.lower() == captain_hex.lower(),
        "zencard": (settings.GAME_PATH / "players" / email / "secret.dunikey").is_file(),
        "quota": quota,
        "security": {"pass_active": pass_file is not None, "restore_disabled": _is_pass_restore_disabled(email)},
        "notifications": {
            "email_channel": bool(cur.get("email_channel", False)), "nostr_channel": bool(cur.get("nostr_channel", False)),
            "flux": channels,
            "flux_meta": {k: {"title": v[0], "desc": v[1]} for k, v in ACCOUNT_FLUX.items()},
            "kin": {"daily": kin.get("daily", True), "weekly": kin.get("weekly", True),
                    "scope": kin.get("scope", "relay"), "types": kin.get("types", _mj._KIN_ALL_TYPES)},
            "kin_types": {t: {"icon": lab[0], "label": lab[1], "desc": lab[2]}
                          for t, lab in ((t, _mj._KIN_TYPE_LABELS.get(t, ("", t, ""))) for t in _mj._KIN_ALL_TYPES)},
        },
        "memory": mem,
        "legacy_mailjet": True,
    }


@router.get("/api/account/overview")
async def account_overview(npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    return JSONResponse(await asyncio.to_thread(_overview, email), headers={"Cache-Control": "no-store"})


@router.post("/api/account/prefs")
async def account_prefs(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {flux?: {cat: {email?, nostr?, enabled?}}, kin?: {daily?, weekly?, scope?, types?}}.
    Seuls les champs présents sont modifiés ; les clés inconnues sont refusées."""
    email = _email_for_authenticated_npub(npub)
    body = await safe_json_body(request)
    cur = _mj._read_optout(email)
    flux_ch = {k: {**d, **cur.get("flux_channels", {}).get(k, {})} for k, d in _mj._FLUX_CHANNEL_DEFAULTS.items()}
    flux_sw = {k: bool(cur.get("flux", {}).get(k, True)) for k in _FLUX_SWITCH}
    for cat, v in (body.get("flux") or {}).items():
        if cat not in flux_ch or not isinstance(v, dict):
            raise HTTPException(status_code=400, detail=f"Flux inconnu : {cat!r}")
        for ch in ("email", "nostr"):
            if ch in v:
                flux_ch[cat][ch] = bool(v[ch])
        if "enabled" in v and cat in _FLUX_SWITCH:
            flux_sw[cat] = bool(v["enabled"])
    kin = dict(cur.get("kin", {}))
    k_in = body.get("kin") or {}
    if "daily" in k_in:
        kin["daily"] = bool(k_in["daily"])
    if "weekly" in k_in:
        kin["weekly"] = bool(k_in["weekly"])
    if "scope" in k_in:
        if k_in["scope"] not in ("n1", "n2", "relay"):
            raise HTTPException(status_code=400, detail="scope invalide (n1 | n2 | relay)")
        kin["scope"] = k_in["scope"]
    if "types" in k_in:
        if not isinstance(k_in["types"], list) or any(t not in _mj._KIN_ALL_TYPES for t in k_in["types"]):
            raise HTTPException(status_code=400, detail="types KIN invalides")
        kin["types"] = k_in["types"]
    # canal global : e-mail actif dès qu'un flux l'utilise ; idem NOSTR
    channels = []
    if any(c["email"] for c in flux_ch.values()):
        channels.append("email")
    if any(c["nostr"] for c in flux_ch.values()):
        channels.append("nostr")
    _mj._write_prefs(email, channels, cur.get("npub", ""), kin, flux_sw, flux_ch)
    try:
        saved = _mj._read_optout(email)
        asyncio.create_task(_mj._publish_mailjet_prefs_nostr(settings.GAME_PATH / "nostr" / email, saved))
    except Exception:
        logger.exception("account_prefs: publication NOSTR des préférences impossible (%s)", email)
    logger.info("Account prefs sauvegardées pour %s", email)
    return JSONResponse((await asyncio.to_thread(_overview, email))["notifications"])


@router.post("/api/account/pass")
async def account_pass(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {disable: bool}. Bloque ou réactive la récupération de compte par email + PASS
    (flag réversible ; le code PASS lui-même n'est jamais détruit)."""
    email = _email_for_authenticated_npub(npub)
    body = await safe_json_body(request)
    disable = bool(body.get("disable"))
    flag: Path = _pass_disabled_flag(email)
    if disable:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        flag.chmod(0o600)
    else:
        flag.unlink(missing_ok=True)
    logger.info("Account: récupération par PASS %s pour %s", "bloquée" if disable else "réactivée", email)
    return JSONResponse({"restore_disabled": _is_pass_restore_disabled(email)})


@router.post("/api/account/pass/reveal")
async def account_pass_reveal(npub: str = Depends(require_nostr_auth)):
    """Affiche le code PASS. La signature NIP-98 prouve la possession de la clé du compte
    (même niveau de preuve que /reveal_pass) ; réponse jamais mise en cache, jamais en GET."""
    email = _email_for_authenticated_npub(npub)
    pf = _resolve_pass_file(email)
    if pf is None:
        raise HTTPException(status_code=404, detail="Aucun code PASS pour ce compte.")
    return JSONResponse({"pass": pf.read_text().strip()}, headers={"Cache-Control": "no-store"})


# ── Services liés : cookies de session, scrapers BRO, clés d'API ─────────────
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,78}[a-z0-9]$")


def _watch():
    """bro_watch_core est chargé paresseusement par routers.mailjet : réutilisé tel quel."""
    return _mj.bro_watch_core


def _links(email: str) -> dict:
    udir = settings.GAME_PATH / "nostr" / email
    try:
        manifest = cookie_store.load_manifest(udir)
    except Exception:
        manifest = {}
    now = datetime.now(timezone.utc)
    services = []
    for it in _mj._list_scrapers_status(email):
        info = manifest.get(it["domain"]) or {}
        age = None
        if info.get("uploaded_at"):
            try:
                age = (now - datetime.fromisoformat(info["uploaded_at"].replace("Z", "+00:00"))).days
            except Exception:
                pass
        services.append({
            "domain": it["domain"], "icon": it["icon"], "description": it["description"],
            "has_cookie": it["has_cookie"], "uploaded_at": info.get("uploaded_at") or "", "age_days": age,
            "scraper": it["available"], "enabled": it["enabled"], "last_run": it["last_run"],
            "watch": [{k: e.get(k) for k in ("channel", "keywords", "learn_from", "watched_channels") if k in e}
                      for e in (it["watch_entries"] or []) if isinstance(e, dict)],
        })
    api = []
    try:
        man = api_access_store.load_manifest(udir)
        for key, spec in api_access_store.API_PROVIDERS.items():
            info = man.get(key)
            api.append({"provider": key, "label": spec["label"], "icon": spec["icon"], "fields": spec["fields"],
                        "howto": spec["howto"], "configured": bool(info), "updated_at": info.get("updated_at") if info else None})
    except Exception as e:
        logger.warning("account_links: vault API illisible pour %s : %s", email, e)
    return {"services": services, "api": api}


@router.get("/api/account/links")
async def account_links(npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    return JSONResponse(await asyncio.to_thread(_links, email), headers={"Cache-Control": "no-store"})


def _domain(body: dict) -> str:
    d = str(body.get("domain") or "").strip().lower()
    if not _DOMAIN_RE.match(d):
        raise HTTPException(status_code=400, detail="domaine invalide.")
    return d


@router.post("/api/account/scraper")
async def account_scraper(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {domain, enabled: bool} — active / suspend le scraper BRO d'un service."""
    email = _email_for_authenticated_npub(npub)
    body = await safe_json_body(request)
    d = _domain(body)
    await asyncio.to_thread(_watch().set_scraper_enabled, email, d, bool(body.get("enabled")))
    logger.info("Account: scraper %s %s pour %s", d, "activé" if body.get("enabled") else "suspendu", email)
    return JSONResponse({"domain": d, "enabled": bool(body.get("enabled"))})


@router.post("/api/account/scraper/config")
async def account_scraper_config(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {domain, channel, keywords: [..]|"a, b", learn_from}."""
    email = _email_for_authenticated_npub(npub)
    body = await safe_json_body(request)
    d = _domain(body)
    channel = str(body.get("channel") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,60}", channel):
        raise HTTPException(status_code=400, detail="canal invalide.")
    kw = body.get("keywords") or []
    if isinstance(kw, str):
        kw = kw.split(",")
    fields = {"keywords": [str(k).strip()[:60] for k in kw if str(k).strip()][:30],
              "learn_from": str(body.get("learn_from") or "").strip().lstrip("@")[:80]}
    await asyncio.to_thread(_watch().update_watch_entry, email, d, channel, **fields)
    return JSONResponse({"domain": d, "channel": channel, **fields})


@router.post("/api/account/scraper/channels")
async def account_scraper_channels(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {urls: [..]} — chaînes YouTube (ou équivalent) à surveiller."""
    email = _email_for_authenticated_npub(npub)
    body = await safe_json_body(request)
    urls = [str(u).strip() for u in (body.get("urls") or []) if str(u).strip() and not str(u).strip().startswith("#")][:100]
    if any(not u.startswith(("http://", "https://")) for u in urls):
        raise HTTPException(status_code=400, detail="chaque chaîne doit être une URL http(s).")
    await asyncio.to_thread(_watch().update_watch_entry, email, "youtube.com", "channel_watch", watched_channels=urls)
    return JSONResponse({"count": len(urls)})


# ── Profil LifeOS et mémoires ────────────────────────────────────────────────
@router.get("/api/account/lifeos")
async def account_lifeos(npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    content = await asyncio.to_thread(get_identity_content, email)
    status = await asyncio.to_thread(get_memory_status, email)
    return JSONResponse({
        "files": [{"filename": fn, "label": IDENTITY_LABELS.get(fn, fn), "placeholder": IDENTITY_PLACEHOLDERS.get(fn, ""),
                   "content": content.get(fn, "")} for fn in IDENTITY_FILENAMES],
        "can_regenerate_mastodon": bool(status.get("can_regenerate_from_mastodon")),
    }, headers={"Cache-Control": "no-store"})


@router.post("/api/account/lifeos")
async def account_lifeos_save(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {filename, content} — un seul fichier identity/*.md à la fois."""
    email = _email_for_authenticated_npub(npub)
    body = await safe_json_body(request)
    fn = body.get("filename")
    if fn not in IDENTITY_FILENAMES:
        raise HTTPException(status_code=400, detail="fichier identity invalide.")
    content = str(body.get("content") or "")
    if len(content) > 20000:
        raise HTTPException(status_code=413, detail="contenu trop long (20 000 caractères max).")
    ok, msg = await asyncio.to_thread(save_identity_file, email, fn, content)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Échec de la sauvegarde : {msg}")
    return JSONResponse({"saved": fn})


@router.post("/api/account/lifeos/regenerate")
async def account_lifeos_regenerate(npub: str = Depends(require_nostr_auth)):
    """Relit les propres posts Mastodon et réapprend le profil LifeOS (15 à 30 s)."""
    email = _email_for_authenticated_npub(npub)
    result = await asyncio.to_thread(regenerate_lifeos_from_mastodon, email)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "Régénération échouée."))
    return JSONResponse({"own_posts_found": result.get("own_posts_found", 0)})


@router.post("/api/account/memory/reset")
async def account_memory_reset(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {scope} ∈ conversations | bro_memory | persona | love | identity_learned | all."""
    email = _email_for_authenticated_npub(npub)
    body = await safe_json_body(request)
    scope = body.get("scope")
    if scope not in RESET_SCOPES:
        raise HTTPException(status_code=400, detail="périmètre invalide.")
    report = await asyncio.to_thread(reset_memory, email, scope)
    logger.info("Account: mémoire %s réinitialisée pour %s (%d élément(s))", scope, email, len(report.get("deleted", [])))
    return JSONResponse({"scope": scope, "deleted": len(report.get("deleted", []))})


# ── « Ce que BRO sait de moi » : mémoire personnelle v2 (NIP-98, jamais en cache) ──
_NOSTORE = {"Cache-Control": "no-store"}
_MEM_SOURCES = ("chat", "love_dm", "nostr_public", "mastodon", "photo", "agenda", "contact")


def _fail(res: dict, status: int = 502):
    if not res.get("ok"):
        raise HTTPException(status_code=status, detail=res.get("error", "opération impossible"))
    return res


@router.get("/api/account/memory/items")
async def account_memory_items(kind: str = "", source: str = "", visibility: str = "", q: str = "",
                               npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    res = await asyncio.to_thread(list_memory_items, email, kind, source, visibility, q)
    return JSONResponse(_fail(res), headers=_NOSTORE)


@router.post("/api/account/memory/items")
async def account_memory_item_add(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {content, kind?, visibility?} — souvenir saisi par la personne (source « manual »)."""
    email = _email_for_authenticated_npub(npub)
    b = await safe_json_body(request)
    res = await asyncio.to_thread(add_memory_item, email, str(b.get("content") or ""),
                                  str(b.get("kind") or "fact"), str(b.get("visibility") or "private"))
    return JSONResponse(_fail(res, 400))


@router.post("/api/account/memory/items/update")
async def account_memory_item_update(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {id, content?, kind?, visibility?} — `visibility` ∈ private | love."""
    email = _email_for_authenticated_npub(npub)
    b = await safe_json_body(request)
    try:
        item_id = int(b.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="id invalide.")
    if b.get("visibility") not in (None, *MEMORY_VISIBILITIES) or b.get("kind") not in (None, *MEMORY_KINDS):
        raise HTTPException(status_code=400, detail="type ou visibilité invalide.")
    res = await asyncio.to_thread(update_memory_item, email, item_id,
                                  b.get("content") if isinstance(b.get("content"), str) else None,
                                  b.get("kind"), b.get("visibility"))
    return JSONResponse(_fail(res, 400))


@router.post("/api/account/memory/items/delete")
async def account_memory_item_delete(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {ids:[…]} — suppression définitive (droit à l'oubli)."""
    email = _email_for_authenticated_npub(npub)
    b = await safe_json_body(request)
    try:
        ids = [int(i) for i in (b.get("ids") or [])]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="ids invalides.")
    res = await asyncio.to_thread(delete_memory_items, email, ids)
    logger.info("Account: %d souvenir(s) supprimé(s) pour %s", res.get("deleted", 0), email)
    return JSONResponse(_fail(res))


@router.get("/api/account/memory/sources")
async def account_memory_sources(npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    return JSONResponse(_fail(await asyncio.to_thread(memory_sources, email)), headers=_NOSTORE)


@router.post("/api/account/memory/sources")
async def account_memory_source_set(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {source, enabled} — coche / décoche une source d'apprentissage."""
    email = _email_for_authenticated_npub(npub)
    b = await safe_json_body(request)
    if b.get("source") not in _MEM_SOURCES or not isinstance(b.get("enabled"), bool):
        raise HTTPException(status_code=400, detail="source ou valeur invalide.")
    res = await asyncio.to_thread(set_memory_source, email, b["source"], b["enabled"])
    if res.get("paused"):
        raise HTTPException(status_code=409, detail=res["error"])
    logger.info("Account: source mémoire %s=%s pour %s", b["source"], b["enabled"], email)
    return JSONResponse(_fail(res))


@router.post("/api/account/memory/sources/forget")
async def account_memory_source_forget(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {source} — efface tout ce que BRO a appris de cette source (et la décoche)."""
    email = _email_for_authenticated_npub(npub)
    b = await safe_json_body(request)
    if b.get("source") not in _MEM_SOURCES:
        raise HTTPException(status_code=400, detail="source invalide.")
    await asyncio.to_thread(set_memory_source, email, b["source"], False)
    res = await asyncio.to_thread(forget_memory_source, email, b["source"])
    return JSONResponse(_fail(res))


@router.post("/api/account/memory/learn-now")
async def account_memory_learn_now(npub: str = Depends(require_nostr_auth)):
    """Lance maintenant l'apprentissage des sources cochées (process détaché, résultat visible
    au prochain rechargement de la liste)."""
    email = _email_for_authenticated_npub(npub)
    ok = await asyncio.to_thread(learn_now, email)
    if not ok:
        raise HTTPException(status_code=503, detail="memory_sources.py introuvable ou non lançable.")
    return JSONResponse({"ok": True, "started": True})


# ── Mon espace de données : composition, couverture des analyses, préférences ─
def _dataspace(email: str) -> dict:
    from services import analysis_prefs, ucloud_embed
    from collections import defaultdict
    udir = cloud_storage.ucloud_dir(email)
    db = cloud_storage.ucloud_db
    if not (db.exists(udir) or (udir / "index.json").is_file()):
        return {"initialized": False}
    conn = db.connect(udir)
    st = db.stats(udir)
    kinds = defaultdict(lambda: {"count": 0, "bytes": 0})
    for mime, origin, n, size in conn.execute(
            "SELECT COALESCE(mime,''), COALESCE(origin,''), COUNT(*), COALESCE(SUM(size),0) FROM entries "
            "WHERE type='file' AND cid IS NOT NULL GROUP BY 1,2"):
        if origin == "facecloud-share":
            k = "received"
        elif mime.startswith("image/"):
            k = "photos"
        elif mime.startswith("video/"):
            k = "videos"
        elif mime.startswith("audio/"):
            k = "audio"
        elif mime.startswith("text/") or mime in ("application/pdf",) or "document" in mime or "sheet" in mime or "presentation" in mime or mime.endswith(("msword", "rtf", "json", "xml")):
            k = "documents"
        else:
            k = "other"
        kinds[k]["count"] += n
        kinds[k]["bytes"] += size
    years = [{"year": r[0], "count": r[1]} for r in conn.execute(
        "SELECT strftime('%Y', COALESCE(taken_at, mtime), 'unixepoch') y, COUNT(*) FROM entries "
        "WHERE type='file' AND cid IS NOT NULL AND mime LIKE 'image/%' AND COALESCE(origin,'')<>'facecloud-share' "
        "AND COALESCE(taken_at, mtime) IS NOT NULL GROUP BY y ORDER BY y DESC LIMIT 20") if r[0]]
    prefs = analysis_prefs.load(udir)
    folders = defaultdict(lambda: {"count": 0, "bytes": 0})
    for parent, n, size in conn.execute("SELECT COALESCE(parent,'/'), COUNT(*), COALESCE(SUM(size),0) FROM entries WHERE type='file' AND cid IS NOT NULL GROUP BY 1"):
        segs = [x for x in str(parent).split("/") if x]
        key = "/" + "/".join(segs[:2]) if segs else "/"
        folders[key]["count"] += n
        folders[key]["bytes"] += size
    flist = sorted(({"path": k, "count": v["count"], "bytes": v["bytes"], "excluded": analysis_prefs.is_excluded(udir, k, prefs)}
                    for k, v in folders.items() if k != "/"), key=lambda x: -x["bytes"])[:30]
    counts = db.analysis_counts(udir)
    try:
        emb = ucloud_embed.counts(udir)
    except Exception:
        emb = {"described": counts.get("described", 0), "indexed": 0, "pending": 0}
    return {"initialized": True, "files": st["files"], "bytes": st["bytes"], "kinds": kinds, "years": years,
            "folders": flist, "coverage": {**counts, "indexed": emb["indexed"]}, "prefs": prefs}


@router.get("/api/account/dataspace")
async def account_dataspace(npub: str = Depends(require_nostr_auth)):
    email = _email_for_authenticated_npub(npub)
    return JSONResponse(await asyncio.to_thread(_dataspace, email), headers={"Cache-Control": "no-store"})


@router.post("/api/account/dataspace/prefs")
async def account_dataspace_prefs(request: Request, npub: str = Depends(require_nostr_auth)):
    """Body : {faces?, describe?, embed?, exclude?: [dossiers]} — ce que la station analyse de votre cloud.
    Un dossier exclu reste dans votre cloud ; il n'est simplement plus analysé (visages, descriptions, recherche)."""
    from services import analysis_prefs
    email = _email_for_authenticated_npub(npub)
    body = await safe_json_body(request)
    udir = cloud_storage.ucloud_dir(email)
    try:
        prefs = await asyncio.to_thread(analysis_prefs.save, udir, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("Account: préférences d'analyse de %s → %s", email, {k: v for k, v in prefs.items() if k != "exclude"} | {"exclude": len(prefs["exclude"])})
    return JSONResponse(prefs)


@router.get("/api/account/face-signature")
async def account_face_signature(npub: str = Depends(require_nostr_auth)):
    """Signature faciale du membre (vecteur moyen de SES visages, cohésion, 3 exemples) — jamais ceux des autres.
    Calculée sur la station dans SA collection faces_<sa clé> ; ne contient aucune photo."""
    from routers.node_admin import _qdrant_my_face
    email = _email_for_authenticated_npub(npub)
    try:
        hex_pub = (settings.GAME_PATH / "nostr" / email / "HEX").read_text().strip().lower()
    except Exception:
        raise HTTPException(status_code=404, detail="Clé du compte introuvable.")
    return JSONResponse(await asyncio.to_thread(_qdrant_my_face, hex_pub), headers={"Cache-Control": "no-store"})
