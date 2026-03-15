
import logging
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel


from services.nostr import verify_nostr_auth, hex_to_npub, npub_to_hex, fetch_nostr_profiles
from utils.helpers import get_env_from_mysh, run_script
from core.config import settings

router = APIRouter()

class PermitDefinitionRequest(BaseModel):
    id: str
    name: str
    description: str
    min_attestations: int = 5
    required_license: Optional[str] = None
    valid_duration_days: int = 0
    revocable: bool = True
    verification_method: str = "peer_attestation"
    metadata: Dict[str, Any] = {}

class PermitDefinitionCreateRequest(BaseModel):
    permit: PermitDefinitionRequest
    npub: str
    bootstrap_emails: Optional[List[str]] = None

@router.post("/api/permit/define")
async def create_permit_definition(request: PermitDefinitionCreateRequest):
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        if not request.npub:
            raise HTTPException(status_code=401, detail="NOSTR authentication required (npub parameter)")
        
        if not await verify_nostr_auth(request.npub, force_check=True):
            raise HTTPException(status_code=401, detail="NOSTR authentication failed (NIP-42)")
        
        permit_req = request.permit
        
        if permit_req.id in app_state.oracle_system.definitions:
            raise HTTPException(
                status_code=400, 
                detail=f"Permit definition {permit_req.id} already exists."
            )
        
        uplanet_g1_key = await get_env_from_mysh("UPLANETNAME_G1", "")
        if not uplanet_g1_key:
            from core.config import settings
            uplanet_g1_key = settings.UPLANETNAME_G1
        issuer_did = f"did:nostr:{uplanet_g1_key[:16]}" if uplanet_g1_key else "did:nostr:unknown"
        
        min_attestations = permit_req.min_attestations
        if min_attestations == 5:
            competencies = permit_req.metadata.get("competencies", [])
            if competencies and len(competencies) > 0:
                min_attestations = max(2, 2 + len(competencies))
        
        from oracle_system import PermitDefinition
        definition = PermitDefinition(
            id=permit_req.id,
            name=permit_req.name,
            description=permit_req.description,
            issuer_did=issuer_did,
            min_attestations=min_attestations,
            required_license=permit_req.required_license,
            valid_duration_days=permit_req.valid_duration_days,
            revocable=permit_req.revocable,
            verification_method=permit_req.verification_method,
            metadata=permit_req.metadata
        )
        
        success = app_state.oracle_system.create_permit_definition(definition, creator_npub=request.npub)
        
        if success:
            response_data = {
                "success": True,
                "message": f"Permit definition {permit_req.id} created",
                "definition_id": permit_req.id,
                "min_attestations": min_attestations
            }
            
            if request.bootstrap_emails and len(request.bootstrap_emails) >= 2:
                try:
                    script_path = settings.TOOLS_PATH / "oracle.WoT_PERMIT.init.sh"
                    if script_path.exists():
                        asyncio.create_task(run_script(
                            str(script_path),
                            permit_req.id,
                            *request.bootstrap_emails,
                            log_file_path=str(settings.ZEN_PATH / "tmp" / f"bootstrap_{permit_req.id}.log")
                        ))
                        response_data["bootstrap_initiated"] = True
                        response_data["bootstrap_emails"] = request.bootstrap_emails
                except Exception as e:
                    logging.error(f"Failed to initiate bootstrap: {e}")
                    response_data["bootstrap_error"] = str(e)
            
            return JSONResponse(response_data)
        else:
            raise HTTPException(status_code=400, detail="Failed to create permit definition")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error creating permit definition: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/permit/credential/{credential_id}")
