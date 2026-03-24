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

async def run_script(script_path, *args, log_file_path=None):
    if log_file_path is None:
        from core.config import settings
        log_file_path = settings.ZEN_PATH / "tmp" / "api.log"
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

def is_origin_mode():
    swarm_key_path = os.path.expanduser("~/.ipfs/swarm.key")
    uplanet_name = ""
    if os.path.exists(swarm_key_path):
        with open(swarm_key_path, 'r') as f:
            lines = f.readlines()
            if lines:
                uplanet_name = lines[-1].strip()
    return not uplanet_name or uplanet_name == "0000000000000000000000000000000000000000000000000000000000000000"

def get_oc_env():
    env_vars = {}
    from core.config import settings
    env_path = settings.ZEN_PATH / "workspace" / "OC2UPlanet" / ".env"
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip().strip('"').strip("'")
    return env_vars

def get_oc_tier_urls():
    oc_env = get_oc_env()
    return {
        "satellite": oc_env.get("OC_URL_SATELLITE", ""),
        "constellation": oc_env.get("OC_URL_CONSTELLATION", ""),
        "cloud": oc_env.get("OC_URL_CLOUD", ""),
        "membre": oc_env.get("OC_URL_MEMBRE", ""),
    }

async def get_env_from_mysh(var_name: str, default: str = "") -> str:
    """Récupérer une variable d'environnement depuis my.sh de façon fiable (asynchrone)"""
    try:
        from core.config import settings
        my_sh_path = settings.TOOLS_PATH / "my.sh"
        if not os.path.exists(my_sh_path):
            return default
        
        import asyncio
        # Utiliser les arguments positionnels pour éviter toute injection shell :
        # $1 = chemin vers my.sh, $2 = nom de la variable (jamais interprété comme code)
        # ${!2} est l'expansion indirecte bash : lit la variable dont le nom est dans $2
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", 'source "$1" && echo "${!2}"', "--",
            str(my_sh_path), var_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        
        if process.returncode == 0 and stdout.decode().strip():
            return stdout.decode().strip()
        return default
    except Exception:
        return default


async def send_server_side_analytics(analytics_data: dict, request) -> None:
    """Send analytics data server-side (for clients without JavaScript)"""
    import logging
    import asyncio
    from datetime import datetime, timezone
    from core.middleware import get_client_ip
    try:
        analytics_data.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        analytics_data.setdefault("source", "server")
        analytics_data.setdefault("current_url", str(request.url))
        analytics_data.setdefault("referer", request.headers.get("referer", ""))
        analytics_data.setdefault("user_agent", request.headers.get("user-agent", ""))
        
        client_ip = get_client_ip(request)
        if client_ip:
            analytics_data["client_ip"] = client_ip
        
        import httpx
        base_url = str(request.base_url).rstrip('/')
        ping_url = f"{base_url}/ping"
        
        async def send_ping():
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.post(ping_url, json=analytics_data)
            except Exception as e:
                logging.debug(f"Analytics ping failed (non-blocking): {e}")
        
        asyncio.create_task(send_ping())
    except Exception as e:
        logging.debug(f"Server-side analytics error (non-blocking): {e}")

def safe_json_load(text: str) -> dict:
    """
    Nettoie une chaîne de caractères (cherche le premier { et le dernier })
    avant de parser le JSON. Utile pour les scripts bash qui peuvent
    renvoyer des erreurs texte avant le JSON.
    """
    import json
    try:
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
            json_str = text[start_idx:end_idx+1]
            return json.loads(json_str)
        # Fallback to standard parsing if no braces found (e.g. for arrays)
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find array brackets if object braces failed
        try:
            start_idx = text.find('[')
            end_idx = text.rfind(']')
            if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
                json_str = text[start_idx:end_idx+1]
                return json.loads(json_str)
        except json.JSONDecodeError:
            pass
        raise ValueError(f"Impossible de parser le JSON depuis la chaîne: {text[:100]}...")

async def get_myipfs_gateway() -> str:
    """Récupérer l'adresse de la gateway IPFS en utilisant my.sh"""
    return await get_env_from_mysh("myIPFS", "http://localhost:8080")

from fastapi import Form
import inspect

def as_form(cls):
    """
    Adds an as_form class method to Pydantic models to allow them to be used
    with FastAPI's Form dependency.
    """
    new_parameters = []
    
    for field_name, model_field in cls.model_fields.items():
        new_parameters.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                default=Form(...) if model_field.is_required() else Form(model_field.default),
                annotation=model_field.annotation,
            )
        )
        
    async def as_form_func(**data):
        return cls(**data)
        
    sig = inspect.signature(as_form_func)
    sig = sig.replace(parameters=new_parameters)
    as_form_func.__signature__ = sig
    setattr(cls, 'as_form', as_form_func)
    return cls

async def get_uplanet_home_url():
    from core.config import settings
    upassport_url = settings.UPASSPORT_URL
    if not upassport_url:
        upassport_url = await get_env_from_mysh("UPASSPORT_URL", "")
    if upassport_url:
        from urllib.parse import urlparse
        parsed = urlparse(upassport_url)
        host = parsed.hostname or ""
        if host.startswith("u."):
            domain = host[2:]
            return f"https://ipfs.{domain}/ipns/{domain}"
    return ""

async def check_balance(g1pub: str):
    """Vérifier le solde d'une g1pub donnée"""
    try:
        import asyncio
        process = await asyncio.create_subprocess_exec(
            str(settings.TOOLS_PATH / "G1check.sh"), g1pub,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        if process.returncode != 0:
            raise ValueError("Erreur dans G1check.sh: " + stderr.decode())
        balance_line = stdout.decode().strip().splitlines()[-1]
        return balance_line
    except asyncio.TimeoutError:
        logging.error(f"Timeout lors de la vérification du solde pour {g1pub}")
        raise ValueError("Timeout lors de la vérification du solde")
    except Exception as e:
        logging.error(f"Erreur lors de la vérification du solde: {e}")
        raise ValueError(f"Erreur: {e}")
