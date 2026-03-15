import os
import logging
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config import settings
from core.state import app_state, ORACLE_ENABLED
from utils.helpers import render_page

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
}

for route_path, template_name in SIMPLE_UI_ROUTES.items():
    @router.get(route_path, response_class=HTMLResponse)
    async def _simple_route(request: Request, _tpl=template_name):
        return render_page(request, _tpl)

@router.get("/video")
async def video_route(): return RedirectResponse(url="/youtube?html=1", status_code=302)

@router.get("/audio")
async def audio_route(): return RedirectResponse(url="/mp3?html=1", status_code=302)

@router.get("/astro_base", response_class=HTMLResponse)
async def get_astro_base(request: Request):
    return render_page(request, "astro_base.html")

@router.get("/cookie", response_class=HTMLResponse)
async def get_cookie_guide(request: Request):
    return render_page(request, "cookie.html")

@router.get("/terms", response_class=HTMLResponse)
async def get_terms_of_service(request: Request):
    from datetime import datetime
    current_date = datetime.now().strftime("%B %d, %Y")
    current_year = datetime.now().strftime("%Y")
    return render_page(request, "terms.html", {
        "current_date": current_date,
        "current_year": current_year
    })

@router.get("/n8n", response_class=HTMLResponse)
async def get_n8n_workflow_builder(request: Request):
    return render_page(request, "n8n.html")

@router.get("/12345")
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

@router.get("/oracle")
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
