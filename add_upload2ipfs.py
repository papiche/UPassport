import os

def add_upload2ipfs():
    with open("routers/media.py", "a") as f:
        f.write("""
@router.post("/upload2ipfs")
async def upload_to_ipfs(request: Request, file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    original_filename = file.filename or "unknown"
    file_location = f"tmp/{original_filename}"
    
    user_pubkey_hex = ""
    user_npub = None
    try:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Nostr "):
            auth_base64 = auth_header.replace("Nostr ", "").strip()
            auth_json = base64.b64decode(auth_base64).decode('utf-8')
            auth_event = json.loads(auth_json)
            
            if auth_event.get("kind") == 27235 and "pubkey" in auth_event:
                user_pubkey_hex = auth_event["pubkey"]
                user_npub = hex_to_npub(user_pubkey_hex) if user_pubkey_hex else None
                logging.info(f"🔑 NIP-98 Auth: Provenance tracking enabled for user: {user_pubkey_hex[:16]}...")
            else:
                logging.warning(f"⚠️ Invalid NIP-98 event: kind={auth_event.get('kind')}")
        else:
            logging.info(f"ℹ️ No NIP-98 Authorization header, uploading without provenance tracking")
    except Exception as e:
        logging.warning(f"⚠️ Could not extract pubkey from NIP-98 Authorization header: {e}")
    
    if user_npub:
        max_size_bytes = get_max_file_size_for_user(user_npub)
    else:
        max_size_bytes = 104857600
    
    if file.size and file.size > max_size_bytes:
        max_size_mb = max_size_bytes // 1048576
        file_size_mb = file.size // 1048576
        raise HTTPException(
            status_code=413,
            detail={
                "status": "error",
                "message": f"File size ({file_size_mb}MB) exceeds maximum allowed size ({max_size_mb}MB per UPlanet_FILE_CONTRACT.md)"
            }
        )
    
    try:
        async with aiofiles.open(file_location, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        temp_file_path = f"tmp/temp_{uuid.uuid4()}.json"

        script_path = "./upload2ipfs.sh"
        
        return_code, last_line = await run_script(script_path, file_location, temp_file_path, user_pubkey_hex)

        if return_code == 0:
          try:
                async with aiofiles.open(temp_file_path, mode="r") as temp_file:
                    json_content = await temp_file.read()
                json_output = json.loads(json_content.strip())

                new_cid = json_output.get("new_cid") or json_output.get("cid", "")
                file_hash = json_output.get("fileHash") or json_output.get("file_hash") or json_output.get("x", "")
                mime_type = json_output.get("mimeType") or json_output.get("file_type") or json_output.get("m", "")
                file_name = json_output.get("fileName") or original_filename or ""
                file_size = json_output.get("file_size") or json_output.get("size", "")
                dimensions = json_output.get("dimensions") or json_output.get("dim", "")
                info_cid = json_output.get("info") or ""
                thumbnail_ipfs = json_output.get("thumbnail_ipfs") or ""
                gifanim_ipfs = json_output.get("gifanim_ipfs") or ""
                upload_chain = json_output.get("upload_chain") or ""
                
                ipfs_gateway = await get_myipfs_gateway().rstrip('/')
                if new_cid and file_name:
                    ipfs_url = f"/ipfs/{new_cid}/{file_name}"
                elif new_cid:
                    ipfs_url = f"/ipfs/{new_cid}"
                else:
                    ipfs_url = ""
                
                tags = []
                
                if ipfs_url:
                    tags.append(["url", ipfs_url])
                
                if file_hash:
                    tags.append(["ox", file_hash])
                    tags.append(["x", file_hash])
                
                if mime_type:
                    tags.append(["m", mime_type])
                
                if file_size:
                    tags.append(["size", str(file_size)])
                
                if dimensions:
                    tags.append(["dim", dimensions])
                
                if info_cid:
                    tags.append(["info", info_cid])
                
                if thumbnail_ipfs:
                    tags.append(["thumbnail_ipfs", thumbnail_ipfs])
                
                if gifanim_ipfs:
                    tags.append(["gifanim_ipfs", gifanim_ipfs])
                
                if upload_chain:
                    tags.append(["upload_chain", upload_chain])
                
                nip96_response = {
                    "status": "success",
                    "message": json_output.get("message", "File uploaded successfully"),
                    "nip94_event": {
                        "tags": tags,
                        "content": ""
                    }
                }
                
                if new_cid:
                    nip96_response["new_cid"] = new_cid
                if file_hash:
                    nip96_response["file_hash"] = file_hash
                if mime_type:
                    nip96_response["file_type"] = mime_type
                if info_cid:
                    nip96_response["info"] = info_cid
                if thumbnail_ipfs:
                    nip96_response["thumbnail_ipfs"] = thumbnail_ipfs
                if gifanim_ipfs:
                    nip96_response["gifanim_ipfs"] = gifanim_ipfs
                if dimensions:
                    nip96_response["dimensions"] = dimensions
                if json_output.get("duration"):
                    nip96_response["duration"] = json_output.get("duration")
                
                os.remove(temp_file_path)
                os.remove(file_location)
                return JSONResponse(content=nip96_response)
          except (json.JSONDecodeError, FileNotFoundError) as e:
                logging.error(f"Failed to decode JSON from temp file: {temp_file_path}, Error: {e}")
                raise HTTPException(status_code=500, detail="Failed to process script output, JSON decode error.")
          finally:
                if os.path.exists(temp_file_path):
                   os.remove(temp_file_path)
                if os.path.exists(file_location):
                  os.remove(file_location)
        else:
           logging.error(f"Script execution failed: {last_line.strip()}")
           raise HTTPException(status_code=500, detail="Script execution failed.")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
""")

add_upload2ipfs()
