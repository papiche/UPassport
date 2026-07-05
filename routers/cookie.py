"""Cookie management — list / get (decrypted) / delete cookies stored via IPFS + NOSTR DID."""

import asyncio
import logging
logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from core.config import settings
from services.cookie_store import decrypt_from_ipfs, load_manifest, save_manifest, publish_manifest_to_nostr
from services.nostr import require_nostr_auth
from utils.crypto import npub_to_hex
from utils.security import find_user_directory_by_hex

router = APIRouter(prefix="/cookie", tags=["cookie"])


def _user_dir(npub: str) -> Path:
    return find_user_directory_by_hex(npub_to_hex(npub))


# ──────────────────────────────────────────────────────────────────────────────
# GET /cookie  — list all stored cookies
# ──────────────────────────────────────────────────────────────────────────────

@router.get("", summary="List cookies for authenticated user")
async def list_cookies(
    request: Request,
    npub: Optional[str] = Query(None, description="User npub (required when no NIP-98 header)"),
    html: Optional[int] = Query(None, description="Set to 1 for HTML page"),
):
    auth_npub = await require_nostr_auth(request, npub, force_check=False)
    user_dir = _user_dir(auth_npub)
    manifest = load_manifest(user_dir)

    now = datetime.now(timezone.utc)
    cookies = []
    seen_domains: set = set()

    # 1. Cookies dans le manifest IPFS chiffré (source primaire)
    for domain, info in manifest.items():
        if domain.startswith("_"):
            # Clés internes (ex: _bro_commands) — ignorer
            continue
        seen_domains.add(domain)
        uploaded_at = info.get("uploaded_at", "")
        age_days: Optional[int] = None
        if uploaded_at:
            try:
                dt = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
                age_days = (now - dt).days
            except Exception:
                pass
        cookies.append({
            "domain":      domain,
            "uploaded_at": uploaded_at,
            "age_days":    age_days,
            "size":        info.get("size", 0),
            "cid":         info.get("cid"),
        })

    # 2. Fichiers .*.cookie sur disque pas encore dans le manifest IPFS
    for f in sorted(user_dir.glob(".*.cookie")):
        domain = f.name[1:-len(".cookie")]  # ".mastodon.social.cookie" → "mastodon.social"
        if domain.startswith("_") or domain in seen_domains:
            continue
        stat = f.stat()
        cookies.append({
            "domain":      domain,
            "uploaded_at": "",
            "age_days":    None,
            "size":        stat.st_size,
            "cid":         None,
        })

    if html:
        return _html_page(auth_npub, cookies)

    return {"cookies": cookies, "count": len(cookies)}


# ──────────────────────────────────────────────────────────────────────────────
# GET /cookie/{domain}  — decrypt + return Netscape cookie file
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/{domain}", summary="Return decrypted Netscape cookie for a domain")
async def get_cookie(
    domain: str,
    request: Request,
    npub: Optional[str] = Query(None),
):
    _validate_domain(domain)
    auth_npub = await require_nostr_auth(request, npub, force_check=False)
    user_dir = _user_dir(auth_npub)
    manifest = load_manifest(user_dir)

    # Résolution avec fallback parent-domain :
    # GET /cookie/notebooklm.google.com → cherche d'abord "notebooklm.google.com"
    # puis "google.com" si absent du manifest (LCA lors de l'upload).
    resolved = domain
    if domain not in manifest:
        parts = domain.split('.')
        while len(parts) > 2:
            parts.pop(0)
            candidate = '.'.join(parts)
            if candidate in manifest:
                resolved = candidate
                logger.info(f"Cookie {domain}: résolu via parent '{resolved}'")
                break
        else:
            raise HTTPException(status_code=404, detail=f"No cookie stored for: {domain}")

    cid = manifest[resolved].get("cid")

    # Try IPFS decrypt (primary — encrypted, private)
    if cid:
        content = await decrypt_from_ipfs(cid, user_dir)
        if content:
            return PlainTextResponse(content.decode("utf-8", errors="replace"))

    # Fallback: disk plaintext (backward compat — still present after upload)
    disk = user_dir / f".{resolved}.cookie"
    if disk.exists():
        logger.info(f"Cookie {resolved}: IPFS decrypt unavailable, serving disk fallback")
        return PlainTextResponse(disk.read_text())

    raise HTTPException(
        status_code=500,
        detail="Cookie unavailable: IPFS decrypt failed and no disk fallback",
    )


# ──────────────────────────────────────────────────────────────────────────────
# DELETE /cookie/{domain}
# ──────────────────────────────────────────────────────────────────────────────

