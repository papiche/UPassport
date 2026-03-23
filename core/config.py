import os
import base64
from pathlib import Path
from typing import Set, List
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Server Configuration
    HOST: str = "127.0.0.1"
    PORT: int = 54321
    
    # Paths
    BASE_DIR: Path = Path(__file__).parent.parent
    ZEN_PATH: Path = Path.home() / ".zen"
    GAME_PATH: Path = Path.home() / ".zen" / "game"
    TOOLS_PATH: Path = Path.home() / ".zen" / "Astroport.ONE" / "tools"
    
    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = 60
    RATE_LIMIT_WINDOW: int = 60
    RATE_LIMIT_CLEANUP_INTERVAL: int = 300
    
    TRUSTED_IPS: Set[str] = {
        "127.0.0.1",
        "::1",
        "192.168.1.1",
    }
    TRUSTED_IP_RANGES: List[str] = [
        "10.99.99.0/24",
    ]
    
    # Cache TTLs
    NOSTR_CACHE_TTL: int = 300
    NOSTR_PROFILE_CACHE_TTL: int = 3600
    
    # External Services
    IPFS_GATEWAY: str = "http://127.0.0.1:8080"
    
    # Secrets
    COINFLIP_SECRET: str = base64.urlsafe_b64encode(os.urandom(32)).decode()
    
    # UPlanet specific
    UPLANETNAME_G1: str = ""
    CAPTAINEMAIL: str = ""
    IPFSNODEID: str = ""
    USE_LOCAL_JS: bool = False
    UPASSPORT_URL: str = ""
    OCAPIKEY: str = ""
    uSPOT: str = "http://127.0.0.1:54321"
    myRELAY: str = "ws://127.0.0.1:7777"
    NOSTR_RELAYS: str = "ws://127.0.0.1:7777 wss://relay.copylaradio.com"
    myIPFS: str = "https://ipfs.copylaradio.com"
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
