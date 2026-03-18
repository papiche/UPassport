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
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from utils.helpers import run_script, get_myipfs_gateway, as_form, render_page
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
from models.schemas import UploadResponse

router = APIRouter()
templates = Jinja2Templates(directory="templates")

from utils.helpers import send_server_side_analytics

@router.get("/theater", response_class=HTMLResponse)
async def theater_modal_route(request: Request, video: Optional[str] = None):
    import json as _json
    use_local_js = True
    video_metadata = None
    video_title = "Unknown"
    video_author = None
    video_kind = None
    video_duration = 0
    video_channel = ""
    video_source_type = ""
    server_video_data = None   # pre-rendered NIP-71 data for client JS

    if video:
        try:
            from services.nostr import fetch_video_event_from_nostr, parse_video_metadata
            video_event = await fetch_video_event_from_nostr(video, timeout=5)
            if video_event:
                video_metadata = await parse_video_metadata(video_event)
                video_title  = video_metadata.get('title', 'Unknown')
                video_author = video_metadata.get('author_id', '')
                video_kind   = video_metadata.get('kind', 21)

                # ── Extract full NIP-71 tags for client-side rendering ──────
                # This lets theater-modal.html set window.videoData directly
                # from server-rendered JSON and skip the external-relay fetch,
                # which would fail for events published only to the local relay.
                tags = video_event.get("tags", [])
                ipfs_url      = ""
                thumbnail_ipfs = ""
                gifanim_ipfs  = ""
                info_cid      = ""
                file_hash     = ""
                upload_chain  = ""
                duration_tag  = 0
                dimensions    = ""
                source_type   = "webcam"
                uploader_tag  = ""

                for tag in tags:
                    if not isinstance(tag, list) or len(tag) < 2:
                        continue
                    t, v = tag[0], tag[1]
                    if t == "url" and ("/ipfs/" in v or "ipfs://" in v):
                        ipfs_url = v.replace("ipfs://", "/ipfs/")
                    elif t == "thumbnail_ipfs" and not thumbnail_ipfs:
                        thumbnail_ipfs = v.split("/ipfs/")[-1] if v.startswith("/ipfs/") else v
                    elif t == "image" and not thumbnail_ipfs:
                        thumbnail_ipfs = v
                    elif t == "gifanim_ipfs":
                        gifanim_ipfs = v.split("/ipfs/")[-1] if v.startswith("/ipfs/") else v
                    elif t == "info":
                        info_cid = v
                    elif t == "x":
                        file_hash = v
                    elif t == "upload_chain":
                        upload_chain = v
                    elif t == "duration":
                        try:
                            duration_tag = int(float(v))
                        except (ValueError, TypeError):
                            pass
                    elif t == "dim":
                        dimensions = v
                    elif t == "t" and v.startswith("Channel-"):
                        video_channel = v
                    elif t == "uploader":
                        uploader_tag = v
                    elif t == "i" and v.startswith("source:"):
                        source_type = v.replace("source:", "")
                    elif t == "t" and t[1:] not in ["analytics", "encrypted", "ipfs"]:
                        pass  # other topic tags – collected above for channel

                if ipfs_url:  # only inject pre-rendered data when we have a playable URL
                    server_video_data = _json.dumps({
                        "event_id":      video,
                        "ipfs_url":      ipfs_url,
                        "thumbnail_ipfs": thumbnail_ipfs,
                        "gifanim_ipfs":  gifanim_ipfs,
                        "info_cid":      info_cid,
                        "file_hash":     file_hash,
                        "upload_chain":  upload_chain,
                        "title":         video_title,
                        "duration":      duration_tag,
                        "dimensions":    dimensions,
                        "channel":       video_channel,
                        "uploader":      uploader_tag or video_author or "",
                        "source_type":   source_type,
                        "author_id":     video_author or "",
                        "kind":          video_kind or 21,
                    }, ensure_ascii=False)
                    logging.info(
                        f"✅ Theater server-preloaded video data for {video[:16]}… "
                        f"(ipfs_url={ipfs_url[:40]}…)"
                    )

        except Exception as e:
            logging.warning(f"⚠️ Could not fetch video metadata for Open Graph: {e}")

    base_url = str(request.base_url).rstrip('/')
    theater_url = f"{base_url}/theater"
    if video:
        theater_url = f"{theater_url}?video={video}"

    return render_page(request, "theater-modal.html", {
        "use_local_js":      use_local_js,
        "video_id":          video,
        "video_metadata":    video_metadata,
        "theater_url":       theater_url,
        "server_video_data": server_video_data,   # JSON string or None
    })

