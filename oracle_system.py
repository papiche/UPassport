#!/usr/bin/env python3
################################################################################
# Script: oracle_system.py
# Description: Multi-signature permit management system (Oracle System)
# 
# The Oracle System manages "permits" (licenses) that require multiple human
# attestations to be validated. It implements the Web of Trust (WoT) permit
# concept as described in the CopyLaRadio article:
# https://www.copylaradio.com/blog/blog-1/post/reinventer-la-societe-avec-la-monnaie-libre-et-la-web-of-trust-148#
#
# Key Features:
# - Permit definitions (e.g., "Driver's License", "ORE Verifier", etc.)
# - Request submission by applicants
# - Multi-signature attestation by certified experts
# - Verifiable Credentials (VC) issuance upon validation
# - Final signature by UPLANETNAME.G1 authority
# - Integration with DID/NOSTR infrastructure
#
# NOSTR Event Kinds:
# - 30500: Permit Definition (Parameterized Replaceable)
# - 30501: Permit Request (Parameterized Replaceable)
# - 30502: Permit Attestation (Parameterized Replaceable)
# - 30503: Permit Credential (Verifiable Credential) (Parameterized Replaceable)
#
# License: AGPL-3.0
# Author: UPlanet/Astroport.ONE Team (support@qo-op.com)
################################################################################

import os
import sys
import json
import hashlib
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from enum import Enum

# Configuration
NOSTR_RELAYS = os.getenv("NOSTR_RELAYS", "ws://127.0.0.1:7777 wss://relay.copylaradio.com").split()
MY_PATH = Path(__file__).parent.parent
TOOLS_PATH = MY_PATH / "Astroport.ONE" / "tools"

# NOSTR Event Kinds for Permit System
class PermitEventKind(Enum):
    PERMIT_DEFINITION = 30500      # License definition
    PERMIT_REQUEST = 30501         # License application
    PERMIT_ATTESTATION = 30502     # Expert attestation
    PERMIT_CREDENTIAL = 30503      # Verifiable Credential (final permit)

# Permit Status
class PermitStatus(Enum):
    PENDING = "pending"            # Waiting for attestations
    ATTESTING = "attesting"        # Collecting attestations
    VALIDATED = "validated"        # All attestations collected
    ISSUED = "issued"              # VC signed by UPlanet authority
    REJECTED = "rejected"          # Failed validation
    REVOKED = "revoked"            # Permit revoked

@dataclass
class PermitDefinition:
    """Defines the rules for a permit/license type"""
    id: str                              # Unique identifier (e.g., "PERMIT_ORE_V1")
    name: str                            # Human-readable name
    description: str                     # Detailed description
    issuer_did: str                      # DID of the authority (e.g., "did:nostr:UPLANETNAME")
    min_attestations: int                # Minimum number of attestations required
    required_license: Optional[str]      # Required license for attesters (None if no requirement)
    valid_duration_days: int             # Validity period in days (0 = unlimited)
    revocable: bool                      # Can this permit be revoked?
    verification_method: str             # Method of verification (peer_attestation, exam, etc.)
    metadata: Dict[str, Any]             # Additional metadata

@dataclass
class PermitRequest:
    """A request for a permit by an applicant"""
    request_id: str                      # Unique request ID
    permit_definition_id: str            # Type of permit requested
    applicant_did: str                   # DID of the applicant
    applicant_npub: str                  # NOSTR pubkey of the applicant
    statement: str                       # Applicant's statement
    evidence: List[str]                  # Links to evidence (IPFS, etc.)
    status: PermitStatus
    created_at: datetime
    updated_at: datetime
    attestations: List[Dict[str, Any]]   # List of attestations
    nostr_event_id: Optional[str]        # NOSTR event ID

@dataclass
class PermitAttestation:
    """An attestation/signature from an expert"""
    attestation_id: str                  # Unique attestation ID
    request_id: str                      # Request this attestation is for
    attester_did: str                    # DID of the attester
    attester_npub: str                   # NOSTR pubkey of the attester
    attester_license_id: Optional[str]   # Attester's own permit ID (if required)
    statement: str                       # Attester's statement
    signature: str                       # Cryptographic signature
    created_at: datetime
    nostr_event_id: Optional[str]        # NOSTR event ID

