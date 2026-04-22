import os
import logging
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config import settings
from core.state import app_state, ORACLE_ENABLED
from utils.helpers import render_page, get_myipfs_gateway, get_env_from_mysh

router = APIRouter()

SIMPLE_UI_ROUTES = {
    "/cloud": "cloud.html",
    "/dev": "dev.html",
    "/g1": "g1nostr.html",
    "/scan": "scan_new.html",
    "/upload": "upload2ipfs.html",
    "/scan_multipass_payment.html": "scan_multipass_payment.html",
    "/vocals": "vocals.html",
    "/vocals-read": "vocals-read.html",
    "/blog": "nostr_blog.html",
}

for route_path, template_name in SIMPLE_UI_ROUTES.items():
    @router.get(route_path, response_class=HTMLResponse)
    async def _simple_route(request: Request, _tpl=template_name):
        return render_page(request, _tpl)

@router.get("/", summary="UPlanet Status", description="UPlanet Status (specify lat, lon, deg to select grid level)")
async def ustats(request: Request, lat: str = None, lon: str = None, deg: str = None):
    import os
    import json
    import aiofiles
    from utils.helpers import run_script
    
    from core.config import settings
    script_path = settings.ZEN_PATH / "Astroport.ONE" / "Ustats.sh"

    args = []
    if lat is not None and lon is not None:
        args.extend([lat, lon, deg])

    return_code, last_line = await run_script(script_path, *args)

    if return_code == 0:
        if os.path.exists(last_line.strip()):
            try:
                async with aiofiles.open(last_line.strip(), 'r') as f:
                    content = await f.read()
                return JSONResponse(content=json.loads(content))
            except Exception as e:
                logging.error(f"Error reading file: {e}")
                raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")
        else:
            try:
                return JSONResponse(content=json.loads(last_line))
            except json.JSONDecodeError as e:
                logging.error(f"Error parsing JSON: {e}")
                raise HTTPException(status_code=500, detail=f"Error parsing JSON: {str(e)}")
    else:
        raise HTTPException(status_code=500, detail="Une erreur s'est produite lors de l'exécution du script. Veuillez consulter les logs dans ./tmp/54321.log.")

@router.get("/video", summary="Video Redirect", description="Redirect to the YouTube video page.")
async def video_route(): return RedirectResponse(url="/youtube?html=1", status_code=302)

@router.get("/audio", summary="Audio Redirect", description="Redirect to the MP3 audio page.")
async def audio_route(): return RedirectResponse(url="/mp3?html=1", status_code=302)

@router.get("/astro", response_class=HTMLResponse, summary="Astro Base", description="Display the Astro Base template.")
async def get_astro(request: Request):
    return render_page(request, "astro_base.html")

@router.get("/cookie", response_class=HTMLResponse, summary="Cookie Guide", description="Serve cookie export guide template.")
async def get_cookie_guide(request: Request):
    return render_page(request, "cookie.html")

@router.get("/terms", response_class=HTMLResponse, summary="Terms of Service", description="Serve Terms of Service template.")
async def get_terms_of_service(request: Request):
    from datetime import datetime
    current_date = datetime.now().strftime("%B %d, %Y")
    current_year = datetime.now().strftime("%Y")
    return render_page(request, "terms.html", {
        "current_date": current_date,
        "current_year": current_year
    })

@router.get("/n8n", response_class=HTMLResponse, summary="N8N Workflow Builder", description="N8N-style workflow builder for cookie-based automation.")
async def get_n8n_workflow_builder(request: Request):
    return render_page(request, "n8n.html")

@router.get("/health", summary="Health Check", description="Health check endpoint.")
async def health_check():
    from datetime import datetime
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }

CREDENTIALS_CONTEXT_V1 = {
    "@context": {
        "@version": 1.1,
        "UPlanetLicense": "https://u.copylaradio.com/credentials/v1#UPlanetLicense",
        "license": "https://u.copylaradio.com/credentials/v1#license",
        "licenseName": "https://u.copylaradio.com/credentials/v1#licenseName",
        "holderNpub": "https://u.copylaradio.com/credentials/v1#holderNpub",
        "attestationsCount": "https://u.copylaradio.com/credentials/v1#attestationsCount",
        "status": "https://u.copylaradio.com/credentials/v1#status",
    }
}