@router.get("/mp3-modal", response_class=HTMLResponse)
async def mp3_modal_route(request: Request, track: Optional[str] = None):
    use_local_js = True
    return render_page(request, "mp3-modal.html", {
        "use_local_js": use_local_js,
        "track_id": track
    })

@router.get("/playlist", response_class=HTMLResponse)
async def playlist_manager_route(request: Request, id: Optional[str] = None):
    return render_page(request, "playlist-manager.html", {
        "playlist_id": id
    })

@router.get("/tags", response_class=HTMLResponse)
async def tags_route(request: Request, video: Optional[str] = None):
    hostname = request.headers.get("host", "u.copylaradio.com")
    if hostname.startswith("u."):
        ipfs_gateway = f"https://ipfs.{hostname[2:]}"
    elif hostname.startswith("127.0.0.1") or hostname.startswith("localhost"):
        ipfs_gateway = "http://127.0.0.1:8080"
    else:
        ipfs_gateway = "https://ipfs.copylaradio.com"
    
    return render_page(request, "tags.html", {
        "myIPFS": ipfs_gateway,
        "video_id": video
    })

@router.get("/contrib", response_class=HTMLResponse)
async def contrib_route(request: Request, video: Optional[str] = None, kind: Optional[str] = None):
    hostname = request.headers.get("host", "u.copylaradio.com")
    if hostname.startswith("u."):
        ipfs_gateway = f"https://ipfs.{hostname[2:]}"
    elif hostname.startswith("127.0.0.1") or hostname.startswith("localhost"):
        ipfs_gateway = "http://127.0.0.1:8080"
    else:
        ipfs_gateway = "https://ipfs.copylaradio.com"
    
    return render_page(request, "contrib.html", {
        "myIPFS": ipfs_gateway,
        "video_id": video,
        "video_kind": kind or "21"
    })

