import os

def add_upload_from_drive():
    with open("routers/media.py", "a") as f:
        f.write("""
from models.schemas import UploadFromDriveRequest, UploadFromDriveResponse

@router.post("/api/upload_from_drive", response_model=UploadFromDriveResponse)
async def upload_from_drive(request: UploadFromDriveRequest):
    if request.owner_hex_pubkey or request.owner_email:
        logging.info(f"Sync from drive - Source owner: {request.owner_email} (hex: {request.owner_hex_pubkey[:12] if request.owner_hex_pubkey else 'N/A'}...)")
    
    request.npub = await require_nostr_auth(request.npub)

    try:
        user_NOSTR_path = get_authenticated_user_directory(request.npub)
        user_drive_path = user_NOSTR_path / "APP" / "uDRIVE"

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error determining user directory: {e}")

    parts = request.ipfs_link.split('/')
    extracted_filename = parts[-1] if parts else "downloaded_file"

    sanitized_filename = sanitize_filename_python(extracted_filename)

    file_type = detect_file_type(b'', sanitized_filename)

    target_directory_name = "Documents"
    if file_type == "image":
        target_directory_name = "Images"
    elif file_type == "audio":
        target_directory_name = "Music"
    elif file_type == "video":
        target_directory_name = "Videos"

    target_directory = user_drive_path / target_directory_name
    target_directory.mkdir(parents=True, exist_ok=True)

    target_file_path = (target_directory / sanitized_filename).resolve()

    if not target_file_path.is_relative_to(user_drive_path):
        raise HTTPException(status_code=400, detail="Invalid file path operation: attempted to write outside user's directory.")

    try:
        full_ipfs_url = f"/ipfs/{request.ipfs_link}"
        logging.info(f"Attempting to download IPFS link: {full_ipfs_url} to {target_file_path}")

        import asyncio
        ipfs_get_command = ["ipfs", "get", "-o", str(target_file_path), full_ipfs_url]
        process = await asyncio.create_subprocess_exec(
            *ipfs_get_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_message = stderr.decode().strip()
            logging.error(f"IPFS download failed for {full_ipfs_url}: {error_message}")
            raise Exception(f"IPFS download failed: {error_message}")

        file_size = target_file_path.stat().st_size
        logging.info(f"File '{sanitized_filename}' downloaded from IPFS and saved to '{target_file_path}' (Size: {file_size} bytes)")

        from services.ipfs import run_uDRIVE_generation_script
        ipfs_result = await run_uDRIVE_generation_script(user_drive_path)
        new_cid_info = ipfs_result.get("final_cid")
        logging.info(f"New IPFS CID generated: {new_cid_info}")

        return UploadFromDriveResponse(
            success=True,
            message="File synchronized successfully from IPFS",
            file_path=str(target_file_path.relative_to(user_drive_path)),
            file_type=file_type,
            new_cid=new_cid_info,
            timestamp=datetime.now().isoformat(),
            auth_verified=True
        )
    except Exception as e:
        logging.error(f"Error downloading from IPFS or saving file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to synchronize file: {e}")
""")

add_upload_from_drive()
