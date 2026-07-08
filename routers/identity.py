import os
import re
import json
import time
import base64
import asyncio
import hashlib
import logging
import tempfile
logger = logging.getLogger(__name__)
import secrets
import string
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from core.config import settings
from utils.helpers import run_script, get_myipfs_gateway, is_origin_mode, get_oc_tier_urls, get_uplanet_home_url
from utils.security import is_multipass_user, is_safe_email
from utils.crypto import npub_to_hex, hex_to_npub, verify_nostr_event
from utils.observability import log_node_event, log_user_event
from services.nostr import generate_nip42_challenge, consume_nip42_challenge

router = APIRouter()
templates = Jinja2Templates(directory="templates")

from pydantic import BaseModel, field_validator

from utils.helpers import as_form

# Caractères autorisés dans salt/pepper : tout caractère imprimable sauf ceux dangereux pour le shell
# Exclus : guillemets simples/doubles, backtick, $, \, retours à la ligne, null
# Permet : espaces, ponctuation courante (;!?.,()+-=*%&#@~), BIP39, emails…
_SAFE_CREDENTIAL_RE = re.compile(r'^[^\x00-\x1f"\'`$\\]{0,56}$')

@as_form
class G1NostrForm(BaseModel):
    email: str
    lang: str
    lat: str
    lon: str
    salt: str = ""
    pepper: str = ""
    format: str = "html"
    pass_code: str = ""
    birth_datetime: str = ""
    birth_place: str = ""
    birth_lat: str = ""             # latitude de naissance — requise pour la phase Phi²
    birth_lon: str = ""             # longitude de naissance — requise pour la phase Phi²
    birth_weight: str = ""
    birth_height: str = ""         # taille naissance (cm) — incluse dans saltRaw
    current_height: str = ""       # taille adulte (cm) — incluse dans saltRaw
    conception_datetime: str = ""
    conception_place: str = ""
    polarity: str = "0"  # 0=homme, 1=femme — encodé dans saltRaw côté client
    pre_stretched: bool = True  # True = salt/pepper déjà PBKDF2-étirés (atomic.html, Zelkova)
                                # False = chaînes brutes → serveur applique PBKDF2 (Cabine-33)

    @field_validator('salt', 'pepper', mode='before')
    @classmethod
    def validate_no_shell_injection(cls, v: str) -> str:
        """Rejette tout salt/pepper contenant des métacaractères shell dangereux."""
        if v and not _SAFE_CREDENTIAL_RE.match(v):
            raise ValueError(
                'Caractères non autorisés (exclus : guillemets " \' ` $ \\ et caractères de contrôle ; max 512 chars)'
            )
        return v

from fastapi import Depends


async def _stretch_credentials(salt: str, pepper: str) -> tuple[str, str]:
    """PBKDF2-HMAC-SHA256 (600k iter, domain-salt 'uplanet-a4l-v1') sur salt et pepper bruts.
    Identique à la Phase 1 de atomic.html côté client.  Exécuté dans un thread executor
    pour ne pas bloquer la boucle asyncio (~0.5 s sur serveur moderne).
    """
    domain = b"uplanet-a4l-v1"
    loop = asyncio.get_event_loop()
    salt_bytes, pepper_bytes = await asyncio.gather(
        loop.run_in_executor(None, hashlib.pbkdf2_hmac, "sha256", salt.encode(), domain, 600000),
        loop.run_in_executor(None, hashlib.pbkdf2_hmac, "sha256", pepper.encode(), domain, 600000),
    )
    return (
        base64.urlsafe_b64encode(salt_bytes).rstrip(b"=").decode(),
        base64.urlsafe_b64encode(pepper_bytes).rstrip(b"=").decode(),
    )


