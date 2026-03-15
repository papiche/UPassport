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
