import logging
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse

from utils.helpers import render_page

router = APIRouter()

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
        
        logging.info(f"Serving NOSTR template: {template_name} (type={type})")
        
        return render_page(request, template_name)
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors du chargement du template NOSTR: {str(e)}")
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
        
        logging.info(f"Analyse N2 pour {hex[:12]}... (range={range}, output={output})")
        
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
        logging.error(f"Erreur lors de l'analyse N2: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'analyse: {str(e)}")

from pydantic import BaseModel

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
    form_data: SendMsgForm = Form(...)
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
        logging.info(f"Invitation UPlanet pour: {friendEmail} de la part de: {yourName}")
        
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
            logging.warning(f"Erreur suppression fichier temp: {e}")
        
        if process.returncode == 0:
            logging.info(f"✅ Invitation envoyée avec succès à {friendEmail}")
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
            logging.error(f"❌ Erreur mailjet.sh: {error_msg}")
            raise HTTPException(
                status_code=500, 
                detail=f"Erreur lors de l'envoi: {error_msg}"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erreur lors de l'envoi d'invitation: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

@router.post("/api/test-nostr")
async def test_nostr_auth(npub: str = Form(...)):
    """Test NOSTR authentication for a given npub"""
    try:
        logging.info(f"Test d'authentification NOSTR pour: {npub}")
        
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
            logging.info("Format détecté: Clé publique hexadécimale")
            hex_pubkey = npub_to_hex(npub)  # Va valider et normaliser
        else:
            logging.info("Format détecté: npub (bech32)")
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
        logging.info(f"Test de connexion au relai: {relay_url}")
        
        try:
            # Test de connexion basique
            async with websockets.connect(relay_url, timeout=5) as websocket:
                relay_connected = True
                logging.info("✅ Connexion au relai réussie")
        except Exception as e:
            relay_connected = False
            logging.error(f"❌ Connexion au relai échouée: {e}")
        
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
                                        logging.info(f"✅ MULTIPASS trouvé pour {hex_pubkey}: {email_dir}")
                                        break
                                except Exception as e:
                                    logging.warning(f"Erreur lors de la lecture de {hex_file}: {e}")
                                    continue
            except Exception as e:
                logging.warning(f"Erreur lors de la recherche du MULTIPASS: {e}")
        else:
            # Si NIP-42 n'est pas valide, on ne révèle PAS si le MULTIPASS existe
            # Pour éviter l'énumération (sniffing)
            logging.info(f"⚠️ NIP-42 non valide pour {hex_pubkey}, informations MULTIPASS non divulguées (sécurité)")
        
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
        logging.error(f"Erreur lors du test NOSTR: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur lors du test: {str(e)}")

@router.get("/api/test-nostr")
async def test_nostr_auth_get(npub: str):
    """Test NOSTR authentication for a given npub (GET version for browser testing)"""
    return await test_nostr_auth(npub)
