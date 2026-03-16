# ==================== SCRIPT DÉSACTIVÉ ====================
# Ce script était utilisé pour injecter du code dans routers/media.py via append.
# PROBLÈME : auto-modification du code = source d'erreurs d'indentation et de duplications.
# SOLUTION : Les fonctions ont été intégrées définitivement dans routers/media_upload.py.
# Ce fichier est conservé uniquement à titre de référence historique.
# NE PAS EXÉCUTER CE SCRIPT.
# ===========================================================

import os

def add_upload_image():
    """
    ATTENTION : Fonction désactivée.
    Les fonctions d'upload d'image sont définitivement intégrées dans routers/media_upload.py.
    Ne plus appeler cette fonction pour modifier des fichiers sources à la volée.
    """
    raise RuntimeError(
        "add_upload_image() est désactivéé. Les fonctions sont dans routers/media_upload.py. "
        "Ne pas utiliser de scripts append pour modifier le code source."
    )

def _reference_only_add_upload_image():
    """Référence historique - NE PAS APPELER"""
    with open("routers/media.py", "a") as f:
        f.write("""
# ==================== IMAGE UPLOAD (TrocZen/ZENBOX compatible) ====================

IMAGE_MAGIC_BYTES = {
    'png': [b'\\x89PNG\\r\\n\\x1a\\n'],
    'jpg': [b'\\xFF\\xD8\\xFF'],
    'jpeg': [b'\\xFF\\xD8\\xFF'],
    'webp': [b'RIFF'],
    'gif': [b'GIF87a', b'GIF89a'],
}

IMAGE_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

IMAGE_MIME_TYPES = {
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'webp': 'image/webp',
    'gif': 'image/gif',
}

MAX_IMAGE_SIZE = 5 * 1024 * 1024

def _validate_image_magic_bytes(file_content: bytes, extension: str) -> bool:
    ext = extension.lower()
    if ext not in IMAGE_MAGIC_BYTES:
        return False
    if ext == 'webp':
        return len(file_content) >= 12 and file_content[:4] == b'RIFF' and file_content[8:12] == b'WEBP'
    for sig in IMAGE_MAGIC_BYTES[ext]:
        if file_content.startswith(sig):
            return True
    return False

def _validate_image_file(filename: str, file_content: bytes) -> tuple:
    if '.' not in filename:
        return False, "File has no extension"
    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in IMAGE_ALLOWED_EXTENSIONS:
        return False, f"Extension '{ext}' not allowed. Allowed: {', '.join(IMAGE_ALLOWED_EXTENSIONS)}"
    if len(file_content) < 12:
        return False, "File too small for validation"
    if not _validate_image_magic_bytes(file_content[:12], ext):
        return False, f"Magic bytes don't match extension '{ext}'. Potentially malicious file."
    return True, None

async def _upload_image_to_ipfs(filepath: str) -> tuple:
    try:
        import aiohttp
        ipfs_api_url = "http://127.0.0.1:5001"
        with open(filepath, 'rb') as f:
            file_content = f.read()
        data = aiohttp.FormData()
        data.add_field('file', file_content,
                       filename=os.path.basename(filepath),
                       content_type='application/octet-stream')
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f'{ipfs_api_url}/api/v0/add', data=data) as response:
                if response.status == 200:
                    result = await response.json()
                    cid = result['Hash']
                    ipfs_gateway = (await get_myipfs_gateway()).rstrip('/')
                    ipfs_url = f"{ipfs_gateway}/ipfs/{cid}"
                    return cid, ipfs_url
        return None, None
    except Exception as e:
        logging.error(f"IPFS image upload error: {e}")
        return None, None

@router.post("/api/upload/image")
async def upload_image(
    file: UploadFile = File(...),
    npub: str = Form(...),
    type: str = Form(default="avatar"),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    image_type = type.lower()
    if image_type not in ('avatar', 'banner', 'logo'):
        raise HTTPException(status_code=400,
                            detail="Invalid image type (must be avatar, banner, or logo)")

    if not npub:
        raise HTTPException(status_code=400, detail="Missing npub")

    file_header = await file.read(12)
    await file.seek(0)

    is_valid, error_msg = _validate_image_file(file.filename, file_header)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=413,
                            detail=f"File size ({len(content) // 1024}KB) exceeds maximum (5MB)")

    original_name = sanitize_filename_python(file.filename)
    ext = original_name.rsplit('.', 1)[1].lower()
    timestamp = int(datetime.now().timestamp())
    npub_short = npub[:16] if len(npub) > 16 else npub
    new_filename = f"{npub_short}_{image_type}_{timestamp}.{ext}"

    uploads_dir = Path("uploads")
    uploads_dir.mkdir(exist_ok=True)
    filepath = uploads_dir / new_filename

    async with aiofiles.open(str(filepath), 'wb') as f:
        await f.write(content)

    file_hash = hashlib.sha256(content).hexdigest()

    local_url = f"/uploads/{new_filename}"

    cid, ipfs_url = await _upload_image_to_ipfs(str(filepath))

    return JSONResponse(content={
        'success': True,
        'url': ipfs_url or local_url,
        'local_url': local_url,
        'ipfs_url': ipfs_url,
        'ipfs_cid': cid,
        'ipfs_status': 'completed' if cid else 'failed',
        'filename': new_filename,
        'checksum': file_hash,
        'size': len(content),
        'uploaded_at': datetime.now().isoformat(),
        'storage': 'ipfs' if cid else 'local',
        'type': image_type,
        'mime_type': IMAGE_MIME_TYPES.get(ext, 'application/octet-stream'),
    }, status_code=201)

@router.get("/uploads/{filename}")
async def serve_upload(filename: str):
    filepath = Path("uploads") / sanitize_filename_python(filename)
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    from fastapi.responses import FileResponse
    return FileResponse(str(filepath))
""")

# add_upload_image()  # DÉSACTIVÉ - ne plus appeler ce script automatiquement
