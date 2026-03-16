import logging
import importlib.util
from typing import Optional, Any
from cachetools import TTLCache
from fastapi import FastAPI
from contextlib import asynccontextmanager

from core.config import settings

# NOTE : OracleSystem est importé en mode lazy (à l'intérieur du lifespan)
# pour éviter les dépendances circulaires :
#   core.state → oracle_system → core.config → core.state
#
# ORACLE_ENABLED est détecté via find_spec() : vérifie la présence du module
# SANS l'importer, donc pas de risque de dépendance circulaire.
ORACLE_ENABLED: bool = importlib.util.find_spec("oracle_system") is not None

class AppState:
    def __init__(self):
        # Caches
        self.nostr_auth_cache = TTLCache(maxsize=1000, ttl=settings.NOSTR_CACHE_TTL)
        self.nostr_profile_cache = TTLCache(maxsize=1000, ttl=settings.NOSTR_PROFILE_CACHE_TTL)
        
        # Hex to Email cache (built once)
        self.hex_to_email_cache = {}
        self.hex_to_directory_cache = TTLCache(maxsize=1000, ttl=3600)
        self.hex_cache_built = False
        
        # Oracle System — typage générique pour éviter l'import circulaire
        self.oracle_system: Optional[Any] = None

app_state = AppState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logging.info("Starting up application...")
    
    # Initialize required directories
    import os
    from pathlib import Path
    
    directories_to_create = [
        "tmp",
        "uploads",
        str(settings.ZEN_PATH / "game" / "permits"),
        str(settings.ZEN_PATH / "tmp" / "swarm"),
        str(settings.ZEN_PATH / "game" / "nostr")
    ]
    
    for directory in directories_to_create:
        os.makedirs(directory, exist_ok=True)
        logging.info(f"Ensured directory exists: {directory}")
    
    # Build hex index cache
    logging.info("🔍 Building hex -> email index cache for MULTIPASS detection...")
    nostr_base_path = settings.GAME_PATH / "nostr"
    
    if nostr_base_path.exists():
        count = 0
        for email_dir in nostr_base_path.iterdir():
            if email_dir.is_dir() and '@' in email_dir.name:
                hex_file_path = email_dir / "HEX"
                if hex_file_path.exists():
                    try:
                        with open(hex_file_path, 'r') as f:
                            stored_hex = f.read().strip().lower()
                        if stored_hex:
                            app_state.hex_to_email_cache[stored_hex] = email_dir.name
                            count += 1
                    except Exception as e:
                        logging.warning(f"⚠️  Error reading {hex_file_path}: {e}")
                        continue
        app_state.hex_cache_built = True
        logging.info(f"✅ Hex index cache built: {count} users indexed")
    else:
        logging.debug(f"ℹ️  NOSTR directory not found: {nostr_base_path}")
        app_state.hex_cache_built = True

    # Import lazy de OracleSystem pour éviter la dépendance circulaire
    # oracle_system.py peut importer core.config ; on diffère l'import au démarrage
    try:
        from oracle_system import OracleSystem  # noqa: PLC0415
        oracle_instance = OracleSystem()
        app_state.oracle_system = oracle_instance
        app.state.oracle = app_state.oracle_system

        # Load permit definitions from NOSTR if definitions are empty
        if len(oracle_instance.definitions) == 0:
            try:
                definitions = oracle_instance.fetch_permit_definitions_from_nostr()
                for definition in definitions:
                    oracle_instance.definitions[definition.id] = definition

                if definitions:
                    oracle_instance.save_data()
                    logging.info(f"✅ Loaded {len(definitions)} permit definitions from NOSTR")
                else:
                    logging.info("ℹ️  No permit definitions found in NOSTR (will load on demand)")
            except Exception as e:
                logging.warning(f"⚠️  Could not load permit definitions from NOSTR: {e}")

    except ImportError as e:
        logging.warning(f"⚠️  Oracle system not available (import skipped): {e}")
        app.state.oracle = None
    except Exception as e:
        logging.error(f"❌ Failed to initialise OracleSystem: {e}")
        app.state.oracle = None
    
    yield
    
    # Shutdown
    logging.info("Shutting down application...")
    # Clean up resources if needed
