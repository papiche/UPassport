import os
import json
import time
import uuid
import base64
import hashlib
import logging
import asyncio
import traceback
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

import aiofiles
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from utils.helpers import run_script, get_myipfs_gateway, as_form
from core.middleware import get_client_ip
from utils.security import (
    get_authenticated_user_directory,
    get_max_file_size_for_user,
    validate_uploaded_file,
    sanitize_filename_python,
    detect_file_type,
    find_user_directory_by_hex,
    is_safe_email
)
from services.nostr import verify_nostr_auth, require_nostr_auth
from utils.crypto import npub_to_hex, hex_to_npub
from services.nostr import fetch_video_event_from_nostr, parse_video_metadata
from models.schemas import UploadResponse, UploadFromDriveResponse

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@as_form
class WebcamForm(BaseModel):
    player: str
    ipfs_cid: str
    title: str
    npub: str
    file_hash: str
    info_cid: str
    thumbnail_ipfs: str = ""
    gifanim_ipfs: str = ""
    mime_type: str = "video/webm"
    upload_chain: str = ""
    duration: str = "0"
    video_dimensions: str = "640x480"
    file_size: str = "0"
    description: str = ""
    publish_nostr: str = "false"
    latitude: str = ""
    longitude: str = ""
    youtube_url: str = ""
    genres: str = ""

@router.get("/webcam", response_class=HTMLResponse)
async def get_webcam_page(request: Request):
    """Render the webcam recording page"""
    return templates.TemplateResponse("webcam.html", {
        "request": request,
        "myIPFS": await get_myipfs_gateway()
    })