@router.get("/youtube")
async def youtube_route(
    request: Request, 
    html: Optional[str] = None, 
    channel: Optional[str] = None, 
    search: Optional[str] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    duration_min: Optional[int] = None,
    duration_max: Optional[int] = None,
    sort_by: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius: Optional[float] = None,
    video: Optional[str] = None
):
    """YouTube video channels and search from NOSTR events"""
    use_local_js = True
    
    try:
        import sys
        from core.config import settings
        sys.path.append(str(settings.ZEN_PATH / "Astroport.ONE" / "IA"))
        try:
            from create_video_channel import fetch_and_process_nostr_events, create_channel_playlist
        except ImportError:
            logging.error("Could not import create_video_channel")
            if html is not None:
                return HTMLResponse(content="<html><body><h1>Error</h1><p>Video channel module not found</p></body></html>", status_code=500)
            raise HTTPException(status_code=500, detail="Video channel module not found")
        
        try:
            video_messages = await asyncio.wait_for(
                fetch_and_process_nostr_events("ws://127.0.0.1:7777", 200),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            logging.warning("⚠️ Timeout fetching NOSTR events, using empty list")
            video_messages = []
        except Exception as fetch_error:
            logging.error(f"❌ Error fetching NOSTR events: {fetch_error}")
            video_messages = []
        
        validated_videos = []
        for video_item in video_messages:
            if not video_item.get('title') or not video_item.get('ipfs_url'):
                continue
            
            info_cid = video_item.get('info_cid', '')
            metadata_ipfs = video_item.get('metadata_ipfs', '') or info_cid
            
            normalized_video = {
                'title': video_item.get('title', ''),
                'uploader': video_item.get('uploader', ''),
                'content': video_item.get('content', ''),
                'duration': int(video_item.get('duration', 0)) if str(video_item.get('duration', 0)).isdigit() else 0,
                'ipfs_url': video_item.get('ipfs_url', ''),
                'youtube_url': video_item.get('youtube_url', '') or video_item.get('original_url', ''),
                'thumbnail_ipfs': video_item.get('thumbnail_ipfs', ''),
                'gifanim_ipfs': video_item.get('gifanim_ipfs', ''),
                'metadata_ipfs': metadata_ipfs,
                'subtitles': video_item.get('subtitles', []),
                'channel_name': video_item.get('channel_name', ''),
                'topic_keywords': video_item.get('topic_keywords', ''),
                'created_at': video_item.get('created_at', ''),
                'download_date': video_item.get('download_date', '') or video_item.get('created_at', ''),
                'file_size': int(video_item.get('file_size', 0)) if str(video_item.get('file_size', 0)).isdigit() else 0,
                'message_id': video_item.get('message_id', ''),
                'author_id': video_item.get('author_id', ''),
                'latitude': video_item.get('latitude'),
                'longitude': video_item.get('longitude'),
                'provenance': video_item.get('provenance', 'unknown'),
                'source_type': video_item.get('source_type', 'webcam'),
                'compliance': video_item.get('compliance', {}),
                'compliance_score': video_item.get('compliance_score', 0),
                'compliance_percent': video_item.get('compliance_percent', 0),
                'compliance_level': video_item.get('compliance_level', 'non-compliant'),
                'is_compliant': video_item.get('is_compliant', False),
                'file_hash': video_item.get('file_hash', ''),
                'info_cid': info_cid,
                'upload_chain': video_item.get('upload_chain', ''),
                'upload_chain_list': video_item.get('upload_chain_list', [])
            }
            validated_videos.append(normalized_video)
        
        video_messages = validated_videos
        filtered_videos = []
        
        for video_item in video_messages:
            if channel and video_item.get('channel_name', '').lower() != channel.lower():
                continue
            
            if search:
                search_lower = search.lower()
                if not (search_lower in video_item.get('title', '').lower() or 
                       search_lower in video_item.get('topic_keywords', '').lower()):
                    continue
            
            if keyword:
                keywords = [k.strip().lower() for k in keyword.split(',')]
                video_keywords = video_item.get('topic_keywords', '').lower()
                if not any(k in video_keywords for k in keywords):
                    continue
            
            if date_from or date_to:
                video_date = video_item.get('created_at', '')
                if video_date:
                    try:
                        from datetime import datetime as dt
                        video_datetime = dt.fromisoformat(video_date.replace('Z', '+00:00'))
                        video_date_str = video_datetime.strftime('%Y-%m-%d')
                        
                        if date_from and video_date_str < date_from:
                            continue
                        if date_to and video_date_str > date_to:
                            continue
                    except:
                        continue
            
            if duration_min is not None or duration_max is not None:
                video_duration = video_item.get('duration', 0)
                if isinstance(video_duration, str):
                    try:
                        video_duration = int(video_duration)
                    except:
                        video_duration = 0
                
                if duration_min is not None and video_duration < duration_min:
                    continue
                if duration_max is not None and video_duration > duration_max:
                    continue
            
            if lat is not None and lon is not None:
                video_lat = video_item.get('latitude')
                video_lon = video_item.get('longitude')
                
                if video_lat is None or video_lon is None:
                    continue
                
                from math import radians, sin, cos, sqrt, atan2
                def haversine_distance(lat1, lon1, lat2, lon2):
                    R = 6371
                    lat1_rad = radians(lat1)
                    lat2_rad = radians(lat2)
                    delta_lat = radians(lat2 - lat1)
                    delta_lon = radians(lon2 - lon1)
                    a = sin(delta_lat/2)**2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon/2)**2
                    c = 2 * atan2(sqrt(a), sqrt(1-a))
                    return R * c
                
                distance = haversine_distance(lat, lon, video_lat, video_lon)
                filter_radius = radius if radius is not None else 2.0
                
                if distance > filter_radius:
                    continue
            
            filtered_videos.append(video_item)
        
        video_messages = filtered_videos
        
        if sort_by:
            if sort_by == 'date':
                video_messages.sort(key=lambda x: x.get('created_at', ''), reverse=True)
            elif sort_by == 'duration':
                video_messages.sort(key=lambda x: int(x.get('duration', 0)) if str(x.get('duration', 0)).isdigit() else 0, reverse=True)
            elif sort_by == 'title':
                video_messages.sort(key=lambda x: x.get('title', '').lower())
            elif sort_by == 'channel':
                video_messages.sort(key=lambda x: x.get('channel_name', '').lower())
        
        channels = {}
        for video_item in video_messages:
            channel_name = video_item.get('channel_name', 'unknown')
            if channel_name not in channels:
                channels[channel_name] = []
            channels[channel_name].append(video_item)
        
        channel_playlists = {}
        for channel_name, videos in channels.items():
            playlist = create_channel_playlist(videos, channel_name)
            channel_playlists[channel_name] = playlist
        
        response_data = {
            "success": True,
            "total_videos": len(video_messages),
            "total_channels": len(channels),
            "channels": channel_playlists,
            "filters": {
                "channel": channel,
                "search": search,
                "keyword": keyword,
                "date_from": date_from,
                "date_to": date_to,
                "duration_min": duration_min,
                "duration_max": duration_max,
                "sort_by": sort_by,
                "lat": lat,
                "lon": lon,
                "radius": radius if radius is not None else 2.0 if lat is not None and lon is not None else None
            },
            "timestamp": datetime.now().isoformat()
        }
        
        if html is not None:
            hostname = request.headers.get("host", "u.copylaradio.com")
            if hostname.startswith("u."):
                ipfs_gateway = f"https://ipfs.{hostname[2:]}"
            elif hostname.startswith("127.0.0.1") or hostname.startswith("localhost"):
                ipfs_gateway = "http://127.0.0.1:8080"
            else:
                ipfs_gateway = "https://ipfs.copylaradio.com"
            
            auto_open_video = None
            if video:
                for channel_name, channel_playlist in channel_playlists.items():
                    playlist_videos = channel_playlist.get('videos', []) if isinstance(channel_playlist, dict) else getattr(channel_playlist, 'videos', [])
                    for v in playlist_videos:
                        if v.get('message_id') == video:
                            auto_open_video = {
                                'event_id': v.get('message_id', ''),
                                'title': v.get('title', ''),
                                'ipfs_url': v.get('ipfs_url', ''),
                                'thumbnail_ipfs': v.get('thumbnail_ipfs', ''),
                                'gifanim_ipfs': v.get('gifanim_ipfs', ''),
                                'author_id': v.get('author_id', ''),
                                'uploader': v.get('uploader', ''),
                                'channel': v.get('channel_name', ''),
                                'duration': v.get('duration', 0),
                                'content': v.get('content', '')
                            }
                            break
                    if auto_open_video:
                        break
                
                if not auto_open_video:
                    for v in video_messages:
                        if v.get('message_id') == video:
                            auto_open_video = {
                                'event_id': v.get('message_id', ''),
                                'title': v.get('title', ''),
                                'ipfs_url': v.get('ipfs_url', ''),
                                'thumbnail_ipfs': v.get('thumbnail_ipfs', ''),
                                'gifanim_ipfs': v.get('gifanim_ipfs', ''),
                                'author_id': v.get('author_id', ''),
                                'uploader': v.get('uploader', ''),
                                'channel': v.get('channel_name', ''),
                                'duration': v.get('duration', 0),
                                'content': v.get('content', '')
                            }
                            break
            
            user_pubkey = None
            try:
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Nostr "):
                    token = auth_header.replace("Nostr ", "")
                    decoded = base64.b64decode(token)
                    auth_event = json.loads(decoded)
                    if auth_event.get("kind") == 27235:
                        user_pubkey = auth_event.get("pubkey")
            except Exception:
                pass
            
            analytics_data = {
                "type": "youtube_page_view",
                "video_event_id": video or "",
                "total_videos": len(video_messages),
                "total_channels": len(channels),
                "has_javascript": True
            }
            await send_server_side_analytics(analytics_data, request)
            
            return templates.TemplateResponse("youtube.html", {
                "request": request,
                "youtube_data": response_data,
                "myIPFS": ipfs_gateway,
                "auto_open_video": auto_open_video,
                "user_pubkey": user_pubkey,
                "use_local_js": use_local_js
            })
        
        analytics_data = {
            "type": "youtube_api_view",
            "video_event_id": video or "",
            "total_videos": len(video_messages),
            "total_channels": len(channels),
            "has_javascript": True
        }
        await send_server_side_analytics(analytics_data, request)
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logging.error(f"Error in youtube_route: {e}", exc_info=True)
        if html is not None:
            return HTMLResponse(content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", status_code=500)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/mp3")
