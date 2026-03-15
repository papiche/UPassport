import os

with open('routers/media.py', 'r') as f:
    lines = f.readlines()

# Find the start of WebcamForm
split_idx = 0
for i, line in enumerate(lines):
    if line.startswith('class WebcamForm(BaseModel):') or line.startswith('@as_form\nclass WebcamForm(BaseModel):'):
        split_idx = i
        if lines[i-1].startswith('@as_form'):
            split_idx = i - 1
        break

library_lines = lines[:split_idx]
upload_lines = lines[split_idx:]

# Add imports to upload_lines
imports = """import os
import json
import time
import uuid
import base64
import hashlib
import logging
import asyncio
import traceback
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

import aiofiles
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from utils.helpers import run_script, get_myipfs_gateway, as_form
from core.middleware import get_client_ip
from utils.security import (
    get_authenticated_user_directory,
    get_max_file_size_for_user,
    validate_uploaded_file,
    sanitize_filename_python,
    detect_file_type,
    find_user_directory_by_hex,
    is_safe_email
)
from services.nostr import verify_nostr_auth, require_nostr_auth
from utils.crypto import npub_to_hex, hex_to_npub
from services.nostr import fetch_video_event_from_nostr, parse_video_metadata
from models.schemas import UploadResponse, UploadFromDriveResponse

router = APIRouter()
templates = Jinja2Templates(directory="templates")

"""

with open('routers/media_library.py', 'w') as f:
    f.writelines(library_lines)

with open('routers/media_upload.py', 'w') as f:
    f.write(imports)
    f.writelines(upload_lines)