@router.post("/webcam", response_class=HTMLResponse)
async def process_webcam_video(
    request: Request,
    form_data: WebcamForm = Depends(WebcamForm.as_form)
):
    player = form_data.player
    ipfs_cid = form_data.ipfs_cid
    title = form_data.title
    npub = form_data.npub
    file_hash = form_data.file_hash
    info_cid = form_data.info_cid
    thumbnail_ipfs = form_data.thumbnail_ipfs
    gifanim_ipfs = form_data.gifanim_ipfs
    mime_type = form_data.mime_type
    upload_chain = form_data.upload_chain
    duration = form_data.duration
    video_dimensions = form_data.video_dimensions
    file_size = form_data.file_size
    description = form_data.description
    publish_nostr = form_data.publish_nostr
    latitude = form_data.latitude
    longitude = form_data.longitude
    youtube_url = form_data.youtube_url
    genres = form_data.genres
    """Process webcam video and publish to NOSTR as NIP-71 video event"""
    if not ipfs_cid or not ipfs_cid.strip():
        return templates.TemplateResponse("webcam.html", {
            "request": request, 
            "error": "No IPFS CID provided. Video must be uploaded via /api/fileupload first.", 
            "recording": False,
            "myIPFS": await get_myipfs_gateway()
        })

    try:
        file_size_from_form = 0
        try:
            file_size_from_form = int(file_size) if file_size and file_size != "0" else 0
        except (ValueError, TypeError):
            file_size_from_form = 0
        
        file_size = file_size_from_form
        video_dimensions_param = video_dimensions if video_dimensions and video_dimensions != "640x480" else "640x480"
        try:
            duration_param = int(float(duration)) if duration else 0
        except (ValueError, TypeError):
            duration_param = 0
        
        video_dimensions = video_dimensions_param
        duration = duration_param
        thumbnail_ipfs_from_info = thumbnail_ipfs if thumbnail_ipfs else ""
        gifanim_ipfs_from_info = gifanim_ipfs if gifanim_ipfs else ""
        
        if info_cid:
            try:
                import httpx
                gateway = await get_myipfs_gateway()
                info_url = f"{gateway}/ipfs/{info_cid}"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    info_response = await client.get(info_url)
                    if info_response.status_code == 200:
                        info_data = info_response.json()
                        if info_data.get("file") and info_data["file"].get("hash"):
                            file_hash = info_data["file"]["hash"]
                        if info_data.get("provenance") and info_data["provenance"].get("upload_chain"):
                            upload_chain = info_data["provenance"]["upload_chain"]
                        
                        protocol_version = info_data.get("protocol", {}).get("version", "1.0.0")
                        is_v2 = protocol_version.startswith("2.")
                        
                        if info_data.get("media"):
                            media = info_data["media"]
                            if media.get("dimensions"):
                                video_dimensions = media["dimensions"]
                            if media.get("duration"):
                                duration = float(media["duration"])
                            if file_size == 0 and media.get("file_size"):
                                try: file_size = int(media["file_size"])
                                except: pass
                            
                            if not thumbnail_ipfs:
                                if is_v2 and media.get("thumbnails"):
                                    thumbnail_cid = media["thumbnails"].get("static") or media["thumbnails"].get("animated")
                                    if thumbnail_cid:
                                        thumbnail_ipfs_from_info = thumbnail_cid.replace("/ipfs/", "").replace("ipfs://", "")
                                elif not is_v2 and media.get("thumbnail_ipfs"):
                                    thumbnail_ipfs_from_info = media["thumbnail_ipfs"].replace("/ipfs/", "").replace("ipfs://", "")
                            
                            if not gifanim_ipfs:
                                if is_v2 and media.get("thumbnails"):
                                    gifanim_cid = media["thumbnails"].get("animated")
                                    if gifanim_cid:
                                        gifanim_ipfs_from_info = gifanim_cid.replace("/ipfs/", "").replace("ipfs://", "")
                                elif not is_v2 and media.get("gifanim_ipfs"):
                                    gifanim_ipfs_from_info = media["gifanim_ipfs"].replace("/ipfs/", "").replace("ipfs://", "")
                        
                        if file_size == 0 and info_data.get("file_size"):
                            try: file_size = int(info_data["file_size"])
                            except: pass
                        if file_size == 0 and info_data.get("fileSize"):
                            try: file_size = int(info_data["fileSize"])
                            except: pass
            except Exception as e:
                logging.warning(f"Could not load metadata from info.json: {e}")
        
        final_thumbnail_ipfs = thumbnail_ipfs if thumbnail_ipfs else thumbnail_ipfs_from_info
        final_gifanim_ipfs = gifanim_ipfs if gifanim_ipfs else gifanim_ipfs_from_info
        
        hex_pubkey = npub_to_hex(npub) if npub else None
        filename = None
        
        if hex_pubkey:
            try:
                user_dir = find_user_directory_by_hex(hex_pubkey)
                directory_email = user_dir.name if '@' in user_dir.name else None
                
                if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player):
                    if directory_email and is_safe_email(directory_email):
                        player = directory_email
                elif not is_safe_email(player):
                    if directory_email and is_safe_email(directory_email):
                        player = directory_email
                
                user_drive_path = user_dir / "APP" / "uDRIVE" / "Videos"
                if user_drive_path.exists():
                    video_files = sorted(user_drive_path.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if video_files:
                        filename = video_files[0].name
                        if file_size == 0:
                            file_size = video_files[0].stat().st_size
            except Exception:
                if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player) or not is_safe_email(player):
                    return templates.TemplateResponse("webcam.html", {
                        "request": request, 
                        "error": "Could not determine user email.", 
                        "recording": False,
                        "myIPFS": await get_myipfs_gateway()
                    })
        
        if not player or not re.match(r"[^@]+@[^@]+\.[^@]+", player) or not is_safe_email(player):
            return templates.TemplateResponse("webcam.html", {
                "request": request, 
                "error": "No valid email address could be determined.", 
                "recording": False,
                "myIPFS": await get_myipfs_gateway()
            })
        
        if not filename:
            filename = f"video_{int(time.time())}.webm"
        
        if file_size == 0:
            return templates.TemplateResponse("webcam.html", {
                "request": request, 
                "error": "File size is missing or invalid.", 
                "recording": False,
                "myIPFS": await get_myipfs_gateway()
            })
        
        ipfs_url = f"/ipfs/{ipfs_cid}/{filename}"
        if not title:
            title = f"Webcam recording {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        nostr_event_id = None
        if publish_nostr.lower() == "true" and npub:
            try:
                if not await verify_nostr_auth(npub):
                    return templates.TemplateResponse("webcam.html", {
                        "request": request, 
                        "error": "NOSTR authentication failed.", 
                        "recording": False
                    })
                
                user_dir = get_authenticated_user_directory(npub)
                secret_file = user_dir / ".secret.nostr"
                
                if not secret_file.exists():
                    return templates.TemplateResponse("webcam.html", {
                        "request": request, 
                        "error": "NOSTR secret file not found.", 
                        "recording": False
                    })
                
                try:
                    lat = float(latitude) if latitude else 0.00
                    lon = float(longitude) if longitude else 0.00
                except:
                    lat = 0.00
                    lon = 0.00
                
                from core.config import settings
                publish_script = settings.TOOLS_PATH / "publish_nostr_video.sh"
                if not os.path.exists(publish_script):
                    return templates.TemplateResponse("webcam.html", {
                        "request": request, 
                        "error": "NOSTR publish script not found.", 
                        "recording": False
                    })
                
                publish_cmd = [
                    "bash", publish_script,
                    "--nsec", str(secret_file),
                    "--ipfs-cid", ipfs_cid,
                    "--filename", filename,
                    "--title", title,
                    "--json"
                ]
                
                if description: publish_cmd.extend(["--description", description])
                if final_thumbnail_ipfs: publish_cmd.extend(["--thumbnail-cid", final_thumbnail_ipfs])
                if final_gifanim_ipfs: publish_cmd.extend(["--gifanim-cid", final_gifanim_ipfs])
                if info_cid: publish_cmd.extend(["--info-cid", info_cid])
                if file_hash: publish_cmd.extend(["--file-hash", file_hash])
                if mime_type: publish_cmd.extend(["--mime-type", mime_type])
                if upload_chain:
                    if isinstance(upload_chain, (list, dict)):
                        upload_chain_str = json.dumps(upload_chain)
                    else:
                        upload_chain_str = str(upload_chain)
                    publish_cmd.extend(["--upload-chain", upload_chain_str])
                
                if isinstance(video_dimensions, dict):
                    width = video_dimensions.get('width', '')
                    height = video_dimensions.get('height', '')
                    if width and height:
                        dimensions_str = f"{width}x{height}"
                    else:
                        dimensions_str = json.dumps(video_dimensions)
                else:
                    dimensions_str = str(video_dimensions)
                
                publish_cmd.extend([
                    "--duration", str(duration),
                    "--dimensions", dimensions_str,
                    "--file-size", str(file_size),
                    "--latitude", str(lat),
                    "--longitude", str(lon),
                    "--channel", player
                ])
                
                if youtube_url:
                    publish_cmd.extend(["--source-type", "youtube"])
                else:
                    publish_cmd.extend(["--source-type", "webcam"])
                
                if genres and genres.strip():
                    try:
                        genres_json = json.loads(genres)
                        if isinstance(genres_json, list) and len(genres_json) > 0:
                            genres_compact = json.dumps(genres_json, ensure_ascii=False, separators=(',', ':'))
                            publish_cmd.extend(["--genres", genres_compact])
                    except:
                        pass
                
                import asyncio
                process = await asyncio.create_subprocess_exec(
                    *publish_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                
                if process.returncode == 0:
                    from utils.helpers import safe_json_load
                    try:
                        result_json = safe_json_load(stdout.decode().strip())
                        nostr_event_id = result_json.get('event_id', '')
                    except ValueError:
                        event_id_match = re.search(r'"event_id"\s*:\s*"([a-f0-9]{64})"', stdout.decode())
                        if event_id_match:
                            nostr_event_id = event_id_match.group(1)
                        else:
                            nostr_event_id = stdout.decode().strip().split('\n')[-1] if stdout.decode() else ""
                            if not re.match(r'^[a-f0-9]{64}$', nostr_event_id):
                                nostr_event_id = ""
            except Exception as e:
                logging.error(f"Error during NOSTR publishing: {e}")

        success_message = f"Video processed successfully! IPFS: {ipfs_url}"
        if nostr_event_id:
            success_message += f" | NOSTR Event: {nostr_event_id}"
        
        return templates.TemplateResponse("webcam.html", {
            "request": request,
            "message": success_message,
            "recording": False,
            "ipfs_url": ipfs_url,
            "nostr_event_id": nostr_event_id,
            "video_info": {
                "title": title,
                "duration": duration,
                "dimensions": video_dimensions,
                "file_size": file_size
            }
        })

    except Exception as e:
        logging.error(f"Error processing webcam video: {e}")
        return templates.TemplateResponse("webcam.html", {
            "request": request, 
            "error": f"Error processing video: {str(e)}", 
            "recording": False
        })


@as_form
class VocalsForm(BaseModel):
    player: str
    ipfs_cid: str
    title: str
    npub: str
    file_hash: str
    info_cid: str = ""
    mime_type: str = "audio/mpeg"
    file_name: str = ""
    duration: str = "0"
    description: str = ""
    publish_nostr: str = "false"
    latitude: str = ""
    longitude: str = ""
    encrypted: str = "false"
    encryption_method: str = "nip44"
    recipients: str = ""
    waveform: str = ""
    kind: str = "1222"
    reply_to_event_id: str = ""
    reply_to_pubkey: str = ""
    expiration: str = ""

@router.post("/vocals", response_class=HTMLResponse)
async def process_vocals_message(
    request: Request,
    form_data: VocalsForm = Depends(VocalsForm.as_form)
):
    player = form_data.player
    ipfs_cid = form_data.ipfs_cid
    title = form_data.title
    npub = form_data.npub
    file_hash = form_data.file_hash
    info_cid = form_data.info_cid
    mime_type = form_data.mime_type
    file_name = form_data.file_name
    duration = form_data.duration
    description = form_data.description
    publish_nostr = form_data.publish_nostr
    latitude = form_data.latitude
    longitude = form_data.longitude
    encrypted = form_data.encrypted
    encryption_method = form_data.encryption_method
    recipients = form_data.recipients
    waveform = form_data.waveform
    kind = form_data.kind
    reply_to_event_id = form_data.reply_to_event_id
    reply_to_pubkey = form_data.reply_to_pubkey
    expiration = form_data.expiration
    """Process voice message and publish to NOSTR as NIP-A0 voice event"""
    if not ipfs_cid or not ipfs_cid.strip():
        return templates.TemplateResponse("vocals.html", {
            "request": request,
            "error": "No IPFS CID provided.",
            "myIPFS": await get_myipfs_gateway()
        })
    
    is_encrypted = encrypted.lower() == "true"
    if is_encrypted:
        if not recipients or not recipients.strip():
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": "Recipients required for encrypted voice messages.",
                "myIPFS": await get_myipfs_gateway()
            })
        try:
            recipients_list = json.loads(recipients)
            if not isinstance(recipients_list, list) or len(recipients_list) == 0:
                raise ValueError("Recipients must be a non-empty array")
        except Exception as e:
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": f"Invalid recipients format: {e}",
                "myIPFS": await get_myipfs_gateway()
            })
    
    try:
        try:
            user_dir = get_authenticated_user_directory(npub)
            secret_file = user_dir / ".secret.nostr"
        except Exception:
            from core.config import settings
            user_dir = settings.GAME_PATH / "nostr" / player
            secret_file = os.path.join(user_dir, ".secret.dunikey")
            if not os.path.exists(secret_file):
                secret_file = os.path.join(user_dir, ".secret.nostr")
        
        if not os.path.exists(secret_file):
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": "NOSTR authentication required.",
                "myIPFS": await get_myipfs_gateway()
            })
        
        try:
            lat = float(latitude) if latitude else 0.00
            lon = float(longitude) if longitude else 0.00
        except:
            lat = 0.00
            lon = 0.00
        
        try:
            voice_kind = int(kind) if kind else 1222
            if voice_kind not in [1222, 1244]:
                voice_kind = 1222
        except:
            voice_kind = 1222
        
        if voice_kind == 1244:
            if not reply_to_event_id or not reply_to_event_id.strip() or not reply_to_pubkey or not reply_to_pubkey.strip():
                voice_kind = 1222
        
        from core.config import settings
        publish_script = settings.TOOLS_PATH / "publish_nostr_vocal.sh"
        if not os.path.exists(publish_script):
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": "NOSTR publish script not found.",
                "myIPFS": await get_myipfs_gateway()
            })
        
        if file_name and file_name.strip():
            actual_filename = file_name.strip()
        else:
            extension = mime_type.split('/')[-1] if '/' in mime_type else 'mp3'
            actual_filename = f"voice_{int(time.time())}.{extension}"
        
        publish_cmd = [
            "bash", publish_script,
            "--nsec", str(secret_file),
            "--ipfs-cid", ipfs_cid,
            "--filename", actual_filename,
            "--title", title,
            "--json",
            "--kind", str(voice_kind)
        ]
        
        if description: publish_cmd.extend(["--description", description])
        if file_hash: publish_cmd.extend(["--file-hash", file_hash])
        if mime_type: publish_cmd.extend(["--mime-type", mime_type])
        if duration: publish_cmd.extend(["--duration", str(duration)])
        if lat != 0.00 or lon != 0.00: publish_cmd.extend(["--latitude", str(lat), "--longitude", str(lon)])
        if waveform: publish_cmd.extend(["--waveform", waveform])
        if info_cid: publish_cmd.extend(["--info-cid", info_cid])
        
        if voice_kind == 1244 and reply_to_event_id and reply_to_pubkey:
            publish_cmd.extend(["--reply-to-event-id", reply_to_event_id])
            publish_cmd.extend(["--reply-to-pubkey", reply_to_pubkey])
        
        if expiration and expiration.strip():
            try:
                exp_timestamp = int(expiration)
                if exp_timestamp > 0:
                    publish_cmd.extend(["--expiration", str(exp_timestamp)])
            except:
                pass
        
        if is_encrypted:
            publish_cmd.extend(["--encrypted", "true"])
            publish_cmd.extend(["--encryption-method", encryption_method])
            if recipients:
                publish_cmd.extend(["--recipients", recipients])
        
        publish_cmd.extend(["--channel", player])
        
        import asyncio
        process = await asyncio.create_subprocess_exec(
            *publish_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        
        if process.returncode == 0:
            from utils.helpers import safe_json_load
            try:
                result_json = safe_json_load(stdout.decode().strip())
                nostr_event_id = result_json.get('event_id', '')
                return templates.TemplateResponse("vocals.html", {
                    "request": request,
                    "success": f"Voice message published successfully! Event ID: {nostr_event_id[:16]}...",
                    "event_id": nostr_event_id,
                    "myIPFS": await get_myipfs_gateway()
                })
            except ValueError:
                return templates.TemplateResponse("vocals.html", {
                    "request": request,
                    "error": "Voice message published but could not parse response.",
                    "myIPFS": await get_myipfs_gateway()
                })
        else:
            return templates.TemplateResponse("vocals.html", {
                "request": request,
                "error": f"Failed to publish voice message: {stderr.decode()}",
                "myIPFS": await get_myipfs_gateway()
            })
            
    except Exception as e:
        return templates.TemplateResponse("vocals.html", {
            "request": request,
            "error": f"Error processing voice message: {str(e)}",
            "myIPFS": await get_myipfs_gateway()
        })

def transform_youtube_metadata_to_structured(flat_metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Transform flat YouTube metadata to structured format"""
    channel_name = flat_metadata.get('channel') or flat_metadata.get('uploader', '')
    channel_info = {
        'display_name': channel_name,
        'name': channel_name,
        'channel_id': flat_metadata.get('channel_id', ''),
        'channel_url': flat_metadata.get('channel_url', ''),
        'channel_follower_count': flat_metadata.get('channel_follower_count')
    }
    
    content_info = {
        'description': flat_metadata.get('description', ''),
        'language': flat_metadata.get('language', ''),
        'license': flat_metadata.get('license', ''),
        'tags': flat_metadata.get('tags', []),
        'categories': flat_metadata.get('categories', [])
    }
    
    technical_info = {
        'abr': flat_metadata.get('abr', 0),
        'acodec': flat_metadata.get('acodec', ''),
        'format_note': flat_metadata.get('format_note', ''),
        'vcodec': flat_metadata.get('vcodec', ''),
        'vbr': flat_metadata.get('vbr', 0),
        'tbr': flat_metadata.get('tbr', 0),
        'resolution': flat_metadata.get('resolution', ''),
        'fps': flat_metadata.get('fps', 0)
    }
    
    statistics = {
        'view_count': flat_metadata.get('view_count', 0),
        'like_count': flat_metadata.get('like_count', 0),
        'comment_count': flat_metadata.get('comment_count', 0),
        'average_rating': flat_metadata.get('average_rating')
    }
    
    dates = {
        'upload_date': flat_metadata.get('upload_date', ''),
        'release_date': flat_metadata.get('release_date', ''),
        'timestamp': flat_metadata.get('timestamp'),
        'release_timestamp': flat_metadata.get('release_timestamp')
    }
    
    media_info = {
        'artist': flat_metadata.get('artist', ''),
        'album': flat_metadata.get('album', ''),
        'track': flat_metadata.get('track', ''),
        'creator': flat_metadata.get('creator', '')
    }
    
    thumbnails = {
        'thumbnail': flat_metadata.get('thumbnail', ''),
        'thumbnails': flat_metadata.get('thumbnails', [])
    }
    
    playlist_info = {}
    if flat_metadata.get('playlist') or flat_metadata.get('playlist_id'):
        playlist_info = {
            'playlist': flat_metadata.get('playlist', ''),
            'playlist_id': flat_metadata.get('playlist_id', ''),
            'playlist_title': flat_metadata.get('playlist_title', ''),
            'playlist_index': flat_metadata.get('playlist_index'),
            'n_entries': flat_metadata.get('n_entries')
        }
    
    subtitles_info = {
        'subtitles': flat_metadata.get('subtitles', {}),
        'automatic_captions': flat_metadata.get('automatic_captions', {})
    }
    
    live_info = {
        'live_status': flat_metadata.get('live_status', ''),
        'was_live': flat_metadata.get('was_live', False),
        'is_live': flat_metadata.get('is_live', False)
    }
    
    structured = {
        'title': flat_metadata.get('title', ''),
        'description': flat_metadata.get('description', ''),
        'duration': flat_metadata.get('duration', 0),
        'youtube_url': flat_metadata.get('youtube_url', ''),
        'youtube_short_url': flat_metadata.get('youtube_short_url', ''),
        'youtube_id': flat_metadata.get('youtube_id', ''),
        'uploader': flat_metadata.get('uploader', ''),
        'uploader_id': flat_metadata.get('uploader_id', ''),
        'uploader_url': flat_metadata.get('uploader_url', ''),
        
        'channel_info': channel_info,
        'content_info': content_info,
        'technical_info': technical_info,
        'statistics': statistics,
        'dates': dates,
        'media_info': media_info,
        'thumbnails': thumbnails,
        'playlist_info': playlist_info if playlist_info else None,
        'subtitles_info': subtitles_info,
        'chapters': flat_metadata.get('chapters', []),
        'live_info': live_info,
        
        'age_limit': flat_metadata.get('age_limit'),
        'availability': flat_metadata.get('availability', ''),
        'format': flat_metadata.get('format', ''),
        'format_id': flat_metadata.get('format_id', ''),
        'ext': flat_metadata.get('ext', ''),
        'filesize': flat_metadata.get('filesize'),
        'filesize_approx': flat_metadata.get('filesize_approx'),
        'location': flat_metadata.get('location'),
        'license': flat_metadata.get('license', ''),
        'languages': flat_metadata.get('languages', []),
        'extractor': flat_metadata.get('extractor', ''),
        'extractor_key': flat_metadata.get('extractor_key', ''),
        
        'youtube_metadata': flat_metadata
    }
    
    for key in ['playlist_info', 'channel_info', 'content_info', 'technical_info', 
                'statistics', 'dates', 'media_info', 'thumbnails', 'subtitles_info', 'live_info']:
        if structured.get(key) is None:
            del structured[key]
        elif isinstance(structured.get(key), dict):
            structured[key] = {k: v for k, v in structured[key].items() if v is not None}
    
    return structured

@router.post("/api/fileupload", response_model=UploadResponse)
@router.post("/api/upload", response_model=UploadResponse)
async def upload_file_to_ipfs(
    file: UploadFile = File(...),
    npub: str = Form(...),
    youtube_metadata: Optional[UploadFile] = File(None)
):
    """Upload file to IPFS with NIP-42 authentication."""
    npub = await require_nostr_auth(npub, force_check=True)

    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    try:
        max_size_bytes = get_max_file_size_for_user(npub)
        if file.size and file.size > max_size_bytes:
            max_size_mb = max_size_bytes // 1048576
            file_size_mb = file.size // 1048576
            raise HTTPException(
                status_code=413,
                detail=f"File size ({file_size_mb}MB) exceeds maximum allowed size ({max_size_mb}MB)"
            )
        
        max_size_mb = max_size_bytes // 1048576
        validation_result = await validate_uploaded_file(file, max_size_mb=max_size_mb)
        if not validation_result["is_valid"]:
            raise HTTPException(status_code=400, detail=validation_result["error"])
        
        user_NOSTR_path = get_authenticated_user_directory(npub)
        user_drive_path = user_NOSTR_path  / "APP" / "uDRIVE"
        
        file_content = await file.read()
        file_type = detect_file_type(file_content, file.filename or "untitled")
        
        if file.filename and file.filename.endswith('.txt'):
            try:
                content_text = file_content.decode('utf-8')
                is_netscape_format = False
                detected_domain = None
                
                if '# Netscape HTTP Cookie File' in content_text or '# HTTP Cookie File' in content_text:
                    is_netscape_format = True
                elif '\t' in content_text:
                    lines = [l.strip() for l in content_text.split('\n') if l.strip() and not l.strip().startswith('#')]
                    if lines:
                        first_line = lines[0]
                        parts = first_line.split('\t')
                        if len(parts) >= 7:
                            is_netscape_format = True
                
                if is_netscape_format:
                    domains = set()
                    lines = content_text.split('\n')
                    for line in lines:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        parts = line.split('\t')
                        if len(parts) >= 7:
                            domain = parts[0].strip()
                            if domain.startswith('.'):
                                domain = domain[1:]
                            domains.add(domain)
                    
                    if domains:
                        base_domains = set()
                        for domain in domains:
                            parts = domain.split('.')
                            if len(parts) >= 2:
                                base_domain = '.'.join(parts[-2:])
                                base_domains.add(base_domain)
                            else:
                                base_domains.add(domain)
                        
                        if len(base_domains) > 1:
                            raise HTTPException(
                                status_code=400, 
                                detail=f"Multi-domain cookie files are not supported."
                            )
                        
                        sorted_domains = sorted(domains, key=len)
                        detected_domain = sorted_domains[0]
                    else:
                        raise HTTPException(
                            status_code=400, 
                            detail="Invalid cookie file: no domains detected"
                        )
                    
                    hex_pubkey = npub_to_hex(npub)
                    user_root_dir = find_user_directory_by_hex(hex_pubkey)
                    
                    cookie_filename = f".{detected_domain}.cookie"
                    cookie_path = user_root_dir / cookie_filename
                    
                    async with aiofiles.open(cookie_path, 'wb') as cookie_file:
                        await cookie_file.write(file_content)
                    
                    os.chmod(cookie_path, 0o600)
                    
                    return UploadResponse(
                        success=True,
                        message=f"Cookie file uploaded successfully for {detected_domain}",
                        file_path=str(cookie_path.relative_to(user_root_dir.parent)),
                        file_type="netscape_cookies",
                        target_directory=str(user_root_dir),
                        new_cid=None,
                        timestamp=datetime.now().isoformat(),
                        auth_verified=True,
                        description=f"Domain: {detected_domain or 'unknown'}"
                    )
            except UnicodeDecodeError:
                pass
            except Exception as e:
                pass
        
        if file_type == 'image':
            target_dir = user_drive_path / "Images"
        elif file_type == 'video':
            target_dir = user_drive_path / "Videos"
        elif file_type == 'audio':
            target_dir = user_drive_path / "Music"
        elif file_type == 'document':
            target_dir = user_drive_path / "Documents"
        elif file_type == 'application':
            target_dir = user_drive_path / "Apps"
        else:
            target_dir = user_drive_path / "Documents"
        
        target_dir.mkdir(parents=True, exist_ok=True)
        
        original_filename = file.filename if file.filename else "untitled_file"
        sanitized_filename = sanitize_filename_python(original_filename)
        
        description = None
        if file_type == 'image':
            try:
                temp_image_path = target_dir / f"temp_{uuid.uuid4()}_{sanitized_filename}"
                async with aiofiles.open(temp_image_path, 'wb') as out_file:
                    await out_file.write(file_content)
                
                from core.config import settings
                describe_script = settings.ZEN_PATH / "Astroport.ONE" / "IA" / "describe_image.py"
                
                custom_prompt = "Décris ce qui se trouve sur cette image en 10-30 mots clés concis et précis. Ne génère qu'une description courte sans phrase complète, ni introduction."
                desc_process = await asyncio.create_subprocess_exec(
                    "python3", describe_script, str(temp_image_path), "--json", "--prompt", custom_prompt,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                desc_stdout, desc_stderr = await desc_process.communicate()
                
                if desc_process.returncode == 0:
                    from utils.helpers import safe_json_load
                    try:
                        desc_json = safe_json_load(desc_stdout.decode())
                        description = desc_json.get('description', '')
                        if description:
                            description = description.strip()
                    except ValueError:
                        pass
                
                if os.path.exists(temp_image_path):
                    os.remove(temp_image_path)
            except Exception:
                pass
        
        file_path = target_dir / sanitized_filename
        
        async with aiofiles.open(file_path, 'wb') as out_file:
            await out_file.write(file_content)
        
        from core.config import settings
        tmp_dir = settings.ZEN_PATH / "tmp"
        os.makedirs(tmp_dir, exist_ok=True)
        temp_file_path = os.path.join(tmp_dir, f"temp_{uuid.uuid4()}.json")
        
        script_path = None
        possible_paths = [
            "./upload2ipfs.sh",
            str(settings.ZEN_PATH / "Astroport.ONE" / "UPassport" / "upload2ipfs.sh"),
            os.path.join(os.path.dirname(__file__), "..", "upload2ipfs.sh")
        ]
        for path in possible_paths:
            if os.path.exists(path) and os.path.isfile(path):
                script_path = os.path.abspath(path)
                break
        
        if not script_path:
            raise HTTPException(status_code=500, detail="upload2ipfs.sh script not found")
        
        user_pubkey_hex = ""
        try:
            if npub and npub != "anonymous":
                user_pubkey_hex = npub_to_hex(npub)
        except Exception:
            pass
        
        youtube_metadata_file = None
        if youtube_metadata:
            try:
                youtube_metadata_content = await youtube_metadata.read()
                youtube_metadata_json = json.loads(youtube_metadata_content.decode('utf-8'))
                structured_metadata = transform_youtube_metadata_to_structured(youtube_metadata_json)
                
                youtube_metadata_file = os.path.join(tmp_dir, f"youtube_metadata_{uuid.uuid4()}.json")
                async with aiofiles.open(youtube_metadata_file, 'w') as metadata_file:
                    await metadata_file.write(json.dumps(structured_metadata, indent=2))
            except Exception:
                youtube_metadata_file = None
        
        if youtube_metadata_file:
            return_code, last_line = await run_script(
                script_path, 
                "--metadata", youtube_metadata_file,
                str(file_path), 
                temp_file_path, 
                user_pubkey_hex
            )
            if os.path.exists(youtube_metadata_file):
                os.remove(youtube_metadata_file)
        else:
            return_code, last_line = await run_script(script_path, str(file_path), temp_file_path, user_pubkey_hex)
        
        if return_code == 0:
            try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip())
                
                response_fileName = json_output.get('fileName') or sanitized_filename
                info_cid = json_output.get('info')
                thumbnail_cid = json_output.get('thumbnail_ipfs') or ''
                gifanim_cid = json_output.get('gifanim_ipfs') or ''
                file_hash = json_output.get('fileHash') or ''
                mime_type = json_output.get('mimeType') or ''
                duration = json_output.get('duration')
                dimensions = json_output.get('dimensions') or ''
                provenance_info = json_output.get('provenance', {})
                upload_chain = provenance_info.get('upload_chain') or ''
                is_reupload = provenance_info.get('is_reupload', False)
                
                file_mime = mime_type or json_output.get('mimeType', '')
                
                if not file_mime.startswith('video/') and not file_mime.startswith('audio/') and user_pubkey_hex:
                    try:
                        user_dir = get_authenticated_user_directory(npub)
                        secret_file = user_dir / ".secret.nostr"
                        
                        if secret_file.exists():
                            from core.config import settings
                            publish_script = settings.TOOLS_PATH / "publish_nostr_file.sh"
                            
                            if os.path.exists(publish_script):
                                file_type_display = file_type.capitalize()
                                event_description = f"{file_type_display}: {response_fileName}"
                                if description:
                                    event_description = f"{description}"
                                
                                if is_reupload:
                                    original_author = provenance_info.get('original_author', '')[:16]
                                    event_description = f"📤 Re-upload: {event_description} (Original: {original_author}...)"
                                
                                publish_cmd = [
                                    "bash", publish_script,
                                    "--auto", temp_file_path,
                                    "--nsec", str(secret_file),
                                    "--title", response_fileName,
                                    "--description", event_description,
                                    "--json"
                                ]
                                
                                import asyncio
                                process = await asyncio.create_subprocess_exec(
                                    *publish_cmd,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE
                                )
                                await asyncio.wait_for(process.communicate(), timeout=30)
                    except Exception:
                        pass
                
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                
                return UploadResponse(
                    success=True,
                    message=f"File uploaded successfully to IPFS",
                    file_path=str(file_path),
                    file_type=file_type,
                    target_directory=str(target_dir),
                    new_cid=json_output.get('cid'),
                    timestamp=datetime.now().isoformat(),
                    auth_verified=True,
                    fileName=response_fileName,
                    description=description,
                    info=info_cid,
                    thumbnail_ipfs=thumbnail_cid if thumbnail_cid else None,
                    gifanim_ipfs=gifanim_cid if gifanim_cid else None,
                    fileHash=file_hash if file_hash else None,
                    mimeType=mime_type if mime_type else None,
                    duration=int(duration) if duration is not None else None,
                    dimensions=dimensions if dimensions else None,
                    upload_chain=upload_chain if upload_chain else None
                )
                
            except (json.JSONDecodeError, FileNotFoundError) as e:
                raise HTTPException(status_code=500, detail=f"Failed to process IPFS upload: {e}")
            finally:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
        else:
            raise HTTPException(status_code=500, detail=f"IPFS upload failed: {last_line.strip()}")
            
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

from models.schemas import UploadFromDriveRequest, UploadFromDriveResponse

@router.post("/api/upload_from_drive", response_model=UploadFromDriveResponse)
async def upload_from_drive(request: UploadFromDriveRequest):
    if request.owner_hex_pubkey or request.owner_email:
        logging.info(f"Sync from drive - Source owner: {request.owner_email} (hex: {request.owner_hex_pubkey[:12] if request.owner_hex_pubkey else 'N/A'}...)")
    
    request.npub = await require_nostr_auth(request.npub)

    try:
        user_NOSTR_path = get_authenticated_user_directory(request.npub)
        user_drive_path = user_NOSTR_path / "APP" / "uDRIVE"

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error determining user directory: {e}")

    parts = request.ipfs_link.split('/')
    extracted_filename = parts[-1] if parts else "downloaded_file"

    sanitized_filename = sanitize_filename_python(extracted_filename)

    file_type = detect_file_type(b'', sanitized_filename)

    target_directory_name = "Documents"
    if file_type == "image":
        target_directory_name = "Images"
    elif file_type == "audio":
        target_directory_name = "Music"
    elif file_type == "video":
        target_directory_name = "Videos"

    target_directory = user_drive_path / target_directory_name
    target_directory.mkdir(parents=True, exist_ok=True)

    target_file_path = (target_directory / sanitized_filename).resolve()

    if not target_file_path.is_relative_to(user_drive_path):
        raise HTTPException(status_code=400, detail="Invalid file path operation: attempted to write outside user's directory.")

    try:
        full_ipfs_url = f"/ipfs/{request.ipfs_link}"
        logging.info(f"Attempting to download IPFS link: {full_ipfs_url} to {target_file_path}")

        import asyncio
        ipfs_get_command = ["ipfs", "get", "-o", str(target_file_path), full_ipfs_url]
        process = await asyncio.create_subprocess_exec(
            *ipfs_get_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_message = stderr.decode().strip()
            logging.error(f"IPFS download failed for {full_ipfs_url}: {error_message}")
            raise Exception(f"IPFS download failed: {error_message}")

        file_size = target_file_path.stat().st_size
        logging.info(f"File '{sanitized_filename}' downloaded from IPFS and saved to '{target_file_path}' (Size: {file_size} bytes)")

        from services.ipfs import run_uDRIVE_generation_script
        ipfs_result = await run_uDRIVE_generation_script(user_drive_path)
        new_cid_info = ipfs_result.get("final_cid")
        logging.info(f"New IPFS CID generated: {new_cid_info}")

        return UploadFromDriveResponse(
            success=True,
            message="File synchronized successfully from IPFS",
            file_path=str(target_file_path.relative_to(user_drive_path)),
            file_type=file_type,
            new_cid=new_cid_info,
            timestamp=datetime.now().isoformat(),
            auth_verified=True
        )
    except Exception as e:
        logging.error(f"Error downloading from IPFS or saving file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to synchronize file: {e}")

@router.post("/upload2ipfs")
async def upload_to_ipfs(request: Request, file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    original_filename = file.filename or "unknown"
    file_location = f"tmp/{original_filename}"
    
    user_pubkey_hex = ""
    user_npub = None
    try:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Nostr "):
            auth_base64 = auth_header.replace("Nostr ", "").strip()
            auth_json = base64.b64decode(auth_base64).decode('utf-8')
            auth_event = json.loads(auth_json)
            
            if auth_event.get("kind") == 27235 and "pubkey" in auth_event:
                user_pubkey_hex = auth_event["pubkey"]
                user_npub = hex_to_npub(user_pubkey_hex) if user_pubkey_hex else None
                logging.info(f"🔑 NIP-98 Auth: Provenance tracking enabled for user: {user_pubkey_hex[:16]}...")
            else:
                logging.warning(f"⚠️ Invalid NIP-98 event: kind={auth_event.get('kind')}")
        else:
            logging.info(f"ℹ️ No NIP-98 Authorization header, uploading without provenance tracking")
    except Exception as e:
        logging.warning(f"⚠️ Could not extract pubkey from NIP-98 Authorization header: {e}")
    
    if user_npub:
        max_size_bytes = get_max_file_size_for_user(user_npub)
    else:
        max_size_bytes = 104857600
    
    if file.size and file.size > max_size_bytes:
        max_size_mb = max_size_bytes // 1048576
        file_size_mb = file.size // 1048576
        raise HTTPException(
            status_code=413,
            detail={
                "status": "error",
                "message": f"File size ({file_size_mb}MB) exceeds maximum allowed size ({max_size_mb}MB per UPlanet_FILE_CONTRACT.md)"
            }
        )
    
    try:
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        temp_file_path = f"tmp/temp_{uuid.uuid4()}.json"

        script_path = "./upload2ipfs.sh"
        
        return_code, last_line = await run_script(script_path, file_location, temp_file_path, user_pubkey_hex)

        if return_code == 0:
          try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip())

                new_cid = json_output.get("new_cid") or json_output.get("cid", "")
                file_hash = json_output.get("fileHash") or json_output.get("file_hash") or json_output.get("x", "")
                mime_type = json_output.get("mimeType") or json_output.get("file_type") or json_output.get("m", "")
                file_name = json_output.get("fileName") or original_filename or ""
                file_size = json_output.get("file_size") or json_output.get("size", "")
                dimensions = json_output.get("dimensions") or json_output.get("dim", "")
                info_cid = json_output.get("info") or ""
                thumbnail_ipfs = json_output.get("thumbnail_ipfs") or ""
                gifanim_ipfs = json_output.get("gifanim_ipfs") or ""
                upload_chain = json_output.get("upload_chain") or ""
                
                ipfs_gateway = (await get_myipfs_gateway()).rstrip('/')
                if new_cid and file_name:
                    ipfs_url = f"/ipfs/{new_cid}/{file_name}"
                elif new_cid:
                    ipfs_url = f"/ipfs/{new_cid}"
                else:
                    ipfs_url = ""
                
                tags = []
                
                if ipfs_url:
                    tags.append(["url", ipfs_url])
                
                if file_hash:
                    tags.append(["ox", file_hash])
                    tags.append(["x", file_hash])
                
                if mime_type:
                    tags.append(["m", mime_type])
                
                if file_size:
                    tags.append(["size", str(file_size)])
                
                if dimensions:
                    tags.append(["dim", dimensions])
                
                if info_cid:
                    tags.append(["info", info_cid])
                
                if thumbnail_ipfs:
                    tags.append(["thumbnail_ipfs", thumbnail_ipfs])
                
                if gifanim_ipfs:
                    tags.append(["gifanim_ipfs", gifanim_ipfs])
                
                if upload_chain:
                    tags.append(["upload_chain", upload_chain])
                
                nip96_response = {
                    "status": "success",
                    "message": json_output.get("message", "File uploaded successfully"),
                    "nip94_event": {
                        "tags": tags,
                        "content": ""
                    }
                }
                
                if new_cid:
                    nip96_response["new_cid"] = new_cid
                if file_hash:
                    nip96_response["file_hash"] = file_hash
                if mime_type:
                    nip96_response["file_type"] = mime_type
                if info_cid:
                    nip96_response["info"] = info_cid
                if thumbnail_ipfs:
                    nip96_response["thumbnail_ipfs"] = thumbnail_ipfs
                if gifanim_ipfs:
                    nip96_response["gifanim_ipfs"] = gifanim_ipfs
                if dimensions:
                    nip96_response["dimensions"] = dimensions
                if json_output.get("duration"):
                    nip96_response["duration"] = json_output.get("duration")
                
                os.remove(temp_file_path)
                os.remove(file_location)
                return JSONResponse(content=nip96_response)
          except (json.JSONDecodeError, FileNotFoundError) as e:
                logging.error(f"Failed to decode JSON from temp file: {temp_file_path}, Error: {e}")
                raise HTTPException(status_code=500, detail="Failed to process script output, JSON decode error.")
          finally:
                if os.path.exists(temp_file_path):
                   os.remove(temp_file_path)
                if os.path.exists(file_location):
                  os.remove(file_location)
        else:
           logging.error(f"Script execution failed: {last_line.strip()}")
           raise HTTPException(status_code=500, detail="Script execution failed.")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

# ==================== IMAGE UPLOAD (TrocZen/ZENBOX compatible) ====================

IMAGE_MAGIC_BYTES = {
    'png': [b'\x89PNG\r\n\x1a\n'],
    'jpg': [b'\xFF\xD8\xFF'],
    'jpeg': [b'\xFF\xD8\xFF'],
    'webp': [b'RIFF'],
    'gif': [b'GIF87a', b'GIF89a'],
}

IMAGE_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

IMAGE_MIME_TYPES = {
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'webp': 'image/webp',
    'gif': 'image/gif',
}

MAX_IMAGE_SIZE = 5 * 1024 * 1024

def _validate_image_magic_bytes(file_content: bytes, extension: str) -> bool:
    ext = extension.lower()
    if ext not in IMAGE_MAGIC_BYTES:
        return False
    if ext == 'webp':
        return len(file_content) >= 12 and file_content[:4] == b'RIFF' and file_content[8:12] == b'WEBP'
    for sig in IMAGE_MAGIC_BYTES[ext]:
        if file_content.startswith(sig):
            return True
    return False

def _validate_image_file(filename: str, file_content: bytes) -> tuple:
    if '.' not in filename:
        return False, "File has no extension"
    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in IMAGE_ALLOWED_EXTENSIONS:
        return False, f"Extension '{ext}' not allowed. Allowed: {', '.join(IMAGE_ALLOWED_EXTENSIONS)}"
    if len(file_content) < 12:
        return False, "File too small for validation"
    if not _validate_image_magic_bytes(file_content[:12], ext):
        return False, f"Magic bytes don't match extension '{ext}'. Potentially malicious file."
    return True, None

async def _upload_image_to_ipfs(filepath: str) -> tuple:
    try:
        import aiohttp
        ipfs_api_url = "http://127.0.0.1:5001"
        with open(filepath, 'rb') as f:
            file_content = f.read()
        data = aiohttp.FormData()
        data.add_field('file', file_content,
                       filename=os.path.basename(filepath),
                       content_type='application/octet-stream')
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f'{ipfs_api_url}/api/v0/add', data=data) as response:
                if response.status == 200:
                    result = await response.json()
                    cid = result['Hash']
                    ipfs_gateway = (await get_myipfs_gateway()).rstrip('/')
                    ipfs_url = f"{ipfs_gateway}/ipfs/{cid}"
                    return cid, ipfs_url
        return None, None
    except Exception as e:
        logging.error(f"IPFS image upload error: {e}")
        return None, None

@router.post("/api/upload/image")
async def upload_image(
    file: UploadFile = File(...),
    npub: str = Form(...),
    type: str = Form(default="avatar"),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    image_type = type.lower()
    if image_type not in ('avatar', 'banner', 'logo'):
        raise HTTPException(status_code=400,
                            detail="Invalid image type (must be avatar, banner, or logo)")

    if not npub:
        raise HTTPException(status_code=400, detail="Missing npub")

    file_header = await file.read(12)
    await file.seek(0)

    is_valid, error_msg = _validate_image_file(file.filename, file_header)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=413,
                            detail=f"File size ({len(content) // 1024}KB) exceeds maximum (5MB)")

    original_name = sanitize_filename_python(file.filename)
    # Gestion sécurisée de l'extension : éviter IndexError si absent
    _parts = original_name.rsplit('.', 1)
    ext = _parts[1].lower() if len(_parts) == 2 and _parts[1] else 'bin'
    timestamp = int(datetime.now().timestamp())
    npub_short = npub[:16] if len(npub) > 16 else npub
    new_filename = f"{npub_short}_{image_type}_{timestamp}.{ext}"

    uploads_dir = Path("uploads")
    uploads_dir.mkdir(exist_ok=True)
    filepath = uploads_dir / new_filename

    async with aiofiles.open(str(filepath), 'wb') as f:
        await f.write(content)

    file_hash = hashlib.sha256(content).hexdigest()

    local_url = f"/uploads/{new_filename}"

    cid, ipfs_url = await _upload_image_to_ipfs(str(filepath))

    return JSONResponse(content={
        'success': True,
        'url': ipfs_url or local_url,
        'local_url': local_url,
        'ipfs_url': ipfs_url,
        'ipfs_cid': cid,
        'ipfs_status': 'completed' if cid else 'failed',
        'filename': new_filename,
        'checksum': file_hash,
        'size': len(content),
        'uploaded_at': datetime.now().isoformat(),
        'storage': 'ipfs' if cid else 'local',
        'type': image_type,
        'mime_type': IMAGE_MIME_TYPES.get(ext, 'application/octet-stream'),
    }, status_code=201)

@router.get("/uploads/{filename}")
async def serve_upload(filename: str):
    uploads_dir = Path("uploads").resolve()
    filepath = (uploads_dir / sanitize_filename_python(filename)).resolve()
    
    if not str(filepath).startswith(str(uploads_dir.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden")
        
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    from fastapi.responses import FileResponse
    return FileResponse(str(filepath))
