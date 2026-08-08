import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

from core.config import settings

router = APIRouter()

logger = logging.getLogger(__name__)

# Alternative numérique aux ZINE imprimables (UPlanet/earth/ZINE*.html) : un
# visiteur qui ne peut pas imprimer remplit les mêmes champs à l'écran et les
# envoie par email au capitaine de la station, via mailjet.sh.
_ZINE_TITLES = {
    "armateur": "📄 Contrat Armateur (COMMODAT) — ZINE.html",
    "adhesion": "🌿 Adhésion Made In Zion — ZINE.MIZ.html",
    "talent":   "🌾 Déclaration de Talent — ZINE.MOUVEMENT.html",
}


def _esc(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _send_to_captain(title: str, body_html: str) -> tuple[bool, str]:
    """Envoie body_html au capitaine de la station via mailjet.sh.

    Retourne (succès, message d'erreur si échec).
    """
    captain = settings.CAPTAINEMAIL
    mailjet_sh = settings.TOOLS_PATH / "mailjet.sh"
    if not (captain and mailjet_sh.exists()):
        return False, "Envoi indisponible sur cette station — merci d'imprimer le ZINE."

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(body_html)
            tmp_path = f.name

        proc = await asyncio.create_subprocess_exec(
            str(mailjet_sh), captain, tmp_path, title,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        Path(tmp_path).unlink(missing_ok=True)

        if proc.returncode == 0:
            logger.info(f"[zine-submit] Mailjet OK → {captain} ({title})")
            return True, ""
        logger.warning(f"[zine-submit] mailjet.sh code {proc.returncode}")
    except Exception as e:
        logger.warning(f"[zine-submit] mailjet.sh erreur: {e}")
    return False, "Échec de l'envoi — merci d'imprimer le ZINE."


@router.post("/api/zine-submit", summary="Envoyer un formulaire ZINE au capitaine par email")
async def post_zine_submit(
    zine: str = Form(...),
    fields: str = Form(...),
    website: Optional[str] = Form(""),
):
    """
    Alternative numérique aux ZINE imprimables (Contrat Armateur, Adhésion Made In
    Zion, Déclaration de Talent). Reçoit les champs remplis dans le navigateur et
    les transmet par email au capitaine de la station via `mailjet.sh`.

    Aucune authentification requise : les visiteurs qui remplissent ces zines
    n'ont pas forcément encore de MULTIPASS. Protection anti-spam : champ
    honeypot `website` (doit rester vide) + rate limiting global (middleware).

    - `zine` : identifiant du zine — "armateur" | "adhesion" | "talent"
    - `fields` : objet JSON stringifié `{label: valeur}` (ordre d'affichage libre)
    - `website` : honeypot, doit être vide
    """
    if website:
        logger.info("[zine-submit] honeypot déclenché, requête ignorée")
        return JSONResponse({"ok": True})

    if zine not in _ZINE_TITLES:
        return JSONResponse({"ok": False, "error": "Type de zine inconnu"}, status_code=400)

    try:
        data = json.loads(fields)
        if not isinstance(data, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "Champ 'fields' invalide"}, status_code=400)

    title = _ZINE_TITLES[zine]
    rows = "".join(
        f"<tr><td style='padding:4px 10px;color:#888'>{_esc(k)}</td>"
        f"<td style='padding:4px 10px'><b>{_esc(v)}</b></td></tr>"
        for k, v in data.items() if str(v).strip()
    )
    body_html = (
        f"<h2>{title}</h2>"
        f"<p>Formulaire rempli en ligne (alternative à l'impression papier).</p>"
        f"<table style='border-collapse:collapse'>{rows}</table>"
        f"<p style='color:#888;font-size:.85em'>Station : {_esc(settings.uSPOT)}</p>"
    )

    ok, error = await _send_to_captain(title, body_html)
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=502)

    logger.info(f"[zine-submit] {zine} envoyé au capitaine")
    return JSONResponse({"ok": True, "message": "Formulaire envoyé au capitaine"})