async def _derive_npub_from_credentials(salt: str, pepper: str) -> Optional[str]:
    """Dérive le npub NOSTR depuis stretchedSalt/stretchedPepper via keygen.
    Identique à keygen -t nostr : scrypt(pepper, salt) → ed25519."""
    keygen = settings.TOOLS_PATH / "keygen"
    if not keygen.exists():
        return None
    cred_file = Path(f"/dev/shm/.npub_{secrets.token_hex(8)}")
    try:
        cred_file.write_text(f"{salt}\n{pepper}\n")
        cred_file.chmod(0o600)
        proc = await asyncio.create_subprocess_exec(
            str(keygen), "-t", "nostr", "-i", str(cred_file),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        return stdout.decode().strip() or None
    except Exception as e:
        logger.warning(f"[npub_derive] {e}")
        return None
    finally:
        cred_file.unlink(missing_ok=True)


def _find_email_by_npub(npub: str) -> Optional[str]:
    """Scan ~/.zen/game/nostr/*/.secret.nostr → email existant pour ce npub."""
    pattern = f"NPUB={npub}"
    try:
        for secret in (settings.GAME_PATH / "nostr").glob("*/.secret.nostr"):
            try:
                if pattern in secret.read_text():
                    return secret.parent.name
            except OSError:
                continue
    except Exception as e:
        logger.warning(f"[npub_scan] {e}")
    return None


# Double-soumission : un seul appel /g1nostr actif par email (partagé avec
# /g1/onboard, même risque de double création concurrente pour un même email)
_g1nostr_in_progress: set[str] = set()


async def _create_multipass_and_wait(
    email: str, lang: str, lat: str, lon: str, salt: str, pepper: str,
    birth_datetime: str = "", birth_place: str = "", birth_weight: str = "",
    conception_datetime: str = "", conception_place: str = "",
    birth_lat: str = "", birth_lon: str = "", polarity: str = "0",
) -> dict:
    """Lance g1.sh en arrière-plan et attend `.multipass.json` (jusqu'à 40 s).

    Retourne le JSON enrichi (is_origin/oc_urls/uplanet_home/uplanetname_g1).
    Lève HTTPException en cas d'échec. Factorisé depuis `/g1nostr` (cas
    "nouvel email, format=json") pour être réutilisé par `POST /g1/onboard`
    sans dupliquer le lancement du script + le polling.
    """
    multipass_json = settings.GAME_PATH / "nostr" / email / ".multipass.json"
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "./g1.sh",
            email, lang, lat, lon, salt, pepper,
            birth_datetime, birth_place, birth_weight,
            conception_datetime, conception_place,
            birth_lat, birth_lon, polarity,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Nettoyage zombie sans bloquer
        asyncio.ensure_future(proc.wait())
    except Exception as e:
        logger.error(f"Failed to launch g1.sh for {email}: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur lancement g1.sh : {e}")

    # Poll jusqu'à 40 s (80 × 0.5 s) — .multipass.json apparaît vers 10–15 s
    for _ in range(80):
        if multipass_json.exists():
            break
        await asyncio.sleep(0.5)

    if not multipass_json.exists():
        logger.error(f"JSON sidecar not found after 40s for {email}")
        raise HTTPException(status_code=500,
                            detail="MULTIPASS non initialisé après 40 s. Contactez le support.")
    try:
        with open(multipass_json) as f:
            data = json.load(f)
        data["is_origin"]      = is_origin_mode()
        data["oc_urls"]        = get_oc_tier_urls()
        data["uplanet_home"]   = await get_uplanet_home_url()
        data["uplanetname_g1"] = settings.UPLANETNAME_G1
        if proc.returncode is None:
            data["status"] = "creating"
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading JSON sidecar for {email}: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur lecture JSON : {e}")


@router.post("/g1nostr")
async def scan_qr(
    request: Request,
    form_data: G1NostrForm = Depends(G1NostrForm.as_form)
):
    """Wrapper d'observabilité additif autour de _scan_qr_impl (MULTIPASS
    création/récupération) : capture succès/échec/latence NODE + BRO (par
    email) sur TOUS les chemins de sortie de l'implémentation — y compris les
    exceptions — sans toucher à la logique métier existante (nombreux
    early-return, cf. cas 1-4 documentés ci-dessous)."""
    _obs_start = time.time()
    _obs_email = form_data.email
    _obs_success = False
    _obs_status = 500
    _obs_extra: dict = {}
    try:
        result = await _scan_qr_impl(request, form_data)
        _obs_status = getattr(result, "status_code", 200)
        _obs_success = _obs_status < 400
        try:
            if isinstance(result, JSONResponse):
                body = json.loads(bytes(result.body))
                if isinstance(body, dict) and body.get("error"):
                    _obs_extra["error"] = body["error"]
        except Exception:
            pass
        return result
    except HTTPException as exc:
        _obs_status = exc.status_code
        _obs_extra["error"] = "HTTPException"
        raise
    except Exception:
        _obs_extra["error"] = "unhandled_exception"
        raise
    finally:
        latency_ms = (time.time() - _obs_start) * 1000
        _obs_extra["status"] = _obs_status
        log_node_event("g1nostr", _obs_success, category="multipass",
                        latency_ms=latency_ms, extra=_obs_extra)
        log_user_event(_obs_email, "multipass", "g1nostr", _obs_success,
                        latency_ms=latency_ms, extra=dict(_obs_extra))


async def _scan_qr_impl(
    request: Request,
    form_data: G1NostrForm
):
    """
    Endpoint to execute the g1.sh script and return the generated file.
    Supports both regular users and swarm subscription aliases.

    Cas 1 — Nouveau email  : crée le MULTIPASS, retourne JSON ou HTML.
    Cas 2 — Email existant, format=json, sans pass_code  : retourne 409 (need_pass).
    Cas 3 — Email existant, format=json, pass_code fourni : vérifie PASS et retourne JSON.
    Cas 4 — Email existant, format=html  : comportement inchangé (retourne HTML).
    """
    email = form_data.email
    lang = form_data.lang
    lat = form_data.lat
    lon = form_data.lon
    salt = form_data.salt
    pepper = form_data.pepper
    # ── Pre-stretching serveur : Cabine-33 et clients légers sans PBKDF2 natif ─
    if not form_data.pre_stretched and salt and pepper:
        salt, pepper = await _stretch_credentials(salt, pepper)
    format = form_data.format
    pass_code = (form_data.pass_code or "").strip()
    birth_datetime      = form_data.birth_datetime or ""
    birth_place         = form_data.birth_place or ""
    birth_lat           = form_data.birth_lat or ""
    birth_lon           = form_data.birth_lon or ""
    birth_weight        = form_data.birth_weight or ""
    conception_datetime = form_data.conception_datetime or ""
    conception_place    = form_data.conception_place or ""
    polarity            = form_data.polarity or "0"

    # ── Détection email existant ──────────────────────────────────────────────
    nostr_dir    = settings.GAME_PATH / "nostr" / email
    email_exists = nostr_dir.exists() and (nostr_dir / "G1PUBNOSTR").exists()

    # ── Pré-vérification identité (salt/pepper fournis) ──────────────────────
    # Dérive le npub localement avant toute création pour détecter les conflits.
    derived_npub: Optional[str] = None
    if salt and pepper and format == "json":
        derived_npub = await _derive_npub_from_credentials(salt, pepper)

    # ── Cas : email différent, même npub → IDENTITY_CONFLICT ─────────────────
    if derived_npub and not email_exists:
        conflict_email = _find_email_by_npub(derived_npub)
        if conflict_email and conflict_email != email:
            logger.warning(f"IDENTITY_CONFLICT: npub {derived_npub[:20]}… already owned by {conflict_email}")
            return JSONResponse(
                status_code=409,
                content={
                    "error": "IDENTITY_CONFLICT",
                    "message": "Ces données biométriques sont déjà associées à une autre identité."
                }
            )

    # ── Cas 2 & 3 : email existant + format JSON ──────────────────────────────
    if email_exists and format == "json":
        multipass_json = nostr_dir / ".multipass.json"
        pass_file      = settings.GAME_PATH / "players" / email / ".pass"

        # Récupération silencieuse : même email + même npub dérivé → pas de PASS requis
        if derived_npub and not pass_code:
            secret_nostr = nostr_dir / ".secret.nostr"
            if secret_nostr.exists():
                try:
                    stored_npub = next(
                        (p.split("=", 1)[1] for p in secret_nostr.read_text().split(";")
                         if p.strip().startswith("NPUB=")), None
                    )
                    if stored_npub and stored_npub.strip() == derived_npub:
                        if multipass_json.exists():
                            with open(multipass_json) as f:
                                data = json.load(f)
                            data["is_origin"]      = is_origin_mode()
                            data["oc_urls"]        = get_oc_tier_urls()
                            data["uplanet_home"]   = await get_uplanet_home_url()
                            data["uplanetname_g1"] = settings.UPLANETNAME_G1
                            logger.info(f"Silent recovery for {email} (npub match)")
                            return JSONResponse(data)
                except Exception as e:
                    logger.warning(f"Silent recovery check failed for {email}: {e}")

        if not pass_code:
            # Cas 2 — demander le PASS
            logger.info(f"MULTIPASS exists for {email}, requesting PASS")
            return JSONResponse(
                status_code=409,
                content={
                    "error": "MULTIPASS_EXISTS",
                    "need_pass": True,
                    "message": "Ce MULTIPASS existe déjà. Saisissez le code PASS reçu par email lors de la création."
                }
            )

        # Cas 3 — vérifier le PASS
        if not pass_file.exists():
            logger.warning(f"PASS file missing for {email}: {pass_file}")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "PASS_UNAVAILABLE",
                    "message": "Code PASS non disponible sur ce nœud. Contactez le support."
                }
            )

        stored_pass = pass_file.read_text().strip()
        if pass_code != stored_pass:
            logger.warning(f"Wrong PASS attempt for {email}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "INVALID_PASS",
                    "message": "Code PASS incorrect."
                }
            )

        # PASS correct — regénérer .multipass.json si absent puis retourner
        if not multipass_json.exists():
            logger.info(f"Regenerating .multipass.json for {email} via g1.sh")
            _salt = salt or ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
            _pep  = pepper or ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
            await run_script("./g1.sh", email, lang, lat, lon, _salt, _pep,
                             birth_datetime, birth_place, birth_weight,
                             conception_datetime, conception_place,
                             birth_lat, birth_lon, polarity)
            for _ in range(6):
                if multipass_json.exists():
                    break
                await asyncio.sleep(0.5)

        if not multipass_json.exists():
            return JSONResponse(
                status_code=500,
                content={"error": "MULTIPASS_JSON_MISSING",
                         "message": "Reconstruction du JSON échouée. Contactez le support."}
            )

        try:
            with open(multipass_json, 'r') as f:
                data = json.load(f)
            data["is_origin"]      = is_origin_mode()
            data["oc_urls"]        = get_oc_tier_urls()
            data["uplanet_home"]   = await get_uplanet_home_url()
            data["uplanetname_g1"] = settings.UPLANETNAME_G1
            logger.info(f"MULTIPASS recovery via PASS OK for {email}")
            return JSONResponse(data)
        except Exception as e:
            logger.error(f"Error reading .multipass.json for {email}: {e}")
            raise HTTPException(status_code=500, detail=f"Erreur lecture JSON : {e}")

    # ── Anti double-soumission (création uniquement) ──────────────────────────
    if email in _g1nostr_in_progress:
        return JSONResponse(
            status_code=429,
            content={
                "error": "CREATION_IN_PROGRESS",
                "message": "Création déjà en cours pour cet email. Veuillez patienter."
            }
        )
    _g1nostr_in_progress.add(email)

    _DISCO_RAND = 24
    if not salt or salt.strip() == "":
        salt = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(_DISCO_RAND))
        logger.info(f"Generated random ZEN Card salt for {email}")
    if not pepper or pepper.strip() == "":
        pepper = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(_DISCO_RAND))
        logger.info(f"Generated random ZEN Card pepper for {email}")

    logger.info(f"ZEN Card credentials: salt={len(salt)} chars, pepper={len(pepper)} chars")

    if format == "json":
        # ── Retour anticipé : lance g1.sh en arrière-plan, poll .multipass.json ──
        try:
            data = await _create_multipass_and_wait(
                email, lang, lat, lon, salt, pepper,
                birth_datetime, birth_place, birth_weight,
                conception_datetime, conception_place,
                birth_lat, birth_lon, polarity,
            )
        finally:
            _g1nostr_in_progress.discard(email)
        logger.info(f"Early return for {email} (status={data.get('status','done')})")
        return JSONResponse(data)

    # ── Format HTML : attendre la fin complète du script ─────────────────────
    try:
        return_code, last_line = await run_script(
            "./g1.sh", email, lang, lat, lon, salt, pepper,
            birth_datetime, birth_place, birth_weight, conception_datetime, conception_place,
            birth_lat, birth_lon, polarity
        )
    finally:
        _g1nostr_in_progress.discard(email)

    if return_code != 0:
        logger.error(f"Erreur script g1.sh : {last_line}")
        raise HTTPException(status_code=500, detail=f"Erreur script g1.sh : {last_line}")

    return FileResponse(last_line.strip())