async def get_permit_credential(credential_id: str):
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        credential = app_state.oracle_system.credentials.get(credential_id)
        
        if not credential:
            raise HTTPException(status_code=404, detail="Credential not found")
        
        definition = app_state.oracle_system.definitions.get(credential.permit_definition_id)
        
        vc = {
            "@context": [
                "https://www.w3.org/2018/credentials/v1",
                "https://w3id.org/security/v2",
                "https://u.copylaradio.com/credentials/v1"
            ],
            "id": f"urn:uuid:{credential.credential_id}",
            "type": ["VerifiableCredential", "UPlanetLicense"],
            "issuer": credential.issued_by,
            "issuanceDate": credential.issued_at.isoformat(),
            "expirationDate": credential.expires_at.isoformat() if credential.expires_at else None,
            "credentialSubject": {
                "id": credential.holder_did,
                "license": credential.permit_definition_id,
                "licenseName": definition.name if definition else "Unknown",
                "holderNpub": credential.holder_npub,
                "attestationsCount": len(credential.attestations),
                "status": credential.status.value
            },
            "proof": credential.proof
        }
        
        return JSONResponse(vc)
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error getting credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/permit/definitions")
async def list_permit_definitions():
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        if len(app_state.oracle_system.definitions) == 0:
            try:
                definitions_nostr = app_state.oracle_system.fetch_permit_definitions_from_nostr()
                for definition in definitions_nostr:
                    app_state.oracle_system.definitions[definition.id] = definition
                
                if definitions_nostr:
                    app_state.oracle_system.save_data()
            except Exception as e:
                logging.warning(f"⚠️  Could not fetch definitions from NOSTR: {e}")
        
        definitions = [
            {
                "id": d.id,
                "name": d.name,
                "description": d.description,
                "min_attestations": d.min_attestations,
                "required_license": d.required_license,
                "valid_duration_days": d.valid_duration_days,
                "verification_method": d.verification_method
            }
            for d in app_state.oracle_system.definitions.values()
        ]
        
        return JSONResponse({
            "success": True,
            "count": len(definitions),
            "definitions": definitions
        })
    
    except Exception as e:
        logging.error(f"Error listing definitions: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/permit/stats")
async def get_permit_statistics():
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        if len(app_state.oracle_system.definitions) == 0:
            try:
                definitions_nostr = app_state.oracle_system.fetch_permit_definitions_from_nostr()
                for definition in definitions_nostr:
                    app_state.oracle_system.definitions[definition.id] = definition
                if definitions_nostr:
                    app_state.oracle_system.save_data()
            except Exception as e:
                logging.warning(f"Could not fetch definitions from NOSTR: {e}")
        
        permit_stats = []
        for def_id, permit_def in app_state.oracle_system.definitions.items():
            holders_count = sum(1 for cred in app_state.oracle_system.credentials.values() 
                              if cred.permit_definition_id == def_id and not cred.revoked)
            
            pending_count = 0
            total_attestations = 0
            
            metadata = permit_def.metadata or {}
            competencies = metadata.get("competencies", [])
            category = metadata.get("category", "general")
            
            level = "Beginner"
            if permit_def.min_attestations >= 10:
                level = "Expert"
            elif permit_def.min_attestations >= 6:
                level = "Advanced"
            elif permit_def.min_attestations >= 3:
                level = "Intermediate"
            
            permit_stats.append({
                "id": def_id,
                "name": permit_def.name,
                "description": permit_def.description,
                "min_attestations": permit_def.min_attestations,
                "required_license": permit_def.required_license,
                "valid_duration_days": permit_def.valid_duration_days,
                "revocable": permit_def.revocable,
                "verification_method": permit_def.verification_method,
                "category": category,
                "competencies": competencies,
                "competencies_count": len(competencies),
                "level": level,
                "holders_count": holders_count,
                "pending_requests_count": pending_count,
                "total_attestations": total_attestations,
                "metadata": metadata
            })
        
        total_permits = len(permit_stats)
        total_holders = sum(s["holders_count"] for s in permit_stats)
        total_pending = sum(s["pending_requests_count"] for s in permit_stats)
        total_attestations = sum(s["total_attestations"] for s in permit_stats)
        
        return JSONResponse({
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "global_stats": {
                "total_permits": total_permits,
                "total_holders": total_holders,
                "total_pending_requests": total_pending,
                "total_attestations": total_attestations
            },
            "permits": permit_stats
        })
    
    except Exception as e:
        logging.error(f"Error getting permit statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/permit/nostr/fetch")
