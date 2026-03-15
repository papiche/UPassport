import os
import json
import time
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.config import settings
from utils.helpers import get_myipfs_gateway, get_env_from_mysh
from services.nostr import verify_nostr_auth
from utils.crypto import hex_to_npub

router = APIRouter()
templates = Jinja2Templates(directory="templates")

class UmapGeolinksResponse(BaseModel):
    success: bool
    message: str
    umap_coordinates: Dict[str, float]
    umaps: Dict[str, str]
    sectors: Dict[str, str]
    regions: Dict[str, str]
    total_adjacent: int
    timestamp: str
    processing_time_ms: int

async def get_umap_geolinks(lat: float, lon: float) -> Dict[str, Any]:
    """Récupérer les liens géographiques des UMAPs, SECTORs et REGIONs adjacentes"""
    start_time = time.time()
    
    try:
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise ValueError("Latitude et longitude doivent être des nombres")
        
        if lat < -90 or lat > 90:
            raise ValueError("Latitude doit être entre -90 et 90")
        
        if lon < -180 or lon > 180:
            raise ValueError("Longitude doit être entre -180 et 180")
        
        from core.config import settings
        script_path = settings.TOOLS_PATH / "Umap_geonostr.sh"
        
        if not os.path.exists(script_path):
            raise FileNotFoundError(f"Script Umap_geonostr.sh non trouvé: {script_path}")
        
        if not os.access(script_path, os.X_OK):
            os.chmod(script_path, 0o755)
        
        import asyncio
        process = await asyncio.create_subprocess_exec(
            script_path, str(lat), str(lon),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip() if stderr else "Erreur inconnue"
            raise RuntimeError(f"Script Umap_geonostr.sh a échoué: {error_msg}")
        
        from utils.helpers import safe_json_load
        try:
            raw_data = safe_json_load(stdout.decode().strip())
        except ValueError as e:
            raise ValueError(f"Sortie JSON invalide du script: {e}")
        
        required_sections = ['umaps', 'sectors', 'regions']
        missing_sections = [section for section in required_sections if section not in raw_data]
        
        if missing_sections:
            raise ValueError(f"Format invalide - sections manquantes: {missing_sections}")
        
        umaps_data = raw_data['umaps']
        sectors_data = raw_data['sectors']
        regions_data = raw_data['regions']
        
        expected_keys = ['north', 'south', 'east', 'west', 'northeast', 'northwest', 'southeast', 'southwest', 'here']
        
        for section_name, section_data in [('umaps', umaps_data), ('sectors', sectors_data), ('regions', regions_data)]:
            missing_keys = [key for key in expected_keys if key not in section_data]
            if missing_keys:
                raise ValueError(f"Clés manquantes dans {section_name}: {missing_keys}")
        
        adjacent_count = len([k for k in umaps_data.keys() if k != 'here'])
        processing_time = int((time.time() - start_time) * 1000)
        
        return {
            "success": True,
            "message": f"Liens géographiques récupérés pour UMAP ({lat}, {lon})",
            "umap_coordinates": {"lat": lat, "lon": lon},
            "umaps": umaps_data,
            "sectors": sectors_data,
            "regions": regions_data,
            "total_adjacentes": adjacent_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "processing_time_ms": processing_time
        }
        
    except Exception as e:
        processing_time = int((time.time() - start_time) * 1000)
        logging.error(f"Erreur lors de la récupération des liens UMAP: {str(e)}")
        
        return {
            "success": False,
            "message": f"Erreur: {str(e)}",
            "umap_coordinates": {"lat": lat, "lon": lon},
            "umaps": {},
            "sectors": {},
            "regions": {},
            "total_adjacentes": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "processing_time_ms": processing_time
        }

@router.get("/chat", response_class=HTMLResponse)
async def chat_route(request: Request, room: Optional[str] = None):
    """UMAP Chat Room - Real-time messaging for geographic locations"""
    try:
        myipfs_gateway = await get_myipfs_gateway()
        
        umap_hex = None
        umap_lat = 0.00
        umap_lon = 0.00
        sector_hex = None
        region_hex = None
        
        if room:
            try:
                coords = room.split(',')
                if len(coords) == 2:
                    umap_lat = float(coords[0].strip())
                    umap_lon = float(coords[1].strip())
                    
                    umap_lat = round(umap_lat * 100) / 100
                    umap_lon = round(umap_lon * 100) / 100
                    
                    geolinks_result = await get_umap_geolinks(umap_lat, umap_lon)
                    
                    if geolinks_result.get('success'):
                        if geolinks_result.get('umaps'):
                            umap_hex = geolinks_result['umaps'].get('here')
                        if geolinks_result.get('sectors'):
                            sector_hex = geolinks_result['sectors'].get('here')
                        if geolinks_result.get('regions'):
                            region_hex = geolinks_result['regions'].get('here')
            except (ValueError, TypeError) as e:
                logging.warning(f"⚠️ Error parsing room coordinates '{room}': {e}")
        else:
            umap_lat = 0.00
            umap_lon = 0.00
            geolinks_result = await get_umap_geolinks(umap_lat, umap_lon)
            
            if geolinks_result.get('success'):
                if geolinks_result.get('umaps'):
                    umap_hex = geolinks_result['umaps'].get('here')
                if geolinks_result.get('sectors'):
                    sector_hex = geolinks_result['sectors'].get('here')
                if geolinks_result.get('regions'):
                    region_hex = geolinks_result['regions'].get('here')
        
        return templates.TemplateResponse("chat.html", {
            "request": request,
            "myIPFS": myipfs_gateway,
            "room": room,
            "umap_lat": umap_lat,
            "umap_lon": umap_lon,
            "umap_hex": umap_hex,
            "sector_hex": sector_hex,
            "region_hex": region_hex
        })
    except Exception as e:
        logging.error(f"Error serving chat page: {e}")
        raise HTTPException(status_code=500, detail=f"Error loading chat page: {str(e)}")

@router.get("/api/umap/geolinks", response_model=UmapGeolinksResponse)
async def get_umap_geolinks_api(lat: float, lon: float):
    """Récupérer les liens géographiques des UMAPs, SECTORs et REGIONs adjacentes"""
    try:
        result = await get_umap_geolinks(lat, lon)
        
        if not result["success"]:
            raise HTTPException(status_code=500, detail=result["message"])
        
        return UmapGeolinksResponse(
            success=True,
            message=result["message"],
            umap_coordinates=result["umap_coordinates"],
            umaps=result["umaps"],
            sectors=result["sectors"],
            regions=result["regions"],
            total_adjacent=result["total_adjacentes"],
            timestamp=result["timestamp"],
            processing_time_ms=result["processing_time_ms"]
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

@router.get("/api/myGPS")
async def get_my_gps_coordinates(npub: str):
    """Get GPS coordinates for authenticated user"""
    try:
        is_authenticated = await verify_nostr_auth(npub, force_check=True)
        
        if not is_authenticated:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "authentication_required",
                    "message": "NIP-42 authentication required to access GPS coordinates"
                }
            )
        
        pubkey_hex = npub
        if npub.startswith('npub1'):
            try:
                from nostr_sdk import PublicKey
                pubkey_hex = PublicKey.from_bech32(npub).to_hex()
            except:
                pass
        
        game_nostr_path = settings.ZEN_PATH / "game" / "nostr"
        
        if not game_nostr_path.exists():
            raise HTTPException(
                status_code=404,
                detail={"error": "directory_not_found", "message": "NOSTR game directory not found"}
            )
        
        gps_file_path = None
        user_email = None
        
        for email_dir in game_nostr_path.iterdir():
            if not email_dir.is_dir():
                continue
            
            pub_key_file = email_dir / "HEX"
            if pub_key_file.exists():
                try:
                    stored_pubkey = pub_key_file.read_text().strip()
                    if stored_pubkey == pubkey_hex or stored_pubkey == npub:
                        gps_file = email_dir / "GPS"
                        if gps_file.exists():
                            gps_file_path = gps_file
                            user_email = email_dir.name
                            break
                except Exception:
                    continue
        
        if not gps_file_path:
            raise HTTPException(
                status_code=404,
                detail={"error": "gps_not_found", "message": "GPS coordinates not found for this user"}
            )
        
        try:
            gps_content = gps_file_path.read_text().strip()
            lat = None
            lon = None
            
            for part in gps_content.split(';'):
                part = part.strip()
                if part.startswith('LAT='):
                    lat = float(part.replace('LAT=', ''))
                elif part.startswith('LON='):
                    lon = float(part.replace('LON=', ''))
            
            if lat is None or lon is None:
                raise ValueError(f"Invalid GPS format: {gps_content}")
            
            lat_rounded = round(lat, 2)
            lon_rounded = round(lon, 2)
            umap_key = f"{lat_rounded:.2f},{lon_rounded:.2f}"
            
            ipfs_node_id = await get_env_from_mysh("IPFSNODEID", "")
            if not ipfs_node_id:
                from core.config import settings
                ipfs_node_id = settings.IPFSNODEID
            
            return {
                "success": True,
                "coordinates": {"lat": lat_rounded, "lon": lon_rounded},
                "umap_key": umap_key,
                "email": user_email,
                "ipfsnodeid": ipfs_node_id,
                "message": "GPS coordinates retrieved successfully",
                "timestamp": datetime.now().isoformat()
            }
            
        except ValueError as e:
            raise HTTPException(status_code=500, detail={"error": "gps_parse_error", "message": str(e)})
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "gps_read_error", "message": str(e)})
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(e)})
