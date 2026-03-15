import os
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException

from utils.helpers import get_env_from_mysh

router = APIRouter()

@router.post('/ping', summary="Analytics Webhook", description="Receive analytics data and send as NOSTR message to CAPTAINEMAIL")
async def get_webhook(request: Request):
    """Receive analytics data and send as NOSTR message to CAPTAINEMAIL
    
    This endpoint receives analytics data and sends it as a NOSTR event (kind 10000)
    to the captain email using nostr_send_note.py instead of mailjet.sh.
    """
    try:
        # Récupérer les données de la requête
        data = await request.json()  # Récupérer le corps de la requête en JSON
        referer = request.headers.get("referer")  # Récupérer l'en-tête Referer

        from core.config import settings
        # Get current player email from ~/.zen/game/players/.current (symbolic link)
        current_player_link = settings.GAME_PATH / "players" / ".current"
        captain_email = None
        
        # Try to read the symbolic link first
        if current_player_link.exists() and current_player_link.is_symlink():
            try:
                # Read the symbolic link to get the target directory path
                target_path = current_player_link.readlink()
                # Extract email from directory name (the symlink points to a directory named with the email)
                captain_email = target_path.name
                if captain_email:
                    logging.debug(f"📧 Using current player email from .current symlink: {captain_email}")
            except Exception as e:
                logging.warning(f"⚠️ Could not read .current symlink: {e}")
        
        # Fallback to CAPTAINEMAIL from my.sh if .current is not available
        if not captain_email:
            captain_email = await get_env_from_mysh("CAPTAINEMAIL", "")
            if captain_email:
                logging.debug(f"📧 Using CAPTAINEMAIL from my.sh: {captain_email}")
            else:
                # Last fallback to environment variable
                from core.config import settings
                captain_email = settings.CAPTAINEMAIL
                if captain_email:
                    logging.debug(f"📧 Using CAPTAINEMAIL from environment variable: {captain_email}")
        
        if not captain_email:
            logging.warning("⚠️ No current player email found (.current symlink or CAPTAINEMAIL env var), skipping NOSTR notification")
            return {"received": data, "referer": referer, "note": "Current player email not configured"}
        
        # Find keyfile for current player email: ~/.zen/game/nostr/{email}/.secret.nostr
        captain_keyfile = settings.GAME_PATH / "nostr" / captain_email / ".secret.nostr"
        
        if not captain_keyfile.exists():
            logging.warning(f"⚠️ Keyfile not found for current player ({captain_email}): {captain_keyfile}")
            return {"received": data, "referer": referer, "note": f"Keyfile not found for {captain_email}"}
        
        # Format analytics data as JSON string for NOSTR message
        analytics_json = json.dumps(data, indent=2, ensure_ascii=False)
        
        # Build message content
        message_lines = [
            "📊 Analytics Data Received",
            "",
            f"Type: {data.get('type', 'unknown')}",
            f"Source: {data.get('source', 'unknown')}",
            f"Timestamp: {data.get('timestamp', datetime.now(timezone.utc).isoformat())}",
        ]
        
        # Add referer if available
        if referer:
            message_lines.append(f"Referer: {referer}")

        # Add URL if available
        if data.get('current_url'):
            message_lines.append(f"URL: {data.get('current_url')}")
        
        # Add video-specific data if present
        if data.get('video_event_id'):
            message_lines.append(f"Video Event ID: {data.get('video_event_id')}")
        if data.get('video_title'):
            message_lines.append(f"Video Title: {data.get('video_title')}")
        
        message_lines.extend([
            "",
            "--- Full Data ---",
            analytics_json
        ])
        
        message_content = "\n".join(message_lines)
        
        # Build tags for NOSTR event (kind 10000 - Analytics)
        tags = [
            ["t", "analytics"],
            ["t", data.get("type", "unknown")]
        ]
        
        # Add source tag if available
        if data.get("source"):
            tags.append(["source", data.get("source")])
        
        # Add URL tag if available
        if data.get("current_url"):
            tags.append(["url", data.get("current_url")])
        
        # Get NOSTR relay from environment or use default
        from core.config import settings
        nostr_relay = settings.myRELAY.split()[0]
        
        # Call nostr_send_note.py to send the message
        nostr_script = settings.TOOLS_PATH / "nostr_send_note.py"
        
        if not os.path.exists(nostr_script):
            logging.error(f"❌ nostr_send_note.py not found at: {nostr_script}")
            return {"received": data, "referer": referer, "note": "nostr_send_note.py not found"}
        
        # Prepare command
        tags_json = json.dumps(tags)
        cmd = [
            "python3",
            nostr_script,
            "--keyfile", str(captain_keyfile),
            "--content", message_content,
            "--kind", "10000",  # Analytics event kind
            "--tags", tags_json,
            "--relays", nostr_relay,
            "--json"  # JSON output mode
        ]
        
        # Execute command (non-blocking, fire and forget)
        try:
            import asyncio
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            
            if process.returncode == 0:
                logging.info(f"✅ Analytics sent to captain via NOSTR: {data.get('type', 'unknown')}")
            else:
                logging.warning(f"⚠️ NOSTR send failed: {stderr.decode()}")
                
        except asyncio.TimeoutError:
            logging.warning("⚠️ NOSTR send timeout")
        except Exception as e:
            logging.warning(f"⚠️ NOSTR send error: {e}")

        return {"received": data, "referer": referer, "sent_via": "nostr"}
        
    except Exception as e:
        logging.error(f"❌ Error in /ping endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Invalid request data: {e}")
