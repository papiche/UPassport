import asyncio
import json
import logging
import os
logger = logging.getLogger(__name__)
from typing import Optional, List
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from core.config import settings
from services.nostr import require_nostr_auth
from utils.crypto import npub_to_hex, hex_to_npub
from utils.helpers import render_page

router = APIRouter()


def _get_node_and_captain_hex() -> tuple:
    """Résout (node_hex, captain_hex) — même logique que admin_captain_info."""
    node_hex = ""
    captain_hex = ""
    secret_file = Path.home() / ".zen" / "game" / "secret.nostr"
    if secret_file.exists():
        try:
            content = secret_file.read_text()
            for part in content.replace(";", "\n").splitlines():
                part = part.strip()
                if part.startswith("HEX="):
                    node_hex = part[4:].strip()
                    break
        except Exception:
            pass
    if node_hex:
        for json_file in (Path.home() / ".zen" / "tmp").glob("*/12345.json"):
            try:
                data = json.loads(json_file.read_text())
                if data.get("NODEHEX") == node_hex:
                    captain_hex = data.get("captainHEX", "") or node_hex
                    break
            except Exception:
                pass
    return node_hex, (captain_hex or node_hex)


def _get_uplanetname() -> str:
    """Lit UPLANETNAME depuis ~/.ipfs/swarm.key (dernière ligne)."""
    swarm_key_path = os.path.expanduser("~/.ipfs/swarm.key")
    try:
        if os.path.exists(swarm_key_path):
            with open(swarm_key_path, 'r') as f:
                lines = f.readlines()
                if lines:
                    return lines[-1].strip()
    except Exception:
        pass
    return "0000000000000000000000000000000000000000000000000000000000000000"


def _validate_uplanetname(submitted: str) -> bool:
    """Valide le UPLANETNAME soumis contre la swarm.key locale."""
    if not submitted or len(submitted) != 64:
        return False
    try:
        int(submitted, 16)
    except ValueError:
        return False
    return submitted.lower() == _get_uplanetname().lower()

@router.get("/nostr", summary="NOSTR Page", description="Route NOSTR avec support de différents types de templates.")
async def get_nostr(request: Request, type: str = "default"):
    """
    Route NOSTR avec support de différents types de templates
    """
    try:
        if type not in ["default", "uplanet"]:
            raise HTTPException(
                status_code=400, 
                detail=f"Type invalide: '{type}'. Types supportés: 'default', 'uplanet'"
            )
        
        if type == "default":
            template_name = "nostr.html"
        elif type == "uplanet":
            template_name = "nostr_uplanet.html"
        
        logger.info(f"Serving NOSTR template: {template_name} (type={type})")
        
        return render_page(request, template_name)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erreur lors du chargement du template NOSTR: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Erreur interne lors du chargement du template: {str(e)}"
        )

import json
import time
import re
import asyncio
import subprocess
from fastapi import Form
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from services.nostr import analyze_n2_network
from models.schemas import N2NetworkResponse

templates = Jinja2Templates(directory="templates")

