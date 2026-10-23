import asyncio
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

        # Cache Ustats.sh — réponse GET / servie depuis RAM, rafraîchie toutes les 60s
        self.ustats_cache: Optional[Any] = None
        self.ustats_cache_time: float = 0.0
        self.USTATS_CACHE_TTL: int = 60

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
    
    # Cache Ustats.sh : relecture du fichier JSON toutes les 60 s, SANS fork.
    # La génération est `services/ustats.py` (remplace Ustats.sh), lancée dans un
    # process court détaché : un fork depuis ce processus (1,7 Go, ~40 threads) s'est interbloqué le
    # 2026-10-23 et a figé l'API 5 h. Il n'est lancé que si le
    # fichier manque ou est périmé, au plus une fois / 10 min.
    async def _refresh_ustats_loop():
        import json as _json
        import subprocess as _sp
        import time as _time
        import sys
        from pathlib import Path
        project_dir = Path(__file__).resolve().parent.parent
        cache_path = settings.ZEN_PATH / "tmp" / "Ustats.json"
        stale_after = 3600
        respawn_every = 600
        last_spawn = 0.0

        def _read():
            with open(cache_path, "r") as f:
                return _json.load(f), cache_path.stat().st_mtime

        def _spawn():
            _sp.Popen([sys.executable, "-m", "services.ustats"], cwd=str(project_dir),
                      stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                      start_new_session=True, close_fds=True)

        consecutive_failures = 0
        while True:
            try:
                data, mtime = await asyncio.to_thread(_read)
                app_state.ustats_cache = data
                app_state.ustats_cache_time = _time.time()
                consecutive_failures = 0
                if _time.time() - mtime > stale_after and _time.time() - last_spawn > respawn_every:
                    last_spawn = _time.time()
                    await asyncio.to_thread(_spawn)
            except Exception as e:
                consecutive_failures += 1
                logging.warning(f"⚠️  Lecture cache Ustats échouée : {e} (échec {consecutive_failures}/3)")
                if consecutive_failures >= 3:
                    app_state.ustats_cache = None
                if _time.time() - last_spawn > respawn_every:
                    last_spawn = _time.time()
                    try:
                        await asyncio.to_thread(_spawn)
                    except Exception as se:
                        logging.error(f"❌ Lancement Ustats.sh impossible : {se}")
            await asyncio.sleep(app_state.USTATS_CACHE_TTL)

    asyncio.create_task(_refresh_ustats_loop())

    yield

    # Shutdown
    logging.info("Shutting down application...")
    # Clean up resources if needed
