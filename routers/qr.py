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

GET  /qr/postcard?html=1                 → Générateur de carte postale (config + preview)
GET  /qr/postcard?data=URL[&title=][&image_url=][&back_title=][&message=][&footer=]
         [&level=H][&version=1]
POST /qr/postcard  (multipart, mêmes champs)
    → Page imprimable 10x15cm (recto QR+image, verso message) — réutilisable
      pour n'importe quel projet, pas seulement UPlanet.
  data        str  requis     URL/texte encodé dans le QR (recto)
  title       str             Titre recto
  image_url   str             Illustration recto (optionnelle)
  back_title  str             Titre verso
  message     str             Corps du message verso (les doubles retours à la
                               ligne \\n\\n séparent les paragraphes)
  footer      str             Signature / ligne de pied verso
"""

import base64
import html as html_lib
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
_TEMPLATE_POSTCARD = Path(__file__).parent.parent / "templates" / "qr_postcard.html"


def _qr_html() -> str:
    try:
        return _TEMPLATE.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Template manquant : %s", _TEMPLATE)
        return "<h1>Template manquant : templates/qr.html</h1>"


def _postcard_config_html() -> str:
    try:
        return _TEMPLATE_POSTCARD.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Template manquant : %s", _TEMPLATE_POSTCARD)
        return "<h1>Template manquant : templates/qr_postcard.html</h1>"


_POSTCARD_PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__FRONT_TITLE__ — Carte Postale</title>
<style>
  :root{--ink:#222;--gold:#c8a83c;--green:#2d5a1b;--paper:#f5f0e8;--brown:#8b4513}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#ccc;font-family:Georgia,serif;color:var(--ink);
       display:flex;flex-direction:column;align-items:center;gap:22px;padding:22px 0}
  .bar{display:flex;gap:10px}
  .bar button{padding:9px 18px;border:none;border-radius:6px;background:var(--gold);
              color:#1a1209;font-weight:700;cursor:pointer;font-size:.9rem}
  .bar button:hover{box-shadow:0 0 10px var(--gold)}
  .card{width:150mm;height:100mm;background:var(--paper);
        box-shadow:0 0 16px rgba(0,0,0,.35);position:relative;overflow:hidden}

  /* ---- RECTO ---- */
  #recto{display:flex;flex-direction:column;align-items:center;justify-content:center;
         text-align:center;padding:8mm;gap:3mm}
  #recto .front-title{font-size:15pt;color:var(--green);text-transform:uppercase;
                       letter-spacing:1.5px}
  #recto .front-img{max-width:70mm;max-height:45mm;object-fit:contain}
  #recto .front-placeholder{font-size:40pt;line-height:1}
  #recto .qr-row{display:flex;align-items:center;gap:6mm;margin-top:2mm}
  #recto .qr-row img{width:24mm;height:24mm;image-rendering:pixelated}
  #recto .qr-cap{font-family:monospace;font-size:8pt;color:#555;max-width:60mm;
                 word-break:break-all;text-align:left}

  /* ---- VERSO ---- */
  #verso{display:flex}
  #verso .msg{flex:1 1 60%;padding:9mm;display:flex;flex-direction:column;gap:2.5mm;
              overflow:hidden}
  #verso .msg h2{font-size:11pt;color:var(--brown);letter-spacing:.5px;
                 border-bottom:1px solid var(--gold);padding-bottom:2mm;margin-bottom:1mm}
  #verso .msg p{font-size:8.5pt;line-height:1.4}
  #verso .msg .footer{margin-top:auto;font-size:8pt;color:var(--green);font-weight:700}
  #verso .side{flex:0 0 40%;border-left:1px dashed var(--brown);padding:9mm 7mm;
               display:flex;flex-direction:column;align-items:center;gap:4mm}
  #verso .stamp{width:22mm;height:22mm;border:1px dashed #999;color:#999;
                font-family:monospace;font-size:6.5pt;display:flex;align-items:center;
                justify-content:center;text-align:center;align-self:flex-end}
  #verso .side img{width:26mm;height:26mm;image-rendering:pixelated}
  #verso .side .cap{font-family:monospace;font-size:7pt;color:#555;text-align:center;
                    word-break:break-all}

  @media print{
    .no-print{display:none!important}
    html,body{background:#fff!important;width:100%!important;height:100%!important;
              margin:0!important;padding:0!important}
    .card{box-shadow:none!important}
    body.p-recto #verso{display:none!important}
    body.p-verso #recto{display:none!important}
  }
</style>
</head>
<body>

<div class="bar no-print">
  <button onclick="printSide('recto')">🖨️ Imprimer le Recto</button>
  <button onclick="printSide('verso')">🖨️ Imprimer le Verso</button>
</div>

<main class="card" id="recto">
  <div class="front-title">__FRONT_TITLE__</div>
  __IMAGE_BLOCK__
  <div class="qr-row">
    <img src="__QR_DATA_URL__" alt="QR">
    <div class="qr-cap">__QR_TARGET__</div>
  </div>
</main>

<div class="card" id="verso">
  <div class="msg">
    __BACK_TITLE_BLOCK__
    __MESSAGE_PARAGRAPHS__
    __FOOTER_BLOCK__
  </div>
  <div class="side">
    <div class="stamp">TIMBRE</div>
    <img src="__QR_DATA_URL__" alt="QR">
    <div class="cap">__QR_TARGET__</div>
  </div>
</div>

<script>
  var pageStyle = document.createElement('style');
  document.head.appendChild(pageStyle);
  function printSide(side){
    pageStyle.innerHTML = '@page { size: 150mm 100mm; margin: 0; }';
    document.body.classList.remove('p-recto','p-verso');
    document.body.classList.add(side === 'recto' ? 'p-recto' : 'p-verso');
    setTimeout(function(){ window.print(); }, 100);
  }
</script>
</body>
</html>
"""