@dataclass
class PermitCredential:
    """A Verifiable Credential for an issued permit"""
    credential_id: str                   # Unique credential ID
    request_id: str                      # Original request ID
    permit_definition_id: str            # Type of permit
    holder_did: str                      # DID of the holder
    holder_npub: str                     # NOSTR pubkey of the holder
    issued_by: str                       # DID of issuer (UPlanet authority)
    issued_at: datetime
    expires_at: Optional[datetime]       # Expiration date (None if unlimited)
    attestations: List[str]              # List of attestation IDs
    proof: Dict[str, Any]                # Cryptographic proof (signature by UPlanet)
    status: PermitStatus
    nostr_event_id: Optional[str]        # NOSTR event ID

class OracleSystem:
    """Core permit management system"""
    
    def __init__(self, data_dir: Path = None):
        self.data_dir = data_dir or Path.home() / ".zen" / "game" / "permits"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.definitions_file = self.data_dir / "definitions.json"
        self.requests_file = self.data_dir / "requests.json"
        self.attestations_file = self.data_dir / "attestations.json"
        self.credentials_file = self.data_dir / "credentials.json"
        
        self.definitions: Dict[str, PermitDefinition] = {}
        self.requests: Dict[str, PermitRequest] = {}
        self.attestations: Dict[str, PermitAttestation] = {}
        self.credentials: Dict[str, PermitCredential] = {}
        
        self.load_data()
    
    def load_data(self):
        """Load permit data from files"""
        if self.definitions_file.exists():
            with open(self.definitions_file, 'r') as f:
                data = json.load(f)
                self.definitions = {k: PermitDefinition(**v) for k, v in data.items()}
        
        if self.requests_file.exists():
            with open(self.requests_file, 'r') as f:
                data = json.load(f)
                for k, v in data.items():
                    v['status'] = PermitStatus(v['status'])
                    v['created_at'] = datetime.fromisoformat(v['created_at'])
                    v['updated_at'] = datetime.fromisoformat(v['updated_at'])
                    self.requests[k] = PermitRequest(**v)
        
        if self.attestations_file.exists():
            with open(self.attestations_file, 'r') as f:
                data = json.load(f)
                for k, v in data.items():
                    v['created_at'] = datetime.fromisoformat(v['created_at'])
                    self.attestations[k] = PermitAttestation(**v)
        
        if self.credentials_file.exists():
            with open(self.credentials_file, 'r') as f:
                data = json.load(f)
                for k, v in data.items():
                    v['status'] = PermitStatus(v['status'])
                    v['issued_at'] = datetime.fromisoformat(v['issued_at'])
                    if v['expires_at']:
                        v['expires_at'] = datetime.fromisoformat(v['expires_at'])
                    self.credentials[k] = PermitCredential(**v)
    
    def save_data(self):
        """Save permit data to files"""
        with open(self.definitions_file, 'w') as f:
            json.dump({k: asdict(v) for k, v in self.definitions.items()}, f, indent=2)
        
        with open(self.requests_file, 'w') as f:
            data = {}
            for k, v in self.requests.items():
                d = asdict(v)
                d['status'] = v.status.value
                d['created_at'] = v.created_at.isoformat()
                d['updated_at'] = v.updated_at.isoformat()
                data[k] = d
            json.dump(data, f, indent=2)
        
        with open(self.attestations_file, 'w') as f:
            data = {}
            for k, v in self.attestations.items():
                d = asdict(v)
                d['created_at'] = v.created_at.isoformat()
                data[k] = d
            json.dump(data, f, indent=2)
        
        with open(self.credentials_file, 'w') as f:
            data = {}
            for k, v in self.credentials.items():
                d = asdict(v)
                d['status'] = v.status.value
                d['issued_at'] = v.issued_at.isoformat()
                if v.expires_at:
                    d['expires_at'] = v.expires_at.isoformat()
                data[k] = d
            json.dump(data, f, indent=2)
    
    def create_permit_definition(self, definition: PermitDefinition) -> bool:
        """Create a new permit definition"""
        if definition.id in self.definitions:
            print(f"❌ Permit definition {definition.id} already exists")
            return False
        
        self.definitions[definition.id] = definition
        self.save_data()
        
        # Publish to NOSTR
        self.publish_permit_definition(definition)
        
        print(f"✅ Permit definition {definition.id} created")
        return True
    
    def request_permit(self, request: PermitRequest) -> bool:
        """Submit a permit request"""
        if request.permit_definition_id not in self.definitions:
            print(f"❌ Permit definition {request.permit_definition_id} not found")
            return False
        
        if request.request_id in self.requests:
            print(f"❌ Request {request.request_id} already exists")
            return False
        
        request.status = PermitStatus.PENDING
        request.created_at = datetime.now()
        request.updated_at = datetime.now()
        request.attestations = []
        
        self.requests[request.request_id] = request
        self.save_data()
        
        # Publish to NOSTR
        self.publish_permit_request(request)
        
        print(f"✅ Permit request {request.request_id} submitted")
        return True
    
    def attest_permit(self, attestation: PermitAttestation) -> bool:
        """Add an attestation to a permit request"""
        if attestation.request_id not in self.requests:
            print(f"❌ Request {attestation.request_id} not found")
            return False
        
        request = self.requests[attestation.request_id]
        definition = self.definitions[request.permit_definition_id]
        
        # Check if attester has required license
        if definition.required_license:
            if not self.check_attester_has_license(attestation.attester_npub, definition.required_license):
                print(f"❌ Attester {attestation.attester_npub} does not have required license {definition.required_license}")
                return False
        
        # Check if attester already attested
        for att_id in request.attestations:
            att = self.attestations.get(att_id)
            if att and att.attester_npub == attestation.attester_npub:
                print(f"❌ Attester {attestation.attester_npub} already attested for this request")
                return False
        
        attestation.created_at = datetime.now()
        self.attestations[attestation.attestation_id] = attestation
        
        # Add attestation to request
        request.attestations.append(attestation.attestation_id)
        request.updated_at = datetime.now()
        
        # Check if enough attestations
        if len(request.attestations) >= definition.min_attestations:
            request.status = PermitStatus.VALIDATED
            print(f"✅ Permit request {request.request_id} validated with {len(request.attestations)} attestations")
            
            # Auto-issue credential
            self.issue_credential(request.request_id)
        else:
            request.status = PermitStatus.ATTESTING
            print(f"✅ Attestation added ({len(request.attestations)}/{definition.min_attestations})")
        
        self.save_data()
        
        # Publish to NOSTR
        self.publish_permit_attestation(attestation)
        
        return True
    
    def issue_credential(self, request_id: str) -> Optional[PermitCredential]:
        """Issue a Verifiable Credential for a validated permit request"""
        if request_id not in self.requests:
            print(f"❌ Request {request_id} not found")
            return None
        
        request = self.requests[request_id]
        
        if request.status != PermitStatus.VALIDATED:
            print(f"❌ Request {request_id} is not validated")
            return None
        
        definition = self.definitions[request.permit_definition_id]
        
        # Generate credential
        credential_id = hashlib.sha256(f"{request_id}:{datetime.now().isoformat()}".encode()).hexdigest()[:16]
        
        expires_at = None
        if definition.valid_duration_days > 0:
            expires_at = datetime.now() + timedelta(days=definition.valid_duration_days)
        
        # Sign credential with UPlanet authority key
        proof = self.sign_credential(request, definition)
        
        credential = PermitCredential(
            credential_id=credential_id,
            request_id=request_id,
            permit_definition_id=request.permit_definition_id,
            holder_did=request.applicant_did,
            holder_npub=request.applicant_npub,
            issued_by=definition.issuer_did,
            issued_at=datetime.now(),
            expires_at=expires_at,
            attestations=request.attestations.copy(),
            proof=proof,
            status=PermitStatus.ISSUED,
            nostr_event_id=None
        )
        
        self.credentials[credential_id] = credential
        request.status = PermitStatus.ISSUED
        request.updated_at = datetime.now()
        
        self.save_data()
        
        # Publish to NOSTR
        self.publish_permit_credential(credential)
        
        # Update holder's DID document
        self.update_holder_did(credential)
        
        print(f"✅ Credential {credential_id} issued for {request.applicant_npub}")
        return credential
    
    def sign_credential(self, request: PermitRequest, definition: PermitDefinition) -> Dict[str, Any]:
        """Sign a credential with UPlanet authority key"""
        # Get UPlanet G1 key for signing
        uplanet_g1_key = os.getenv("UPLANETNAME_G1", "")
        
        # Create proof structure
        proof = {
            "@context": "https://w3id.org/security/v2",
            "type": "Ed25519Signature2020",
            "created": datetime.now().isoformat(),
            "verificationMethod": f"{definition.issuer_did}#uplanet-authority",
            "proofPurpose": "assertionMethod",
            "proofValue": ""  # Will be filled by actual signing
        }
        
        # Create credential data for signing
        credential_data = {
            "request_id": request.request_id,
            "permit_definition_id": request.permit_definition_id,
            "holder_did": request.applicant_did,
            "holder_npub": request.applicant_npub,
            "attestations": len(request.attestations),
            "issued_at": datetime.now().isoformat()
        }
        
        # Sign with UPlanet authority
        data_str = json.dumps(credential_data, sort_keys=True)
        signature = hashlib.sha256(f"{data_str}:{uplanet_g1_key}".encode()).hexdigest()
        
        proof["proofValue"] = signature
        
        return proof
    
    def check_attester_has_license(self, attester_npub: str, required_license: str) -> bool:
        """Check if attester has the required license"""
        # Look for existing credential for this attester
        for credential in self.credentials.values():
            if (credential.holder_npub == attester_npub and 
                credential.permit_definition_id == required_license and
                credential.status == PermitStatus.ISSUED):
                
                # Check if not expired
                if credential.expires_at and credential.expires_at < datetime.now():
                    continue
                
                return True
        
        return False
    
    def update_holder_did(self, credential: PermitCredential):
        """Update holder's DID document with new credential"""
        # Get holder's email from NOSTR key
        email = self.get_email_from_npub(credential.holder_npub)
        if not email:
            print(f"⚠️  Could not find email for {credential.holder_npub}")
            return
        
        # Update DID using did_manager_nostr.sh
        did_manager = TOOLS_PATH / "did_manager_nostr.sh"
        if did_manager.exists():
            try:
                # Add credential to DID document
                subprocess.run([
                    str(did_manager),
                    "update",
                    email,
                    "PERMIT_ISSUED",
                    "0",
                    "0"
                ], check=False)
                
                print(f"✅ DID updated for {email}")
            except Exception as e:
                print(f"⚠️  Failed to update DID: {e}")
    
    def get_email_from_npub(self, npub: str) -> Optional[str]:
        """Get email from NOSTR pubkey (scan ~/.zen/game/nostr)"""
        nostr_dir = Path.home() / ".zen" / "game" / "nostr"
        if not nostr_dir.exists():
            return None
        
        for email_dir in nostr_dir.iterdir():
            if not email_dir.is_dir():
                continue
            
            npub_file = email_dir / "NPUB"
            if npub_file.exists():
                stored_npub = npub_file.read_text().strip()
                if stored_npub == npub:
                    return email_dir.name
        
        return None
    
    def publish_permit_definition(self, definition: PermitDefinition):
        """Publish permit definition to NOSTR"""
        # Prepare NOSTR event (kind 30500)
        event_data = {
            "kind": PermitEventKind.PERMIT_DEFINITION.value,
            "content": json.dumps(asdict(definition)),
            "tags": [
                ["d", definition.id],
                ["t", "permit"],
                ["t", "definition"],
                ["t", definition.id]
            ]
        }
        
        self._publish_to_nostr(event_data)
    
    def publish_permit_request(self, request: PermitRequest):
        """Publish permit request to NOSTR"""
        # Prepare NOSTR event (kind 30501)
        event_data = {
            "kind": PermitEventKind.PERMIT_REQUEST.value,
            "content": json.dumps({
                "request_id": request.request_id,
                "permit_definition_id": request.permit_definition_id,
                "applicant_did": request.applicant_did,
                "statement": request.statement,
                "evidence": request.evidence,
                "status": request.status.value
            }),
            "tags": [
                ["d", request.request_id],
                ["l", request.permit_definition_id, "permit_type"],
                ["p", request.applicant_npub],
                ["t", "permit"],
                ["t", "request"]
            ]
        }
        
        self._publish_to_nostr(event_data, request.applicant_npub)
    
    def publish_permit_attestation(self, attestation: PermitAttestation):
        """Publish permit attestation to NOSTR"""
        # Prepare NOSTR event (kind 30502)
        event_data = {
            "kind": PermitEventKind.PERMIT_ATTESTATION.value,
            "content": json.dumps({
                "attestation_id": attestation.attestation_id,
                "request_id": attestation.request_id,
                "attester_did": attestation.attester_did,
                "statement": attestation.statement,
                "signature": attestation.signature
            }),
            "tags": [
                ["d", attestation.attestation_id],
                ["e", attestation.request_id],
                ["p", attestation.attester_npub],
                ["t", "permit"],
                ["t", "attestation"]
            ]
        }
        
        self._publish_to_nostr(event_data, attestation.attester_npub)
    
    def publish_permit_credential(self, credential: PermitCredential):
        """Publish permit credential to NOSTR"""
        # Prepare NOSTR event (kind 30503)
        event_data = {
            "kind": PermitEventKind.PERMIT_CREDENTIAL.value,
            "content": json.dumps({
                "credential_id": credential.credential_id,
                "permit_definition_id": credential.permit_definition_id,
                "holder_did": credential.holder_did,
                "issued_by": credential.issued_by,
                "issued_at": credential.issued_at.isoformat(),
                "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
                "attestations": len(credential.attestations),
                "proof": credential.proof,
                "status": credential.status.value
            }),
            "tags": [
                ["d", credential.credential_id],
                ["l", credential.permit_definition_id, "permit_type"],
                ["p", credential.holder_npub],
                ["t", "permit"],
                ["t", "credential"],
                ["t", "verifiable-credential"]
            ]
        }
        
        # Use UPlanet authority key for signing
        self._publish_to_nostr(event_data, None, use_uplanet_key=True)
    
    def _publish_to_nostr(self, event_data: Dict[str, Any], signer_npub: Optional[str] = None, use_uplanet_key: bool = False):
        """Publish an event to NOSTR relays using nostr_send_note.py"""
        import subprocess
        import tempfile
        
        # Save event for manual publishing (backup)
        events_dir = self.data_dir / "nostr_events"
        events_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        event_file = events_dir / f"{event_data['kind']}_{timestamp}.json"
        
        with open(event_file, 'w') as f:
            json.dump(event_data, f, indent=2)
        
        print(f"📡 NOSTR event saved: {event_file}")
        
        # Publish to NOSTR using nostr_send_note.py
        try:
            # Find the nostr_send_note.py script
            nostr_script = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "nostr_send_note.py"
            
            if not nostr_script.exists():
                print(f"⚠️  nostr_send_note.py not found at {nostr_script}")
                return
            
            # Determine which keyfile to use
            if use_uplanet_key:
                # Use UPLANETNAME.G1 key (standardized location)
                keyfile = Path.home() / ".zen" / "game" / "uplanet.G1.nostr"
            elif signer_npub:
                # Try to find keyfile by email/npub
                email = self.get_email_from_npub(signer_npub)
                if email:
                    keyfile = Path.home() / ".zen" / "game" / "nostr" / email / ".secret.nostr"
                else:
                    print(f"⚠️  Could not find keyfile for {signer_npub}")
                    return
            else:
                print("⚠️  No signer specified")
                return
            
            if not keyfile.exists():
                print(f"⚠️  Keyfile not found: {keyfile}")
                return
            
            # Prepare event content and tags
            content = event_data.get('content', '')
            tags = event_data.get('tags', [])
            kind = event_data.get('kind', 1)
            
            # Convert tags to JSON string for command line
            tags_json = json.dumps(tags)
            
            # Call nostr_send_note.py
            cmd = [
                'python3',
                str(nostr_script),
                '--keyfile', str(keyfile),
                '--content', content,
                '--kind', str(kind),
                '--tags', tags_json,
                '--relays', ' '.join(NOSTR_RELAYS)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                print(f"✅ Event published to NOSTR (kind {kind})")
            else:
                print(f"⚠️  Failed to publish event: {result.stderr}")
        
        except Exception as e:
            print(f"⚠️  Error publishing to NOSTR: {e}")
    
    def get_request_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get the status of a permit request"""
        if request_id not in self.requests:
            return None
        
        request = self.requests[request_id]
        definition = self.definitions[request.permit_definition_id]
        
        attestations_data = [
            {
                "attester_npub": self.attestations[att_id].attester_npub,
                "attester_did": self.attestations[att_id].attester_did,
                "statement": self.attestations[att_id].statement,
                "created_at": self.attestations[att_id].created_at.isoformat()
            }
            for att_id in request.attestations
            if att_id in self.attestations
        ]
        
        return {
            "request_id": request.request_id,
            "permit_type": definition.name,
            "permit_definition_id": request.permit_definition_id,
            "applicant_did": request.applicant_did,
            "applicant_npub": request.applicant_npub,
            "status": request.status.value,
            "attestations_count": len(request.attestations),
            "required_attestations": definition.min_attestations,
            "attestations": attestations_data,
            "created_at": request.created_at.isoformat(),
            "updated_at": request.updated_at.isoformat()
        }
    
    def list_requests(self, applicant_npub: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all permit requests (optionally filtered by applicant)"""
        results = []
        
        for request in self.requests.values():
            if applicant_npub and request.applicant_npub != applicant_npub:
                continue
            
            status_data = self.get_request_status(request.request_id)
            if status_data:
                results.append(status_data)
        
        return sorted(results, key=lambda x: x['created_at'], reverse=True)
    
    def list_credentials(self, holder_npub: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all issued credentials (optionally filtered by holder)"""
        results = []
        
        for credential in self.credentials.values():
            if holder_npub and credential.holder_npub != holder_npub:
                continue
            
            definition = self.definitions[credential.permit_definition_id]
            
            results.append({
                "credential_id": credential.credential_id,
                "permit_type": definition.name,
                "permit_definition_id": credential.permit_definition_id,
                "holder_did": credential.holder_did,
                "holder_npub": credential.holder_npub,
                "issued_by": credential.issued_by,
                "issued_at": credential.issued_at.isoformat(),
                "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
                "status": credential.status.value,
                "attestations_count": len(credential.attestations)
            })
        
        return sorted(results, key=lambda x: x['issued_at'], reverse=True)
    
    def fetch_nostr_events(self, kind: int, author_hex: Optional[str] = None, since_timestamp: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch NOSTR events from strfry relay using nostr_get_events.sh"""
        import subprocess
        
        try:
            # Find the nostr_get_events.sh script
            nostr_script = Path.home() / ".zen" / "Astroport.ONE" / "tools" / "nostr_get_events.sh"
            
            if not nostr_script.exists():
                print(f"⚠️  nostr_get_events.sh not found at {nostr_script}")
                print(f"⚠️  Cannot query NOSTR events - strfry query tool missing")
                return []
            
            # Build command
            cmd = [str(nostr_script), '--kind', str(kind)]
            
            if author_hex:
                cmd.extend(['--author', author_hex])
            
            if since_timestamp:
                cmd.extend(['--since', str(since_timestamp)])
            
            # Execute query
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                print(f"⚠️  Error querying strfry: {result.stderr}")
                return []
            
            # Parse JSON events (one per line)
            events = []
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    try:
                        event = json.loads(line)
                        events.append(event)
                    except json.JSONDecodeError as e:
                        print(f"⚠️  Error parsing event JSON: {e}")
            
            print(f"✅ Fetched {len(events)} events of kind {kind} from strfry")
            return events
        
        except subprocess.TimeoutExpired:
            print(f"⚠️  Query timeout for kind {kind}")
            return []
        
        except Exception as e:
            print(f"⚠️  Error fetching NOSTR events: {e}")
            return []
    
    def fetch_permit_definitions_from_nostr(self) -> List[PermitDefinition]:
        """Fetch permit definitions (kind 30500) from NOSTR"""
        events = self.fetch_nostr_events(kind=30500)
        
        definitions = []
        for event in events:
            try:
                # Parse tags to extract permit information
                permit_id = None
                for tag in event.get('tags', []):
                    if tag[0] == 'd':
                        permit_id = tag[1]
                        break
                
                if permit_id:
                    content = json.loads(event.get('content', '{}'))
                    
                    definition = PermitDefinition(
                        id=permit_id,
                        name=content.get('name', ''),
                        description=content.get('description', ''),
                        issuer_did=content.get('issuer_did', ''),
                        min_attestations=content.get('min_attestations', 5),
                        required_license=content.get('required_license'),
                        valid_duration_days=content.get('valid_duration_days', 0),
                        revocable=content.get('revocable', True),
                        verification_method=content.get('verification_method', 'peer_attestation'),
                        metadata=content.get('metadata', {})
                    )
                    
                    definitions.append(definition)
            
            except Exception as e:
                print(f"⚠️  Error parsing permit definition: {e}")
        
        return definitions
    
    def fetch_permit_requests_from_nostr(self, permit_id: Optional[str] = None) -> List[PermitRequest]:
        """Fetch permit requests (kind 30501) from NOSTR"""
        events = self.fetch_nostr_events(kind=30501)
        
        requests = []
        for event in events:
            try:
                request_id = None
                permit_definition_id = None
                
                for tag in event.get('tags', []):
                    if tag[0] == 'd':
                        request_id = tag[1]
                    elif tag[0] == 'permit_id':
                        permit_definition_id = tag[1]
                
                if permit_id and permit_definition_id != permit_id:
                    continue
                
                if request_id and permit_definition_id:
                    content = json.loads(event.get('content', '{}'))
                    
                    permit_request = PermitRequest(
                        request_id=request_id,
                        permit_definition_id=permit_definition_id,
                        applicant_did=f"did:nostr:{event.get('pubkey', '')}",
                        applicant_npub=event.get('pubkey', ''),
                        statement=content.get('statement', ''),
                        evidence=content.get('evidence', []),
                        status=PermitStatus.PENDING,
                        created_at=datetime.fromtimestamp(event.get('created_at', 0)),
                        updated_at=datetime.now(),
                        attestations=[],
                        nostr_event_id=event.get('id')
                    )
                    
                    requests.append(permit_request)
            
            except Exception as e:
                print(f"⚠️  Error parsing permit request: {e}")
        
        return requests
    
    def fetch_permit_credentials_from_nostr(self, holder_npub: Optional[str] = None) -> List[PermitCredential]:
        """Fetch permit credentials (kind 30503) from NOSTR"""
        events = self.fetch_nostr_events(kind=30503)
        
        credentials = []
        for event in events:
            try:
                credential_id = None
                holder_pubkey = None
                
                for tag in event.get('tags', []):
                    if tag[0] == 'd':
                        credential_id = tag[1]
                    elif tag[0] == 'p':
                        holder_pubkey = tag[1]
                
                if holder_npub and holder_pubkey != holder_npub:
                    continue
                
                if credential_id and holder_pubkey:
                    content = json.loads(event.get('content', '{}'))
                    
                    credential = PermitCredential(
                        credential_id=credential_id,
                        request_id=content.get('request_id', ''),
                        permit_definition_id=content.get('permit_id', ''),
                        holder_did=f"did:nostr:{holder_pubkey}",
                        holder_npub=holder_pubkey,
                        issued_by=f"did:nostr:{event.get('pubkey', '')}",
                        issued_at=datetime.fromtimestamp(event.get('created_at', 0)),
                        expires_at=datetime.fromisoformat(content.get('expires_at')) if content.get('expires_at') else None,
                        attestations=content.get('attestations', []),
                        proof=content.get('proof', {}),
                        status=PermitStatus.ISSUED,
                        nostr_event_id=event.get('id')
                    )
                    
                    credentials.append(credential)
            
            except Exception as e:
                print(f"⚠️  Error parsing permit credential: {e}")
        
        return credentials

def main():
    """CLI interface for oracle system"""
    import argparse
    
    parser = argparse.ArgumentParser(description="UPlanet Oracle System - Multi-signature Permit Management")
    subparsers = parser.add_subparsers(dest='command', help='Command')
    
    # Create definition
    create_def_parser = subparsers.add_parser('create-definition', help='Create a new permit definition')
    create_def_parser.add_argument('id', help='Permit definition ID')
    create_def_parser.add_argument('name', help='Permit name')
    create_def_parser.add_argument('--min-attestations', type=int, default=5, help='Minimum attestations required')
    create_def_parser.add_argument('--required-license', help='Required license for attesters')
    create_def_parser.add_argument('--valid-days', type=int, default=0, help='Validity period (0=unlimited)')
    
    # Request permit
    request_parser = subparsers.add_parser('request', help='Request a permit')
    request_parser.add_argument('permit_id', help='Permit definition ID')
    request_parser.add_argument('applicant_npub', help='Applicant NOSTR pubkey')
    request_parser.add_argument('statement', help='Applicant statement')
    
    # Attest permit
    attest_parser = subparsers.add_parser('attest', help='Attest a permit request')
    attest_parser.add_argument('request_id', help='Request ID')
    attest_parser.add_argument('attester_npub', help='Attester NOSTR pubkey')
    attest_parser.add_argument('statement', help='Attestation statement')
    
    # Status
    status_parser = subparsers.add_parser('status', help='Get request status')
    status_parser.add_argument('request_id', help='Request ID')
    
    # List
    list_parser = subparsers.add_parser('list', help='List requests or credentials')
    list_parser.add_argument('type', choices=['requests', 'credentials'], help='What to list')
    list_parser.add_argument('--npub', help='Filter by NOSTR pubkey')
    
    args = parser.parse_args()
    
    oracle = OracleSystem()
    
    if args.command == 'create-definition':
        definition = PermitDefinition(
            id=args.id,
            name=args.name,
            description=f"Permit: {args.name}",
            issuer_did=f"did:nostr:{os.getenv('UPLANETNAME_G1', '')}",
            min_attestations=args.min_attestations,
            required_license=args.required_license,
            valid_duration_days=args.valid_days,
            revocable=True,
            verification_method="peer_attestation",
            metadata={}
        )
        oracle.create_permit_definition(definition)
    
    elif args.command == 'request':
        request_id = hashlib.sha256(f"{args.applicant_npub}:{args.permit_id}:{time.time()}".encode()).hexdigest()[:16]
        request = PermitRequest(
            request_id=request_id,
            permit_definition_id=args.permit_id,
            applicant_did=f"did:nostr:{args.applicant_npub}",
            applicant_npub=args.applicant_npub,
            statement=args.statement,
            evidence=[],
            status=PermitStatus.PENDING,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            attestations=[],
            nostr_event_id=None
        )
        oracle.request_permit(request)
    
    elif args.command == 'attest':
        attestation_id = hashlib.sha256(f"{args.attester_npub}:{args.request_id}:{time.time()}".encode()).hexdigest()[:16]
        attestation = PermitAttestation(
            attestation_id=attestation_id,
            request_id=args.request_id,
            attester_did=f"did:nostr:{args.attester_npub}",
            attester_npub=args.attester_npub,
            attester_license_id=None,
            statement=args.statement,
            signature=hashlib.sha256(f"{args.statement}:{time.time()}".encode()).hexdigest(),
            created_at=datetime.now(),
            nostr_event_id=None
        )
        oracle.attest_permit(attestation)
    
    elif args.command == 'status':
        status = oracle.get_request_status(args.request_id)
        if status:
            print(json.dumps(status, indent=2))
        else:
            print(f"❌ Request {args.request_id} not found")
    
    elif args.command == 'list':
        if args.type == 'requests':
            results = oracle.list_requests(applicant_npub=args.npub)
            print(json.dumps(results, indent=2))
        elif args.type == 'credentials':
            results = oracle.list_credentials(holder_npub=args.npub)
            print(json.dumps(results, indent=2))
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()

