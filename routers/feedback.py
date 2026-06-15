import asyncio
import logging
import re
import tempfile
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

from core.config import settings

router = APIRouter()

logger = logging.getLogger(__name__)

# Repo slug validation: lowercase letters, digits, hyphens, dots
_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9\-\.]{0,98}$')


# ─── Kind 30800 helpers ───────────────────────────────────────────────────────

async def _get_coop_config(key: str) -> str:
    """Lit une valeur depuis le DID NOSTR coopératif (kind 30800) via cooperative_config.sh."""
    coop_script = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "cooperative_config.sh"
    if not coop_script.exists():
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", 'source "$1" 2>/dev/null && coop_config_get "$2" 2>/dev/null',
            "--", str(coop_script), key,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        value = stdout.decode().strip()
        if value and proc.returncode == 0:
            return value
    except Exception as e:
        logger.debug(f"cooperative_config.sh indisponible pour {key}: {e}")
    return ""


# ─── Git helpers ──────────────────────────────────────────────────────────────

def _is_github(host: str) -> bool:
    return "github.com" in host


def _resolve_repo(source: Optional[str], owner: str) -> str:
    """Construit owner/repo depuis le champ source.
    source="coracle" → "{owner}/coracle" ; None → "{owner}/zelkova".
    """
    if not source:
        return f"{owner}/zelkova"
    slug = source.strip().lower()
    if "/" in slug:
        slug = slug.split("/")[-1]
    if _SLUG_RE.match(slug):
        return f"{owner}/{slug}"
    logger.warning(f"[feedback] slug source invalide: {source!r}, fallback")
    return f"{owner}/zelkova"


async def _post_github(token: str, repo: str, title: str, body: str, label: str) -> dict:
    url = f"https://api.github.com/repos/{repo}/issues"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body, "labels": [label]},
        )
    return {"status": resp.status_code, "data": resp.json() if resp.status_code in (200, 201) else {}, "text": resp.text}


async def _post_gitlab(host: str, token: str, repo: str, title: str, body: str, label: str) -> dict:
    encoded = repo.replace("/", "%2F")
    url = f"{host.rstrip('/')}/api/v4/projects/{encoded}/issues"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            headers={"PRIVATE-TOKEN": token},
            json={"title": title, "description": body, "labels": label},
        )
    return {"status": resp.status_code, "data": resp.json() if resp.status_code in (200, 201) else {}, "text": resp.text}


# ─── Mailjet fallback ─────────────────────────────────────────────────────────

async def _send_mailjet_fallback(title: str, body_html: str, repo: str) -> bool:
    """Envoie le feedback par email via mailjet.sh si Git n'est pas disponible.

    Destine le message à MJ_SENDER_EMAIL (adresse support de la coopérative, kind 30800).
    Retourne True si l'envoi a réussi (code 0).
    """
    mailjet_sh = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "mailjet.sh"
    if not mailjet_sh.exists():
        logger.debug("[feedback] mailjet.sh introuvable")
        return False

    dest_email = await _get_coop_config("MJ_SENDER_EMAIL")
    if not dest_email:
        logger.debug("[feedback] MJ_SENDER_EMAIL absent, mailjet.sh ignoré")
        return False

    subject = f"[feedback] {repo}: {title}"

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(body_html)
            tmp_path = f.name

        proc = await asyncio.create_subprocess_exec(
            str(mailjet_sh), dest_email, tmp_path, subject,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        Path(tmp_path).unlink(missing_ok=True)

        if proc.returncode == 0:
            logger.info(f"[feedback] Mailjet fallback OK → {dest_email} ({subject})")
            return True
        logger.warning(f"[feedback] mailjet.sh code {proc.returncode} pour {dest_email}")
    except Exception as e:
        logger.warning(f"[feedback] mailjet.sh erreur: {e}")
    return False


# ─── Endpoint ─────────────────────────────────────────────────────────────────

@router.post("/api/feedback", summary="Envoyer un feedback ou rapport de bug")
async def post_feedback(
    title: str = Form(...),
    description: str = Form(...),
    source: Optional[str] = Form(None),
    pubkey: Optional[str] = Form(None),
    category: Optional[str] = Form("bug"),
    app_version: Optional[str] = Form(None),
    platform: Optional[str] = Form(None),
):
    """
    Crée une issue Git ou envoie un email de fallback.

    **Priorité** :
    1. GitHub (`GIT_HOST=https://github.com`) ou GitLab (`GIT_HOST=https://git.p2p.legal`)
    2. Si Git absent/inaccessible → email via `mailjet.sh` au capitaine de la station
    3. Si Mailjet également absent → réponse `ok=true, stored=local`

    **Configuration** (kind 30800) : `GIT_HOST`, `GIT_TOKEN`, `GIT_OWNER`

    **Routing** : `source="coracle"` → `{GIT_OWNER}/coracle`
    """
    git_host = await _get_coop_config("GIT_HOST") or "https://github.com"
    git_token = await _get_coop_config("GIT_TOKEN")
    git_owner = await _get_coop_config("GIT_OWNER") or "papiche"
    repo = _resolve_repo(source, git_owner)

    # Build issue body (shared between Git and email)
    meta = []
    if source:
        meta.append(f"**Source:** {source}")
    if app_version:
        meta.append(f"**Version:** {app_version}")
    if platform:
        meta.append(f"**Plateforme:** {platform}")
    if pubkey:
        meta.append(f"**Pubkey:** `{pubkey}`")

    issue_body = description
    if meta:
        issue_body += "\n---\n" + "  \n".join(meta)

    # HTML version for Mailjet (same content, rendered)
    body_html = f"<h3>{title}</h3><p>{description.replace(chr(10), '<br>')}</p>"
    if meta:
        body_html += "<hr><small>" + " &nbsp;|&nbsp; ".join(meta) + "</small>"

    label = "bug" if (category or "bug").lower() == "bug" else "feedback"

    # ── 1. Try Git issue ──────────────────────────────────────────────────────
    if git_token:
        try:
            if _is_github(git_host):
                result = await _post_github(git_token, repo, title, issue_body, label)
            else:
                result = await _post_gitlab(git_host, git_token, repo, title, issue_body, label)

            if result["status"] in (200, 201):
                issue = result["data"]
                issue_url = issue.get("html_url") or issue.get("web_url", "")
                issue_number = issue.get("number") or issue.get("iid")
                logger.info(f"[feedback] Issue créée: {repo}#{issue_number} — {title!r}")
                return JSONResponse({
                    "ok": True,
                    "stored": "git",
                    "issue_url": issue_url,
                    "issue_number": issue_number,
                    "repo": repo,
                    "provider": "github" if _is_github(git_host) else "gitlab",
                })
            logger.warning(f"[feedback] Git API {result['status']} pour {repo}: {result['text'][:200]}")

        except httpx.RequestError as e:
            logger.warning(f"[feedback] Erreur réseau Git: {e}")

    # ── 2. Fallback Mailjet ───────────────────────────────────────────────────
    sent = await _send_mailjet_fallback(title, body_html, repo)
    if sent:
        return JSONResponse({"ok": True, "stored": "email", "message": "Feedback envoyé par email au capitaine"})

    # ── 3. Stored locally (silent) ────────────────────────────────────────────
    logger.info(f"[feedback] Stockage local: {title!r} ({repo})")
    return JSONResponse({"ok": True, "stored": "local", "message": "Feedback reçu"})