@router.get("/api/getN2", response_model=N2NetworkResponse)
async def get_n2_network(
    request: Request,
    hex: str,
    range: str = "default",
    output: str = "json"
):
    """Analyser le réseau N2 (amis d'amis) d'une clé publique NOSTR"""
    try:
        if not hex or len(hex) != 64:
            raise HTTPException(
                status_code=400,
                detail="Paramètre 'hex' requis: clé publique hexadécimale de 64 caractères"
            )
        
        try:
            int(hex, 16)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Format hexadécimal invalide pour le paramètre 'hex'"
            )
        
        if range not in ["default", "full"]:
            raise HTTPException(
                status_code=400,
                detail="Paramètre 'range' doit être 'default' ou 'full'"
            )
        
        if output not in ["json", "html"]:
            raise HTTPException(
                status_code=400,
                detail="Paramètre 'output' doit être 'json' ou 'html'"
            )
        
        logger.info(f"Analyse N2 pour {hex[:12]}... (range={range}, output={output})")
        
        network_data = await analyze_n2_network(hex, range)
        
        if output == "html":
            serializable_data = {
                "center_pubkey": network_data["center_pubkey"],
                "total_n1": network_data["total_n1"],
                "total_n2": network_data["total_n2"],
                "total_nodes": network_data["total_nodes"],
                "range_mode": network_data["range_mode"],
                "nodes": [node.dict() for node in network_data["nodes"]],
                "connections": network_data["connections"],
                "timestamp": network_data["timestamp"],
                "processing_time_ms": network_data["processing_time_ms"]
            }
            
            return templates.TemplateResponse(
                "n2.html",
                {
                    "request": request,
                    "network_data": json.dumps(serializable_data),
                    "center_pubkey": hex,
                    "range_mode": range,
                    "total_n1": network_data["total_n1"],
                    "total_n2": network_data["total_n2"],
                    "total_nodes": network_data["total_nodes"],
                    "processing_time": network_data["processing_time_ms"]
                }
            )
        
        return N2NetworkResponse(**network_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erreur lors de l'analyse N2: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'analyse: {str(e)}")

from pydantic import BaseModel
from utils.helpers import as_form
from fastapi import Depends

@as_form
class SendMsgForm(BaseModel):
    friendEmail: str
    friendName: str = ""
    yourName: str = ""
    personalMessage: str = ""
    memberInfo: str = ""
    relation: str = ""
    pubkeyUpassport: str = ""
    ulat: str = ""
    ulon: str = ""
    pubkey: str = ""
    uid: str = ""

@router.post("/sendmsg")
async def send_invitation_message(
    form_data: SendMsgForm = Depends(SendMsgForm.as_form)
):
    friendEmail = form_data.friendEmail
    friendName = form_data.friendName
    yourName = form_data.yourName
    personalMessage = form_data.personalMessage
    memberInfo = form_data.memberInfo
    relation = form_data.relation
    pubkeyUpassport = form_data.pubkeyUpassport
    ulat = form_data.ulat
    ulon = form_data.ulon
    pubkey = form_data.pubkey
    uid = form_data.uid
    """Envoyer une invitation UPlanet à un ami via email"""
    try:
        logger.info(f"Invitation UPlanet pour: {friendEmail} de la part de: {yourName}")
        
        if not friendEmail or not friendEmail.strip():
            raise HTTPException(status_code=400, detail="Email de l'ami requis")
        
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', friendEmail):
            raise HTTPException(status_code=400, detail="Format d'email invalide")
        
        friend_name = friendName.strip() if friendName else "Ami"
        sender_name = yourName.strip() if yourName else "Un membre UPlanet"
        personal_msg = personalMessage.strip() if personalMessage else ""
        
        invitation_html = f"""
        <html>
        <head>
            <meta charset="utf-8">
            <title>Invitation UPlanet</title>
        </head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; text-align: center;">
                <h1>🌍 Invitation UPlanet</h1>
                <p>De la part de {sender_name}</p>
            </div>
            
            <div style="background-color: #f9f9f9; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <pre style="white-space: pre-wrap; font-family: Arial, sans-serif; margin: 0;">{personal_msg}</pre>
            </div>
            
            <div style="text-align: center; margin: 30px 0;">
                <a href="https://qo-op.com" style="background-color: #4CAF50; color: white; padding: 15px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">🚀 Rejoindre UPlanet</a>
            </div>
            
            <footer style="text-align: center; color: #666; font-size: 12px; margin-top: 30px;">
                <p>Ce message a été envoyé via UPlanet - Réseau social décentralisé</p>
            </footer>
        </body>
        </html>
        """
        
        import os
        timestamp = int(time.time())
        temp_message_file = f"/tmp/uplanet_invitation_{timestamp}.html"
        
        with open(temp_message_file, 'w', encoding='utf-8') as f:
            f.write(invitation_html)
        
        subject = f"🌍 {sender_name} vous invite à rejoindre UPlanet !"
        
        from core.config import settings
        from core.config import settings
        mailjet_script = settings.TOOLS_PATH / "mailjet.sh"
        
        if not os.path.exists(mailjet_script):
            raise HTTPException(status_code=500, detail="Script mailjet.sh non trouvé")
        
        process = await asyncio.create_subprocess_exec(
            mailjet_script,
            friendEmail,
            temp_message_file,
            subject,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        try:
            os.remove(temp_message_file)
        except Exception as e:
            logger.warning(f"Erreur suppression fichier temp: {e}")
        
        if process.returncode == 0:
            logger.info(f"✅ Invitation envoyée avec succès à {friendEmail}")
            return JSONResponse({
                "success": True,
                "message": f"Invitation envoyée avec succès à {friend_name} ({friendEmail}) !",
                "details": {
                    "recipient": friendEmail,
                    "sender": sender_name,
                    "subject": subject
                }
            })
        else:
            error_msg = stderr.decode().strip() if stderr else "Erreur inconnue"
            logger.error(f"❌ Erreur mailjet.sh: {error_msg}")
            raise HTTPException(
                status_code=500, 
                detail=f"Erreur lors de l'envoi: {error_msg}"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erreur lors de l'envoi d'invitation: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

@router.post("/api/test-nostr")
async def test_nostr_auth(npub: str = Form(...)):
    """Test NOSTR authentication for a given npub"""
    try:
        logger.info(f"Test d'authentification NOSTR pour: {npub}")
        
        # Validation du format plus flexible
        is_hex_format = len(npub) == 64
        is_npub_format = npub.startswith('npub1')
        
        if not is_hex_format and not is_npub_format:
            raise HTTPException(
                status_code=400,
                detail=f"Format de clé invalide: '{npub}'. "
                       f"Doit être soit une npub (npub1...) soit une clé hex de 64 caractères. "
                       f"Longueur actuelle: {len(npub)} caractères."
            )
        
        from utils.crypto import npub_to_hex
        # Convertir vers le format hex standardisé
        if is_hex_format:
            logger.info("Format détecté: Clé publique hexadécimale")
            hex_pubkey = npub_to_hex(npub)  # Va valider et normaliser
        else:
            logger.info("Format détecté: npub (bech32)")
            hex_pubkey = npub_to_hex(npub)
            
        if not hex_pubkey:
            raise HTTPException(
                status_code=400,
                detail=f"Impossible de convertir la clé en format hexadécimal. "
                       f"Vérifiez que {'la clé hex est valide' if is_hex_format else 'la npub est correctement formatée'}."
            )
        
        # Tester la connexion au relai
        from services.nostr import get_nostr_relay_url, verify_nostr_auth
        import websockets
        from datetime import datetime, timezone
        
        relay_url = get_nostr_relay_url()
        logger.info(f"Test de connexion au relai: {relay_url}")
        
        try:
            # Test de connexion basique
            async with websockets.connect(relay_url, timeout=5) as websocket:
                relay_connected = True
                logger.info("✅ Connexion au relai réussie")
        except Exception as e:
            relay_connected = False
            logger.error(f"❌ Connexion au relai échouée: {e}")
        
        # Vérifier l'authentification NIP42
        auth_result = await verify_nostr_auth(hex_pubkey)  # Utiliser la clé hex validée
        
        # Vérifier la présence du fichier HEX dans le répertoire MULTIPASS
        # SÉCURITÉ: Ne divulguer ces informations QUE si NIP-42 est valide
        multipass_registered = False
        multipass_email = None
        multipass_dir = None
        hex_file_path = None
        
        # Seulement si NIP-42 est valide, on vérifie et divulgue les infos MULTIPASS
        if auth_result:
            try:
                # Chercher le répertoire MULTIPASS correspondant à cette clé hex
                from core.config import settings
                nostr_base_path = settings.GAME_PATH / "nostr"
                
                if nostr_base_path.exists():
                    # Parcourir tous les dossiers email dans nostr/
                    for email_dir in nostr_base_path.iterdir():
                        if email_dir.is_dir() and '@' in email_dir.name:
                            hex_file = email_dir / "HEX"
                            
                            if hex_file.exists():
                                try:
                                    with open(hex_file, 'r') as f:
                                        stored_hex = f.read().strip().lower()
                                    
                                    if stored_hex == hex_pubkey.lower():
                                        multipass_registered = True
                                        multipass_email = email_dir.name
                                        multipass_dir = str(email_dir)
                                        hex_file_path = str(hex_file)
                                        logger.info(f"✅ MULTIPASS trouvé pour {hex_pubkey}: {email_dir}")
                                        break
                                except Exception as e:
                                    logger.warning(f"Erreur lors de la lecture de {hex_file}: {e}")
                                    continue
            except Exception as e:
                logger.warning(f"Erreur lors de la recherche du MULTIPASS: {e}")
        else:
            # Si NIP-42 n'est pas valide, on ne révèle PAS si le MULTIPASS existe
            # Pour éviter l'énumération (sniffing)
            logger.info(f"⚠️ NIP-42 non valide pour {hex_pubkey}, informations MULTIPASS non divulguées (sécurité)")
        
        # Préparer la réponse détaillée
        response_data = {
            "input_key": npub,
            "input_format": "hex" if is_hex_format else "npub",
            "hex_pubkey": hex_pubkey,
            "relay_url": relay_url,
            "relay_connected": relay_connected,
            "auth_verified": auth_result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {
                "key_format_valid": True,
                "hex_conversion_success": hex_pubkey is not None,
                "relay_connection": relay_connected,
                "nip42_events_found": auth_result
            }
        }
        
        # Ajouter les informations MULTIPASS SEULEMENT si NIP-42 est valide
        if auth_result:
            response_data["multipass_registered"] = multipass_registered
            response_data["checks"]["multipass_hex_file_exists"] = multipass_registered
            
            # Ajouter les détails MULTIPASS si trouvés
            if multipass_registered:
                response_data["multipass_email"] = multipass_email
                response_data["multipass_directory"] = multipass_dir
                response_data["hex_file_path"] = hex_file_path
        else:
            # Si NIP-42 n'est pas valide, on ne révèle PAS l'état du MULTIPASS
            # Pour éviter l'énumération
            response_data["multipass_registered"] = None
            response_data["checks"]["multipass_hex_file_exists"] = None
        
        # Déterminer le statut global
        # SÉCURITÉ: Les informations MULTIPASS ne sont divulguées que si NIP-42 est valide
        if auth_result and multipass_registered:
            response_data["message"] = "✅ Connexion complète - NIP42 vérifié et MULTIPASS inscrit sur le relai"
            response_data["status"] = "complete"
        elif auth_result:
            # NIP-42 valide mais MULTIPASS non trouvé (on peut le dire car NIP-42 est valide)
            response_data["message"] = "⚠️ Authentification NIP42 OK mais MULTIPASS non trouvé sur le relai"
            response_data["status"] = "partial"
            response_data["recommendations"] = [
                f"Le fichier HEX n'a pas été trouvé dans ~/.zen/game/nostr/*@*/HEX pour la clé {hex_pubkey}",
                "Vérifiez que votre MULTIPASS est bien inscrit sur le relai",
                "Le répertoire MULTIPASS doit contenir un fichier HEX avec votre clé publique"
            ]
        elif relay_connected:
            # NIP-42 non valide - on ne révèle PAS si MULTIPASS existe (sécurité)
            response_data["message"] = "⚠️ Connexion au relai OK mais aucun événement NIP42 récent trouvé"
            response_data["status"] = "partial"
            response_data["recommendations"] = [
                "Vérifiez que votre client NOSTR a bien envoyé un événement d'authentification",
                "L'événement doit être de kind 22242 (NIP42)",
                "L'événement doit dater de moins de 24 heures",
                f"Vérifiez que la clé publique {hex_pubkey} correspond bien à votre identité NOSTR",
                "Une fois NIP-42 validé, les informations MULTIPASS seront vérifiées"
            ]
        else:
            response_data["message"] = "❌ Impossible de se connecter au relai NOSTR"
            response_data["status"] = "error"
            response_data["recommendations"] = [
                f"Vérifiez que le relai NOSTR est démarré sur {relay_url}",
                "Vérifiez la configuration réseau",
                "Le relai doit accepter les connexions WebSocket"
            ]
        
        return response_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erreur lors du test NOSTR: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur lors du test: {str(e)}")

@router.get("/api/test-nostr")
async def test_nostr_auth_get(npub: str):
    """Test NOSTR authentication for a given npub (GET version for browser testing)"""
    return await test_nostr_auth(npub)


# ─── Admin NOSTR — protégé par UPLANETNAME ────────────────────────────────────

@router.get("/api/nostr/admin/events")
async def admin_get_nostr_events(
    uplanetname: str,
    kind: Optional[str] = None,
    author: Optional[str] = None,
    tag_d: Optional[str] = None,
    tag_p: Optional[str] = None,
    tag_t: Optional[str] = None,
    tag_g: Optional[str] = None,
    since: Optional[int] = None,
    until: Optional[int] = None,
    limit: int = 100
):
    """Interroge les événements NOSTR du relay local (strfry) — auth UPLANETNAME requise."""
    if not _validate_uplanetname(uplanetname):
        raise HTTPException(status_code=403, detail="UPLANETNAME invalide")

    from core.config import settings
    script = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "nostr_get_events.sh"
    if not script.exists():
        raise HTTPException(status_code=500, detail="nostr_get_events.sh introuvable")

    cmd = [str(script), "--limit", str(min(limit, 500)), "--output", "json"]
    if kind:
        cmd += ["--kind", kind]
    if author:
        cmd += ["--author", author]
    if tag_d:
        cmd += ["--tag-d", tag_d]
    if tag_p:
        cmd += ["--tag-p", tag_p]
    if tag_t:
        cmd += ["--tag-t", tag_t]
    if tag_g:
        cmd += ["--tag-g", tag_g]
    if since:
        cmd += ["--since", str(since)]
    if until:
        cmd += ["--until", str(until)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(script.parent)
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        raw = stdout.decode('utf-8', errors='ignore')
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout lors de la requête strfry")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    events = []
    for line in raw.strip().split('\n'):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass

    return JSONResponse({"events": events, "count": len(events)})


@router.post("/api/nostr/admin/delete")
async def admin_delete_nostr_events(request: Request):
    """Supprime des événements NOSTR par IDs via strfry delete — auth UPLANETNAME requise."""
    body = await request.json()
    uplanetname = body.get("uplanetname", "")
    ids: list = body.get("ids", [])

    if not _validate_uplanetname(uplanetname):
        raise HTTPException(status_code=403, detail="UPLANETNAME invalide")
    if not ids or not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="Liste d'IDs requise")

    # Valider que chaque ID est bien un hex de 64 chars
    clean_ids = []
    for ev_id in ids:
        ev_id = str(ev_id).strip()
        if len(ev_id) == 64:
            try:
                int(ev_id, 16)
                clean_ids.append(ev_id)
            except ValueError:
                pass
    if not clean_ids:
        raise HTTPException(status_code=400, detail="Aucun ID valide fourni")

    strfry_dir = Path.home() / ".zen" / "strfry"
    strfry_bin = strfry_dir / "strfry"
    if not strfry_bin.exists():
        raise HTTPException(status_code=500, detail="strfry introuvable")

    ids_json = json.dumps({"ids": clean_ids})

    try:
        proc = await asyncio.create_subprocess_exec(
            str(strfry_bin), "delete", f"--filter={ids_json}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(strfry_dir)
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        ok = proc.returncode == 0
        out = stdout.decode('utf-8', errors='ignore') + stderr.decode('utf-8', errors='ignore')
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout lors de la suppression")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not ok:
        raise HTTPException(status_code=500, detail=f"Erreur strfry delete: {out[:300]}")

    logger.info(f"Admin NOSTR: {len(clean_ids)} événement(s) supprimé(s)")
    return JSONResponse({"deleted": len(clean_ids), "ids": clean_ids})


@router.get("/api/nostr/admin/captain_info")
async def admin_captain_info():
    """Retourne les pubkeys NOSTR publiques du node et du capitaine — sans auth (info publique)."""
    node_hex, captain_hex = _get_node_and_captain_hex()
    return JSONResponse({"node_hex": node_hex, "captain_hex": captain_hex})


# ─── Admin NOSTR — état des mémoires MULTIPASS (BRO mémoire) ─────────────────

def _multipass_accounts() -> list:
    """Liste {email, hex, npub} de tous les MULTIPASS de la station — même
    pattern de scan que routers/identity.py::_find_email_by_npub, mais énuméré
    au lieu de cibler un seul compte."""
    from services.memory_status import list_multipass_emails
    accounts = []
    for email in list_multipass_emails():
        hex_pubkey = ""
        hex_file = settings.GAME_PATH / "nostr" / email / "HEX"
        if hex_file.exists():
            try:
                hex_pubkey = hex_file.read_text().strip()
            except Exception:
                pass
        accounts.append({
            "email": email,
            "hex": hex_pubkey,
            "npub": hex_to_npub(hex_pubkey) if hex_pubkey else "",
        })
    return accounts


@router.get("/api/nostr/admin/multipass_list")
async def admin_multipass_list(uplanetname: str):
    """Liste tous les comptes MULTIPASS de la station (email + hex + npub) —
    point de départ de la vue admin 'BRO mémoire' (aucune liste de ce type
    n'existait auparavant, cf. nostr_admin.html)."""
    if not _validate_uplanetname(uplanetname):
        raise HTTPException(status_code=403, detail="UPLANETNAME invalide")
    accounts = _multipass_accounts()
    return JSONResponse({"accounts": accounts, "count": len(accounts)})


@router.get("/api/nostr/admin/memory_status")
async def admin_memory_status(uplanetname: str, email: str):
    """État des mémoires (fichiers + Qdrant) d'un MULTIPASS donné — vue admin.
    Les cookies liés au compte (kind 31903) se lisent côté client via
    GET /api/nostr/admin/events?kind=31903&author=<hex> (déjà existant),
    pas dupliqués ici."""
    if not _validate_uplanetname(uplanetname):
        raise HTTPException(status_code=403, detail="UPLANETNAME invalide")
    from services.memory_status import get_memory_status
    status = await asyncio.to_thread(get_memory_status, email)
    return JSONResponse(status)


@router.post("/api/nostr/admin/memory_reset")
async def admin_memory_reset(request: Request):
    """Réinitialise un périmètre de mémoire d'un MULTIPASS — action du capitaine
    depuis la vue admin 'BRO mémoire'. Body JSON : {uplanetname, email, scope}."""
    body = await request.json()
    uplanetname = body.get("uplanetname", "")
    email = body.get("email", "")
    scope = body.get("scope", "")

    if not _validate_uplanetname(uplanetname):
        raise HTTPException(status_code=403, detail="UPLANETNAME invalide")
    if not email:
        raise HTTPException(status_code=400, detail="email requis")

    from services.memory_status import reset_memory, RESET_SCOPES
    if scope not in RESET_SCOPES:
        raise HTTPException(status_code=400, detail=f"scope invalide (attendu: {', '.join(RESET_SCOPES)})")

    report = await asyncio.to_thread(reset_memory, email, scope)
    logger.info(f"Admin memory_reset: {email} scope={scope} — {len(report['deleted'])} élément(s) supprimé(s)")
    return JSONResponse(report)


@router.post("/api/nostr/admin/memory_regenerate")
async def admin_memory_regenerate(request: Request):
    """Régénère le profil LifeOS d'un MULTIPASS depuis ses propres posts
    Mastodon (si un cookie mastodon.social est déposé) — action du capitaine
    depuis la vue admin 'BRO mémoire'. Body JSON : {uplanetname, email}."""
    body = await request.json()
    uplanetname = body.get("uplanetname", "")
    email = body.get("email", "")

    if not _validate_uplanetname(uplanetname):
        raise HTTPException(status_code=403, detail="UPLANETNAME invalide")
    if not email:
        raise HTTPException(status_code=400, detail="email requis")

    from services.memory_status import regenerate_lifeos_from_mastodon
    result = await asyncio.to_thread(regenerate_lifeos_from_mastodon, email)
    logger.info(f"Admin memory_regenerate (Mastodon): {email} → {result}")
    return JSONResponse(result)


@router.post("/api/nostr/dm/delete_node_messages")
async def delete_node_messages(request: Request):
    """Supprime des self-DM envoyés par NODE et destinés à l'appelant — réservé
    au capitaine (seul destinataire légitime du canal NODE dans atomic_chat.html).

    Auth NIP-42 (kind 22242 déjà publié sur le relais) ou NIP-98 via
    require_nostr_auth — jamais l'UPLANETNAME globale de /api/nostr/admin/delete,
    qui autoriserait la suppression de N'IMPORTE QUEL event du relay. Scope
    strictement limité : seuls les IDs qui correspondent réellement à un event
    kind 4, pubkey==NODE_HEX, #p==l'appelant authentifié sont supprimés — un ID
    arbitraire fourni par le client ne suffit jamais à lui seul.
    """
    body = await request.json()
    ids = [str(i).strip() for i in body.get("ids", []) if str(i).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="Liste d'IDs requise")

    auth_npub = await require_nostr_auth(request, body.get("npub"), force_check=False)
    hex_pubkey = npub_to_hex(auth_npub) if auth_npub.startswith("npub") else auth_npub

    node_hex, captain_hex = _get_node_and_captain_hex()
    if not node_hex:
        raise HTTPException(status_code=500, detail="NODE_HEX introuvable sur cette station")
    if not captain_hex or hex_pubkey.lower() != captain_hex.lower():
        raise HTTPException(status_code=403, detail="Réservé au capitaine de la station")

    # Scope : ne récupère QUE les self-DM de NODE vers CET appelant, puis ne
    # garde que les IDs demandés qui y figurent réellement (jamais de confiance
    # aveugle dans la liste d'IDs fournie par le client).
    script = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "nostr_get_events.sh"
    if not script.exists():
        raise HTTPException(status_code=500, detail="nostr_get_events.sh introuvable")

    cmd = [str(script), "--kind", "4", "--author", node_hex, "--tag-p", hex_pubkey,
           "--limit", "500", "--output", "json"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(script.parent),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout lors de la requête strfry")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    node_event_ids = set()
    for line in stdout.decode("utf-8", errors="ignore").strip().splitlines():
        if not line.strip():
            continue
        try:
            node_event_ids.add(json.loads(line)["id"])
        except Exception:
            pass

    valid_ids = [i for i in ids if i in node_event_ids]
    if not valid_ids:
        raise HTTPException(status_code=400,
                             detail="Aucun ID valide (doit être un message de NODE qui vous est destiné)")

    strfry_dir = Path.home() / ".zen" / "strfry"
    strfry_bin = strfry_dir / "strfry"
    if not strfry_bin.exists():
        raise HTTPException(status_code=500, detail="strfry introuvable")

    ids_json = json.dumps({"ids": valid_ids})
    try:
        proc2 = await asyncio.create_subprocess_exec(
            str(strfry_bin), "delete", f"--filter={ids_json}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(strfry_dir),
        )
        stdout2, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=20.0)
        ok = proc2.returncode == 0
        out = stdout2.decode("utf-8", errors="ignore") + stderr2.decode("utf-8", errors="ignore")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout lors de la suppression")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not ok:
        raise HTTPException(status_code=500, detail=f"Erreur strfry delete: {out[:300]}")

    logger.info(f"NODE DM delete: {len(valid_ids)} événement(s) supprimé(s) pour capitaine {hex_pubkey[:16]}…")
    return JSONResponse({"deleted": len(valid_ids), "ids": valid_ids})


@router.post("/api/nostr/admin/constellation_delete")
async def admin_constellation_delete(request: Request):
    """Supprime par auteur en local (strfry) + relaie aux NODEs constellation via DM BRO nostr_delete."""
    body = await request.json()
    uplanetname = body.get("uplanetname", "")
    kind = body.get("kind", None)

    # Accepte "authors" (liste) ou "author" (singulier, rétrocompat)
    authors_raw = body.get("authors", None)
    if authors_raw is None:
        single = body.get("author", "")
        authors_raw = [single] if single else []
    if isinstance(authors_raw, str):
        authors_raw = [authors_raw]
    authors_list = [a for a in authors_raw if isinstance(a, str) and len(a) == 64]
    for a in authors_list:
        try:
            int(a, 16)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"author hex invalide : {a[:12]}…")

    if not _validate_uplanetname(uplanetname):
        raise HTTPException(status_code=403, detail="UPLANETNAME invalide")
    if not authors_list:
        raise HTTPException(status_code=400, detail="authors (liste de pubkeys hex 64 chars) requis")

    # 1. Suppression locale strfry (tous les auteurs en un seul appel)
    strfry_dir = Path.home() / ".zen" / "strfry"
    strfry_bin = strfry_dir / "strfry"
    local_ok = False
    if strfry_bin.exists():
        filter_obj: dict = {"authors": authors_list}
        if kind is not None:
            try:
                filter_obj["kinds"] = [int(kind)]
            except (ValueError, TypeError):
                pass
        try:
            proc = await asyncio.create_subprocess_exec(
                str(strfry_bin), "delete", f"--filter={json.dumps(filter_obj)}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(strfry_dir)
            )
            await asyncio.wait_for(proc.communicate(), timeout=20.0)
            local_ok = proc.returncode == 0
        except Exception as e:
            logger.error(f"strfry local delete error: {e}")

    # 2. Lire NODE_NSEC depuis ~/.zen/game/secret.nostr
    node_nsec = ""
    secret_nostr = Path.home() / ".zen" / "game" / "secret.nostr"
    if secret_nostr.exists():
        for segment in secret_nostr.read_text().split(";"):
            segment = segment.strip()
            if segment.startswith("NSEC="):
                node_nsec = segment[5:].strip().strip("'\"")
                break

    if not node_nsec:
        return JSONResponse({
            "local_deleted": local_ok,
            "constellation_nodes": [],
            "warning": "NODE_NSEC absent — suppression constellation ignorée"
        })

    # 3. Trouver les NODEHEX constellation via ~/.zen/tmp/swarm/*/HEX
    # (chaque nœud publie son HEX dans /ipns/{IPFSNODEID}/HEX, mis en cache localement)
    swarm_dir = Path.home() / ".zen" / "tmp" / "swarm"
    node_hexes: list = []
    if swarm_dir.exists():
        for hex_file in swarm_dir.glob("*/HEX"):
            try:
                nodehex = hex_file.read_text().strip()
                if nodehex and len(nodehex) == 64:
                    int(nodehex, 16)
                    node_hexes.append(nodehex)
            except Exception:
                pass

    # 4. Envoyer DM BRO channel "nostr_delete" à chaque NODE constellation
    intercom = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "nostr_node_intercom.py"
    dm_payload = json.dumps({"authors": ",".join(authors_list), "kind": str(kind) if kind else ""})

    from core.config import settings
    relay_list = settings.myRELAY or "wss://relay.copylaradio.com"
    if "relay.copylaradio.com" not in relay_list:
        relay_list = relay_list + " wss://relay.copylaradio.com"

    relay_args = relay_list.split()  # split "wss://a wss://b" → ["wss://a", "wss://b"]

    results: list = []
    if intercom.exists() and node_hexes:
        for node_hex in node_hexes:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python3", str(intercom), "send",
                    "--nsec-stdin",
                    "--to", node_hex,
                    "--channel", "nostr_delete",
                    "--payload", dm_payload,
                    "--relays", *relay_args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=(node_nsec + "\n").encode()),
                    timeout=15.0
                )
                ok = proc.returncode == 0
                entry: dict = {"node": node_hex[:12] + "...", "ok": ok}
                if not ok and stderr:
                    entry["error"] = stderr.decode(errors="replace").strip()[:120]
                results.append(entry)
            except Exception as e:
                results.append({"node": node_hex[:12] + "...", "ok": False, "error": str(e)[:80]})
    elif not intercom.exists():
        logger.warning("nostr_node_intercom.py introuvable — relay constellation ignoré")

    logger.info(f"Admin constellation delete: authors={[a[:12] for a in authors_list]}, {len(results)}/{len(node_hexes)} NODEs notifiés")
    return JSONResponse({
        "local_deleted": local_ok,
        "constellation_nodes": results,
        "nodes_notified": len([r for r in results if r.get("ok")])
    })