# ═══════════════════════════════════════════════════════════════════════════
# ATOM4LOVE — activation/complétion d'un MULTIPASS déjà existant
#
# Endpoint dédié, authentifié — remplace l'ancienne convention `+a4l@email`
# détectée dans /g1nostr (retirée : n'importe qui pouvait POST
# "victime+a4l@domain" avec de fausses données de naissance et faire signer
# un event NOSTR par la clé de la victime, sans aucune preuve de possession).
#
# Authentification, l'une des deux :
#   - pass_code : le code PASS reçu par email à la création du MULTIPASS
#     (même mécanisme que /g1nostr, fichier ~/.zen/game/players/{email}/.pass)
#   - auth_event : event NOSTR kind 22242 (NIP-42) signé avec le nsec
#     PRINCIPAL du compte, prouvant sa possession sans jamais le transmettre.
#     Challenge à obtenir au préalable via GET /atom4love/challenge.
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/atom4love/challenge")
async def get_atom4love_challenge(email: str):
    """Émet un challenge NIP-42 scopé à la clé NOSTR principale de `email`."""
    email = (email or "").strip().lower()
    hex_file = settings.GAME_PATH / "nostr" / email / "HEX"
    if not hex_file.exists():
        raise HTTPException(status_code=404, detail="MULTIPASS introuvable pour cet email.")
    hex_pubkey = hex_file.read_text().strip()
    challenge = generate_nip42_challenge(hex_pubkey)
    return JSONResponse({"challenge": challenge, "pubkey_hex": hex_pubkey, "expires_in": 120})


