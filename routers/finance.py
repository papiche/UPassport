import logging
from typing import Optional
from fastapi import APIRouter, Request, HTTPException

from utils.helpers import execute_bash_json_script, render_page

router = APIRouter()

@router.get("/check_society", summary="Check Society", description="Check the society status and optionally return HTML.")
async def check_society_route(request: Request, html: Optional[str] = None, nostr: Optional[str] = None):
    args = ["--nostr"] if nostr is not None else []
    data = await execute_bash_json_script("G1society.sh", args, timeout=120)
    
    if html:
        data["has_nostr_data"] = len(data.get('nostr_did_data', [])) > 0
        data["nostr_count"] = len(data.get('nostr_did_data', []))
        return render_page(request, "society.html", data)
    return data

@router.get("/check_revenue", summary="Check Revenue", description="Check the revenue for a specific year and optionally return HTML.")
async def check_revenue_route(request: Request, html: Optional[str] = None, year: Optional[str] = None):
    data = await execute_bash_json_script("G1revenue.sh", [year or "all"], timeout=60)
    
    if html:
        data["filter_year"] = year or 'all'
        return render_page(request, "revenue.html", data)
    return data

@router.get("/check_zencard", summary="Check Zencard", description="Check the zencard history for a specific email and optionally return HTML.")
async def check_zencard_route(request: Request, email: str, html: Optional[str] = None):
    if not email:
        raise HTTPException(status_code=400, detail="Email requis")
        
    data = await execute_bash_json_script("G1zencard_history.sh", [email, "true"], timeout=60)
    
    if html:
        data["zencard_email"] = email
        return render_page(request, "zencard_api.html", data)
    return data

@router.get("/check_impots", summary="Check Impots", description="Check the impots status and optionally return HTML.")
async def check_impots_route(request: Request, html: Optional[str] = None):
    data = await execute_bash_json_script("G1impots.sh", timeout=60)
    
    if not data:
        data = {
            "wallet": "N/A", "total_provisions_g1": 0, "total_provisions_zen": 0, "total_transactions": 0,
            "breakdown": {"tva": {"total_g1": 0, "total_zen": 0, "transactions": 0}, "is": {"total_g1": 0, "total_zen": 0, "transactions": 0}},
            "provisions": []
        }
        
    if html:
        flat_data = {**data}
        flat_data["tva_total_zen"] = data["breakdown"]["tva"]["total_zen"]
        flat_data["tva_total_g1"] = data["breakdown"]["tva"]["total_g1"]
        flat_data["tva_transactions"] = data["breakdown"]["tva"]["transactions"]
        flat_data["is_total_zen"] = data["breakdown"]["is"]["total_zen"]
        flat_data["is_total_g1"] = data["breakdown"]["is"]["total_g1"]
        flat_data["is_transactions"] = data["breakdown"]["is"]["transactions"]
        return render_page(request, "impots.html", flat_data)
    return data
