from pydantic import BaseModel, EmailStr
from typing import Optional

class MessageData(BaseModel):
    ulat: str
    ulon: str
    pubkey: str
    uid: str
    relation: str
    pubkeyUpassport: str
    email: str
    message: str

class UploadResponse(BaseModel):
    success: bool
    message: str
    file_path: str
    file_type: str
    target_directory: str
    new_cid: Optional[str] = None
    timestamp: str
    auth_verified: Optional[bool] = False
    fileName: Optional[str] = None
    description: Optional[str] = None
    info: Optional[str] = None
    thumbnail_ipfs: Optional[str] = None
    gifanim_ipfs: Optional[str] = None
    fileHash: Optional[str] = None
    mimeType: Optional[str] = None
    duration: Optional[int] = None
    dimensions: Optional[str] = None
    upload_chain: Optional[str] = None

class CoinflipStartRequest(BaseModel):
    token: str

class CoinflipStartResponse(BaseModel):
    ok: bool
    sid: str
    exp: int

class CoinflipFlipRequest(BaseModel):
    token: str

class CoinflipFlipResponse(BaseModel):
    ok: bool
    sid: str
    result: str
    consecutive: int

class CoinflipPayoutRequest(BaseModel):
    token: str
    player_id: Optional[str] = None

class CoinflipPayoutResponse(BaseModel):
    ok: bool
    sid: str
    zen: int
    g1_amount: str
    tx: Optional[str] = None

class UploadFromDriveRequest(BaseModel):
    ipfs_link: str
    npub: str
    owner_hex_pubkey: Optional[str] = None
    owner_email: Optional[str] = None

class UploadFromDriveResponse(BaseModel):
    success: bool
    message: str
    file_path: str
    file_type: str
    new_cid: Optional[str] = None
    timestamp: str
    auth_verified: bool

from typing import List, Dict

class N2NetworkNode(BaseModel):
    pubkey: str
    level: int  # 0 = center, 1 = N1, 2 = N2
    is_follower: bool = False  # True si cette clé suit la clé centrale
    is_followed: bool = False  # True si la clé centrale suit cette clé
    mutual: bool = False  # True si c'est un suivi mutuel
    connections: List[str] = []  # Liste des pubkeys auxquels ce nœud est connecté
    # Profile information for vocals messaging
    npub: Optional[str] = None  # Bech32 encoded public key (npub1...)
    email: Optional[str] = None  # User email from profile
    display_name: Optional[str] = None  # Display name from profile
    name: Optional[str] = None  # Name from profile
    picture: Optional[str] = None  # Profile picture URL
    about: Optional[str] = None  # About/bio from profile

class N2NetworkResponse(BaseModel):
    center_pubkey: str
    total_n1: int
    total_n2: int
    total_nodes: int
    range_mode: str  # "default" ou "full"
    nodes: List[N2NetworkNode]
    connections: List[Dict[str, str]]  # Liste des connexions {from: pubkey, to: pubkey}
    timestamp: str
    processing_time_ms: int