@router.delete("/{domain}", summary="Delete a stored cookie")
async def delete_cookie(
    domain: str,
    request: Request,
    npub: Optional[str] = Query(None),
):
    _validate_domain(domain)
    auth_npub = await require_nostr_auth(request, npub, force_check=False)
    user_dir = _user_dir(auth_npub)
    manifest = load_manifest(user_dir)

    # Résolution parent-domain (cohérence avec GET)
    resolved = domain
    if domain not in manifest:
        parts = domain.split('.')
        while len(parts) > 2:
            parts.pop(0)
            candidate = '.'.join(parts)
            if candidate in manifest:
                resolved = candidate
                break
        else:
            raise HTTPException(status_code=404, detail=f"No cookie for: {domain}")

    cid = manifest[resolved].get("cid")
    del manifest[resolved]
    save_manifest(user_dir, manifest)
    asyncio.create_task(publish_manifest_to_nostr(user_dir, manifest))

    # Remove disk file (utiliser resolved, pas domain)
    disk = user_dir / f".{resolved}.cookie"
    if disk.exists():
        disk.unlink()

    # Unpin from IPFS (non-fatal)
    if cid:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ipfs", "pin", "rm", cid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            pass

    return {"success": True, "domain": resolved, "requested": domain, "cid_unpinned": cid}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _validate_domain(domain: str):
    if any(c in domain for c in ("/", "\\", "..", "\x00")):
        raise HTTPException(status_code=400, detail="Invalid domain name")


def _html_page(npub: str, cookies: list) -> HTMLResponse:
    """Minimal terminal-style HTML listing cookies with age and IPFS CID."""
    rows = ""
    for c in sorted(cookies, key=lambda x: x.get("uploaded_at", ""), reverse=True):
        age = c["age_days"]
        age_str   = f"{age}j" if age is not None else "?"
        age_color = ("#2ecc71" if (age or 0) < 14
                     else "#f39c12" if (age or 0) < 30
                     else "#e74c3c")
        cid_short = (c["cid"][:12] + "…") if c.get("cid") else "—"
        date_str  = c["uploaded_at"][:10] if c.get("uploaded_at") else "—"
        domain    = c["domain"]
        storage   = '🔐 IPFS' if c.get("cid") else '💾 local'
        s_color   = "#2ecc71" if c.get("cid") else "#f39c12"
        rows += (
            f'<tr>'
            f'<td style="color:#0ff">{domain}</td>'
            f'<td>{date_str}</td>'
            f'<td style="color:{age_color};font-weight:700">{age_str}</td>'
            f'<td style="font-size:.75em;font-family:monospace;color:#888">{cid_short}</td>'
            f'<td style="font-size:.75em;color:{s_color}">{storage}</td>'
            f'<td><button onclick="del(\'{domain}\')" '
            f'style="background:#e74c3c;color:#fff;border:none;padding:3px 8px;'
            f'border-radius:4px;cursor:pointer;font-size:.85em">🗑️</button></td>'
            f'</tr>'
        )
    if not rows:
        rows = '<tr><td colspan=6 style="color:#555;text-align:center;padding:20px">Aucun cookie stocké</td></tr>'

    npub_qs = f"?npub={npub}" if npub else ""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<style>
  body{{font:13px 'Inconsolata',monospace;background:#0a0a12;color:#e0e0e0;padding:20px;margin:0}}
  h2{{color:#0f0;margin:0 0 14px;letter-spacing:1px}}
  table{{width:100%;border-collapse:collapse}}
  th,td{{border:1px solid #222;padding:7px 10px;text-align:left}}
  th{{background:#0f0;color:#000;font-weight:700}}
  tr:hover{{background:#0d0d18}}
  .upload-link{{display:inline-block;margin-top:14px;padding:8px 18px;
    border:1px solid #0ff;color:#0ff;text-decoration:none;border-radius:4px;font-size:.9em}}
  .upload-link:hover{{background:#0ff;color:#000}}
</style></head><body>
<h2>🍪 Cookies chiffrés — IPFS + NOSTR DID</h2>
<table>
  <thead><tr><th>Domaine</th><th>Uploadé</th><th>Âge</th><th>CID IPFS</th><th>Stockage</th><th></th></tr></thead>
  <tbody id="tb">{rows}</tbody>
</table>
<a class="upload-link" href="/cookie.html">+ Uploader un cookie</a>
<script>
async function del(d){{
  if(!confirm('Supprimer le cookie pour '+d+' ?'))return;
  const r=await fetch('/cookie/'+d+'{npub_qs}',{{method:'DELETE'}});
  const j=await r.json().catch(()=>({{}}));
  if(r.ok){{document.querySelector('#tb tr:has([onclick*="'+d+'"])').remove();}}
  else alert('Erreur '+(j.detail||r.status));
}}
</script></body></html>""")
