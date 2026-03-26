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

    # ── Duniter v2 — Indexeur Squid GraphQL ──────────────────────────────────
    # Nœud primaire (surchargeable via SQUID_URL= dans .env)
    # Bootstraps issus de duniter_getnode.sh (hardcodés comme toujours valides)
    SQUID_URL: str = "https://squid.g1.gyroi.de/v1/graphql"
    # Nœuds Squid de secours — triés par disponibilité historique
    SQUID_FALLBACKS: List[str] = [
        "https://squid.g1.gyroi.de/v1/graphql",
        "https://g1-squid.axiom-team.fr/v1/graphql",
        "https://squid.g1.coinduf.eu/v1/graphql",
        "https://indexer.duniter.org/v1/graphql",
        "https://g1-squid.cgeek.fr/v1/graphql",
    ]

    # ── Duniter v2 — Nœuds RPC WebSocket (gcli / fallback balance) ───────────
    # Nœud primaire (surchargeable via G1_WS_NODE= dans .env)
    G1_WS_NODE: str = "wss://g1.1000i100.fr/ws"
    # Nœuds RPC de secours — bootstraps duniter_getnode.sh
    G1_RPC_FALLBACKS: List[str] = [
        "wss://g1.1000i100.fr/ws",
        "wss://g1-v2s.cgeek.fr",
        "wss://g1.coinduf.eu",
        "wss://g1.gyroi.de",
        "wss://g1.p2p.legal/ws",
        "wss://rpc.duniter.org",
        "wss://g1.axiom-team.fr:443/ws/",
    ]

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
