import os
import re
import json
import time
import uuid
import asyncio
import logging
logger = logging.getLogger(__name__)
import subprocess
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
from cachetools import TTLCache

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from core.config import settings
from core.state import app_state

from utils.helpers import run_script, get_myipfs_gateway, get_env_from_mysh
from utils.security import (
    find_user_directory_by_hex,
    is_safe_email,
    is_safe_g1pub,
    get_safe_user_path
)
from services.nostr import verify_nostr_auth
from utils.crypto import npub_to_hex
from utils.observability import log_node_event, log_user_event
from models.schemas import (
    CoinflipStartRequest, CoinflipStartResponse,
    CoinflipFlipRequest, CoinflipFlipResponse,
    CoinflipPayoutRequest, CoinflipPayoutResponse
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Cache balance : TTL 30 s, max 2000 entrées
app_state.balance_cache = TTLCache(maxsize=2000, ttl=30)

# Cache vérification OC : TTL 1h, max 500 entrées
_oc_member_cache: "TTLCache" = TTLCache(maxsize=500, ttl=3600)

# --- Coinflip server-authoritative state ---
import base64
import hmac
import hashlib
import secrets

COINFLIP_SECRET = settings.COINFLIP_SECRET
_COINFLIP_SESSION_TTL = 300  # secondes, identique au TTL du token

def _session_path(sid: str) -> Path:
    return Path(settings.ZEN_PATH) / "tmp" / f"coinflip_session_{sid}.json"

def _load_session(sid: str) -> Optional[Dict[str, Any]]:
    path = _session_path(sid)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if int(time.time()) - data.get("created_at", 0) > _COINFLIP_SESSION_TTL:
            path.unlink(missing_ok=True)
            return None
        return data
    except Exception:
        return None

def _save_session(sid: str, data: Dict[str, Any]) -> None:
    try:
        _session_path(sid).write_text(json.dumps(data))
    except Exception as e:
        logger.warning(f"[coinflip] save session failed: {e}")

def _delete_session(sid: str) -> None:
    try:
        _session_path(sid).unlink(missing_ok=True)
    except Exception:
        pass

def sign_token(payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(COINFLIP_SECRET.encode(), body, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(body).decode().rstrip('=') + "." + base64.urlsafe_b64encode(sig).decode().rstrip('=')
    return token

def verify_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        body_b64, sig_b64 = parts
        pad = lambda s: s + "=" * (-len(s) % 4)
        body = base64.urlsafe_b64decode(pad(body_b64))
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        expected = hmac.new(COINFLIP_SECRET.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(body.decode())
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None

from utils.helpers import check_balance
# Service natif Python : remplace G1history.sh, G1balance.sh et gcli
# Chaîne : Squid GraphQL → SubstrateInterface RPC → gcli → G1check.sh
from services.g1_squid import (
    get_g1_history_native,
    get_g1_balance_native,
    get_g1_balance_rpc_native,
    get_g1_balances_batch,
    get_squid_urls,
    g1pub_to_ss58,
)

def convert_g1_to_zen(g1_balance: str) -> str:
    """Convertir une balance Ğ1 en ẐEN en utilisant la formule (balance - 1) * 10"""
    try:
        clean_balance = g1_balance.replace('Ğ1', '').replace('G1', '').strip()
        balance_float = float(clean_balance)
        zen_amount = max(0, (balance_float - 1) * 10)
        return f"{int(zen_amount)} Ẑ"
    except (ValueError, TypeError):
        return g1_balance

async def generate_balance_html_page(identifier: str, balance_data: Dict[str, Any]) -> HTMLResponse:
    """Générer une page HTML pour afficher les balances en utilisant le template message.html"""
    try:
        template_path = Path("templates") / "message.html"
        
        if not template_path.exists():
            raise HTTPException(status_code=500, detail="Template HTML non trouvé")
        
        with open(template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        if "@" in identifier:
            badges = []
            if "balance" in balance_data:
                badges.append(f"👛 {convert_g1_to_zen(balance_data['balance'])} Ẑ")
            if "g1pub_zencard" in balance_data:
                badges.append("💳 ZenCard")
            suffix = "  ·  ".join(badges)
            title = f"{identifier}  ·  {suffix}  ·  {timestamp}"
        else:
            title = f"{identifier[:32]}…  ·  {timestamp}" if len(identifier) > 32 else f"{identifier}  ·  {timestamp}"
        
        message_parts = []
        has_multiple_balances = "g1pub_zencard" in balance_data
        
        async def get_nostr_profile_url(email_param):
            ipfs_gateway = await get_myipfs_gateway()
            hex_pubkey = None
            if email_param and "@" in email_param:
                try:
                    from core.config import settings
                    script_path = settings.TOOLS_PATH / "search_for_this_email_in_nostr.sh"
                    import asyncio
                    process = await asyncio.create_subprocess_exec(
                        script_path, email_param,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                    except asyncio.TimeoutError:
                        try:
                            process.kill() # <-- TUE LE PROCESSUS BASH ENFANT
                        except ProcessLookupError:
                            pass
                            raise HTTPException(status_code=504, detail="timeout")
                    if process.returncode == 0:
                        last_line = stdout.decode().strip().split('\n')[-1]
                        import re
                        hex_match = re.search(r'HEX=([a-fA-F0-9]+)', last_line)
                        if hex_match:
                            hex_pubkey = hex_match.group(1)
                except Exception:
                    pass
            
            if hex_pubkey:
                return f"{ipfs_gateway}/ipns/copylaradio.com/nostr_profile_viewer.html?hex={hex_pubkey}"
            else:
                return f"{ipfs_gateway}/ipns/copylaradio.com/nostr_profile_viewer.html"
        
        email_param = identifier if "@" in identifier else balance_data.get('email', identifier)

        if not has_multiple_balances:
            zen_balance = convert_g1_to_zen(balance_data['balance'])
            nostr_url = await get_nostr_profile_url(email_param)
            g1pub_display = balance_data.get('g1pub', '')
            g1pub_html = f'<div class="g1pub">{g1pub_display[:24]}…{g1pub_display[-8:]}</div>' if g1pub_display else ''

            message_parts.append(f"""
            <div class="ucard" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); max-width: 320px; margin: 0 auto;">
                <h3>👛 MULTIPASS</h3>
                <div class="balance">{zen_balance}</div>
                <div class="unit">Ẑen</div>
                {g1pub_html}
                <a class="btn" href="{nostr_url}" target="_blank">🔗 Profil NOSTR</a>
            </div>
            """)
        else:
            nostr_url = await get_nostr_profile_url(email_param)
            message_parts.append('<div class="cards-grid">')

            if "balance" in balance_data:
                zen_balance = convert_g1_to_zen(balance_data['balance'])
                g1pub_display = balance_data.get('g1pub', '')
                g1pub_html = f'<div class="g1pub">{g1pub_display[:24]}…{g1pub_display[-8:]}</div>' if g1pub_display else ''

                message_parts.append(f"""
                <div class="ucard" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);">
                    <h3>👛 MULTIPASS</h3>
                    <div class="balance">{zen_balance}</div>
                    <div class="unit">Ẑen</div>
                    {g1pub_html}
                    <a class="btn" href="{nostr_url}" target="_blank">🔗 Profil NOSTR</a>
                </div>
                """)

            if "g1pub_zencard" in balance_data:
                # ZenCard : la valeur = ẐEN ayant transité via SOCIETY (pas le solde G1 = 1 Ğ1)
                g1pub_zc = balance_data.get('g1pub_zencard', '')
                g1pub_zc_html = f'<div class="g1pub">{g1pub_zc[:24]}…{g1pub_zc[-8:]}</div>' if g1pub_zc else ''

                message_parts.append(f"""
                <div class="ucard" style="background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);">
                    <h3>💳 ZEN Card</h3>
                    <div class="unit" style="font-size:0.85em;opacity:0.85;">ẐEN reçus via société</div>
                    {g1pub_zc_html}
                    <a class="btn" href="/check_zencard?email={email_param}&html=1" target="_blank">📊 Historique transit</a>
                </div>
                """)

            message_parts.append('</div>')
        
        message = "".join(message_parts)
        html_content = template_content.replace("_TITLE_", title).replace("_MESSAGE_", message)
        
        return HTMLResponse(content=html_content)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la génération HTML: {str(e)}")

@router.post("/zen_send", deprecated=True)
async def zen_send(request: Request):
    """Wrapper d'observabilité additif autour de _zen_send_impl : capture
    succès/échec/latence NODE + BRO (par email, résolu en lecture seule à
    partir du npub du formulaire — Starlette met le form en cache, un second
    appel à request.form() ne relit ni ne rejoue rien côté requête) sur tous
    les chemins de sortie, y compris les exceptions."""
    _obs_start = time.time()
    _obs_success = False
    _obs_status = 500
    _obs_extra: dict = {}
    _obs_email: Optional[str] = None
    try:
        result = await _zen_send_impl(request)
        _obs_status = getattr(result, "status_code", 200)
        try:
            if isinstance(result, JSONResponse):
                body = json.loads(bytes(result.body))
                if isinstance(body, dict):
                    _obs_success = bool(body.get("ok"))
                    if not _obs_success and body.get("error"):
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
        try:
            form_data = await request.form()
            npub = form_data.get("npub")
            if npub:
                is_hex = len(npub) == 64 and all(c in '0123456789abcdefABCDEF' for c in npub)
                hex_pub = npub.lower() if is_hex else npub_to_hex(npub)
                if hex_pub:
                    user_dir = find_user_directory_by_hex(hex_pub)
                    if user_dir and "@" in user_dir.name:
                        _obs_email = user_dir.name
        except Exception:
            pass
        latency_ms = (time.time() - _obs_start) * 1000
        _obs_extra["status"] = _obs_status
        log_node_event("zen_send", _obs_success, category="finance",
                        latency_ms=latency_ms, extra=_obs_extra)
        log_user_event(_obs_email, "zen_send", "zen_send", _obs_success,
                        latency_ms=latency_ms, extra=dict(_obs_extra))


async def _zen_send_impl(request: Request):
    """Send ZEN using the sender's ZEN card. Nostr authentication (NIP-42) is mandatory.

    DEPRECATED — use Kind 7 NOSTR reaction (+N content) instead.
    The relay's 7.sh write-policy plugin processes G1 payments automatically.
    """
    form_data = await request.form()
    zen = form_data.get("zen")
    g1source = form_data.get("g1source")
    g1dest = form_data.get("g1dest")
    npub = form_data.get("npub")

    if not npub or not str(npub).strip():
        raise HTTPException(status_code=400, detail="Nostr public key (npub) is required")

    if not g1dest:
        raise HTTPException(status_code=400, detail="Missing destination (g1dest)")

    sender_hex = None
    is_hex_format = len(npub) == 64 and all(c in '0123456789abcdefABCDEF' for c in npub)
    sender_hex = npub.lower() if is_hex_format else npub_to_hex(npub)
    if not sender_hex or len(sender_hex) != 64:
        raise HTTPException(status_code=400, detail="Invalid Nostr public key format")

    auth_ok = await verify_nostr_auth(sender_hex)
    if not auth_ok:
        raise HTTPException(status_code=401, detail="Nostr authentication failed (NIP-42)")

    try:
        user_dir = find_user_directory_by_hex(sender_hex)
        g1pubnostr_path = user_dir / "G1PUBNOSTR"
        if g1pubnostr_path.exists():
            with open(g1pubnostr_path, 'r') as f:
                resolved_g1source = f.read().strip()
            g1source = resolved_g1source
    except Exception:
        pass

    try:
        from core.config import settings
        marker_path = settings.ZEN_PATH / "tmp" / f"nostr_auth_ok_{sender_hex}"
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, 'w') as marker:
            marker.write(str(int(time.time())))
    except Exception:
        pass

    if not zen or not g1dest:
        raise HTTPException(status_code=400, detail="Missing required fields: zen, g1dest, npub")

    script_path = "./zen_send.sh"
    args = [zen, g1source or "", g1dest, sender_hex]

    return_code, last_line = await run_script(script_path, *args)

    try:
        script_output = last_line.strip()
        if script_output.startswith('{'):
            result = json.loads(script_output)
            if result.get("success"):
                # Limite : une partie de coinflip par joueur par jour
                today = datetime.now().strftime("%Y%m%d")
                daily_marker = Path(settings.ZEN_PATH) / "tmp" / f"coinflip_{sender_hex}_{today}"
                if daily_marker.exists():
                    return JSONResponse({
                        "ok": False,
                        "error": "Une partie par jour autorisée. Revenez demain !",
                        "type": "daily_limit"
                    })
                session_id = uuid.uuid4().hex
                exp = int(time.time()) + 300
                token = sign_token({"npub": sender_hex, "sid": session_id, "exp": exp})
                _save_session(session_id, {
                    "npub": sender_hex,
                    "consecutive": 1,
                    "paid": True,
                    "created_at": int(time.time()),
                })
                try:
                    daily_marker.touch()
                except Exception:
                    pass
                return JSONResponse({
                    "ok": True,
                    "zen_send_result": result,
                    "token": token,
                    "sid": session_id,
                    "exp": exp
                })
            else:
                return JSONResponse({
                    "ok": False,
                    "error": result.get("error", "Unknown error"),
                    "type": result.get("type", "unknown_error")
                })
        else:
            return JSONResponse({
                "ok": True,
                "html": script_output,
                "message": "Legacy response format"
            })
    except json.JSONDecodeError:
        return JSONResponse({
            "ok": False,
            "error": f"Failed to parse script output: {script_output}",
            "type": "parse_error"
        })
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "error": f"Script execution failed: {last_line.strip()}",
            "type": "execution_error"
        })

@router.get("/check_balance")
async def check_balance_route(g1pub: str, html: Optional[str] = None):
    try:
        if '@' in g1pub:
            email = g1pub
            
            if not is_safe_email(email):
                raise HTTPException(status_code=400, detail="Format d'email invalide")
            
            nostr_g1pub = None
            nostr_g1pub_path = get_safe_user_path("nostr", email, "G1PUBNOSTR")
            
            if nostr_g1pub_path and os.path.exists(nostr_g1pub_path):
                try:
                    with open(nostr_g1pub_path, 'r') as f:
                        nostr_g1pub = f.read().strip()
                except Exception:
                    pass
            
            zencard_g1pub = None
            zencard_g1pub_path = get_safe_user_path("players", email, ".g1pub")
            
            if zencard_g1pub_path and os.path.exists(zencard_g1pub_path):
                try:
                    with open(zencard_g1pub_path, 'r') as f:
                        zencard_g1pub = f.read().strip()
                except Exception:
                    pass
            
            if not nostr_g1pub and not zencard_g1pub:
                raise HTTPException(status_code=404, detail="Aucune g1pub trouvée pour cet email")
            
            result = {}
            
            if nostr_g1pub:
                try:
                    nostr_balance = await check_balance(nostr_g1pub)
                    result.update({
                        "balance": nostr_balance,
                        "g1pub": nostr_g1pub
                    })
                except Exception:
                    result.update({
                        "balance": "error",
                        "g1pub": nostr_g1pub
                    })
            
            if zencard_g1pub:
                # ZenCard : solde G1 toujours = 1 Ğ1 (PAF) — ne pas appeler check_balance.
                # La valeur réelle = ẐEN ayant transité, exposée par /check_zencard (G1zencard_history.sh).
                result["g1pub_zencard"] = zencard_g1pub
            
            return await generate_balance_html_page(email, result)
            
        else:
            if not is_safe_g1pub(g1pub):
                raise HTTPException(status_code=400, detail="Format de g1pub invalide")

            if g1pub in app_state.balance_cache:
                cached = app_state.balance_cache[g1pub]
                # Invalider le cache s'il n'a pas encore le champ zen
                if "zen" not in cached:
                    del app_state.balance_cache[g1pub]
                else:
                    return cached

            balance = await check_balance(g1pub)
            # Calcul zen : (solde_Ğ1 - 1_PAF) × 10, min 0
            zen_float = None
            try:
                clean = balance.replace('Ğ1', '').replace('G1', '').strip()
                zen_float = round(max(0.0, (float(clean) - 1.0) * 10.0), 2)
            except (ValueError, TypeError):
                pass
            result = {"balance": balance, "g1pub": g1pub}
            if zen_float is not None:
                result["zen"] = zen_float

            app_state.balance_cache[g1pub] = result
            return result
            
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erreur interne du serveur")

@router.get("/check_g1history")
async def check_g1history_route(g1pub: str, limit: int = 100):
    """Historique des transactions G1 d'un portefeuille MULTIPASS.
    Accepte un g1pub SS58/Base58 ou un email (résolution locale puis swarm).
    Retourne {"history": [...], "g1pub": "...", "total": N}
    """
    try:
        if '@' in g1pub:
            email = g1pub
            if not is_safe_email(email):
                raise HTTPException(status_code=400, detail="Format d'email invalide")
            resolved = None
            nostr_path = get_safe_user_path("nostr", email, "G1PUBNOSTR")
            if nostr_path and os.path.exists(nostr_path):
                try:
                    with open(nostr_path, 'r') as f:
                        resolved = f.read().strip()
                except Exception:
                    pass
            if not resolved:
                resolved = await _resolve_g1pubnostr_from_swarm(email)
            if not resolved:
                raise HTTPException(status_code=404, detail="g1pub introuvable pour cet email")
            g1pub = resolved

        if not is_safe_g1pub(g1pub):
            raise HTTPException(status_code=400, detail="Format g1pub invalide")

        data = await get_g1_history_native(g1pub, limit)
        history = data.get("history", [])
        return {"history": history, "g1pub": g1pub, "total": len(history)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erreur interne du serveur")

@router.get("/check_balances")
async def check_balances_route(g1pubs: str):
    """
    Balance batch pour plusieurs clés G1 (séparées par virgule, max 20).
    Utilise une seule requête Squid GraphQL via get_g1_balances_batch().
    Réponse : {"balances": {"<g1pub>": {"balance": "2.48", "zen": "14.80"}, ...}}
    """
    pubkey_list = [p.strip() for p in g1pubs.split(",") if p.strip()][:20]
    valid = [p for p in pubkey_list if is_safe_g1pub(p)]
    if not valid:
        raise HTTPException(status_code=400, detail="Aucune g1pub valide")

    batch_raw = await get_g1_balances_batch(valid)

    result = {}
    for g1pub in valid:
        b = batch_raw.get(g1pub, {"pending": 0, "blockchain": 0, "total": 0})
        centimes = b.get("total", 0)
        g1_val = centimes / 100
        zen = max(0.0, (g1_val - 1) * 10)
        result[g1pub] = {"balance": f"{g1_val:.2f}", "zen": f"{zen:.2f}"}

    return {"balances": result}


def _is_origin_mode():
    from core.config import settings
    swarm_key_path = os.path.expanduser("~/.ipfs/swarm.key")
    uplanet_name = ""
    if os.path.exists(swarm_key_path):
        with open(swarm_key_path, 'r') as f:
            lines = f.readlines()
            if lines:
                uplanet_name = lines[-1].strip()
    return not uplanet_name or uplanet_name == "0000000000000000000000000000000000000000000000000000000000000000"

def _get_oc_api_url():
    ## Priorité : OC_API depuis .env → sinon auto-detect via swarm.key
    oc_env = _get_oc_env()
    if oc_env.get("OC_API"):
        return oc_env["OC_API"]
    if _is_origin_mode():
        return "https://api-staging.opencollective.com/graphql/v2"
    return "https://api.opencollective.com/graphql/v2"

async def _resolve_g1pubnostr_from_swarm(email: str) -> str:
    """
    Résout le G1PUBNOSTR d'un email dans le swarm IPFS.

    Stratégie en 3 étapes (comme search_for_this_email_in_nostr.sh) :
      1. Cache swarm local  : ~/.zen/tmp/*/TW/{email}/G1PUBNOSTR
      2. Script Astroport   : search_for_this_email_in_nostr.sh (lookup CACHE + SWARM_DIR)
      3. IPFS gateway IPNS  : http://localhost:8080/ipns/{IPFSNODEID}/TW/{email}/G1PUBNOSTR
    Retourne le G1PUBNOSTR (str) si trouvé, sinon chaîne vide.
    """
    import glob

    home = os.path.expanduser("~")

    # 1. Swarm cache local : ~/.zen/tmp/swarm/*/TW/{email}/G1PUBNOSTR
    pattern = os.path.join(home, ".zen", "tmp", "swarm", "*", "TW", email, "G1PUBNOSTR")
    for fpath in glob.glob(pattern):
        try:
            g1pub = Path(fpath).read_text().strip()
            if g1pub:
                logger.info(f"🔍 G1PUBNOSTR trouvé en cache swarm local pour {email}: {g1pub[:12]}…")
                return g1pub
        except Exception:
            continue

    # 2. Script search_for_this_email_in_nostr.sh (lookup étendu swarm)
    from core.config import settings
    search_script = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "search_for_this_email_in_nostr.sh"
    if search_script.exists():
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(search_script), email,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(search_script.parent)
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            output = stdout.decode()
            ## Parse : "export source=… G1PUBNOSTR=XYZ …"
            m = re.search(r'G1PUBNOSTR=(\S+)', output)
            if m:
                g1pub = m.group(1)
                logger.info(f"🔍 G1PUBNOSTR trouvé via search_for_this_email_in_nostr.sh pour {email}: {g1pub[:12]}…")
                return g1pub
        except Exception as e:
            logger.warning(f"search_for_this_email_in_nostr.sh indisponible ou timeout: {e}")

    # 3. IPFS gateway — interrogation des nœuds swarm connus
    swarm_ids_dir = os.path.join(home, ".zen", "tmp")
    if os.path.isdir(swarm_ids_dir):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                for node_id in os.listdir(swarm_ids_dir):
                    ipns_url = f"http://localhost:8080/ipns/{node_id}/TW/{email}/G1PUBNOSTR"
                    try:
                        resp = await client.get(ipns_url)
                        if resp.status_code == 200:
                            g1pub = resp.text.strip()
                            if g1pub:
                                logger.info(f"🔍 G1PUBNOSTR trouvé via IPFS IPNS ({node_id[:12]}…) pour {email}")
                                return g1pub
                    except Exception:
                        continue
        except Exception:
            pass

    logger.warning(f"❌ G1PUBNOSTR introuvable dans le swarm pour {email}")
    return ""

def _get_oc_env():
    env_vars = {}
    from core.config import settings
    env_path = settings.ZEN_PATH / "workspace" / "OC2UPlanet" / ".env"
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip().strip('"').strip("'")
    return env_vars

async def _get_coop_config(key: str) -> str:
    """
    Lit une valeur depuis le DID NOSTR coopératif (kind 30800) via cooperative_config.sh.
    Permet à toutes les stations du même essaim (même swarm.key) de partager OCAPIKEY/OCSLUG
    sans avoir besoin d'un .env local.
    Retourne la valeur déchiffrée ou chaîne vide.
    """
    from core.config import settings
    coop_script = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "cooperative_config.sh"
    if not coop_script.exists():
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", 'source "$1" 2>/dev/null && coop_config_get "$2" 2>/dev/null',
            "--", str(coop_script), key,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        value = stdout.decode().strip()
        if value and proc.returncode == 0:
            logger.info(f"✅ {key} lu depuis le DID NOSTR coopératif")
            return value
    except Exception as e:
        logger.debug(f"cooperative_config.sh indisponible ou timeout pour {key}: {e}")
    return ""

def _classify_societaire(tier_slug: str) -> str:
    """Détermine le statut sociétaire depuis le slug de tier OC."""
    ts = tier_slug.lower() if tier_slug else ""
    if any(k in ts for k in ["gpu", "module-gpu", "constellation", "love-box-deluxe", "love-box-gpu"]):
        return "constellation"
    if any(k in ts for k in ["128-go", "extension-128", "satellite", "love-box-le-claude", "love-box", "lovebox"]):
        return "satellite"
    if ts:
        return "membre"
    return "inconnu"

async def _get_oc_token():
    """Retourne l'OCAPIKEY : .env local → DID NOSTR coopératif → settings."""
    oc_env = _get_oc_env()
    from core.config import settings
    token = oc_env.get("OCAPIKEY") or getattr(settings, "OCAPIKEY", "")
    if not token:
        ## Fallback : DID NOSTR coopératif (kind 30800, chiffré avec $UPLANETNAME)
        ## Partage automatique entre toutes les stations du même essaim IPFS
        token = await _get_coop_config("OCAPIKEY")
    return token

@router.post("/oc_webhook")
async def oc_webhook(request: Request):
    """Wrapper d'observabilité additif autour de _oc_webhook_impl (émission ẐEN
    déclenchée par une contribution OpenCollective) : capture succès/échec/
    latence NODE + BRO (par email, extrait a posteriori du corps de la réponse
    ou du détail de l'exception — sans dupliquer la logique de résolution
    email/tier de l'implémentation) sur tous les chemins de sortie."""
    _obs_start = time.time()
    _obs_success = False
    _obs_status = 500
    _obs_extra: dict = {}
    _obs_email: Optional[str] = None
    try:
        result = await _oc_webhook_impl(request)
        _obs_status = getattr(result, "status_code", 200)
        _obs_success = _obs_status < 400
        try:
            if isinstance(result, JSONResponse):
                body = json.loads(bytes(result.body))
                if isinstance(body, dict):
                    _obs_email = body.get("email")
                    _obs_extra["status_field"] = body.get("status")
                    if body.get("type"):
                        _obs_extra["emission_type"] = body["type"]
        except Exception:
            pass
        return result
    except HTTPException as exc:
        _obs_status = exc.status_code
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        _obs_email = detail.get("email")
        _obs_extra["error"] = detail.get("status", "HTTPException")
        raise
    except Exception:
        _obs_extra["error"] = "unhandled_exception"
        raise
    finally:
        latency_ms = (time.time() - _obs_start) * 1000
        _obs_extra["status"] = _obs_status
        log_node_event("oc_webhook", _obs_success, category="finance",
                        latency_ms=latency_ms, extra=_obs_extra)
        log_user_event(_obs_email, "oc_webhook", "oc_webhook", _obs_success,
                        latency_ms=latency_ms, extra=dict(_obs_extra))


async def _oc_webhook_impl(request: Request):
    """OpenCollective webhook endpoint for COLLECTIVE_TRANSACTION_CREATED events."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    event_type = payload.get("type", "")
    if event_type != "collective.transaction.created":
        return JSONResponse({"status": "ignored", "type": event_type})

    data = payload.get("data", {})
    tx = data.get("transaction", {})
    from_collective = data.get("fromCollective", {})

    amount_raw = tx.get("amount", 0)
    currency = tx.get("currency", "EUR")
    slug = from_collective.get("slug", "")
    name = from_collective.get("name", "")
    collective_id = payload.get("CollectiveId")
    webhook_id = payload.get("id")

    amount_eur = amount_raw / 100.0 if isinstance(amount_raw, int) and amount_raw > 99 else amount_raw

    from core.config import settings
    processed_file = settings.ZEN_PATH / "game" / "oc_webhook_processed.log"
    tx_id = f"{webhook_id}:{slug}:{amount_eur}"
    if os.path.exists(processed_file):
        with open(processed_file, 'r') as f:
            if tx_id in f.read():
                return JSONResponse({"status": "already_processed", "tx_id": tx_id})

    email = None
    tier_slug = ""
    oc_token = await _get_oc_token()
    if oc_token and slug:
        import httpx
        oc_api = _get_oc_api_url()
        query = {
            "query": """query($slug: String) {
                account(slug: $slug) {
                    emails
                    transactions(limit: 5, type: CREDIT, orderBy: {field: CREATED_AT, direction: DESC}) {
                        nodes {
                            amount { value }
                            order { tier { slug name } }
                            createdAt
                        }
                    }
                }
            }""",
            "variables": {"slug": slug}
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    oc_api,
                    json=query,
                    headers={
                        "Content-Type": "application/json",
                        "Personal-Token": oc_token
                    }
                )
                if resp.status_code == 200:
                    result = resp.json()
                    account = result.get("data", {}).get("account", {})
                    emails = account.get("emails", [])
                    if emails:
                        email = emails[0]
                    tx_nodes = account.get("transactions", {}).get("nodes", [])
                    for node in tx_nodes:
                        node_amount = node.get("amount", {}).get("value", 0)
                        order = node.get("order") or {}
                        tier = order.get("tier") or {}
                        t_slug = tier.get("slug", "")
                        if t_slug and abs(node_amount - amount_eur) < 1.0:
                            tier_slug = t_slug
                            break
                    if not tier_slug and tx_nodes:
                        first = tx_nodes[0]
                        order = (first.get("order") or {})
                        tier = (order.get("tier") or {})
                        tier_slug = tier.get("slug", "")
        except Exception:
            pass

    if not email:
        from core.config import settings
        map_file = settings.ZEN_PATH / "workspace" / "OC2UPlanet" / "data" / "slug_email_map.json"
        if os.path.exists(map_file):
            try:
                with open(map_file, 'r') as f:
                    slug_map = json.load(f)
                email = slug_map.get(slug)
            except Exception:
                pass

    if not email:
        return JSONResponse({"status": "no_email", "slug": slug}, status_code=200)

    from core.config import settings
    multipass_dir = settings.GAME_PATH / "nostr" / email

    ## Si le MULTIPASS n'est pas en local → tenter une résolution IPFS swarm
    if not os.path.isdir(multipass_dir):
        g1pubnostr = await _resolve_g1pubnostr_from_swarm(email)
        if g1pubnostr:
            ## Créer le répertoire temporaire avec le G1PUBNOSTR trouvé dans le swarm
            logger.info(f"✅ G1PUBNOSTR résolu depuis le swarm pour {email}: {g1pubnostr}")
            os.makedirs(multipass_dir, exist_ok=True)
            (multipass_dir / "G1PUBNOSTR").write_text(g1pubnostr)
        else:
            ## MULTIPASS introuvable ni localement ni dans le swarm → file d'attente
            pending_file = settings.ZEN_PATH / "game" / "oc_webhook_pending.json"
            pending_entry = {
                "email": email,
                "amount": amount_eur,
                "tier_slug": tier_slug,
                "slug": slug,
                "webhook_id": webhook_id,
                "queued_at": datetime.now().isoformat(),
                "tx_id": tx_id
            }
            os.makedirs(pending_file.parent, exist_ok=True)
            pending_list = []
            if pending_file.exists():
                try:
                    with open(pending_file, 'r') as f:
                        pending_list = json.load(f)
                except Exception:
                    pending_list = []
            if not any(p.get("tx_id") == tx_id for p in pending_list):
                pending_list.append(pending_entry)
                with open(pending_file, 'w') as f:
                    json.dump(pending_list, f, indent=2)
                logger.warning(f"⏳ OC webhook pending — MULTIPASS introuvable localement ni en swarm: {email}")
            return JSONResponse({
                "status": "pending_multipass",
                "email": email,
                "message": "MULTIPASS introuvable localement — transaction mise en file d'attente pour traitement swarm",
                "pending_file": str(pending_file)
            }, status_code=200)

    zen_amount = f"{amount_eur:.2f}"
    from core.config import settings
    astroport_path = settings.ZEN_PATH / "Astroport.ONE"
    script = os.path.join(astroport_path, "UPLANET.official.sh")

    if not os.path.isfile(script):
        return JSONResponse({"error": "UPLANET.official.sh not found"}, status_code=500)

    emission_type = "locataire"
    cmd_args = ["bash", script, "-l", email, "-m", zen_amount]

    if any(k in tier_slug for k in ["128-go", "extension-128", "satellite", "love-box-le-claude", "love-box-claude"]):
        emission_type = "societaire_satellite"
        cmd_args = ["bash", script, "-s", email, "-t", "satellite", "-m", zen_amount]
    elif any(k in tier_slug for k in ["gpu", "module-gpu", "constellation", "love-box-deluxe", "love-box-gpu"]):
        emission_type = "societaire_constellation"
        cmd_args = ["bash", script, "-s", email, "-t", "constellation", "-m", zen_amount]
    elif any(k in tier_slug for k in ["cotisation", "cloud-usage", "services-cloud"]):
        emission_type = "locataire_cloud"
    elif any(k in tier_slug for k in ["membre-resident", "soutien-mensuel"]):
        emission_type = "locataire_membre"

    try:
        import asyncio
        process = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=astroport_path
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        return_code = process.returncode

        os.makedirs(os.path.dirname(processed_file), exist_ok=True)
        with open(processed_file, 'a') as f:
            status = "OK" if return_code == 0 else "FAIL"
            f.write(f"{tx_id}:{zen_amount}:{emission_type}:{datetime.now().isoformat()}:{status}\n")

        if return_code == 0:
            return JSONResponse({
                "status": "ok",
                "email": email,
                "zen": zen_amount,
                "type": emission_type,
                "tx_id": tx_id
            })
        else:
            raise HTTPException(status_code=500, detail={
                "status": "emission_failed",
                "email": email,
                "return_code": return_code,
                "stderr": stderr.decode()[:500]
            })

    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise HTTPException(status_code=504, detail="timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/coinflip/can_play")
async def coinflip_can_play(pubkey: str):
    """
    Vérifie si un joueur peut jouer en mode live sur cette station.
    Retourne is_local=True si le répertoire NIP-42 existe localement sans flag .roaming.
    Remplace la vérification hostname côté client dans coinflip.html.
    """
    if not pubkey or len(pubkey) != 64 or not all(c in "0123456789abcdef" for c in pubkey.lower()):
        return JSONResponse({"is_local": False, "source": "invalid_pubkey"})
    try:
        user_dir = find_user_directory_by_hex(pubkey.lower())
        if not user_dir or not user_dir.exists():
            return JSONResponse({"is_local": False, "source": "unknown"})
        roaming_flag = user_dir / ".roaming"
        source_file = user_dir / "SOURCE"
        source = source_file.read_text().strip() if source_file.exists() else "LOCAL"
        if roaming_flag.exists():
            return JSONResponse({"is_local": False, "source": source})
        return JSONResponse({"is_local": True, "source": source})
    except HTTPException:
        return JSONResponse({"is_local": False, "source": "unknown"})
    except Exception as e:
        logger.warning(f"[coinflip/can_play] {e}")
        return JSONResponse({"is_local": False, "source": "error"})


@router.post("/coinflip/start", response_model=CoinflipStartResponse)
async def coinflip_start(payload: CoinflipStartRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    sess = await asyncio.to_thread(_load_session, sid) if sid else None
    if not sess:
        raise HTTPException(status_code=400, detail="Unknown session")
    exp = int(time.time()) + 300
    token = sign_token({"npub": sess["npub"], "sid": sid, "exp": exp})
    return CoinflipStartResponse(ok=True, sid=sid, exp=exp)

@router.post("/coinflip/flip", response_model=CoinflipFlipResponse)
async def coinflip_flip(payload: CoinflipFlipRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    sess = await asyncio.to_thread(_load_session, sid) if sid else None
    if not sess:
        raise HTTPException(status_code=400, detail="Unknown session")
    if not sess.get("paid"):
        raise HTTPException(status_code=402, detail="Payment required")
    result = 'Heads' if secrets.randbits(1) == 0 else 'Tails'
    if result == 'Heads':
        sess["consecutive"] = int(sess.get("consecutive", 1)) + 1
        await asyncio.to_thread(_save_session, sid, sess)
    return CoinflipFlipResponse(ok=True, sid=sid, result=result, consecutive=int(sess["consecutive"]))

@router.post("/coinflip/payout", response_model=CoinflipPayoutResponse)
async def coinflip_payout(payload: CoinflipPayoutRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    sess = await asyncio.to_thread(_load_session, sid) if sid else None
    if not sess:
        raise HTTPException(status_code=400, detail="Unknown session")
    if not sess.get("paid"):
        raise HTTPException(status_code=402, detail="Payment required")
    consecutive = int(sess.get("consecutive", 1))
    raw = 2 ** (consecutive - 1)
    zen_amount = raw
    g1_amount = f"{zen_amount / 10:.1f}"
    player_id = payload.player_id or ""
    script_path = "./zen_send.sh"
    sender_hex = data.get("npub")
    args = [str(zen_amount), "", "PLAYER", sender_hex]
    if player_id:
        args.append(player_id)
    return_code, last_line = await run_script(script_path, *args)
    if return_code != 0:
        raise HTTPException(status_code=500, detail="Payout script failed")
    await asyncio.to_thread(_delete_session, sid)
    return CoinflipPayoutResponse(ok=True, sid=sid, zen=zen_amount, g1_amount=g1_amount, tx=last_line.strip())

def generate_society_html_page(request: Request, g1pub: str, society_data: Dict[str, Any]):
    """Generate HTML page to display SOCIETY wallet transaction history using template"""
    try:
        nostr_did_data = society_data.get('nostr_did_data', [])
        has_nostr_data = len(nostr_did_data) > 0
        
        return templates.TemplateResponse(request, "society.html", {
            "g1pub": g1pub,
            "total_outgoing_zen": society_data['total_outgoing_zen'],
            "total_outgoing_g1": society_data['total_outgoing_g1'],
            "total_transfers": society_data['total_transfers'],
            "transfers": society_data['transfers'],
            "timestamp": society_data['timestamp'],
            "nostr_did_data": nostr_did_data,
            "has_nostr_data": has_nostr_data,
            "nostr_count": len(nostr_did_data)
        })
    except Exception as e:
        logger.error(f"Error generating society HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_revenue_html_page(request: Request, g1pub: str, revenue_data: Dict[str, Any]):
    """Generate HTML page to display revenue history (Chiffre d'Affaires) using template"""
    try:
        return templates.TemplateResponse(request, "revenue.html", {
            "g1pub": g1pub,
            "filter_year": revenue_data.get('filter_year', 'all'),
            "total_revenue_zen": revenue_data['total_revenue_zen'],
            "total_revenue_g1": revenue_data['total_revenue_g1'],
            "total_transactions": revenue_data['total_transactions'],
            "yearly_summary": revenue_data.get('yearly_summary', []),
            "transactions": revenue_data['transactions'],
            "timestamp": revenue_data['timestamp']
        })
    except Exception as e:
        logger.error(f"Error generating revenue HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_impots_html_page(request: Request, impots_data: Dict[str, Any]):
    """Generate HTML page to display tax provisions history (TVA + IS) using template"""
    try:
        return templates.TemplateResponse(request, "impots.html", {
            "g1pub": impots_data.get('wallet', 'N/A'),
            "total_provisions_zen": impots_data['total_provisions_zen'],
            "total_provisions_g1": impots_data['total_provisions_g1'],
            "total_transactions": impots_data['total_transactions'],
            "tva_total_zen": impots_data['breakdown']['tva']['total_zen'],
            "tva_total_g1": impots_data['breakdown']['tva']['total_g1'],
            "tva_transactions": impots_data['breakdown']['tva']['transactions'],
            "is_total_zen": impots_data['breakdown']['is']['total_zen'],
            "is_total_g1": impots_data['breakdown']['is']['total_g1'],
            "is_transactions": impots_data['breakdown']['is']['transactions'],
            "provisions": impots_data['provisions']
        })
    except Exception as e:
        logger.error(f"Error generating impots HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_zencard_html_page(request: Request, email: str, zencard_data: Dict[str, Any]):
    """Generate HTML page to display ZEN Card social shares history using template"""
    try:
        return templates.TemplateResponse(request, "zencard_api.html", {
            "zencard_email": zencard_data.get('zencard_email', email),
            "zencard_g1pub": zencard_data.get('zencard_g1pub', 'N/A'),
            "filter_years": zencard_data.get('filter_years', 3),
            "filter_period": zencard_data.get('filter_period', 'Dernières 3 années'),
            "total_received_g1": zencard_data.get('total_received_g1', 0),
            "total_received_zen": zencard_data.get('total_received_zen', 0),
            "valid_balance_g1": zencard_data.get('valid_balance_g1', 0),
            "valid_balance_zen": zencard_data.get('valid_balance_zen', 0),
            "total_transfers": zencard_data.get('total_transfers', 0),
            "valid_transfers": zencard_data.get('valid_transfers', 0),
            "transfers": zencard_data.get('transfers', []),
            "timestamp": zencard_data.get('timestamp', '')
        })
    except Exception as e:
        logger.error(f"Error generating ZEN Card HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

@router.get("/check_society")
async def check_society_route(request: Request, html: Optional[str] = None, nostr: Optional[str] = None):
    """Check transaction history of SOCIETY wallet to see capital contributions"""
    try:
        from core.config import settings
        script_path = settings.TOOLS_PATH / "G1society.sh"
        
        cmd = [script_path]
        if nostr is not None:
            cmd.append("--nostr")
        
        import asyncio
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="Transaction history retrieval timeout")

        if process.returncode != 0:
            raise ValueError(f"Error in G1society.sh: {stderr.decode()}")

        from utils.helpers import safe_json_load
        try:
            society_data = safe_json_load(stdout.decode().strip())
        except ValueError as e:
            raise ValueError(f"Invalid JSON from G1society.sh: {e}")

        if "error" in society_data:
            raise HTTPException(status_code=500, detail=society_data['error'])

        if html is not None:
            g1pub = society_data.get("g1pub", "N/A")
            return generate_society_html_page(request, g1pub, society_data)

        return society_data

    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Transaction history retrieval timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

# ─────────────────────────────────────────────────────────────────────────────
# CHECK_OC_MEMBER  —  Vérifie l'inscription OpenCollective d'un email
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/check_oc_member")
async def check_oc_member(email: str):
    """Vérifie si un email est inscrit sur OpenCollective.

    Stratégie :
      1. Cache local slug_email_map.json + tx.json  (rapide, sans réseau)
      2. Requête live OC GraphQL                    (si non trouvé localement)
    Retourne : {is_member, tier_slug, sociétaire_status, name, ...}
    """
    email_lc = email.lower().strip()
    if not is_safe_email(email_lc):
        raise HTTPException(status_code=400, detail="Email invalide")

    cache_key = f"oc_member_{email_lc}"
    if cache_key in _oc_member_cache:
        return JSONResponse(_oc_member_cache[cache_key])

    from core.config import settings

    # ── 1. Cache local : slug_email_map.json ─────────────────────────────────
    slug_found = None
    map_file = settings.ZEN_PATH / "workspace" / "OC2UPlanet" / "data" / "slug_email_map.json"
    if map_file.exists():
        try:
            slug_map = json.loads(map_file.read_text())
            for s, em in slug_map.items():
                if em.lower() == email_lc:
                    slug_found = s
                    break
        except Exception:
            pass

    # ── 2. Tier depuis tx.json ────────────────────────────────────────────────
    tier_slug, tier_name, amount, member_name, last_tx = "", "", 0.0, "", ""
    tx_file = settings.ZEN_PATH / "workspace" / "OC2UPlanet" / "data" / "tx.json"
    if slug_found and tx_file.exists():
        try:
            nodes = json.loads(tx_file.read_text()) \
                .get("data", {}).get("account", {}) \
                .get("transactions", {}).get("nodes", [])
            for node in nodes:
                fa = node.get("fromAccount", {})
                if fa.get("slug") == slug_found or \
                        email_lc in [e.lower() for e in fa.get("emails", [])]:
                    member_name  = fa.get("name", "")
                    amount       = node.get("amount", {}).get("value", 0)
                    tier         = (node.get("order") or {}).get("tier") or {}
                    tier_slug    = tier.get("slug", "")
                    tier_name    = tier.get("name", "")
                    last_tx      = node.get("createdAt", "")
                    break
        except Exception:
            pass

    if slug_found:
        result = {
            "is_member": True, "email": email_lc, "slug": slug_found,
            "name": member_name, "tier_slug": tier_slug, "tier_name": tier_name,
            "societaire_status": _classify_societaire(tier_slug),
            "amount": amount, "last_contribution": last_tx, "source": "cache_local"
        }
        _oc_member_cache[cache_key] = result
        return JSONResponse(result)

    # ── 3. Requête live OC GraphQL ────────────────────────────────────────────
    oc_token = await _get_oc_token()
    if not oc_token:
        return JSONResponse({"is_member": False, "email": email_lc,
                             "message": "Données OC non disponibles sur cette station"})

    oc_env  = _get_oc_env()
    oc_api  = _get_oc_api_url()
    oc_slug = oc_env.get("OCSLUG", "") or getattr(settings, "OCSLUG", "") \
              or await _get_coop_config("OCSLUG")
    if not oc_slug:
        return JSONResponse({"is_member": False, "email": email_lc,
                             "message": "OCSLUG non configuré sur cette station"})

    query = {
        "query": """query($slug: String) {
            account(slug: $slug) {
                members(role: BACKER, limit: 200) {
                    nodes { account { name slug emails } }
                }
                transactions(limit: 100, type: CREDIT) {
                    nodes {
                        fromAccount { name slug emails }
                        amount { value }
                        order { tier { slug name } }
                        createdAt
                    }
                }
            }
        }""",
        "variables": {"slug": oc_slug}
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                oc_api, json=query,
                headers={"Content-Type": "application/json", "Personal-Token": oc_token}
            )
            if resp.status_code == 200:
                account = resp.json().get("data", {}).get("account", {})
                # Chercher dans la liste des membres
                found_acc = None
                for node in account.get("members", {}).get("nodes", []):
                    acc = node.get("account", {})
                    if email_lc in [e.lower() for e in acc.get("emails", [])]:
                        found_acc = acc; break
                if not found_acc:
                    result = {"is_member": False, "email": email_lc,
                              "message": "Non inscrit sur OpenCollective"}
                    _oc_member_cache[cache_key] = result
                    return JSONResponse(result)
                # Trouver le tier dans les transactions
                for node in account.get("transactions", {}).get("nodes", []):
                    fa = node.get("fromAccount", {})
                    if email_lc in [e.lower() for e in fa.get("emails", [])]:
                        member_name  = fa.get("name", "")
                        tier         = (node.get("order") or {}).get("tier") or {}
                        tier_slug    = tier.get("slug", "")
                        tier_name    = tier.get("name", "")
                        amount       = node.get("amount", {}).get("value", 0)
                        last_tx      = node.get("createdAt", "")
                        break
                result = {
                    "is_member": True, "email": email_lc,
                    "slug": found_acc.get("slug", ""),
                    "name": member_name or found_acc.get("name", ""),
                    "tier_slug": tier_slug, "tier_name": tier_name,
                    "societaire_status": _classify_societaire(tier_slug),
                    "amount": amount, "last_contribution": last_tx, "source": "live_oc"
                }
                _oc_member_cache[cache_key] = result
                return JSONResponse(result)
    except Exception as e:
        logger.warning(f"[check_oc_member] OC GraphQL KO: {e}")

    return JSONResponse({"is_member": False, "email": email_lc,
                         "message": "Erreur lors de la vérification OpenCollective"})

# ─────────────────────────────────────────────────────────────────────────────
# CONSTELLATION_REGISTER  —  Diffuse un kind 30078 sur les relais pairs
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/constellation_register")
async def constellation_register(request: Request):
    """Publie un événement NOSTR signé (kind 30078) sur les relais constellation pairs.

    Corps JSON : {"event": {id, pubkey, kind, created_at, tags, content, sig}}
    Découverte des pairs : ~/.zen/tmp/*/12345.json → champ myRELAY
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON invalide")

    event = body.get("event")
    if not event or not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Champ 'event' manquant")

    required_fields = {"id", "pubkey", "created_at", "kind", "tags", "content", "sig"}
    if not required_fields.issubset(event.keys()):
        raise HTTPException(status_code=400, detail="Événement NOSTR incomplet")
    if event.get("kind") != 30078:
        raise HTTPException(status_code=400, detail="Seuls les kind 30078 sont acceptés")

    # ── Découvrir les relais pairs depuis les fichiers station JSON ───────────
    from core.config import settings
    import glob, websockets

    peer_relays: set = set()
    for fpath in glob.glob(str(settings.ZEN_PATH / "tmp" / "*" / "12345.json")):
        try:
            data = json.loads(Path(fpath).read_text())
            relay = data.get("myRELAY") or data.get("relay") or data.get("RELAY", "")
            if relay and relay.startswith("wss://") \
                    and "127.0.0.1" not in relay and "localhost" not in relay:
                peer_relays.add(relay)
        except Exception:
            pass

    if not peer_relays:
        return JSONResponse({"published_count": 0, "total_peers": 0,
                             "message": "Aucun pair constellation découvert"})

    msg = json.dumps(["EVENT", event])

    async def _publish_one(relay_url: str) -> bool:
        try:
            async with websockets.connect(
                relay_url, open_timeout=5, close_timeout=3,
                extra_headers={"User-Agent": "UPassport/1.0"}
            ) as ws:
                await asyncio.wait_for(ws.send(msg), timeout=5)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    r = json.loads(raw)
                    return r[0] == "OK" and r[2] is True
                except Exception:
                    return True  # Envoyé sans confirmation — considéré OK
        except Exception as e:
            logger.debug(f"[constellation_register] {relay_url} KO: {e}")
            return False

    peers_list = list(peer_relays)[:8]  # Limiter à 8 pairs pour éviter la saturation
    results = await asyncio.gather(*[_publish_one(r) for r in peers_list], return_exceptions=True)
    published = sum(1 for r in results if r is True)

    logger.info(f"[constellation_register] kind=30078 id={event.get('id','')[:8]}… "
                f"→ {published}/{len(peers_list)} pairs")
    return JSONResponse({
        "published_count": published,
        "total_peers": len(peers_list),
        "relays": peers_list
    })

@router.get("/check_revenue")
async def check_revenue_route(request: Request, html: Optional[str] = None, year: Optional[str] = None):
    """Check revenue history from ZENCOIN transactions (Chiffre d'Affaires)"""
    try:
        if year and year != "all" and not re.match(r"^\d{4}$", year):
             raise HTTPException(status_code=400, detail="Invalid year format")

        from core.config import settings
        script_path = settings.TOOLS_PATH / "G1revenue.sh"
        
        year_filter = year if year else "all"
        import asyncio
        process = await asyncio.create_subprocess_exec(
            script_path, year_filter,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="Revenue history retrieval timeout")

        if process.returncode != 0:
            raise ValueError(f"Error in G1revenue.sh: {stderr.decode()}")

        from utils.helpers import safe_json_load
        try:
            revenue_data = safe_json_load(stdout.decode().strip())
        except ValueError as e:
            raise ValueError(f"Invalid JSON from G1revenue.sh: {e}")

        if "error" in revenue_data:
            raise HTTPException(status_code=500, detail=revenue_data['error'])

        if html is not None:
            g1pub = revenue_data.get("g1pub", "N/A")
            return generate_revenue_html_page(request, g1pub, revenue_data)

        return revenue_data

    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Revenue history retrieval timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@router.get("/check_zencard")
async def check_zencard_route(request: Request, email: str, html: Optional[str] = None):
    """Check ZEN Card social shares history for a given email"""
    try:
        if not email:
            raise HTTPException(status_code=400, detail="Email parameter is required")
        
        if not is_safe_email(email):
            raise HTTPException(status_code=400, detail="Invalid email format")

        from core.config import settings
        script_path = settings.TOOLS_PATH / "G1zencard_history.sh"
        import asyncio
        process = await asyncio.create_subprocess_exec(
            script_path, email, "true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="ZEN Card history retrieval timeout")

        from utils.helpers import safe_json_load
        try:
            zencard_data = safe_json_load(stdout.decode().strip())
        except ValueError as e:
            raise ValueError(f"Invalid JSON from G1zencard_history.sh: {e}")
        
        if "error" in zencard_data:
            error_msg = zencard_data.get('error', 'Unknown error')
            # ZenCard introuvable = utilisateur constellation (home station différente)
            # → retourner 200 avec données vides plutôt qu'un 404 bloquant
            if "not found" in error_msg.lower():
                constellation_data = {
                    "zencard_email": email,
                    "zencard_g1pub": "",
                    "transfers": [],
                    "total_received_zen": 0,
                    "total_received_g1": 0,
                    "total_transfers": 0,
                    "valid_transfers": 0,
                    "valid_balance_zen": 0,
                    "valid_balance_g1": 0,
                    "constellation": True,
                    "filter_period": "ZenCard introuvable sur cette station"
                }
                if html is not None:
                    return generate_zencard_html_page(request, email, constellation_data)
                return constellation_data
            raise HTTPException(status_code=500, detail=error_msg)

        if html is not None:
            return generate_zencard_html_page(request, email, zencard_data)

        return zencard_data
        
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="ZEN Card history retrieval timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@router.get("/check_impots")
async def check_impots_route(request: Request, html: Optional[str] = None):
    """Check tax provisions history (TVA + IS)"""
    try:
        from core.config import settings
        script_path = settings.TOOLS_PATH / "G1impots.sh"
        
        if not os.path.exists(script_path):
            raise HTTPException(status_code=500, detail="G1impots.sh script not found")
        
        import asyncio
        process = await asyncio.create_subprocess_exec(
            script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="Tax provisions retrieval timeout")

        if process.returncode != 0:
            raise ValueError(f"Error in G1impots.sh: return code {process.returncode}")
        
        if not stdout or not stdout.decode().strip():
            impots_data = {
                "wallet": "N/A",
                "total_provisions_g1": 0,
                "total_provisions_zen": 0,
                "total_transactions": 0,
                "breakdown": {
                    "tva": {"total_g1": 0, "total_zen": 0, "transactions": 0, "description": "TVA collectée sur locations ZENCOIN (20%)"},
                    "is": {"total_g1": 0, "total_zen": 0, "transactions": 0, "description": "Impôt sur les Sociétés provisionné (15% ou 25%)"}
                },
                "provisions": []
            }
        else:
            from utils.helpers import safe_json_load
            try:
                impots_data = safe_json_load(stdout.decode().strip())
            except ValueError as e:
                raise ValueError(f"Invalid JSON from G1impots.sh: {e}")
        
        if html is not None:
            return generate_impots_html_page(request, impots_data)
        
        return impots_data
        
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Tax provisions retrieval timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
