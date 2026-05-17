"""
QR code router — génération amzqr complète + interface web.

GET  /qr?html=1                          → Interface de configuration
GET  /qr?data=URL[&version=1][&level=H][&colorized=0]
         [&contrast=1.0][&brightness=1.0][&color=000000][&bgcolor=ffffff]
         [&picture_url=URL][&format=json|png]
POST /qr  (multipart : data, version, level, colorized, contrast,
           brightness, color, bgcolor, format, picture, picture_url)

Options amzqr :
  version     int  1-40       version QR (auto-incrémentée si overflow)
  level       str  L|M|Q|H   correction d'erreur (L=7% M=15% Q=25% H=30%)
  colorized   int  0|1        coloriser depuis l'image de fond
  contrast    float 0.1-3.0  contraste image de fond
  brightness  float 0.1-3.0  luminosité image de fond
  picture     file            image de fond (POST multipart)
  picture_url str             URL d'une image à télécharger (GET ou POST)
  color       str  RRGGBB    couleur modules (qrencode fallback)
  bgcolor     str  RRGGBB    couleur fond   (qrencode fallback)
"""

import base64
import os
import asyncio
import logging
import tempfile
import shutil
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx

from fastapi import APIRouter, Request, BackgroundTasks, Query
from fastapi.responses import Response, JSONResponse, HTMLResponse, RedirectResponse

from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

_TEMPLATE = Path(__file__).parent.parent / "templates" / "qr.html"


def _qr_html() -> str:
    try:
        return _TEMPLATE.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Template manquant : %s", _TEMPLATE)
        return "<h1>Template manquant : templates/qr.html</h1>"


# ── Mailjet ───────────────────────────────────────────────────────────────────

async def _notify_captain(data: str, visitor_ip: str) -> None:
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
    tmp_msg = tempfile.NamedTemporaryFile(suffix=".html", mode="w", delete=False, encoding="utf-8")
    try:
        tmp_msg.write(body)
        tmp_msg.close()
        proc = await asyncio.create_subprocess_exec(
            str(mailjet_sh), "--expire", "0s", captain, tmp_msg.name,
            f"🐷 MULTIPASS demandé — {settings.uSPOT}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
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

def _generate_qr_png(
    data: str,
    version: int = 1,
    level: str = "H",
    colorized: bool = False,
    contrast: float = 1.0,
    brightness: float = 1.0,
    picture_path: Optional[str] = None,
    color: str = "000000",
    bgcolor: str = "ffffff",
) -> tuple[bytes | None, str]:
    """Génère un PNG QR. Retourne (png_bytes | None, moteur_utilisé)."""
    level = level.upper() if level.upper() in ("L", "M", "Q", "H") else "H"
    version = max(1, min(40, int(version)))
    use_picture = picture_path and os.path.isfile(picture_path)

    logger.info(
        "QR gen — data=%.60r version=%s level=%s colorized=%s picture=%s",
        data, version, level, colorized, use_picture,
    )

    # ── amzqr — auto-monte la version si overflow ─────────────────
    try:
        import amzqr as _amzqr
        for v in range(version, 41):
            tmp = tempfile.mkdtemp()
            out = os.path.join(tmp, "qr.png")
            try:
                logger.debug("amzqr.run v%s picture=%s colorized=%s", v, use_picture, colorized and use_picture)
                _amzqr.run(
                    data,
                    version=v,
                    level=level,
                    picture=picture_path if use_picture else None,
                    colorized=bool(colorized and use_picture),
                    contrast=float(contrast),
                    brightness=float(brightness),
                    save_name="qr.png",
                    save_dir=tmp,
                    verbose=False,
                )
                if os.path.isfile(out):
                    result = Path(out).read_bytes()
                    shutil.rmtree(tmp, ignore_errors=True)
                    logger.info("amzqr OK v%s → %d bytes", v, len(result))
                    return result, "amzqr"
                logger.debug("amzqr v%s: fichier absent après run", v)
            except Exception as e:
                shutil.rmtree(tmp, ignore_errors=True)
                msg = str(e).lower()
                if "overflow" in msg or "too long" in msg or "capacity" in msg:
                    logger.debug("amzqr v%s overflow → essai v%s", v, v + 1)
                    continue
                logger.warning("amzqr v%s erreur inattendue: %s", v, e)
                break
            shutil.rmtree(tmp, ignore_errors=True)
            break
    except ImportError:
        logger.warning("amzqr non installé — fallback qrencode (pip install amzqr)")

    # ── qrencode fallback ─────────────────────────────────────────
    try:
        import subprocess
        tmp2 = tempfile.mktemp(suffix=".png")
        cmd = [
            "qrencode", "-s", "8", "-t", "PNG",
            "-l", level,
            "--foreground", color.upper().lstrip("#"),
            "--background", bgcolor.upper().lstrip("#"),
            "-o", tmp2, "--", data,
        ]
        logger.debug("qrencode: %s", " ".join(cmd[:6]))
        subprocess.run(cmd, check=True, capture_output=True)
        png = Path(tmp2).read_bytes()
        os.unlink(tmp2)
        logger.info("qrencode OK → %d bytes (image de fond ignorée)", len(png))
        if use_picture:
            logger.warning("L'image de fond a été ignorée : amzqr requis pour les QR artistiques")
        return png, "qrencode"
    except Exception as e:
        logger.error("qrencode échec: %s", e)

    return None, "none"


async def _download_picture_url(url: str) -> Optional[str]:
    """Télécharge une image depuis une URL, renvoie le chemin du fichier temporaire."""
    logger.info("Téléchargement image: %s", url[:80])
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "image/png")
            ext = ".jpg" if "jpeg" in ct else ".gif" if "gif" in ct else ".png"
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            tmp.write(r.content)
            tmp.close()
            logger.info("Image téléchargée → %s (%d bytes)", tmp.name, len(r.content))
            return tmp.name
    except Exception as e:
        logger.warning("Échec téléchargement image %s : %s", url[:60], e)
        return None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/qr")
