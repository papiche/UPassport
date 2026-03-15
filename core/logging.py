import logging
import sys
from logging.handlers import RotatingFileHandler
from core.config import settings

import os

def setup_logging():
    log_file = settings.ZEN_PATH / "tmp" / "api.log"
    os.makedirs(settings.ZEN_PATH / "tmp", exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5
    )
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            file_handler
        ]
    )
    
    # Set specific log levels for noisy libraries if needed
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