async def mp3_route(
    request: Request,
    html: Optional[str] = None,
    search: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    sort_by: Optional[str] = None,
    limit: Optional[int] = 100
):
    """MP3 music library from NOSTR events (kind 1063 - NIP-94)"""
    try:
        ipfs_gateway = await get_myipfs_gateway()
        nostr_script_path = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "nostr_get_events.sh"
        
        if not nostr_script_path.exists():
            if html is not None:
                return HTMLResponse(content=f"<html><body><h1>Error</h1><p>nostr_get_events.sh not found</p></body></html>", status_code=500)
            raise HTTPException(status_code=500, detail="nostr_get_events.sh not found")
        
        cmd = [
            str(nostr_script_path),
            "--kind", "1063",
            "--limit", str(limit or 100),
            "--output", "json"
        ]
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(nostr_script_path.parent)
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15.0)
            if process.returncode != 0:
                mp3_events_raw = ""
            else:
                mp3_events_raw = stdout.decode('utf-8', errors='ignore')
        except Exception:
            mp3_events_raw = ""
        
        mp3_tracks = []
        if mp3_events_raw:
            for line in mp3_events_raw.strip().split('\n'):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    tags = {}
                    for tag in event.get('tags', []):
                        if len(tag) > 0:
                            tag_name = tag[0]
                            tag_value = tag[1] if len(tag) > 1 else None
                            if tag_name not in tags:
                                tags[tag_name] = tag_value
                    
                    mime_type = tags.get('m', '').lower()
                    if not mime_type or 'audio' not in mime_type:
                        continue
                    
                    url = tags.get('url', '')
                    if not url:
                        continue
                    
                    if url.startswith('ipfs://'):
                        url = url.replace('ipfs://', f'{ipfs_gateway}/ipfs/')
                    elif url.startswith('/ipfs/'):
                        url = f'{ipfs_gateway}{url}'
                    elif not url.startswith('http'):
                        url = f'{ipfs_gateway}/ipfs/{url}'
                    
                    title_tag = tags.get('title')
                    title = title_tag if title_tag else event.get('content', '').strip() or 'Unknown Title'
                    
                    artist_name = tags.get('artist')
                    if not artist_name:
                        p_tag = tags.get('p')
                        if p_tag:
                            artist_name = f"Artist ({p_tag[:8]}...)"
                        else:
                            author_hex = event.get('pubkey', '')
                            artist_name = f"Artist ({author_hex[:8]}...)" if author_hex else "Unknown Artist"
                    
                    album_name = tags.get('album', '—')
                    
                    thumbnail = tags.get('thumb') or tags.get('image')
                    if thumbnail:
                        if thumbnail.startswith('ipfs://'):
                            thumbnail = thumbnail.replace('ipfs://', f'{ipfs_gateway}/ipfs/')
                        elif thumbnail.startswith('/ipfs/'):
                            thumbnail = f'{ipfs_gateway}{thumbnail}'
                        elif not thumbnail.startswith('http'):
                            thumbnail = f'{ipfs_gateway}/ipfs/{thumbnail}'
                    
                    duration = None
                    if tags.get('duration'):
                        try: duration = float(tags.get('duration'))
                        except: pass
                    
                    size = None
                    if tags.get('size'):
                        try: size = int(tags.get('size'))
                        except: pass
                    
                    file_hash = tags.get('x', '')
                    
                    source_type = None
                    for tag in event.get('tags', []):
                        if tag[0] == 'i' and len(tag) > 1 and tag[1].startswith('source:'):
                            source_type = tag[1].replace('source:', '')
                            break
                    
                    summary = tags.get('summary', '')
                    description = summary if summary else event.get('content', '').strip()
                    
                    track = {
                        'event_id': event.get('id', ''),
                        'author_id': event.get('pubkey', ''),
                        'title': title,
                        'artist': artist_name,
                        'album': album_name,
                        'url': url,
                        'thumbnail': thumbnail,
                        'description': description,
                        'duration': duration,
                        'size': size,
                        'hash': file_hash,
                        'mime_type': mime_type,
                        'source_type': source_type,
                        'created_at': event.get('created_at', 0),
                        'date': datetime.fromtimestamp(event.get('created_at', 0)).isoformat() if event.get('created_at') else None
                    }
                    mp3_tracks.append(track)
                except Exception:
                    continue
        
        filtered_tracks = []
        for track in mp3_tracks:
            if search:
                search_lower = search.lower()
                if not (search_lower in track.get('title', '').lower() or
                      search_lower in track.get('artist', '').lower() or
                      search_lower in track.get('album', '').lower() or
                      search_lower in track.get('description', '').lower()):
                    continue
            if artist and artist.lower() not in track.get('artist', '').lower():
                continue
            if album and album.lower() not in track.get('album', '').lower():
                continue
            filtered_tracks.append(track)
        
        if sort_by:
            if sort_by == 'date':
                filtered_tracks.sort(key=lambda x: x.get('created_at', 0), reverse=True)
            elif sort_by == 'title':
                filtered_tracks.sort(key=lambda x: x.get('title', '').lower())
            elif sort_by == 'artist':
                filtered_tracks.sort(key=lambda x: x.get('artist', '').lower())
            elif sort_by == 'album':
                filtered_tracks.sort(key=lambda x: x.get('album', '').lower())
        else:
            filtered_tracks.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        
        response_data = {
            'tracks': filtered_tracks,
            'total_tracks': len(filtered_tracks),
            'total_all': len(mp3_tracks),
            'filters': {
                'search': search,
                'artist': artist,
                'album': album,
                'sort_by': sort_by,
                'limit': limit
            }
        }
        
        if html is not None:
            use_local_js = True
            return templates.TemplateResponse("mp3.html", {
                "request": request,
                "mp3_data": response_data,
                "myIPFS": ipfs_gateway if not use_local_js else "",
                "use_local_js": use_local_js
            })
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logging.error(f"Error in mp3_route: {e}", exc_info=True)
        if html is not None:
            return HTMLResponse(content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", status_code=500)
        raise HTTPException(status_code=500, detail=str(e))

from pydantic import BaseModel

