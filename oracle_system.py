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
# - Final signature by oracle authority (myswarm_secret.nostr for stations, UPLANETNAME_G1 for primary)
# - Integration with DID/NOSTR infrastructure
# - Resilient architecture: each oracle signs with its own key, only primary uses UPLANETNAME_G1
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

def canonicalize_json(data: Any) -> str:
    """
    Canonicalize JSON according to RFC 8785 (JCS - JSON Canonicalization Scheme).
    
    This ensures that the same JSON data always produces the same string representation,
    which is critical for cryptographic signatures. The output is:
    - Keys sorted lexicographically
    - No whitespace between tokens
    - No trailing commas
    - Consistent number formatting
    
    Args:
        data: Python object to serialize (dict, list, etc.)
    
    Returns:
        Canonical JSON string ready for signing
    
    Reference: https://datatracker.ietf.org/doc/html/rfc8785
    """
    return json.dumps(
        data,
        sort_keys=True,           # Lexicographic key ordering
        separators=(',', ':'),   # No whitespace (compact)
        ensure_ascii=False,      # Preserve Unicode
        allow_nan=False          # Reject NaN/Infinity (not in JSON spec)
    )

# Configuration
NOSTR_RELAYS = os.getenv("NOSTR_RELAYS", "ws://127.0.0.1:7777 wss://relay.copylaradio.com").split()
TOOLS_PATH = Path(os.path.expandvars(os.path.expanduser("$HOME"))) / "Astroport.ONE" / "tools"

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
    
    def create_permit_definition(self, definition: PermitDefinition, creator_npub: Optional[str] = None) -> bool:
        """Create a new permit definition
        
        Args:
            definition: The permit definition to create
            creator_npub: Optional NOSTR pubkey of the creator (for saving event in their directory)
        """
        if definition.id in self.definitions:
            print(f"❌ Permit definition {definition.id} already exists")
            return False
        
        self.definitions[definition.id] = definition
        self.save_data()
        
        # Publish to NOSTR (signed by oracle key, but saved in creator's directory if provided)
        self.publish_permit_definition(definition, creator_npub=creator_npub)
        
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
        
        # WoTx2 system: starts with 1 signature, then 2, 3, 4...
        # The system progresses as more attestations are collected
        # ORACLE.refresh.sh will check and issue 30503 when threshold is reached
        required_attestations = definition.min_attestations
        
        # Check if enough attestations
        if len(request.attestations) >= required_attestations:
            request.status = PermitStatus.VALIDATED
            print(f"✅ Permit request {request.request_id} validated with {len(request.attestations)} attestations (required: {required_attestations})")
            
            # Auto-issue credential (ORACLE.refresh.sh will also check and issue)
            self.issue_credential(request.request_id)
        else:
            request.status = PermitStatus.ATTESTING
            print(f"✅ Attestation added ({len(request.attestations)}/{required_attestations}) - WoTx2 progressing")
        
        self.save_data()
        
        # Publish to NOSTR
        self.publish_permit_attestation(attestation)
        
        return True
    
    def issue_credential(self, request_id: str) -> Optional[PermitCredential]:
        """Issue a Verifiable Credential for a validated permit request
        
        Reads request from local storage or from Nostr if not found locally.
        """
        # Try to get request from local storage first
        request = self.requests.get(request_id)
        
        # If not found locally, try to fetch from Nostr (WoTx2 system)
        if not request:
            print(f"📡 Request {request_id} not found locally, fetching from Nostr...")
            nostr_requests = self.fetch_permit_requests_from_nostr()
            for nr in nostr_requests:
                if nr.request_id == request_id:
                    request = nr
                    # Store in local cache for future use
                    self.requests[request_id] = request
                    print(f"✅ Request {request_id} loaded from Nostr")
                    break
        
        if not request:
            print(f"❌ Request {request_id} not found in local storage or Nostr")
            return None
        
        # For requests from Nostr, check attestations count
        if request_id not in self.requests:
            # Count attestations from Nostr
            attestations = self.fetch_nostr_events(kind=30502)
            request_attestations = [
                att for att in attestations
                if any(tag[0] == 'e' and tag[1] == request_id for tag in att.get('tags', []))
            ]
            request.attestations = [att.get('id', '') for att in request_attestations]
            print(f"📊 Found {len(request.attestations)} attestations for request {request_id} from Nostr")
        
        # Check if request has enough attestations
        definition = self.definitions.get(request.permit_definition_id)
        if not definition:
            print(f"❌ Permit definition {request.permit_definition_id} not found")
            return None
        
        required_attestations = definition.min_attestations
        if len(request.attestations) < required_attestations:
            print(f"❌ Request {request_id} has {len(request.attestations)} attestations, needs {required_attestations}")
            return None
        
        # Mark as validated if not already
        if request.status != PermitStatus.VALIDATED:
            request.status = PermitStatus.VALIDATED
        
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
        
        # Emit NIP-58 badge for this credential
        self.emit_badge_for_credential(credential, definition)
        
        # Update holder's DID document
        self.update_holder_did(credential)
        
        print(f"✅ Credential {credential_id} issued for {request.applicant_npub}")
        return credential
    
    def _is_primary_station(self) -> bool:
        """Check if this is the primary station (ORACLE des ORACLES)
        
        Returns True if IPFSNODEID matches the first STRAP in A_boostrap_nodes.txt
        """
        ipfs_node_id = os.getenv("IPFSNODEID", "")
        if not ipfs_node_id:
            return False
        
        # Check bootstrap nodes file
        strapfile = None
        if Path.home().joinpath(".zen/game/MY_boostrap_nodes.txt").exists():
            strapfile = Path.home() / ".zen/game/MY_boostrap_nodes.txt"
        elif Path.home().joinpath(".zen/Astroport.ONE/A_boostrap_nodes.txt").exists():
            strapfile = Path.home() / ".zen/Astroport.ONE/A_boostrap_nodes.txt"
        
        if strapfile and strapfile.exists():
            try:
                with open(strapfile, 'r') as f:
                    straps = []
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            # Extract IPFSNODEID from line (same logic as bash: rev | cut -d '/' -f 1 | rev)
                            strap_id = line.split('/')[-1].strip()
                            if strap_id:
                                straps.append(strap_id)
                    
                    if straps and straps[0] == ipfs_node_id:
                        return True
            except Exception as e:
                print(f"⚠️  Error reading bootstrap nodes file: {e}")
        
        return False
    
    def _get_oracle_keyfile(self) -> Optional[Path]:
        """Get the oracle keyfile (myswarm_secret.nostr or UPLANETNAME_G1 for primary)
        
        Returns:
            Path to keyfile, or None if not found
        """
        # Primary station uses UPLANETNAME_G1
        if self._is_primary_station():
            keyfile = Path.home() / ".zen/game/uplanet.G1.nostr"
            if keyfile.exists():
                print("⭐ Primary station - Using UPLANETNAME_G1 key")
                return keyfile
        
        # Other stations use myswarm_secret.nostr directly
        # nostr_send_note.py reads NSEC= from the file, so we can use it as-is
        myswarm_secret = Path.home() / ".zen/game/myswarm_secret.nostr"
        if myswarm_secret.exists():
            print(f"🔑 Oracle station - Using myswarm_secret.nostr")
            return myswarm_secret
        
        return None
    
    def _get_oracle_pubkey(self) -> str:
        """Get the oracle pubkey (HEX format) for DID issuer
        
        Returns:
            HEX pubkey string, or empty string if not found
        """
        # Primary station uses UPLANETNAME_G1
        if self._is_primary_station():
            g1_hex = os.getenv("UPLANETNAME_G1", "")
            if g1_hex:
                return g1_hex
        
        # Other stations use myswarm_secret.nostr
        oracle_keyfile = self._get_oracle_keyfile()
        if oracle_keyfile:
            pubkey = self._get_pubkey_from_keyfile(oracle_keyfile)
            if pubkey:
                return pubkey
        
        # Fallback: try to get from environment
        return os.getenv("UPLANETNAME_G1", "")
    
    def sign_credential(self, request: PermitRequest, definition: PermitDefinition) -> Dict[str, Any]:
        """Sign a credential with oracle authority key (myswarm_secret.nostr or UPLANETNAME_G1 for primary)"""
        # Get oracle key for signing (myswarm_secret.nostr or UPLANETNAME_G1)
        oracle_key = ""
        if self._is_primary_station():
            oracle_key = os.getenv("UPLANETNAME_G1", "")
        else:
            # Read HEX from myswarm_secret.nostr
            myswarm_secret = Path.home() / ".zen/game/myswarm_secret.nostr"
            if myswarm_secret.exists():
                try:
                    content = myswarm_secret.read_text()
                    for line in content.split('\n'):
                        if line.startswith('HEX='):
                            oracle_key = line.split('HEX=')[1].split(';')[0].strip()
                            break
                except Exception as e:
                    print(f"⚠️  Error reading HEX from myswarm_secret.nostr: {e}")
        
        # Create proof structure
        proof = {
            "@context": "https://w3id.org/security/v2",
            "type": "Ed25519Signature2020",
            "created": datetime.now().isoformat(),
            "verificationMethod": f"{definition.issuer_did}#oracle-authority",
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
        
        # Sign with oracle authority
        # CRITICAL: Use canonical JSON (RFC 8785) for signature consistency
        data_str = canonicalize_json(credential_data)
        signature = hashlib.sha256(f"{data_str}:{oracle_key}".encode()).hexdigest()
        
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
    
    def publish_permit_definition(self, definition: PermitDefinition, creator_npub: Optional[str] = None):
        """Publish permit definition to NOSTR
        
        Args:
            definition: The permit definition to publish
            creator_npub: Optional NOSTR pubkey of the creator (for saving event in their directory)
        """
        # Prepare NOSTR event (kind 30500)
        # CRITICAL: Content must be canonicalized JSON (RFC 8785) for signature consistency
        tags = [
            ["d", definition.id],
            ["t", "permit"],
            ["t", "definition"],
            ["t", definition.id]
        ]
        
        # Add IPFSNODEID tag to filter events by Astroport (prevents conflicts in multi-station constellations)
        ipfs_node_id = os.getenv("IPFSNODEID", "")
        if ipfs_node_id:
            tags.append(["ipfs_node", ipfs_node_id])
        
        event_data = {
            "kind": PermitEventKind.PERMIT_DEFINITION.value,
            "content": canonicalize_json(asdict(definition)),
            "tags": tags
        }
        
        # Event is signed by oracle key (myswarm_secret.nostr or UPLANETNAME_G1 for primary)
        self._publish_to_nostr(event_data, signer_npub=creator_npub, use_oracle_key=True)
    
    def publish_permit_request(self, request: PermitRequest):
        """Publish permit request to NOSTR"""
        # Prepare NOSTR event (kind 30501)
        # CRITICAL: Content must be canonicalized JSON (RFC 8785) for signature consistency
        tags = [
            ["d", request.request_id],
            ["l", request.permit_definition_id, "permit_type"],
            ["p", request.applicant_npub],
            ["t", "permit"],
            ["t", "request"]
        ]
        
        # Add IPFSNODEID tag to filter events by Astroport (prevents conflicts in multi-station constellations)
        ipfs_node_id = os.getenv("IPFSNODEID", "")
        if ipfs_node_id:
            tags.append(["ipfs_node", ipfs_node_id])
        
        event_data = {
            "kind": PermitEventKind.PERMIT_REQUEST.value,
            "content": canonicalize_json({
                "request_id": request.request_id,
                "permit_definition_id": request.permit_definition_id,
                "applicant_did": request.applicant_did,
                "statement": request.statement,
                "evidence": request.evidence,
                "status": request.status.value
            }),
            "tags": tags
        }
        
        self._publish_to_nostr(event_data, request.applicant_npub)
    
    def publish_permit_attestation(self, attestation: PermitAttestation):
        """Publish permit attestation to NOSTR"""
        # Prepare NOSTR event (kind 30502)
        # CRITICAL: Content must be canonicalized JSON (RFC 8785) for signature consistency
        # NIP-101/42: tag "p" MUST be the applicant (subject of attestation), not the attester (event pubkey)
        request = self.requests.get(attestation.request_id)
        applicant_npub = request.applicant_npub if request else attestation.attester_npub
        tags = [
            ["d", attestation.attestation_id],
            ["e", attestation.request_id],
            ["p", applicant_npub],
            ["t", "permit"],
            ["t", "attestation"]
        ]
        if request:
            tags.append(["permit", request.permit_definition_id])
        # Add IPFSNODEID tag to filter events by Astroport (prevents conflicts in multi-station constellations)
        ipfs_node_id = os.getenv("IPFSNODEID", "")
        if ipfs_node_id:
            tags.append(["ipfs_node", ipfs_node_id])
        
        event_data = {
            "kind": PermitEventKind.PERMIT_ATTESTATION.value,
            "content": canonicalize_json({
                "attestation_id": attestation.attestation_id,
                "request_id": attestation.request_id,
                "attester_did": attestation.attester_did,
                "statement": attestation.statement,
                "signature": attestation.signature
            }),
            "tags": tags
        }
        
        self._publish_to_nostr(event_data, attestation.attester_npub)
    
    def publish_permit_credential(self, credential: PermitCredential):
        """Publish permit credential to NOSTR"""
        # Prepare NOSTR event (kind 30503)
        # CRITICAL: Content must be canonicalized JSON (RFC 8785) for signature consistency
        # This is especially important for Verifiable Credentials (W3C VC standard)
        tags = [
            ["d", credential.credential_id],
            ["l", credential.permit_definition_id, "permit_type"],
            ["p", credential.holder_npub],
            ["t", "permit"],
            ["t", "credential"],
            ["t", "verifiable-credential"]
        ]
        
        # Add IPFSNODEID tag to filter events by Astroport (prevents conflicts in multi-station constellations)
        ipfs_node_id = os.getenv("IPFSNODEID", "")
        if ipfs_node_id:
            tags.append(["ipfs_node", ipfs_node_id])
        
        event_data = {
            "kind": PermitEventKind.PERMIT_CREDENTIAL.value,
            "content": canonicalize_json({
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
            "tags": tags
        }
        
        # Use oracle key for signing (myswarm_secret.nostr or UPLANETNAME_G1 for primary)
        self._publish_to_nostr(event_data, None, use_oracle_key=True)
    
    def _publish_to_nostr(self, event_data: Dict[str, Any], signer_npub: Optional[str] = None, use_oracle_key: bool = False):
        """Publish an event to NOSTR relays using nostr_send_note.py
        
        Args:
            event_data: The event data to publish
            signer_npub: Optional NOSTR pubkey of the signer (for saving event in their directory)
            use_oracle_key: If True, use oracle key (myswarm_secret.nostr or UPLANETNAME_G1 for primary)
        """
        import subprocess
        import tempfile
        
        # Determine where to save the event
        # If signer_npub is provided, save in their MULTIPASS directory
        if signer_npub:
            email = self.get_email_from_npub(signer_npub)
            if email:
                events_dir = Path.home() / ".zen" / "game" / "nostr" / email
                events_dir.mkdir(parents=True, exist_ok=True)
                print(f"📁 Saving event to MULTIPASS directory: {email}")
            else:
                # Fallback to default location if email not found
                events_dir = Path.home() / ".zen" / "tmp" / "nostr_events"
                events_dir.mkdir(exist_ok=True)
                print(f"⚠️  Could not find email for {signer_npub}, using default location")
        else:
            # Default location for events without signer
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
            if use_oracle_key:
                # Use oracle key (myswarm_secret.nostr or UPLANETNAME_G1 for primary)
                keyfile = self._get_oracle_keyfile()
                if not keyfile:
                    print("⚠️  Oracle keyfile not found (myswarm_secret.nostr or uplanet.G1.nostr)")
                    return
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
    
    def emit_badge_for_credential(self, credential: PermitCredential, definition: PermitDefinition):
        """Emit NIP-58 badge for a credential (kind 30503)
        
        This function:
        1. Creates badge definition (kind 30009) if it doesn't exist
        2. Emits badge award (kind 8) for the credential holder
        
        Args:
            credential: The PermitCredential that was just issued
            definition: The PermitDefinition for this credential
        """
        try:
            # Generate badge ID from permit ID
            badge_id = self._get_badge_id_from_permit(definition.id)
            
            # Get oracle pubkey for badge issuer
            oracle_keyfile = self._get_oracle_keyfile()
            if not oracle_keyfile:
                print("⚠️  Cannot emit badge: Oracle keyfile not found")
                return
            
            # Read oracle pubkey from keyfile
            oracle_pubkey = self._get_pubkey_from_keyfile(oracle_keyfile)
            if not oracle_pubkey:
                print("⚠️  Cannot emit badge: Could not read oracle pubkey")
                return
            
            # 1. Create badge definition (kind 30009) if it doesn't exist
            self._ensure_badge_definition(badge_id, definition, oracle_pubkey, oracle_keyfile)
            
            # 2. Emit badge award (kind 8)
            self._emit_badge_award(badge_id, credential, oracle_pubkey, oracle_keyfile)
            
            print(f"✅ Badge {badge_id} emitted for credential {credential.credential_id}")
            
        except Exception as e:
            print(f"⚠️  Error emitting badge: {e}")
            import traceback
            traceback.print_exc()
    
    def _get_badge_id_from_permit(self, permit_id: str) -> str:
        """Convert permit ID to badge ID
        
        Examples:
            PERMIT_ORE_V1 -> ore_verifier
            PERMIT_MAITRE_NAGEUR_X5 -> permit_maitre_nageur_x5
        """
        # Convert to lowercase and replace special chars
        badge_id = permit_id.lower().replace("PERMIT_", "").replace("_", "_")
        
        # For official permits, use friendly names
        official_badges = {
            "ore_v1": "ore_verifier",
            "driver": "driver_license",
            "medical_first_aid": "first_aid_provider",
            "building_artisan": "building_artisan",
            "wot_dragon": "wot_dragon"
        }
        
        # Check if it's an official permit
        for key, value in official_badges.items():
            if badge_id.startswith(key):
                return value
        
        # For WoTx2 permits, keep the full name with level
        return f"permit_{badge_id}"
    
    def _get_pubkey_from_keyfile(self, keyfile: Path) -> Optional[str]:
        """Extract pubkey (HEX) from keyfile"""
        try:
            content = keyfile.read_text()
            for line in content.split('\n'):
                if line.startswith('HEX='):
                    hex_key = line.split('HEX=')[1].split(';')[0].strip()
                    return hex_key
                elif line.startswith('NPUB='):
                    # If only NPUB is available, we'd need to decode it
                    # For now, try to find HEX
                    continue
        except Exception as e:
            print(f"⚠️  Error reading keyfile: {e}")
        return None
    
    def _ensure_badge_definition(self, badge_id: str, definition: PermitDefinition, oracle_pubkey: str, oracle_keyfile: Path):
        """Create badge definition (kind 30009) if it doesn't exist"""
        # Check if badge definition already exists (query Nostr)
        # For now, we'll create it anyway (replaceable event)
        
        # Generate badge name and description
        badge_name = definition.name
        level = definition.metadata.get("level", "")
        label = ""
        if level:
            label = self._get_level_label(level)
            badge_name = f"{definition.name} - Niveau {level} ({label})"
        
        badge_description = definition.description
        if definition.metadata.get("auto_proclaimed"):
            badge_description += " Auto-proclaimed mastery with progressive validation."
        
        # Generate badge images automatically using AI
        badge_image_url, badge_thumb_256, badge_thumb_64 = self._generate_badge_images(
            badge_id, definition.name, badge_description, level, label
        )
        
        # Prepare tags for badge definition
        tags = [
            ["d", badge_id],
            ["name", badge_name],
            ["description", badge_description],
            ["image", badge_image_url, "1024x1024"],
            ["thumb", badge_thumb_256, "256x256"],
            ["thumb", badge_thumb_64, "64x64"],
            ["permit_id", definition.id],
            ["t", "uplanet"],
            ["t", "oracle"],
            ["t", "badge"]
        ]
        
        # Add level info for WoTx2
        if definition.metadata.get("level"):
            tags.append(["level", definition.metadata.get("level")])
            tags.append(["label", self._get_level_label(definition.metadata.get("level"))])
            tags.append(["t", "wotx2"])
        
        # Publish badge definition
        event_data = {
            "kind": 30009,  # Badge Definition
            "content": "",
            "tags": tags
        }
        
        self._publish_to_nostr(event_data, None, use_oracle_key=True)
        print(f"📜 Badge definition {badge_id} created/updated")
    
    def _generate_badge_images(self, badge_id: str, permit_name: str, permit_description: str, 
                               level: str = "", label: str = "") -> tuple:
        """Generate badge images automatically using AI and ComfyUI
        
        Returns:
            tuple: (badge_image_url, badge_thumb_256, badge_thumb_64)
        """
        try:
            # Path to badge generation script
            script_path = Path.home() / ".zen" / "Astroport.ONE" / "IA" / "generate_badge_image.sh"
            
            if not script_path.exists():
                print(f"⚠️  Badge generation script not found: {script_path}")
                print("   Using fallback static URLs")
                return self._get_fallback_badge_urls(badge_id)
            
            # Prepare arguments for the script
            import subprocess
            import json
            
            # Escape arguments for shell
            import shlex
            args = [
                str(script_path),
                badge_id,
                permit_name,
                permit_description
            ]
            
            if level:
                args.append(level)
            if label:
                args.append(label)
            
            print(f"🎨 Generating badge images for: {badge_id}")
            print(f"   Calling: {script_path}")
            
            # Execute the script
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=300  # 5 minutes max
            )
            
            if result.returncode == 0:
                # Parse JSON response
                try:
                    badge_data = json.loads(result.stdout)
                    if badge_data.get("success") and badge_data.get("badge_image_url"):
                        print(f"✅ Badge images generated successfully")
                        return (
                            badge_data.get("badge_image_url", ""),
                            badge_data.get("badge_thumb_256", ""),
                            badge_data.get("badge_thumb_64", "")
                        )
                except json.JSONDecodeError:
                    print(f"⚠️  Failed to parse badge generation JSON")
                    print(f"   Output: {result.stdout[:200]}")
            
            print(f"⚠️  Badge generation failed (exit code: {result.returncode})")
            if result.stderr:
                print(f"   Error: {result.stderr[:200]}")
            
        except subprocess.TimeoutExpired:
            print(f"⚠️  Badge generation timed out after 5 minutes")
        except Exception as e:
            print(f"⚠️  Error generating badge images: {e}")
            import traceback
            traceback.print_exc()
        
        # Fallback to static URLs if generation fails
        return self._get_fallback_badge_urls(badge_id)
    
    def _get_fallback_badge_urls(self, badge_id: str) -> tuple[str, str, str]:
        """Get fallback badge URLs (static IPFS paths)"""
        myipfs = os.getenv("myIPFS", "https://ipfs.copylaradio.com")
        badge_image_url = f"{myipfs}/ipns/copylaradio.com/badges/{badge_id}.png"
        badge_thumb_256 = f"{myipfs}/ipns/copylaradio.com/badges/{badge_id}_256x256.png"
        badge_thumb_64 = f"{myipfs}/ipns/copylaradio.com/badges/{badge_id}_64x64.png"
        return (badge_image_url, badge_thumb_256, badge_thumb_64)
    
    def _get_level_label(self, level: str) -> str:
        """Get label for WoTx2 level"""
        try:
            level_num = int(level.replace("X", ""))
            if level_num <= 4:
                return "Débutant"
            elif level_num <= 10:
                return "Expert"
            elif level_num <= 50:
                return "Maître"
            elif level_num <= 100:
                return "Grand Maître"
            else:
                return "Maître Absolu"
        except:
            return "Niveau"
    
    def _emit_badge_award(self, badge_id: str, credential: PermitCredential, oracle_pubkey: str, oracle_keyfile: Path):
        """Emit badge award (kind 8) for credential holder"""
        # Get relay URL
        relay_url = NOSTR_RELAYS[0] if NOSTR_RELAYS else "wss://relay.copylaradio.com"
        
        # Prepare tags for badge award
        tags = [
            ["a", f"30009:{oracle_pubkey}:{badge_id}"],
            ["p", credential.holder_npub, relay_url],
            ["credential_id", credential.credential_id],
            ["permit_id", credential.permit_definition_id],
            ["t", "uplanet"],
            ["t", "oracle"]
        ]
        
        # Publish badge award
        event_data = {
            "kind": 8,  # Badge Award
            "content": f"Credential issued: {credential.permit_definition_id}",
            "tags": tags
        }
        
        self._publish_to_nostr(event_data, None, use_oracle_key=True)
        print(f"🏅 Badge award emitted for {credential.holder_npub}")
    
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
        """Fetch permit definitions (kind 30500) from NOSTR relay and MULTIPASS directories"""
        # First, fetch from NOSTR relay
        events = self.fetch_nostr_events(kind=30500)
        
        # Also scan MULTIPASS directories for local events
        nostr_dir = Path.home() / ".zen" / "game" / "nostr"
        if nostr_dir.exists():
            for email_dir in nostr_dir.iterdir():
                if not email_dir.is_dir():
                    continue
                
                # Look for 30500 events in this MULTIPASS directory
                for event_file in email_dir.glob("30500_*.json"):
                    try:
                        with open(event_file, 'r') as f:
                            event = json.load(f)
                            if event.get('kind') == 30500:
                                events.append(event)
                    except Exception as e:
                        print(f"⚠️  Error reading event from {event_file}: {e}")
        
        definitions = []
        seen_ids = set()  # Avoid duplicates
        
        for event in events:
            try:
                # Parse tags to extract permit information
                permit_id = None
                for tag in event.get('tags', []):
                    if tag[0] == 'd':
                        permit_id = tag[1]
                        break
                
                if permit_id and permit_id not in seen_ids:
                    seen_ids.add(permit_id)
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
                    elif tag[0] == 'l' and len(tag) > 1:
                        # Tag format: ['l', permit_id, 'permit_type']
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
            issuer_did=f"did:nostr:{oracle._get_oracle_pubkey()}",
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