@router.post("/qr")
async def generate_qr(
    request:     Request,
    background_tasks: BackgroundTasks,
    data:        Optional[str]   = Query(None),
    color:       Optional[str]   = Query(None),
    bgcolor:     Optional[str]   = Query(None),
    format:      Optional[str]   = Query(None),
    html:        Optional[int]   = Query(None),
    version:     Optional[int]   = Query(None),
    level:       Optional[str]   = Query(None),
    colorized:   Optional[int]   = Query(None),
    contrast:    Optional[float] = Query(None),
    brightness:  Optional[float] = Query(None),
    picture_url: Optional[str]   = Query(None),
):
    if html:
        return HTMLResponse(_qr_html())

    picture_path: Optional[str] = None

    if request.method == "POST":
        form = await request.form()

        def _f(k: str, default: str = "") -> str:
            return str(form.get(k) or default)

        data        = data        or _f("data")
        color       = color       or _f("color",       "000000")
        bgcolor     = bgcolor     or _f("bgcolor",      "ffffff")
        format      = format      or _f("format",       "png")
        picture_url = picture_url or (_f("picture_url") or None)
        version     = version     if version   is not None else int(_f("version",    "1") or "1")
        level       = level                            or _f("level",      "H")
        colorized   = colorized   if colorized is not None else int(_f("colorized", "0") or "0")
        contrast    = contrast    if contrast  is not None else float(_f("contrast",  "1.0") or "1.0")
        brightness  = brightness  if brightness is not None else float(_f("brightness","1.0") or "1.0")

        pic = form.get("picture")
        if pic and hasattr(pic, "read"):
            pic_bytes = await pic.read()
            if pic_bytes:
                suffix = Path(getattr(pic, "filename", "pic.png")).suffix or ".png"
                tmp_pic = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                tmp_pic.write(pic_bytes)
                tmp_pic.close()
                picture_path = tmp_pic.name
                logger.info("Fichier uploadé: %s (%d bytes) → %s", getattr(pic, "filename", "?"), len(pic_bytes), picture_path)

    # Télécharger picture_url si fournie et aucun fichier uploadé
    if picture_url and not picture_path:
        picture_path = await _download_picture_url(picture_url)

    # Valeurs par défaut
    data       = (data or "").strip()
    color      = (color   or "000000").lstrip("#")
    bgcolor    = (bgcolor  or "ffffff").lstrip("#")
    format     = format     or "png"
    version    = max(1, min(40, int(version or 1)))
    level      = (level    or "H").upper()
    if level not in ("L", "M", "Q", "H"):
        level = "H"
    colorized  = bool(int(colorized  or 0))
    contrast   = float(contrast   or 1.0)
    brightness = float(brightness or 1.0)

    if not data:
        return JSONResponse({"error": "missing data parameter"}, status_code=400)

    if data in ("/", ""):
        data = str(settings.uSPOT).rstrip("/") + "/g1nostr"

    visitor_ip = request.client.host if request.client else "?"
    background_tasks.add_task(_notify_captain, data, visitor_ip)

    try:
        png, engine = await asyncio.to_thread(
            _generate_qr_png,
            data, version, level, colorized,
            contrast, brightness, picture_path, color, bgcolor,
        )
    finally:
        if picture_path:
            try:
                os.unlink(picture_path)
            except OSError:
                pass

    logger.info("Réponse: format=%s engine=%s png=%s", format, engine, bool(png))

    if format == "json":
        if png:
            return JSONResponse({
                "dataUrl": "data:image/png;base64," + base64.b64encode(png).decode(),
                "data":    data,
                "engine":  engine,
            })
        fallback = (
            f"https://api.qrserver.com/v1/create-qr-code/"
            f"?size=180x180&data={urllib.parse.quote_plus(data)}&color={color}"
        )
        return JSONResponse({"fallback": fallback, "data": data, "engine": "fallback"})

    if png:
        return Response(content=png, media_type="image/png")

    fallback = (
        f"https://api.qrserver.com/v1/create-qr-code/"
        f"?size=180x180&data={urllib.parse.quote_plus(data)}&color={color}"
    )
    return RedirectResponse(url=fallback)
