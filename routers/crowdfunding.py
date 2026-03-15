import os
import json
import logging
import subprocess
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

from core.config import settings

# Path to CROWDFUNDING.sh script
CROWDFUNDING_SCRIPT = settings.TOOLS_PATH / "CROWDFUNDING.sh"
CROWDFUNDING_DIR = settings.GAME_PATH / "crowdfunding"

class CrowdfundingCreateRequest(BaseModel):
    lat: float
    lon: float
    name: str
    description: str = ""
    npub: str  # Creator's NOSTR pubkey for verification

class CrowdfundingAddOwnerRequest(BaseModel):
    project_id: str
    email: str
    mode: str  # "commons" or "cash"
    amount: float
    currency: str = "ZEN"  # or "EUR"
    npub: str

async def run_crowdfunding_command(args: List[str], timeout: int = 30) -> Dict[str, Any]:
    """Execute CROWDFUNDING.sh with given arguments and return result"""
    if not os.path.exists(CROWDFUNDING_SCRIPT):
        return {"success": False, "error": "CROWDFUNDING.sh script not found"}
    
    try:
        import asyncio
        process = await asyncio.create_subprocess_exec(
            str(CROWDFUNDING_SCRIPT), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.dirname(CROWDFUNDING_SCRIPT)
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        
        return {
            "success": process.returncode == 0,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
            "returncode": process.returncode
        }
    except asyncio.TimeoutError:
        return {"success": False, "error": "Command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def parse_project_json(project_id: str) -> Optional[Dict[str, Any]]:
    """Read project.json for a given project ID"""
    project_file = os.path.join(CROWDFUNDING_DIR, project_id, "project.json")
    if os.path.exists(project_file):
        try:
            with open(project_file, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return None

@router.post("/api/crowdfunding/create")
async def crowdfunding_create(request: CrowdfundingCreateRequest):
    """Create a new crowdfunding project"""
    logging.info(f"Creating crowdfunding project: {request.name} at ({request.lat}, {request.lon})")
    
    result = await run_crowdfunding_command([
        "create",
        str(request.lat),
        str(request.lon),
        request.name,
        request.description
    ], timeout=60)
    
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("error", result.get("stderr", "Creation failed")))
    
    import re
    match = re.search(r'(CF-\d{8}-[A-F0-9]{8})', result["stdout"])
    if match:
        project_id = match.group(1)
        project_data = parse_project_json(project_id)
        
        return JSONResponse({
            "success": True,
            "project_id": project_id,
            "project": project_data,
            "bien_identity": project_data.get("bien_identity") if project_data else None,
            "message": f"Project {project_id} created successfully"
        })
    
    return JSONResponse({
        "success": True,
        "message": "Project created",
        "output": result["stdout"]
    })

@router.get("/api/crowdfunding/list")
async def crowdfunding_list(status: str = "all"):
    """List crowdfunding projects"""
    projects = []
    
    if os.path.exists(CROWDFUNDING_DIR):
        for project_dir in os.listdir(CROWDFUNDING_DIR):
            project_data = parse_project_json(project_dir)
            if project_data:
                if status == "active" and project_data.get("status") not in ["crowdfunding", "vote_pending"]:
                    continue
                if status == "completed" and project_data.get("status") != "completed":
                    continue
                
                projects.append({
                    "id": project_data.get("id"),
                    "name": project_data.get("name"),
                    "status": project_data.get("status"),
                    "location": project_data.get("location"),
                    "bien_identity": project_data.get("bien_identity"),
                    "totals": project_data.get("totals"),
                    "owners_count": len(project_data.get("owners", [])),
                    "created_at": project_data.get("created_at")
                })
    
    return JSONResponse({
        "success": True,
        "count": len(projects),
        "projects": projects
    })

@router.get("/api/crowdfunding/status/{project_id}")
async def crowdfunding_status(project_id: str):
    """Get detailed status of a crowdfunding project"""
    project_data = parse_project_json(project_id)
    
    if not project_data:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    
    bien_g1pub = project_data.get("bien_identity", {}).get("g1pub")
    bien_balance = None
    
    if bien_g1pub:
        try:
            g1check_script = settings.TOOLS_PATH / "G1check.sh"
            if os.path.exists(g1check_script):
                import asyncio
                process = await asyncio.create_subprocess_exec(
                    str(g1check_script), bien_g1pub,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
                if process.returncode == 0:
                    bien_balance = stdout.decode().strip()
        except Exception:
            pass
    
    return JSONResponse({
        "success": True,
        "project": project_data,
        "bien_balance_g1": bien_balance
    })

@router.post("/api/crowdfunding/add-owner")
async def crowdfunding_add_owner(request: CrowdfundingAddOwnerRequest):
    """Add an owner to a crowdfunding project"""
    logging.info(f"Adding owner {request.email} to project {request.project_id}")
    
    args = [
        "add-owner",
        request.project_id,
        request.email,
        request.mode,
        str(request.amount)
    ]
    
    if request.currency:
        args.append(request.currency)
    
    result = await run_crowdfunding_command(args, timeout=30)
    
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("stderr", "Failed to add owner"))
    
    project_data = parse_project_json(request.project_id)
    
    return JSONResponse({
        "success": True,
        "message": f"Owner {request.email} added with mode {request.mode}",
        "project": project_data
    })

@router.get("/api/crowdfunding/bien-balance/{project_id}")
async def crowdfunding_bien_balance(project_id: str):
    """Get the Bien wallet balance"""
    project_data = parse_project_json(project_id)
    
    if not project_data:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    
    bien_g1pub = project_data.get("bien_identity", {}).get("g1pub")
    
    if not bien_g1pub:
        raise HTTPException(status_code=400, detail="Project has no Bien wallet")
    
    try:
        g1check_script = settings.TOOLS_PATH / "G1check.sh"
        if os.path.exists(g1check_script):
            import asyncio
            process = await asyncio.create_subprocess_exec(
                str(g1check_script), bien_g1pub,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode == 0:
                g1_balance = float(stdout.decode().strip() or "0")
                zen_balance = max(0, (g1_balance - 1) * 10)
                
                return JSONResponse({
                    "success": True,
                    "project_id": project_id,
                    "bien_g1pub": bien_g1pub,
                    "g1_balance": g1_balance,
                    "zen_balance": zen_balance
                })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Balance check failed: {str(e)}")
    
    raise HTTPException(status_code=500, detail="G1check script not available")