async def fetch_permits_from_nostr(
    kind: Optional[int] = None, 
    type: Optional[str] = None,
    npub: Optional[str] = None
):
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    if type and not kind:
        type_map = {
            "definitions": 30500,
            "requests": 30501,
            "credentials": 30503
        }
        if type not in type_map:
            raise HTTPException(status_code=400, detail=f"Invalid type '{type}'")
        kind = type_map[type]
    
    if not kind:
        raise HTTPException(status_code=400, detail="Either 'kind' or 'type' parameter is required")
    
    try:
        if kind == 30500:
            definitions = app_state.oracle_system.fetch_permit_definitions_from_nostr()
            return JSONResponse({
                "success": True,
                "kind": kind,
                "count": len(definitions),
                "events": [
                    {
                        "id": d.id,
                        "name": d.name,
                        "description": d.description,
                        "min_attestations": d.min_attestations
                    }
                    for d in definitions
                ]
            })
        
        elif kind == 30501:
            requests = app_state.oracle_system.fetch_permit_requests_from_nostr()
            if npub:
                requests = [r for r in requests if r.applicant_npub == npub]
            
            return JSONResponse({
                "success": True,
                "kind": kind,
                "count": len(requests),
                "events": [
                    {
                        "request_id": r.request_id,
                        "permit_id": r.permit_definition_id,
                        "applicant_npub": r.applicant_npub,
                        "statement": r.statement,
                        "created_at": r.created_at.isoformat()
                    }
                    for r in requests
                ]
            })
        
        elif kind == 30503:
            credentials = app_state.oracle_system.fetch_permit_credentials_from_nostr(holder_npub=npub)
            
            return JSONResponse({
                "success": True,
                "kind": kind,
                "count": len(credentials),
                "events": [
                    {
                        "credential_id": c.credential_id,
                        "permit_id": c.permit_definition_id,
                        "holder_npub": c.holder_npub,
                        "issued_at": c.issued_at.isoformat(),
                        "expires_at": c.expires_at.isoformat() if c.expires_at else None
                    }
                    for c in credentials
                ]
            })
        
        else:
            raise HTTPException(status_code=400, detail="Invalid kind")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching from NOSTR: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/permit/issue/{request_id}")
