import os
import json
import logging
import asyncio
import aiofiles
from datetime import datetime
from typing import Dict, Any
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from core.config import settings

templates = Jinja2Templates(directory="templates")

async def execute_bash_json_script(script_name: str, args: list = None, timeout: int = 60) -> Dict[str, Any]:
    """Exécute un script bash de manière asynchrone et parse sa sortie JSON. Ne bloque pas FastAPI."""
    args = args or []
    script_path = os.path.expanduser(f"~/.zen/Astroport.ONE/tools/{script_name}")
    
    if not os.path.exists(script_path):
        raise HTTPException(status_code=500, detail=f"Script introuvable: {script_name}")

    try:
        process = await asyncio.create_subprocess_exec(
            script_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        if process.returncode != 0:
            logging.error(f"Script {script_name} a échoué: {stderr.decode()}")
            raise ValueError(f"Erreur d'exécution: {stderr.decode()}")

        output_str = stdout.decode().strip()
        if not output_str:
            return {}

        parsed_data = json.loads(output_str)
        if "error" in parsed_data:
            raise ValueError(parsed_data["error"])
            
        return parsed_data

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Timeout lors de l'exécution de {script_name}")
    except json.JSONDecodeError as e:
        logging.error(f"Erreur JSON depuis {script_name}. Sortie: {output_str[:200]}")
        raise HTTPException(status_code=500, detail="Sortie du script invalide (Non-JSON)")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

def render_page(request: Request, template_name: str, context: dict = None):
    """Raccourci pour rendre un template avec les variables de base (myIPFS)."""
    base_context = {
        "request": request,
        "myIPFS": settings.IPFS_GATEWAY
    }
    if context:
        base_context.update(context)
    return templates.TemplateResponse(template_name, base_context)

async def run_script(script_path, *args, log_file_path=os.path.expanduser("~/.zen/tmp/54321.log")):
    """
    Fonction générique pour exécuter des scripts shell avec gestion des logs
    """
    logging.info(f"Running script: {script_path} with args: {args}")

    os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
    
    if not os.path.exists(log_file_path):
        try:
            with open(log_file_path, 'w') as f:
                f.write(f"Log file created at {datetime.now().isoformat()}\n")
            logging.info(f"Created log file: {log_file_path}")
        except Exception as e:
            logging.error(f"Failed to create log file {log_file_path}: {e}")

    process = await asyncio.create_subprocess_exec(
        script_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    last_line = ""
    try:
        async with aiofiles.open(log_file_path, "a") as log_file:
            async for line in process.stdout:
                line = line.decode().strip()
                last_line = line
                await log_file.write(line + "\n")
                logging.info(f"Script output: {line}")
    except Exception as e:
        logging.error(f"Error writing to log file {log_file_path}: {e}")
        async for line in process.stdout:
            line = line.decode().strip()
            last_line = line
            logging.info(f"Script output (no log file): {line}")

    return_code = await process.wait()
    logging.info(f"Script finished with return code: {return_code}")

    return return_code, last_line

import subprocess

def check_balance(g1pub: str):
    """Vérifier le solde d'une g1pub donnée"""
    try:
        result = subprocess.run(
            [os.path.expanduser("~/.zen/Astroport.ONE/tools/G1check.sh"), g1pub],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            raise ValueError("Erreur dans G1check.sh: " + result.stderr)
        balance_line = result.stdout.strip().splitlines()[-1]
        return balance_line
    except subprocess.TimeoutExpired:
        logging.error(f"Timeout lors de la vérification du solde pour {g1pub}")
        raise ValueError("Timeout lors de la vérification du solde")
    except Exception as e:
        logging.error(f"Erreur lors de la vérification du solde: {e}")
        raise ValueError(f"Erreur: {e}")
