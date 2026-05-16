"""
QR code router — génération amzqr + notification Mailjet capitaine.

GET  /qr?data=URL[&color=RRGGBB][&format=json|png]
POST /qr  (form: data, color, format)

Retourne l'image PNG directement (format=png, défaut) ou un JSON
{dataUrl, data} (format=json) pour les appels fetch() depuis le portail.
"""

import io
import os
import json
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Request, BackgroundTasks, Query
from fastapi.responses import Response, JSONResponse

from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Mailjet ───────────────────────────────────────────────────────────────────

def _load_mj_keys() -> dict:
    """Lit ~/.zen/MJ_APIKEY (export KEY=value) puis les variables d'env."""
    keys = {
        "MJ_APIKEY_PUBLIC":  os.environ.get("MJ_APIKEY_PUBLIC", ""),
        "MJ_APIKEY_PRIVATE": os.environ.get("MJ_APIKEY_PRIVATE", ""),
        "SENDER_EMAIL":      os.environ.get("SENDER_EMAIL", "noreply@qo-op.com"),
    }
    mj_file = Path.home() / ".zen" / "MJ_APIKEY"
    try:
        for line in mj_file.read_text().splitlines():
            line = line.strip().removeprefix("export ")
            if "=" in line:
                k, _, v = line.partition("=")
                keys[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return keys


async def _notify_captain(data: str, visitor_ip: str) -> None:
    """Envoie un mail via Astroport.ONE/tools/mailjet.sh (tâche de fond)."""
    captain = settings.CAPTAINEMAIL
    mailjet_sh = settings.TOOLS_PATH / "mailjet.sh"
    if not (captain and mailjet_sh.exists()):
        return

    body = (
        "<h2>🎟️ Nouveau MULTIPASS demandé</h2>"
        f"<p><b>URL :</b> {data}</p>"
        f"<p><b>Station :</b> {settings.uSPOT}</p>"
        f"<p><b>IP visiteur :</b> {visitor_ip}</p>"
    )
    tmp_msg = tempfile.NamedTemporaryFile(
        suffix=".html", mode="w", delete=False, encoding="utf-8"
    )
    try:
        tmp_msg.write(body)
        tmp_msg.close()
        proc = await asyncio.create_subprocess_exec(
            str(mailjet_sh),
            "--expire", "0s",
            captain,
            tmp_msg.name,
            f"🐷 MULTIPASS demandé — {settings.uSPOT}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
    except Exception as exc:
        logger.warning("Mailjet notification failed: %s", exc)
    finally:
        try:
            os.unlink(tmp_msg.name)
        except OSError:
            pass


# ── Génération QR ─────────────────────────────────────────────────────────────

def _generate_qr_png(data: str) -> bytes | None:
    """Génère un PNG QR via amzqr (priorité) puis qrencode."""
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "qr.png")
    try:
        import amzqr  # pip install amzqr
        amzqr.run(
            data,
            version=5,
            level="H",
            picture=None,
            colorized=False,
            contrast=1.0,
            brightness=1.0,
            save_name="qr.png",
            save_dir=tmp,
            verbose=False,
        )
        if os.path.isfile(out):
            return Path(out).read_bytes()
    except Exception:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Fallback : qrencode
    try:
        import subprocess
        tmp2 = tempfile.mktemp(suffix=".png")
        subprocess.run(
            ["qrencode", "-s", "6", "-t", "PNG", "-o", tmp2, "--", data],
            check=True, capture_output=True,
        )
        png = Path(tmp2).read_bytes()
        os.unlink(tmp2)
        return png
    except Exception:
        pass

    return None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/qr")
@router.post("/qr")
async def generate_qr(
    request: Request,
    background_tasks: BackgroundTasks,
    data:   Optional[str] = Query(None),
    color:  Optional[str] = Query("ff3399"),
    format: Optional[str] = Query("png"),
):
    # Lire data depuis form si POST
    if request.method == "POST":
        form = await request.form()
        data   = data   or form.get("data", "")
        color  = color  or form.get("color", "ff3399")
        format = format or form.get("format", "png")

    data = (data or "").strip()
    if not data:
        return JSONResponse({"error": "missing data parameter"}, status_code=400)

    # Construire l'URL UPassport MULTIPASS si data est juste "/"
    if data in ("/", "https://qo-op.com", "qo-op.com", ""):
        data = str(settings.uSPOT).rstrip("/") + "/g1nostr"

    visitor_ip = request.client.host if request.client else "?"
    background_tasks.add_task(_notify_captain, data, visitor_ip)

    png = await asyncio.to_thread(_generate_qr_png, data)

    if format == "json":
        if png:
            import base64
            return JSONResponse({"dataUrl": "data:image/png;base64," + base64.b64encode(png).decode(), "data": data})
        else:
            import urllib.parse
            fallback = f"https://api.qrserver.com/v1/create-qr-code/?size=180x180&data={urllib.parse.quote_plus(data)}&color={color}"
            return JSONResponse({"fallback": fallback, "data": data})

    if png:
        return Response(content=png, media_type="image/png")

    # Ultime fallback : redirect vers qrserver
    import urllib.parse
    from fastapi.responses import RedirectResponse
    fallback = f"https://api.qrserver.com/v1/create-qr-code/?size=180x180&data={urllib.parse.quote_plus(data)}&color={color}"
    return RedirectResponse(url=fallback)
