import os
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any
from fastapi import Request, HTTPException
from starlette.responses import StreamingResponse
import httpx

from core.config import settings

async def proxy_ipfs_gateway(request: Request):
    """Proxy /ipfs/ and /ipns/ requests to the local IPFS gateway."""
    gw_path = request.url.path  # e.g. /ipfs/Qm... or /ipns/domain/file
    gw_url = f"{settings.IPFS_GATEWAY}{gw_path}"
    if request.url.query:
        gw_url += f"?{request.url.query}"

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            gw_resp = await client.request(
                method=request.method,
                url=gw_url,
                headers={
                    k: v for k, v in request.headers.items()
                    if k.lower() not in ('host', 'connection')
                },
            )

        # Stream response back with original headers
        excluded = {'transfer-encoding', 'content-encoding', 'connection'}
        headers = {
            k: v for k, v in gw_resp.headers.items()
            if k.lower() not in excluded
        }
        return StreamingResponse(
            content=iter([gw_resp.content]),
            status_code=gw_resp.status_code,
            headers=headers,
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="IPFS gateway unavailable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="IPFS gateway timeout")

async def run_uDRIVE_generation_script(source_dir: Path, enable_logging: bool = False) -> Dict[str, Any]:
    """Exécuter le script de génération IPFS spécifique à l'utilisateur dans le répertoire de son uDRIVE."""
    
    app_udrive_path = source_dir 
    script_path = app_udrive_path / "generate_ipfs_structure.sh"
    
    app_udrive_path.mkdir(parents=True, exist_ok=True)
    
    if not script_path.exists() or not script_path.is_symlink():
        generic_script_path = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "generate_ipfs_structure.sh"
        
        if generic_script_path.exists():
            if script_path.exists():
                script_path.unlink()
                logging.warning(f"Fichier existant non symlinké ou cassé supprimé: {script_path}")

            script_path.symlink_to(generic_script_path)
            logging.info(f"Lien symbolique créé vers {script_path}")
        else:
            fallback_script_path = settings.BASE_DIR / "generate_ipfs_structure.sh"
            if fallback_script_path.exists():
                if script_path.exists():
                    script_path.unlink()
                    logging.warning(f"Fichier existant non symlinké ou cassé supprimé: {script_path} (fallback)")
                script_path.symlink_to(fallback_script_path)
                logging.info(f"Lien symbolique créé (fallback) de {fallback_script_path} vers {script_path}")
            else:
                raise HTTPException(
                    status_code=500, 
                    detail=f"Script generate_ipfs_structure.sh non trouvé dans {generic_script_path} ni dans {fallback_script_path}"
                )
    else:
        logging.info(f"Utilisation du script utilisateur existant (lien symbolique): {script_path}")
    
    if not os.access(script_path.resolve(), os.X_OK):
        try:
            os.chmod(script_path.resolve(), 0o755)
            logging.info(f"Rendu exécutable le script cible: {script_path.resolve()}")
        except Exception as e:
            logging.error(f"Impossible de rendre exécutable le script cible {script_path.resolve()}: {e}")
            raise HTTPException(status_code=500, detail=f"Script IPFS non exécutable: {e}")

    cmd = [str(script_path)]
    if enable_logging:
        cmd.append("--log")
    
    cmd.append(".") 
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=app_udrive_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return_code = process.returncode

        if return_code == 0:
            final_cid = stdout.decode().strip().split('\n')[-1] if stdout.strip() else None
            
            logging.info(f"Script IPFS exécuté avec succès depuis {app_udrive_path}")
            logging.info(f"Nouveau CID généré: {final_cid}")
            logging.info(f"Répertoire traité: {source_dir}")
            
            return {
                "success": True,
                "final_cid": final_cid,
                "stdout": stdout.decode() if enable_logging else None,
                "stderr": stderr.decode() if stderr.strip() else None,
                "script_used": str(script_path),
                "working_directory": str(app_udrive_path),
                "processed_directory": str(source_dir)
            }
        else:
            logging.error(f"Script failed with return code {return_code}")
            logging.error(f"Stderr: {stderr.decode()}")
            raise HTTPException(
                status_code=500,
                detail=f"Erreur lors de l'exécution du script: {stderr.decode()}"
            )
            
    except Exception as e:
        logging.error(f"Exception lors de l'exécution du script: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

async def fetch_info_json(cid: str) -> Dict[str, Any]:
    """Fetch info.json from IPFS"""
    try:
        clean_cid = cid.replace("/ipfs/", "").replace("ipfs://", "")
        info_url = f"{settings.IPFS_GATEWAY}/ipfs/{clean_cid}/info.json"
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            info_response = await client.get(info_url)
            if info_response.status_code == 200:
                return info_response.json()
    except Exception as e:
        logging.warning(f"⚠️ Could not fetch info.json from IPFS: {e}")
    return {}