def _render_postcard_html(
    data: str,
    qr_data_url: str,
    front_title: str,
    front_image_url: Optional[str],
    back_title: str,
    message: str,
    footer: str,
) -> str:
    """Compose la page HTML imprimable (recto QR+image / verso message)."""
    esc = html_lib.escape

    image_block = (
        f'<img class="front-img" src="{esc(front_image_url)}" alt="">'
        if front_image_url else
        '<div class="front-placeholder">🔲</div>'
    )
    back_title_block = f"<h2>{esc(back_title)}</h2>" if back_title else ""
    paragraphs = "".join(
        f"<p>{esc(p.strip())}</p>" for p in message.split("\n\n") if p.strip()
    )
    footer_block = f'<div class="footer">{esc(footer)}</div>' if footer else ""

    return (
        _POSTCARD_PAGE
        .replace("__FRONT_TITLE__", esc(front_title))
        .replace("__IMAGE_BLOCK__", image_block)
        .replace("__QR_DATA_URL__", qr_data_url)
        .replace("__QR_TARGET__", esc(data))
        .replace("__BACK_TITLE_BLOCK__", back_title_block)
        .replace("__MESSAGE_PARAGRAPHS__", paragraphs)
        .replace("__FOOTER_BLOCK__", footer_block)
    )


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
    import subprocess as _sp
    _astro_amzqr = os.path.expanduser("~/.astro/bin/amzqr")
    _amzqr_bin = shutil.which("amzqr") or (_astro_amzqr if os.path.isfile(_astro_amzqr) else None)
    if _amzqr_bin:
        for v in range(version, 41):
            tmp = tempfile.mkdtemp()
            out = os.path.join(tmp, "qr.png")
            try:
                cmd = [_amzqr_bin, data, "-v", str(v), "-l", level, "-n", "qr.png", "-d", tmp]
                if use_picture:
                    cmd += ["-p", picture_path]
                    if colorized:
                        cmd += ["-c"]
                if contrast != 1.0:
                    cmd += ["-con", str(contrast)]
                if brightness != 1.0:
                    cmd += ["-bri", str(brightness)]
                logger.debug("amzqr v%s picture=%s colorized=%s", v, use_picture, colorized and use_picture)
                result_proc = _sp.run(cmd, capture_output=True, text=True)
                if os.path.isfile(out):
                    result = Path(out).read_bytes()
                    shutil.rmtree(tmp, ignore_errors=True)
                    logger.info("amzqr OK v%s → %d bytes", v, len(result))
                    return result, "amzqr"
                stderr = result_proc.stderr.lower()
                if "overflow" in stderr or "too long" in stderr or "capacity" in stderr:
                    logger.debug("amzqr v%s overflow → essai v%s", v, v + 1)
                    shutil.rmtree(tmp, ignore_errors=True)
                    continue
                logger.warning("amzqr v%s échec: %s", v, result_proc.stderr.strip())
                shutil.rmtree(tmp, ignore_errors=True)
                break
            except Exception as e:
                shutil.rmtree(tmp, ignore_errors=True)
                logger.warning("amzqr v%s erreur inattendue: %s", v, e)
                break
    else:
        logger.warning("amzqr non trouvé dans PATH — fallback qrencode")

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


@router.get("/qr/postcard")
@router.post("/qr/postcard")
async def generate_postcard(
    request:    Request,
    data:       Optional[str] = Query(None),
    title:      Optional[str] = Query(None),
    image_url:  Optional[str] = Query(None),
    back_title: Optional[str] = Query(None),
    message:    Optional[str] = Query(None),
    footer:     Optional[str] = Query(None),
    level:      Optional[str] = Query(None),
    version:    Optional[int] = Query(None),
    html:       Optional[int] = Query(None),
):
    """Carte postale imprimable 10x15cm — recto QR+image, verso message.

    Générique : réutilisable pour n'importe quel projet (`data` est la seule
    valeur obligatoire), pas seulement pour un manuel UPlanet donné.
    """
    if html:
        return HTMLResponse(_postcard_config_html())

    if request.method == "POST":
        form = await request.form()

        def _f(k: str, default: str = "") -> str:
            return str(form.get(k) or default)

        data       = data       or _f("data")
        title      = title      or _f("title")
        image_url  = image_url  or (_f("image_url") or None)
        back_title = back_title or _f("back_title")
        message    = message    or _f("message")
        footer     = footer     or _f("footer")
        level      = level      or _f("level", "H")
        version    = version    if version is not None else int(_f("version", "1") or "1")

    data = (data or "").strip()
    if not data:
        return JSONResponse({"error": "missing data parameter"}, status_code=400)

    title      = (title or "Carte Postale").strip()
    image_url  = (image_url or "").strip() or None
    back_title = (back_title or "").strip()
    message    = (message or "").strip()
    footer     = (footer or "").strip()
    level      = (level or "H").upper()
    if level not in ("L", "M", "Q", "H"):
        level = "H"
    version = max(1, min(40, int(version or 1)))

    png, engine = await asyncio.to_thread(_generate_qr_png, data, version, level)
    qr_data_url = ("data:image/png;base64," + base64.b64encode(png).decode()) if png else ""
    logger.info("Postcard — data=%.60r engine=%s png=%s", data, engine, bool(png))

    page = _render_postcard_html(
        data=data,
        qr_data_url=qr_data_url,
        front_title=title,
        front_image_url=image_url,
        back_title=back_title,
        message=message,
        footer=footer,
    )
    return HTMLResponse(page)
