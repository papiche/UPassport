import os
import re
import json
import time
import uuid
import asyncio
import logging
import subprocess
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from utils.helpers import run_script, get_myipfs_gateway, get_env_from_mysh
from utils.security import (
    find_user_directory_by_hex,
    is_safe_email,
    is_safe_g1pub,
    get_safe_user_path
)
from services.nostr import verify_nostr_auth
from utils.crypto import npub_to_hex
from models.schemas import (
    CoinflipStartRequest, CoinflipStartResponse,
    CoinflipFlipRequest, CoinflipFlipResponse,
    CoinflipPayoutRequest, CoinflipPayoutResponse
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# --- Coinflip server-authoritative state ---
import base64
import hmac
import hashlib
import secrets

from core.config import settings
COINFLIP_SECRET = settings.COINFLIP_SECRET
COINFLIP_SESSIONS: Dict[str, Dict[str, Any]] = {}

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

def convert_g1_to_zen(g1_balance: str) -> str:
    """Convertir une balance Ğ1 en ẐEN en utilisant la formule (balance - 1) * 10"""
    try:
        clean_balance = g1_balance.replace('Ğ1', '').replace('G1', '').strip()
        balance_float = float(clean_balance)
        zen_amount = (balance_float - 1) * 10
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
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if "@" in identifier:
            title_parts = [f"{timestamp} - {identifier}"]
            if "balance" in balance_data:
                zen_balance = convert_g1_to_zen(balance_data['balance'])
                title_parts.append(f"👛 {zen_balance}")
            if "balance_zencard" in balance_data:
                title_parts.append(f"💳")
            title = " / ".join(title_parts)
        else:
            title = f"{timestamp} - {identifier}"
        
        message_parts = []
        has_multiple_balances = "balance_zencard" in balance_data
        
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
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
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
        
        if not has_multiple_balances:
            zen_balance = convert_g1_to_zen(balance_data['balance'])
            email_param = identifier if "@" in identifier else balance_data.get('email', identifier)
            nostr_url = await get_nostr_profile_url(email_param)
            
            message_parts.append(f"""
            <div style="text-align: center; margin: 10px 0; padding: 15px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 12px; color: white; box-shadow: 0 4px 16px rgba(0,0,0,0.2); max-width: 300px; margin-left: auto; margin-right: auto;">
                <h2 style="margin: 0 0 8px 0; font-size: 1.2em;">👛 MULTIPASS</h2>
                <div style="font-size: 1.6em; font-weight: bold; margin: 8px 0;">{zen_balance}</div>
                <a href='{nostr_url}' target='_blank' style='color: #fff; text-decoration: none; background: rgba(255,255,255,0.2); padding: 6px 12px; border-radius: 15px; display: inline-block; margin-top: 8px; font-size: 0.85em;'>🔗 Profil MULTIPASS</a>
            </div>
            """)
        else:
            message_parts.append("""
            <div style="display: flex; gap: 15px; justify-content: center; flex-wrap: wrap; margin: 10px 0; max-width: 600px; margin-left: auto; margin-right: auto;">
            """)
            
            if "balance" in balance_data:
                zen_balance = convert_g1_to_zen(balance_data['balance'])
                email_param = identifier if "@" in identifier else balance_data.get('email', identifier)
                nostr_url = await get_nostr_profile_url(email_param)
                
                message_parts.append(f"""
                <div style="flex: 1; min-width: 200px; text-align: center; padding: 15px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 12px; color: white; box-shadow: 0 6px 24px rgba(0,0,0,0.2);">
                    <h3 style="margin: 0 0 10px 0; font-size: 1.1em;">MULTIPASS 👛</h3>
                    <div style="font-size: 1.4em; font-weight: bold; margin: 6px 0;">{zen_balance}</div>
                    <a href='{nostr_url}' target='_blank' style='color: #fff; text-decoration: none; background: rgba(255,255,255,0.2); padding: 4px 8px; border-radius: 10px; display: inline-block; margin-top: 5px; font-size: 0.8em;'>🔗 Profil NOSTR</a>
                </div>
                """)
            
            if "balance_zencard" in balance_data:
                zen_balance_zencard = convert_g1_to_zen(balance_data['balance_zencard'])
                email_param = identifier if "@" in identifier else balance_data.get('email', identifier)
                
                message_parts.append(f"""
                <div style="flex: 1; min-width: 180px; text-align: center; padding: 15px; background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); border-radius: 12px; color: white; box-shadow: 0 4px 16px rgba(0,0,0,0.2);">
                    <h3 style="margin: 0 0 15px 0; font-size: 1.1em;">💳 ZEN Card</h3>
                    <a href='/check_zencard?email={email_param}&html=1' target='_blank' style='color: #fff; text-decoration: none; background: rgba(255,255,255,0.2); padding: 6px 12px; border-radius: 10px; display: inline-block; margin-top: 5px; font-size: 0.8em;'>📊 Historique</a>
                </div>
                """)
            
            message_parts.append("</div>")
        
        message = "".join(message_parts)
        html_content = template_content.replace("_TITLE_", title).replace("_MESSAGE_", message)
        
        return HTMLResponse(content=html_content)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la génération HTML: {str(e)}")

@router.post("/zen_send")
async def zen_send(request: Request):
    """Send ZEN using the sender's ZEN card. Nostr authentication (NIP-42) is mandatory."""
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
                session_id = uuid.uuid4().hex
                exp = int(time.time()) + 300
                token = sign_token({"npub": sender_hex, "sid": session_id, "exp": exp})
                COINFLIP_SESSIONS[session_id] = {
                    "npub": sender_hex,
                    "consecutive": 1,
                    "paid": True,
                    "created_at": int(time.time()),
                }
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
                try:
                    zencard_balance = await check_balance(zencard_g1pub)
                    result.update({
                        "g1pub_zencard": zencard_g1pub,
                        "balance_zencard": zencard_balance
                    })
                except Exception:
                    result.update({
                        "g1pub_zencard": zencard_g1pub,
                        "balance_zencard": "error"
                    })
            
            return await generate_balance_html_page(email, result)
            
        else:
            if not is_safe_g1pub(g1pub):
                raise HTTPException(status_code=400, detail="Format de g1pub invalide")
            
            balance = await check_balance(g1pub)
            result = {"balance": balance, "g1pub": g1pub}
            
            return result
            
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erreur interne du serveur")

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

    # 1. Swarm cache local : ~/.zen/tmp/*/TW/{email}/G1PUBNOSTR
    pattern = os.path.join(home, ".zen", "tmp", "*", "TW", email, "G1PUBNOSTR")
    for fpath in glob.glob(pattern):
        try:
            g1pub = Path(fpath).read_text().strip()
            if g1pub:
                logging.info(f"🔍 G1PUBNOSTR trouvé en cache swarm local pour {email}: {g1pub[:12]}…")
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
                logging.info(f"🔍 G1PUBNOSTR trouvé via search_for_this_email_in_nostr.sh pour {email}: {g1pub[:12]}…")
                return g1pub
        except Exception as e:
            logging.warning(f"search_for_this_email_in_nostr.sh indisponible ou timeout: {e}")

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
                                logging.info(f"🔍 G1PUBNOSTR trouvé via IPFS IPNS ({node_id[:12]}…) pour {email}")
                                return g1pub
                    except Exception:
                        continue
        except Exception:
            pass

    logging.warning(f"❌ G1PUBNOSTR introuvable dans le swarm pour {email}")
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

def _get_coop_config(key: str) -> str:
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
        result = subprocess.run(
            ["bash", "-c", f'source "{coop_script}" 2>/dev/null && coop_config_get "{key}" 2>/dev/null'],
            capture_output=True, text=True, timeout=15
        )
        value = result.stdout.strip()
        if value and result.returncode == 0:
            logging.info(f"✅ {key} lu depuis le DID NOSTR coopératif")
            return value
    except Exception as e:
        logging.debug(f"cooperative_config.sh indisponible ou timeout pour {key}: {e}")
    return ""

def _get_oc_token():
    """Retourne l'OCAPIKEY : .env local → DID NOSTR coopératif → settings."""
    oc_env = _get_oc_env()
    from core.config import settings
    token = oc_env.get("OCAPIKEY") or getattr(settings, "OCAPIKEY", "")
    if not token:
        ## Fallback : DID NOSTR coopératif (kind 30800, chiffré avec $UPLANETNAME)
        ## Partage automatique entre toutes les stations du même essaim IPFS
        token = _get_coop_config("OCAPIKEY")
    return token

@router.post("/oc_webhook")
async def oc_webhook(request: Request):
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
    processed_file = settings.ZEN_PATH / "tmp" / "oc_webhook_processed.log"
    tx_id = f"{webhook_id}:{slug}:{amount_eur}"
    if os.path.exists(processed_file):
        with open(processed_file, 'r') as f:
            if tx_id in f.read():
                return JSONResponse({"status": "already_processed", "tx_id": tx_id})

    email = None
    tier_slug = ""
    oc_token = _get_oc_token()
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
            logging.info(f"✅ G1PUBNOSTR résolu depuis le swarm pour {email}: {g1pubnostr}")
            os.makedirs(multipass_dir, exist_ok=True)
            (multipass_dir / "G1PUBNOSTR").write_text(g1pubnostr)
        else:
            ## MULTIPASS introuvable ni localement ni dans le swarm → file d'attente
            pending_file = settings.ZEN_PATH / "tmp" / "oc_webhook_pending.json"
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
                logging.warning(f"⏳ OC webhook pending — MULTIPASS introuvable localement ni en swarm: {email}")
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

    if any(k in tier_slug for k in ["128-go", "extension-128", "satellite"]):
        emission_type = "societaire_satellite"
        cmd_args = ["bash", script, "-s", email, "-t", "satellite", "-m", zen_amount]
    elif any(k in tier_slug for k in ["gpu", "module-gpu", "constellation"]):
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
        raise HTTPException(status_code=504, detail="timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/coinflip/start", response_model=CoinflipStartResponse)
async def coinflip_start(payload: CoinflipStartRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    if not sid or sid not in COINFLIP_SESSIONS:
        raise HTTPException(status_code=400, detail="Unknown session")
    sess = COINFLIP_SESSIONS[sid]
    exp = int(time.time()) + 300
    token = sign_token({"npub": sess["npub"], "sid": sid, "exp": exp})
    return CoinflipStartResponse(ok=True, sid=sid, exp=exp)

@router.post("/coinflip/flip", response_model=CoinflipFlipResponse)
async def coinflip_flip(payload: CoinflipFlipRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    if not sid or sid not in COINFLIP_SESSIONS:
        raise HTTPException(status_code=400, detail="Unknown session")
    sess = COINFLIP_SESSIONS[sid]
    if not sess.get("paid"):
        raise HTTPException(status_code=402, detail="Payment required")
    result = 'Heads' if secrets.randbits(1) == 0 else 'Tails'
    if result == 'Heads':
        sess["consecutive"] = int(sess.get("consecutive", 1)) + 1
    return CoinflipFlipResponse(ok=True, sid=sid, result=result, consecutive=int(sess["consecutive"]))

@router.post("/coinflip/payout", response_model=CoinflipPayoutResponse)
async def coinflip_payout(payload: CoinflipPayoutRequest):
    data = verify_token(payload.token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sid = data.get("sid")
    if not sid or sid not in COINFLIP_SESSIONS:
        raise HTTPException(status_code=400, detail="Unknown session")
    sess = COINFLIP_SESSIONS[sid]
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
    try:
        del COINFLIP_SESSIONS[sid]
    except Exception:
        pass
    return CoinflipPayoutResponse(ok=True, sid=sid, zen=zen_amount, g1_amount=g1_amount, tx=last_line.strip())

def generate_society_html_page(request: Request, g1pub: str, society_data: Dict[str, Any]):
    """Generate HTML page to display SOCIETY wallet transaction history using template"""
    try:
        nostr_did_data = society_data.get('nostr_did_data', [])
        has_nostr_data = len(nostr_did_data) > 0
        
        return templates.TemplateResponse("society.html", {
            "request": request,
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
        logging.error(f"Error generating society HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_revenue_html_page(request: Request, g1pub: str, revenue_data: Dict[str, Any]):
    """Generate HTML page to display revenue history (Chiffre d'Affaires) using template"""
    try:
        return templates.TemplateResponse("revenue.html", {
            "request": request,
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
        logging.error(f"Error generating revenue HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_impots_html_page(request: Request, impots_data: Dict[str, Any]):
    """Generate HTML page to display tax provisions history (TVA + IS) using template"""
    try:
        return templates.TemplateResponse("impots.html", {
            "request": request,
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
        logging.error(f"Error generating impots HTML page: {e}")
        raise HTTPException(status_code=500, detail="Error generating HTML page")

def generate_zencard_html_page(request: Request, email: str, zencard_data: Dict[str, Any]):
    """Generate HTML page to display ZEN Card social shares history using template"""
    try:
        return templates.TemplateResponse("zencard_api.html", {
            "request": request,
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
        logging.error(f"Error generating ZEN Card HTML page: {e}")
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
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        
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

@router.get("/check_revenue")
async def check_revenue_route(request: Request, html: Optional[str] = None, year: Optional[str] = None):
    """Check revenue history from ZENCOIN transactions (Chiffre d'Affaires)"""
    try:
        from core.config import settings
        script_path = settings.TOOLS_PATH / "G1revenue.sh"
        
        year_filter = year if year else "all"
        import asyncio
        process = await asyncio.create_subprocess_exec(
            script_path, year_filter,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        
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
        
        from core.config import settings
        script_path = settings.TOOLS_PATH / "G1zencard_history.sh"
        import asyncio
        process = await asyncio.create_subprocess_exec(
            script_path, email, "true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        
        from utils.helpers import safe_json_load
        try:
            zencard_data = safe_json_load(stdout.decode().strip())
        except ValueError as e:
            raise ValueError(f"Invalid JSON from G1zencard_history.sh: {e}")
        
        if "error" in zencard_data:
            error_msg = zencard_data.get('error', 'Unknown error')
            status_code = 404 if "not found" in error_msg.lower() or "not configured" in error_msg.lower() else 500
            raise HTTPException(status_code=status_code, detail=error_msg)
        
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
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        
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