NS_CONTEXT_V1 = {
    "@context": {
        "@version": 1.1,
        "CooperativeWallet": "https://u.copylaradio.com/ns/v1#CooperativeWallet",
        "IPFSGateway": "https://u.copylaradio.com/ns/v1#IPFSGateway",
        "g1pub": "https://u.copylaradio.com/ns/v1#g1pub",
        "walletType": "https://u.copylaradio.com/ns/v1#walletType",
        "cooperative": "https://u.copylaradio.com/ns/v1#cooperative",
        "cesiumLink": "https://u.copylaradio.com/ns/v1#cesiumLink",
        "CooperativeDID": "https://u.copylaradio.com/ns/v1#CooperativeDID",
        "rootDID": "https://u.copylaradio.com/ns/v1#rootDID",
        "configSource": "https://u.copylaradio.com/ns/v1#configSource",
        "contractStatus": "https://u.copylaradio.com/ns/v1#contractStatus",
        "description": "https://u.copylaradio.com/ns/v1#description",
    }
}

@router.get("/credentials/v1", response_class=JSONResponse, summary="Credentials Context", description="JSON-LD context for UPlanet Verifiable Credentials.")
@router.get("/credentials/v1/", response_class=JSONResponse, include_in_schema=False)
async def credentials_context_v1():
    return JSONResponse(
        content=CREDENTIALS_CONTEXT_V1,
        media_type="application/ld+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )

@router.get("/ns/v1", response_class=JSONResponse, summary="Namespace Context", description="JSON-LD context for UPlanet DID namespace.")
@router.get("/ns/v1/", response_class=JSONResponse, include_in_schema=False)
async def ns_context_v1():
    return JSONResponse(
        content=NS_CONTEXT_V1,
        media_type="application/ld+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )

@router.get("/rate-limit-status", summary="Rate Limit Status", description="Get current rate limit status for the requesting IP.")
async def rate_limit_status(request: Request):
    from core.middleware import rate_limiter, get_client_ip
    from core.config import settings
    from datetime import datetime
    
    client_ip = get_client_ip(request)
    remaining = rate_limiter.get_remaining_requests(client_ip)
    reset_time = rate_limiter.get_reset_time(client_ip)
    
    return {
        "client_ip": client_ip,
        "remaining_requests": remaining,
        "rate_limit": settings.RATE_LIMIT_REQUESTS,
        "window_seconds": settings.RATE_LIMIT_WINDOW,
        "reset_time": reset_time,
        "reset_time_iso": datetime.fromtimestamp(reset_time).isoformat() if reset_time else None,
        "is_blocked": remaining == 0
    }

@router.get("/12345", summary="Proxy 12345", description="Proxy route for 12345.")
async def proxy_12345(request: Request):
    import httpx
    
    query_params = str(request.url.query)
    target_url = f"http://127.0.0.1:12345"
    if query_params:
        target_url += f"?{query_params}"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(target_url)
            return JSONResponse(
                content=response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
                status_code=response.status_code
            )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"error": "Timeout connecting to 127.0.0.1:12345"}
        )
    except httpx.ConnectError:
        return JSONResponse(
            status_code=502,
            content={"error": "Cannot connect to 127.0.0.1:12345"}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Proxy error: {str(e)}"}
        )