async def issue_permit_credential(request_id: str):
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        credential = app_state.oracle_system.issue_credential(request_id)
        
        if credential:
            return JSONResponse({
                "success": True,
                "message": "Credential issued",
                "credential_id": credential.credential_id,
                "holder_npub": credential.holder_npub,
                "permit_id": credential.permit_definition_id
            })
        else:
            existing = None
            for cred in app_state.oracle_system.credentials.values():
                if cred.request_id == request_id:
                    existing = cred
                    break
            
            if existing:
                return JSONResponse({
                    "success": True,
                    "message": "Credential already issued",
                    "credential_id": existing.credential_id,
                    "holder_npub": existing.holder_npub,
                    "permit_id": existing.permit_definition_id
                })
            else:
                raise HTTPException(status_code=400, detail="Request not ready for issuance or not found")
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error issuing credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/permit/revoke/{credential_id}")
async def revoke_permit_credential(credential_id: str, reason: Optional[str] = None):
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        if credential_id not in app_state.oracle_system.credentials:
            raise HTTPException(status_code=404, detail="Credential not found")
        
        credential = app_state.oracle_system.credentials[credential_id]
        
        definition = app_state.oracle_system.definitions.get(credential.permit_definition_id)
        if definition and not definition.revocable:
            raise HTTPException(status_code=400, detail="This permit type cannot be revoked")
        
        from oracle_system import PermitStatus
        credential.status = PermitStatus.REVOKED
        
        app_state.oracle_system.save_data()
        
        return JSONResponse({
            "success": True,
            "message": "Credential revoked",
            "credential_id": credential_id,
            "reason": reason or "No reason provided"
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error revoking credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/permit/user/credentials")
async def get_user_credentials(
    npub: Optional[str] = None,
    hex_pubkey: Optional[str] = None,
    include_expired: bool = True
):
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    if not npub and not hex_pubkey:
        raise HTTPException(status_code=400, detail="Either 'npub' or 'hex_pubkey' parameter is required")
    
    try:
        holder_npub = npub
        if hex_pubkey and not npub:
            holder_npub = hex_to_npub(hex_pubkey)
        
        credentials = app_state.oracle_system.fetch_permit_credentials_from_nostr(holder_npub=holder_npub)
        
        now = datetime.now()
        thirty_days_later = now + timedelta(days=30)
        
        all_credentials = []
        expiring_soon = []
        expired = []
        
        for cred in credentials:
            cred_data = {
                "credential_id": cred.credential_id,
                "permit_id": cred.permit_definition_id,
                "permit_name": "",
                "holder_npub": cred.holder_npub,
                "issued_at": cred.issued_at.isoformat() if cred.issued_at else None,
                "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                "status": cred.status.value if hasattr(cred, 'status') else "active",
                "attestations_count": len(cred.attestations) if hasattr(cred, 'attestations') else 0,
                "days_until_expiry": None,
                "expiration_status": "ok"
            }
            
            if cred.permit_definition_id in app_state.oracle_system.definitions:
                definition = app_state.oracle_system.definitions[cred.permit_definition_id]
                cred_data["permit_name"] = definition.name
            
            if cred.expires_at:
                if cred.expires_at < now:
                    cred_data["expiration_status"] = "expired"
                    cred_data["days_until_expiry"] = (cred.expires_at - now).days
                    if include_expired:
                        expired.append(cred_data)
                elif cred.expires_at < thirty_days_later:
                    cred_data["expiration_status"] = "expiring_soon"
                    cred_data["days_until_expiry"] = (cred.expires_at - now).days
                    expiring_soon.append(cred_data)
                else:
                    cred_data["days_until_expiry"] = (cred.expires_at - now).days
            
            all_credentials.append(cred_data)
        
        return JSONResponse({
            "success": True,
            "credentials": all_credentials,
            "expiring_soon": expiring_soon,
            "expired": expired,
            "total": len(all_credentials),
            "timestamp": datetime.now().isoformat()
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error getting user credentials: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/permit/masters")
async def get_available_masters(
    permit_id: str,
    exclude_npub: Optional[str] = None
):
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        permit_name = permit_id
        if permit_id in app_state.oracle_system.definitions:
            permit_name = app_state.oracle_system.definitions[permit_id].name
        
        all_credentials = app_state.oracle_system.fetch_permit_credentials_from_nostr()
        
        now = datetime.now()
        masters = []
        seen_pubkeys = set()
        
        for cred in all_credentials:
            cred_permit_id = cred.permit_definition_id
            
            is_matching_permit = False
            cred_level = "X1"
            
            if cred_permit_id == permit_id:
                is_matching_permit = True
            elif "_X" in permit_id and "_X" in cred_permit_id:
                base_requested = permit_id.rsplit("_X", 1)[0]
                base_cred = cred_permit_id.rsplit("_X", 1)[0]
                
                if base_requested == base_cred:
                    try:
                        level_requested = int(permit_id.rsplit("_X", 1)[1])
                        level_cred = int(cred_permit_id.rsplit("_X", 1)[1])
                        cred_level = f"X{level_cred}"
                        
                        if level_cred >= level_requested:
                            is_matching_permit = True
                    except ValueError:
                        pass
            
            if not is_matching_permit:
                continue
            
            if cred.expires_at and cred.expires_at < now:
                continue
            
            if cred.holder_npub == exclude_npub:
                continue
            if cred.holder_npub in seen_pubkeys:
                continue
            
            seen_pubkeys.add(cred.holder_npub)
            
            profile_info = {}
            try:
                holder_hex = npub_to_hex(cred.holder_npub) if cred.holder_npub.startswith("npub") else cred.holder_npub
                if holder_hex:
                    profiles = await fetch_nostr_profiles([holder_hex])
                    if holder_hex in profiles:
                        profile_info = profiles[holder_hex]
            except Exception as e:
                logging.debug(f"Could not fetch profile for {cred.holder_npub}: {e}")
            
            masters.append({
                "npub": cred.holder_npub,
                "hex_pubkey": npub_to_hex(cred.holder_npub) if cred.holder_npub.startswith("npub") else cred.holder_npub,
                "name": profile_info.get("name", ""),
                "display_name": profile_info.get("display_name", ""),
                "picture": profile_info.get("picture", ""),
                "level": cred_level,
                "competencies": [],
                "credential_id": cred.credential_id,
                "credential_expires_at": cred.expires_at.isoformat() if cred.expires_at else None
            })
        
        return JSONResponse({
            "success": True,
            "permit_id": permit_id,
            "permit_name": permit_name,
            "masters": masters,
            "total": len(masters),
            "timestamp": datetime.now().isoformat()
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error getting available masters: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class RenewalRequestData(BaseModel):
    permit_id: str
    previous_credential_id: Optional[str] = None
    statement: str = "Annual renewal request"
    evidence: List[str] = []
    npub: str

@router.post("/api/permit/renewal/request")
async def create_renewal_request(renewal_data: RenewalRequestData):
    from core.state import app_state, ORACLE_ENABLED
    if not ORACLE_ENABLED or app_state.oracle_system is None:
        raise HTTPException(status_code=503, detail="Oracle system not available")
    
    try:
        if renewal_data.permit_id not in app_state.oracle_system.definitions:
            definitions = app_state.oracle_system.fetch_permit_definitions_from_nostr()
            found = False
            for d in definitions:
                if d.id == renewal_data.permit_id:
                    found = True
                    break
            if not found:
                raise HTTPException(status_code=404, detail=f"Permit definition '{renewal_data.permit_id}' not found")
        
        import json
        request_id = f"renewal_{renewal_data.npub[:16]}_{int(datetime.now().timestamp())}"
        
        event_template = {
            "kind": 30501,
            "created_at": int(datetime.now().timestamp()),
            "tags": [
                ["d", request_id],
                ["l", renewal_data.permit_id, "permit_type"],
                ["t", "permit"],
                ["t", "request"],
                ["t", "renewal"]
            ],
            "content": json.dumps({
                "request_id": request_id,
                "request_type": "renewal",
                "permit_definition_id": renewal_data.permit_id,
                "previous_credential_id": renewal_data.previous_credential_id,
                "applicant_did": f"did:nostr:{renewal_data.npub}",
                "statement": renewal_data.statement,
                "evidence": renewal_data.evidence,
                "status": "pending"
            })
        }
        
        if renewal_data.previous_credential_id:
            event_template["tags"].append(["e", renewal_data.previous_credential_id, "", "previous_credential"])
        
        return JSONResponse({
            "success": True,
            "message": "Renewal request template created. Sign with NIP-07 and publish.",
            "request_id": request_id,
            "event_template": event_template,
            "instructions": {
                "step1": "Sign this event with your NOSTR private key (NIP-07)",
                "step2": "Publish to your configured relay",
                "step3": "A certified master must create a 30502 attestation",
                "step4": "Once attested, Oracle will issue new 30503 credential"
            }
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error creating renewal request: {e}")
        raise HTTPException(status_code=500, detail=str(e))