@as_form
class Atom4LoveActivateForm(BaseModel):
    email: str
    birth_datetime: str
    birth_lat: str
    birth_lon: str
    birth_weight: str = ""
    birth_place: str = ""
    conception_datetime: str = ""
    conception_place: str = ""
    polarity: str = "0"
    pass_code: str = ""    # Option A — code PASS
    auth_event: str = ""   # Option B — event kind 22242 signé (JSON), voir /atom4love/challenge


@router.post("/atom4love/activate")
async def atom4love_activate(
    request: Request,
    form_data: Atom4LoveActivateForm = Depends(Atom4LoveActivateForm.as_form)
):
    email = (form_data.email or "").strip().lower()
    if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email):
        raise HTTPException(status_code=400, detail="Email invalide.")

    nostr_dir = settings.GAME_PATH / "nostr" / email
    if not (nostr_dir / ".secret.nostr").exists() or not (nostr_dir / "G1PUBNOSTR").exists():
        return JSONResponse(
            status_code=404,
            content={"error": "PRIMARY_ACCOUNT_NOT_FOUND",
                     "message": "Créez d'abord votre MULTIPASS avant d'activer ATOM4LOVE."}
        )

    pass_code = (form_data.pass_code or "").strip()
    auth_event_raw = (form_data.auth_event or "").strip()

    if auth_event_raw:
        try:
            ev = json.loads(auth_event_raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="auth_event: JSON invalide.")

        if not verify_nostr_event(ev):
            return JSONResponse(status_code=401, content={
                "error": "INVALID_SIGNATURE", "message": "Signature NOSTR invalide."})
        if ev.get("kind") != 22242:
            return JSONResponse(status_code=400, content={
                "error": "INVALID_AUTH_KIND",
                "message": "auth_event doit être un event kind 22242 (NIP-42)."})

        hex_file = nostr_dir / "HEX"
        account_hex = hex_file.read_text().strip() if hex_file.exists() else ""
        if not account_hex or ev.get("pubkey", "").lower() != account_hex.lower():
            logger.warning(f"ATOM4LOVE auth: pubkey mismatch for {email}")
            return JSONResponse(status_code=401, content={
                "error": "PUBKEY_MISMATCH",
                "message": "Cette signature n'appartient pas au compte ciblé."})

        challenge = next(
            (t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "challenge"), None)
        expected = consume_nip42_challenge(account_hex)  # usage unique
        if not challenge or not expected or challenge != expected:
            return JSONResponse(status_code=401, content={
                "error": "INVALID_CHALLENGE",
                "message": "Challenge NIP-42 expiré, invalide ou déjà utilisé."})

    elif pass_code:
        pass_file = settings.GAME_PATH / "players" / email / ".pass"
        if not pass_file.exists():
            return JSONResponse(status_code=503, content={
                "error": "PASS_UNAVAILABLE",
                "message": "Code PASS non disponible sur ce nœud. Contactez le support."})
        if pass_code != pass_file.read_text().strip():
            logger.warning(f"ATOM4LOVE: wrong PASS attempt for {email}")
            return JSONResponse(status_code=401, content={
                "error": "INVALID_PASS", "message": "Code PASS incorrect."})

    else:
        return JSONResponse(status_code=401, content={
            "error": "AUTH_REQUIRED",
            "message": "Authentification requise : code PASS ou signature NOSTR (voir /atom4love/challenge)."})

    return_code, last_line = await run_script(
        str(settings.TOOLS_PATH / "atom4love_activate.sh"),
        email, form_data.birth_datetime, form_data.birth_place,
        form_data.birth_lat, form_data.birth_lon, form_data.birth_weight,
        form_data.conception_datetime, form_data.conception_place, form_data.polarity,
    )
    try:
        data = json.loads(last_line.strip())
    except (json.JSONDecodeError, AttributeError):
        data = {}
    if return_code != 0 or not data.get("activated"):
        logger.warning(f"ATOM4LOVE activation failed for {email}: {last_line}")
        return JSONResponse(status_code=500, content={
            "error": data.get("error", "ACTIVATION_FAILED"),
            "message": "Échec de l'activation ATOM4LOVE."})

    logger.info(f"ATOM4LOVE activated for {email}")
    return JSONResponse(data)


