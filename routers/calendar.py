import asyncio
import logging
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.config import settings
from services.nostr import verify_nip98_auth

router = APIRouter()

logger = logging.getLogger(__name__)

# Un VEVENT tient en quelques centaines d'octets ; large marge pour une description longue.
_MAX_ICS_LEN = 20_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _esc(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class CalendarEmailRequest(BaseModel):
    to_email: str = Field(..., max_length=254)
    subject: str = Field(..., max_length=200)
    summary: str = Field(..., max_length=200)
    description: str = Field(default="", max_length=4000)
    ics: str = Field(..., max_length=_MAX_ICS_LEN)
    from_name: str = Field(default="", max_length=120)


@router.post("/api/calendar/email", summary="Envoyer un événement de calendrier par email (.ics joint)")
async def post_calendar_email(
    payload: CalendarEmailRequest,
    pubkey: str = Depends(verify_nip98_auth),
):
    """
    Envoie un événement construit côté client (UPlanet/earth/calendars.html —
    calendrier lunaire NIP-52, ou événement personnel) par email à un destinataire
    arbitraire, avec le fichier .ics en pièce jointe : la plupart des clients mail
    proposent alors directement "Ajouter à l'agenda" au destinataire.

    Auth NIP-98 obligatoire (Authorization: Nostr <event>, kind 27235) — même
    mécanisme que /api/cloud/enroll et /api/fileupload. Seul un MULTIPASS signé
    peut déclencher un envoi, ce qui écarte l'abus en relais de spam ouvert ; le
    rate limiting global (middleware) s'applique par-dessus.
    """
    if not _EMAIL_RE.match(payload.to_email):
        return JSONResponse({"ok": False, "error": "Adresse email invalide"}, status_code=400)

    if "BEGIN:VCALENDAR" not in payload.ics or "BEGIN:VEVENT" not in payload.ics:
        return JSONResponse({"ok": False, "error": "Contenu .ics invalide"}, status_code=400)

    mailjet_sh = settings.TOOLS_PATH / "mailjet.sh"
    if not mailjet_sh.exists():
        return JSONResponse({"ok": False, "error": "Envoi d'email indisponible sur cette station"}, status_code=502)

    sender_label = _esc(payload.from_name) or "Un membre de la coopérative UPlanet"
    body_html = (
        f"<h2>📅 {_esc(payload.summary)}</h2>"
        f"<p>{sender_label} vous invite à cet événement.</p>"
        + (f"<p style='white-space:pre-wrap'>{_esc(payload.description)}</p>" if payload.description.strip() else "")
        + "<p style='color:#888;font-size:.85em'>La pièce jointe .ics permet de l'ajouter directement à votre agenda.</p>"
    )

    ics_path = None
    html_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ics", delete=False, encoding="utf-8") as f:
            f.write(payload.ics)
            ics_path = f.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(body_html)
            html_path = f.name

        proc = await asyncio.create_subprocess_exec(
            str(mailjet_sh),
            "--channel", "calendar",
            "--expire", "7d",
            "--attach", ics_path,
            "--attach-name", "evenement.ics",
            "--attach-type", "text/calendar",
            payload.to_email, html_path, payload.subject,
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
            return JSONResponse({"ok": False, "error": "Délai d'envoi dépassé"}, status_code=504)

        if proc.returncode != 0:
            logger.warning("[calendar-email] mailjet.sh code %s (pubkey %s…)", proc.returncode, pubkey[:8])
            return JSONResponse({"ok": False, "error": "Échec de l'envoi"}, status_code=502)
    finally:
        if ics_path:
            Path(ics_path).unlink(missing_ok=True)
        if html_path:
            Path(html_path).unlink(missing_ok=True)

    logger.info("[calendar-email] %s… → %s", pubkey[:8], payload.to_email)
    return JSONResponse({"ok": True, "message": "Email envoyé"})