@router.get("/oracle", summary="Oracle System", description="Oracle System Interface - Multi-signature permit management.")
async def get_oracle(
    request: Request, 
    html: Optional[str] = None,
    type: Optional[str] = None,
    npub: Optional[str] = None
):
    from datetime import datetime
    try:
        oracle_system = getattr(request.app.state, "oracle", None)
        if not ORACLE_ENABLED or oracle_system is None:
            error_msg = "Oracle system not available"
            if html is not None:
                return HTMLResponse(
                    content=f"<html><body><h1>Oracle System</h1><p>{error_msg}</p></body></html>",
                    status_code=503
                )
            raise HTTPException(status_code=503, detail=error_msg)
        
        oracle_data = {
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
        
        definitions = []
        for def_id, definition in oracle_system.definitions.items():
            definitions.append({
                "id": def_id,
                "name": definition.name,
                "description": definition.description,
                "min_attestations": definition.min_attestations,
                "required_license": definition.required_license,
                "valid_duration_days": definition.valid_duration_days,
                "revocable": definition.revocable,
                "verification_method": definition.verification_method,
                "metadata": definition.metadata
            })
        
        requests_list = []
        try:
            nostr_requests = oracle_system.fetch_permit_requests_from_nostr()
            for req in nostr_requests:
                if npub and req.applicant_npub != npub:
                    continue
                
                requests_list.append({
                    "id": req.request_id,
                    "permit_definition_id": req.permit_definition_id,
                    "applicant_npub": req.applicant_npub,
                    "statement": req.statement,
                    "evidence": req.evidence if hasattr(req, 'evidence') else [],
                    "status": req.status.value if hasattr(req.status, 'value') else str(req.status),
                    "attestations": req.attestations if hasattr(req, 'attestations') else [],
                    "created_at": req.created_at.isoformat() if req.created_at else None,
                    "issued_credential_id": None
                })
        except Exception as e:
            logging.warning(f"Could not fetch requests from Nostr: {e}")
        
        credentials_list = []
        for cred_id, cred in oracle_system.credentials.items():
            if npub and cred.subject_npub != npub:
                continue
            
            credentials_list.append({
                "id": cred_id,
                "permit_definition_id": cred.permit_definition_id,
                "subject_npub": cred.subject_npub,
                "issued_at": cred.issued_at.isoformat() if cred.issued_at else None,
                "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                "revoked": cred.revoked,
                "revoked_at": cred.revoked_at.isoformat() if cred.revoked_at else None,
                "revocation_reason": cred.revocation_reason,
                "attestations": cred.attestations,
                "nostr_event_id": cred.nostr_event_id
            })
        
        if type == "definitions" or type is None:
            oracle_data["definitions"] = definitions
            oracle_data["total_definitions"] = len(definitions)
        
        if type == "requests" or type is None:
            oracle_data["requests"] = requests_list
            oracle_data["total_requests"] = len(requests_list)
        
        if type == "credentials" or type is None:
            oracle_data["credentials"] = credentials_list
            oracle_data["total_credentials"] = len(credentials_list)
        
        oracle_data["filters"] = {
            "type": type,
            "npub": npub
        }
        
        if html is not None:
            return render_page(request, "oracle.html", {"oracle_data": oracle_data})
        
        return JSONResponse(content=oracle_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error in get_oracle: {e}", exc_info=True)
        if html is not None:
            return HTMLResponse(
                content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", 
                status_code=500
            )
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/wotx2", response_class=HTMLResponse)
async def get_wotx2(request: Request, npub: Optional[str] = None, permit_id: Optional[str] = None):
    """WoTx2 Permit Interface - Evolving Web of Trust for Professional Permits"""
    try:
        myipfs_gateway = await get_myipfs_gateway()
        
        all_permits = []
        selected_permit_data = {}
        selected_permit_id = permit_id or "PERMIT_DE_NAGER"
        
        oracle_system = getattr(request.app.state, "oracle", None)
        if ORACLE_ENABLED and oracle_system is not None:
            try:
                nostr_definitions = oracle_system.fetch_permit_definitions_from_nostr()
                
                seen_permit_ids = set()
                
                for permit_def in nostr_definitions:
                    seen_permit_ids.add(permit_def.id)
                    all_permits.append({
                        "id": permit_def.id,
                        "name": permit_def.name,
                        "description": permit_def.description,
                        "min_attestations": permit_def.min_attestations,
                        "holders_count": 0,
                        "pending_count": 0,
                        "category": permit_def.metadata.get("category", "general") if permit_def.metadata else "general"
                    })
                
                for def_id, local_def in oracle_system.definitions.items():
                    if def_id not in seen_permit_ids:
                        all_permits.append({
                            "id": local_def.id,
                            "name": local_def.name,
                            "description": local_def.description,
                            "min_attestations": local_def.min_attestations,
                            "holders_count": 0,
                            "pending_count": 0,
                            "category": local_def.metadata.get("category", "general") if local_def.metadata else "general"
                        })
                        nostr_definitions.append(local_def)
                
                selected_permit = next((p for p in nostr_definitions if p.id == selected_permit_id), None)
                if not selected_permit and nostr_definitions:
                    selected_permit = nostr_definitions[0]
                    selected_permit_id = selected_permit.id if selected_permit else None
                
                if selected_permit:
                    selected_permit_data = {
                        "id": selected_permit.id,
                        "name": selected_permit.name,
                        "description": selected_permit.description,
                        "min_attestations": selected_permit.min_attestations,
                        "valid_duration_days": selected_permit.valid_duration_days,
                        "required_license": selected_permit.required_license,
                        "revocable": selected_permit.revocable,
                        "verification_method": selected_permit.verification_method,
                        "metadata": selected_permit.metadata
                    }
            except Exception as e:
                logging.warning(f"Error fetching permits from Nostr: {e}")
        
        is_primary_station = False
        ipfs_node_id = await get_env_from_mysh("IPFSNODEID", "")
        if not ipfs_node_id:
            ipfs_node_id = settings.IPFSNODEID
        if ipfs_node_id:
            strapfile = None
            if os.path.exists(settings.GAME_PATH / "MY_boostrap_nodes.txt"):
                strapfile = settings.GAME_PATH / "MY_boostrap_nodes.txt"
            elif os.path.exists(settings.ZEN_PATH / "Astroport.ONE" / "A_boostrap_nodes.txt"):
                strapfile = settings.ZEN_PATH / "Astroport.ONE" / "A_boostrap_nodes.txt"
            
            if strapfile and os.path.exists(strapfile):
                try:
                    with open(strapfile, 'r') as f:
                        straps = []
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                strap_id = line.split('/')[-1].strip()
                                if strap_id:
                                    straps.append(strap_id)
                        
                        if straps and straps[0] == ipfs_node_id:
                            is_primary_station = True
                            logging.info(f"⭐ PRIMARY STATION DETECTED - IPFSNODEID {ipfs_node_id} matches first STRAP")
                except Exception as e:
                    logging.warning(f"Error reading bootstrap nodes file: {e}")
        
        return render_page(request, "wotx2.html", {
            "permit_data": selected_permit_data,
            "all_permits": all_permits,
            "selected_permit_id": permit_id or "PERMIT_DE_NAGER",
            "npub": npub,
            "uSPOT": settings.uSPOT,
            "nostr_relay": settings.myRELAY.split()[0] if settings.NOSTR_RELAYS else "ws://127.0.0.1:7777",
            "IPFSNODEID": ipfs_node_id,
            "is_primary_station": is_primary_station
        })
        
    except Exception as e:
        logging.error(f"Error in get_wotx2: {e}", exc_info=True)
        return HTMLResponse(
            content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", 
            status_code=500
        )

@router.get("/wotx2_renewal", response_class=HTMLResponse)
async def get_wotx2_renewal(
    request: Request, 
    permit_id: Optional[str] = None, 
    credential_id: Optional[str] = None,
    npub: Optional[str] = None
):
    """WoTx2 Renewal Interface - Renew expiring certifications"""
    try:
        myipfs_gateway = await get_myipfs_gateway()
        ipfs_node_id = await get_env_from_mysh("IPFSNODEID", "")
        
        return render_page(request, "wotx2_renewal.html", {
            "permit_id": permit_id or "",
            "credential_id": credential_id or "",
            "npub": npub or "",
            "uSPOT": settings.uSPOT,
            "nostr_relay": settings.myRELAY.split()[0],
            "IPFSNODEID": ipfs_node_id
        })
        
    except Exception as e:
        logging.error(f"Error in get_wotx2_renewal: {e}", exc_info=True)
        return HTMLResponse(
            content=f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", 
            status_code=500
        )