@as_form
class UPassportForm(BaseModel):
    parametre: str
    imageData: Optional[str] = None
    zlat: Optional[str] = None
    zlon: Optional[str] = None
    format: str = "html"

@router.post("/upassport")
async def scan_qr_upassport(
    form_data: UPassportForm = Depends(UPassportForm.as_form)
):
    parametre = form_data.parametre
    imageData = form_data.imageData
    zlat = form_data.zlat
    zlon = form_data.zlon
    import base64
    image_dir = "./tmp"
    # Assign default 0.00 values if zlat or zlon are empty
    zlat = zlat if zlat is not None else "0.00"
    zlon = zlon if zlon is not None else "0.00"

    # Ensure the image directory exists
    os.makedirs(image_dir, exist_ok=True)

    # Vérification si imageData est un PIN de 4 chiffres
    if imageData and imageData.isdigit() and len(imageData) == 4:
        logger.info(f"Received a PIN: {imageData}")
        image_path = imageData
    else:
        # Génération du nom de fichier à partir du hash de parametre
        image_filename = f"qr_image_{hashlib.sha256(parametre.encode()).hexdigest()[:10]}.png"
        image_path = os.path.join(image_dir, image_filename)

        if imageData:
            try:
                # Remove the data URL prefix if present
                if ',' in imageData:
                    image_data = imageData.split(',')[1]
                else:
                    image_data = imageData

                # Decode and save the image
                with open(image_path, "wb") as image_file:
                    image_file.write(base64.b64decode(image_data))
                    logger.info("Saved image to: %s", image_path)

            except Exception as e:
                logger.error("Error saving image: %s", e)

    # Log zlat and zlon values
    logger.info(f"zlat: {zlat}, zlon: {zlon}")

    ## Running External Script > get last line > send file content back to client.
    script_path = "./upassport.sh"
    return_code, last_line = await run_script(script_path, parametre, image_path, zlat, zlon)

    if return_code == 0:
        returned_file_path = last_line.strip()
        logger.info(f"Returning file: {returned_file_path}")
        
        # Vérifier si le fichier existe
        if not os.path.exists(returned_file_path):
            error_message = f"Le fichier {returned_file_path} n'existe pas"
            logger.error(error_message)
            raise HTTPException(status_code=404, detail=error_message)
            
        # Vérifier si c'est bien un fichier HTML
        if not returned_file_path.endswith('.html'):
            error_message = f"Le fichier {returned_file_path} n'est pas un fichier HTML"
            logger.error(error_message)
            raise HTTPException(status_code=400, detail=error_message)
            
        try:
            return FileResponse(
                returned_file_path,
                media_type='text/html',
                filename=os.path.basename(returned_file_path)
            )
        except Exception as e:
            error_message = f"Erreur lors de l'envoi du fichier: {str(e)}"
            logger.error(error_message)
            raise HTTPException(status_code=500, detail=error_message)
    else:
        error_message = f"Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs."
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message)

