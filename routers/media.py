import os
import json
import base64
import logging
import asyncio
from typing import Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from core.config import settings
from services.nostr import fetch_video_event_from_nostr, parse_video_metadata
from utils.helpers import render_page

router = APIRouter()

def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"

async def send_server_side_analytics(analytics_data: dict, request: Request) -> None:
    try:
        analytics_data.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        analytics_data.setdefault("source", "server")
        analytics_data.setdefault("current_url", str(request.url))
        analytics_data.setdefault("referer", request.headers.get("referer", ""))
        analytics_data.setdefault("user_agent", request.headers.get("user-agent", ""))
        
        client_ip = get_client_ip(request)
        if client_ip:
            analytics_data["client_ip"] = client_ip
        
        import httpx
        base_url = str(request.base_url).rstrip('/')
        ping_url = f"{base_url}/ping"
        
        async def send_ping():
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.post(ping_url, json=analytics_data)
            except Exception as e:
                logging.debug(f"Analytics ping failed: {e}")
        
        asyncio.create_task(send_ping())
    except Exception as e:
        logging.debug(f"Server-side analytics error: {e}")

@router.get("/theater", response_class=HTMLResponse)
async def theater_modal_route(request: Request, video: Optional[str] = None):
    use_local_js = settings.USE_LOCAL_JS
    video_metadata = None
    video_title = "Unknown"
    video_author = None
    video_kind = None
    video_duration = 0
    video_channel = ""
    video_source_type = ""
    
    if video:
        try:
            video_event = await fetch_video_event_from_nostr(video, timeout=5)
            if video_event:
                video_metadata = await parse_video_metadata(video_event)
                video_title = video_metadata.get('title', 'Unknown')
                video_author = video_metadata.get('author_id', '')
                video_kind = video_metadata.get('kind', 21)
                
                tags = video_event.get("tags", [])
                for tag in tags:
                    if isinstance(tag, list) and len(tag) >= 2:
                        if tag[0] == "t" and tag[1] not in ["analytics", "encrypted", "ipfs"]:
                            video_channel = tag[1]
                        elif tag[0] == "source":
                            video_source_type = tag[1]
        except Exception as e:
            logging.warning(f"⚠️ Could not fetch video metadata: {e}")
    
    analytics_data = {
        "type": "theater_page_view",
        "video_event_id": video or "",
        "video_title": video_title,
        "video_author": video_author or "",
        "video_kind": video_kind or 21,
        "video_duration": video_duration,
        "video_channel": video_channel,
        "video_source_type": video_source_type,
        "has_javascript": True
    }
    await send_server_side_analytics(analytics_data, request)
    
    base_url = str(request.base_url).rstrip('/')
    theater_url = f"{base_url}/theater"
    if video:
        theater_url = f"{theater_url}?video={video}"
    
    return render_page(request, "theater-modal.html", {
        "use_local_js": use_local_js,
        "video_id": video,
        "video_metadata": video_metadata,
        "theater_url": theater_url
    })

@router.get("/mp3-modal", response_class=HTMLResponse)
async def mp3_modal_route(request: Request, track: Optional[str] = None):
    use_local_js = settings.USE_LOCAL_JS
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
    return render_page(request, "tags.html", {
        "video_id": video
    })

@router.get("/contrib", response_class=HTMLResponse)
async def contrib_route(request: Request, video: Optional[str] = None, kind: Optional[str] = None):
    return render_page(request, "contrib.html", {
        "video_id": video,
        "video_kind": kind or "21"
    })