@as_form
class SSSSForm(BaseModel):
    cardns: str
    ssss: str
    zerocard: Optional[str] = None

@router.post("/ssss")
async def ssss(request: Request, form_data: SSSSForm = Depends(SSSSForm.as_form)):
    cardns = form_data.cardns
    ssss = form_data.ssss
    zerocard = form_data.zerocard

    logger.info(f"Received Card NS: {cardns}")
    logger.info(f"Received SSSS key: [REDACTED - {len(ssss)} chars]")
    logger.info(f"ZEROCARD: {zerocard}")

    script_path = "./check_ssss.sh"
    return_code, last_line = await run_script(script_path, cardns, ssss, zerocard)

    if return_code == 0:
        returned_file_path = last_line.strip()
        return FileResponse(returned_file_path)
    else:
        raise HTTPException(status_code=500, detail="Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs.")

@router.get("/.well-known/nostr/nip96.json")
async def nip96_discovery(request: Request):
    """
    NIP-96 discovery endpoint for file storage server.
    Returns server capabilities and configuration for NOSTR clients.
    """
    import base64
    
    # Get base URL from request
    base_url = str(request.base_url).rstrip('/')
    
    # Try to extract pubkey from NIP-98 Authorization header
    user_pubkey_hex = None
    is_multipass = False
    
    try:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Nostr "):
            # Decode the base64-encoded NIP-98 event
            auth_base64 = auth_header.replace("Nostr ", "").strip()
            auth_json = base64.b64decode(auth_base64).decode('utf-8')
            auth_event = json.loads(auth_json)
            
            # Extract pubkey from the NIP-98 event (kind 27235)
            if auth_event.get("kind") == 27235 and "pubkey" in auth_event:
                user_pubkey_hex = auth_event["pubkey"]
                logger.info(f"🔑 NIP-96 Discovery: Checking MULTIPASS status for: {user_pubkey_hex[:16]}...")
                
                # Check if user is recognized as MULTIPASS by UPlanet
                is_multipass = is_multipass_user(user_pubkey_hex)
            else:
                logger.warning(f"⚠️  NIP-96 Discovery: Invalid NIP-98 event: kind={auth_event.get('kind')}")
    except Exception as e:
        logger.warning(f"⚠️  NIP-96 Discovery: Could not extract pubkey from NIP-98: {e}")
    
    # Determine plans based on MULTIPASS status
    if is_multipass:
        # MULTIPASS users: 650MB quota
        plans = {
            "multipass": {
                "name": "MULTIPASS IPFS Storage",
                "is_nip98_required": True,
                "max_byte_size": 681574400,  # 650MB
                "file_expiration": [0, 0],  # No expiration
                "media_transformations": {
                    "image": ["thumbnail"],
                    "video": ["thumbnail", "gif_animation"]
                }
            }
        }
    else:
        # Non-recognized NOSTR users: 100MB quota
        plans = {
            "free": {
                "name": "Free IPFS Storage (NOSTR)",
                "is_nip98_required": True,
                "max_byte_size": 104857600,  # 100MB
                "file_expiration": [0, 0],  # No expiration
                "media_transformations": {
                    "image": ["thumbnail"],
                    "video": ["thumbnail", "gif_animation"]
                }
            }
        }
    
    return {
        "api_url": f"{base_url}/api/fileupload",
        "download_url": "https://ipfs.copylaradio.com",
        "supported_nips": [96, 98, 94, 71],
        "tos_url": f"{base_url}/terms",
        "content_types": [
            "image/*",
            "video/*",
            "audio/*",
            "application/pdf"
        ],
        "plans": plans,
        "extensions": {
            "ipfs": True,
            "provenance": True,
            "twin_key": True,
            "info_json": True,
            "tmdb_metadata": True
        }
    }


# ── Alerte capitaine : tentatives PASS échouées ───────────────────────────────

class PassAlertBody(BaseModel):
    email: str
    attempts: int


async def _notify_pass_alert(email: str, attempts: int, ip: str) -> None:
    captain = settings.CAPTAINEMAIL
    mailjet_sh = settings.TOOLS_PATH / "mailjet.sh"
    if not (captain and mailjet_sh.exists()):
        logger.warning("PASS alert: mailjet.sh introuvable ou CAPTAINEMAIL non défini")
        return
    body = (
        "<h2>⚠️ Tentatives PASS échouées — MULTIPASS bloqué</h2>"
        f"<p><b>Email :</b> {email}</p>"
        f"<p><b>Tentatives :</b> {attempts}</p>"
        f"<p><b>IP :</b> {ip}</p>"
        f"<p><b>Station :</b> {settings.uSPOT}</p>"
        "<p>Le code PASS a été invalidé. L'utilisateur devra contacter la station pour réinitialisation.</p>"
    )
    tmp_msg = tempfile.NamedTemporaryFile(suffix=".html", mode="w", delete=False, encoding="utf-8")
    try:
        tmp_msg.write(body)
        tmp_msg.close()
        proc = await asyncio.create_subprocess_exec(
            str(mailjet_sh), "--expire", "0s", captain, tmp_msg.name,
            f"⚠️ PASS bloqué {attempts}× — {email} — {settings.uSPOT}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
    except Exception as exc:
        logger.warning("Mailjet PASS alert failed: %s", exc)
    finally:
        try:
            os.unlink(tmp_msg.name)
        except OSError:
            pass


@router.post("/g1nostr/alert")
async def pass_attempts_alert(request: Request, data: PassAlertBody) -> JSONResponse:
    """Invalide le PASS et notifie le capitaine après 3 échecs consécutifs."""
    email = data.email.strip().lower()

    # Supprimer le fichier .pass pour bloquer toute nouvelle tentative
    pass_file = settings.GAME_PATH / "players" / email / ".pass"
    invalidated = False
    if pass_file.exists():
        try:
            pass_file.unlink()
            invalidated = True
            logger.warning("PASS invalidé pour %s après %d tentatives depuis %s",
                           email, data.attempts, request.client.host if request.client else "?")
        except OSError as exc:
            logger.error("Impossible d'invalider .pass pour %s : %s", email, exc)

    ip = request.client.host if request.client else "inconnu"
    await _notify_pass_alert(email, data.attempts, ip)

    # Observabilité additive : tentatives PASS échouées répétées = signal fort
    # pour BRO/capitaine (bruteforce potentiel ou utilisateur bloqué légitime).
    log_node_event("pass_alert", invalidated, category="multipass_security",
                    extra={"attempts": data.attempts, "ip": ip})
    log_user_event(email, "multipass", "pass_alert", invalidated,
                   extra={"attempts": data.attempts})

    return JSONResponse({"status": "alerted", "invalidated": invalidated})


# ═══════════════════════════════════════════════════════════════════════════
# ONBOARDING /g1  —  Point d'entrée réservé aux membres OpenCollective
#
# Politique spécifique à CETTE page (u.DOMAIN/g1, templates/g1nostr.html) :
# la création d'un nouveau MULTIPASS y est bloquée pour les emails non inscrits
# sur OpenCollective. zelkova / atomic.html / miz.html continuent d'utiliser
# /g1nostr directement, SANS aucune restriction — contrat inchangé pour eux.
#
# Permet en option de faire don d'un ancien portefeuille Ğ1 v1 (Cesium) à la
# coopérative : le solde est vidé vers UPLANETNAME_G1 et crédité en ẐEN
# (floor(Ğ1_donnés / 10)) sur le MULTIPASS — dont la clé est TOUJOURS générée
# aléatoirement côté serveur, indépendamment du wallet donné.
#
# Ordre des opérations (irréversible en dernier — voir memory
# feedback_g1_financial_ops_safety) :
#   1. Vérification adhésion OpenCollective (avant toute création).
#   2. Création du MULTIPASS (clé aléatoire).
#   3. Seulement si 2 a réussi : don du wallet v1 (best-effort).
#   4. Seulement si 3 a réussi (donated=true, credited_zen>0) : crédit ẐEN.
# ═══════════════════════════════════════════════════════════════════════════

@as_form
class G1OnboardForm(BaseModel):
    email: str
    lang: str
    lat: str
    lon: str
    v1_login: str = ""
    v1_password: str = ""
    confirm_donation: bool = False

    @field_validator('v1_login', 'v1_password', mode='before')
    @classmethod
    def validate_no_shell_injection(cls, v: str) -> str:
        """Mêmes règles que G1NostrForm.salt/pepper — ces valeurs transitent
        vers un fichier JSON, jamais un argument shell, mais on reste strict."""
        if v and not _SAFE_CREDENTIAL_RE.match(v):
            raise ValueError(
                'Caractères non autorisés (exclus : guillemets " \' ` $ \\ et caractères de contrôle ; max 56 chars)'
            )
        return v


async def _donate_g1v1_and_credit(email: str, v1_login: str, v1_password: str) -> dict:
    """Écrit les credentials v1 dans un fichier 0600 sous /dev/shm (jamais en
    argument de ligne de commande — voir memory feedback_g1_financial_ops_safety),
    appelle donate_g1v1_wallet.sh (drain vers UPLANETNAME_G1), puis crédite le
    ẐEN correspondant via UPLANET.official.sh.

    Best-effort : un échec ici n'invalide jamais le MULTIPASS déjà créé —
    l'appelant doit avoir déjà confirmé la création avant d'appeler ceci.
    """
    credfile = Path(f"/dev/shm/.g1v1_onboard_{secrets.token_hex(8)}")
    try:
        credfile.write_text(json.dumps({"salt": v1_login, "password": v1_password}))
        credfile.chmod(0o600)
        script = str(settings.TOOLS_PATH / "donate_g1v1_wallet.sh")
        return_code, last_line = await run_script(
            script, str(credfile), settings.UPLANETNAME_G1, f"UPLANET:DON_LEGACY:{email}"
        )
    except Exception as e:
        logger.error(f"donate_g1v1_wallet.sh launch failed for {email}: {e}")
        return {"donated": False, "error": "DONATION_SCRIPT_FAILED"}
    finally:
        # Double sécurité : le script bash supprime déjà CREDFILE (trap EXIT),
        # on s'assure qu'il ne traîne pas si le lancement lui-même a échoué.
        credfile.unlink(missing_ok=True)

    try:
        donation = json.loads(last_line.strip())
    except (json.JSONDecodeError, AttributeError):
        logger.warning(f"donate_g1v1_wallet.sh unparsable output for {email}: {last_line}")
        return {"donated": False, "error": "DONATION_OUTPUT_UNPARSABLE"}

    if return_code != 0 or not donation.get("donated"):
        logger.info(f"Donation not applied for {email}: {donation}")
        return donation

    credited_zen = int(donation.get("credited_zen") or 0)
    donation["zen_credited"] = False
    if credited_zen > 0:
        try:
            await run_script(
                str(settings.ZEN_PATH / "Astroport.ONE" / "UPLANET.official.sh"),
                "-l", email, "-m", str(credited_zen),
            )
            donation["zen_credited"] = True
            logger.info(f"Credited {credited_zen} Ẑ to {email} for legacy G1v1 donation")
        except Exception as e:
            logger.error(f"UPLANET.official.sh credit failed for {email} ({credited_zen} Ẑ): {e}")
            donation["credit_error"] = str(e)

    return donation


@router.post("/g1/onboard")
async def g1_onboard(
    request: Request,
    form_data: G1OnboardForm = Depends(G1OnboardForm.as_form)
):
    """Onboarding MULTIPASS gate OpenCollective + don optionnel de wallet Ğ1 v1.

    Réutilise entièrement _scan_qr_impl (via un G1NostrForm construit ici, avec
    salt/pepper vides — donc générés aléatoirement côté serveur) pour la
    création elle-même : pas de duplication de la logique g1.sh / anti double-
    soumission / enrichissement JSON, déjà éprouvée sur /g1nostr.
    """
    email = (form_data.email or "").strip().lower()
    if not is_safe_email(email):
        raise HTTPException(status_code=400, detail="Email invalide.")

    nostr_dir    = settings.GAME_PATH / "nostr" / email
    email_exists = nostr_dir.exists() and (nostr_dir / "G1PUBNOSTR").exists()

    # ── Gate OpenCollective — uniquement pour une NOUVELLE création ──────────
    # Un membre déjà inscrit n'a pas à re-prouver son adhésion pour récupérer
    # son MULTIPASS existant.
    if not email_exists:
        from routers.finance import get_oc_member_info
        oc_info = await get_oc_member_info(email)
        if not oc_info.get("is_member"):
            return JSONResponse(
                status_code=403,
                content={
                    "error": "OC_MEMBERSHIP_REQUIRED",
                    "message": "Devenez membre OpenCollective avec cet email avant de créer votre MULTIPASS.",
                    "oc_urls": get_oc_tier_urls(),
                }
            )

    # ── Création (ou récupération) — délègue entièrement à /g1nostr ─────────
    g1nostr_form = G1NostrForm(
        email=email, lang=form_data.lang, lat=form_data.lat, lon=form_data.lon,
        format="json",
    )
    result = await _scan_qr_impl(request, g1nostr_form)

    status_code = getattr(result, "status_code", 200)
    if status_code != 200:
        return result  # 409 (need_pass/conflict), 429 (in_progress), etc. — inchangé

    data = json.loads(bytes(result.body))

    # ── Don optionnel du wallet Ğ1 v1 — SEULEMENT après création réussie ────
    # (email_exists était False au moment du gate : c'est donc une création
    # neuve, jamais une récupération d'un compte déjà existant.)
    if (not email_exists and form_data.confirm_donation
            and form_data.v1_login and form_data.v1_password):
        data["donation"] = await _donate_g1v1_and_credit(
            email, form_data.v1_login, form_data.v1_password
        )

    return JSONResponse(data)
