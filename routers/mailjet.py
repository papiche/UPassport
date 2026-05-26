"""
Route /mailjet — Gestion des opt-outs notifications UPlanet.
Écrit ~/.zen/game/nostr/$email/.mailjet (JSON) comme marqueur.
Vérifié par Astroport.ONE/tools/mailjet.sh avant tout envoi.
"""
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse

from core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _uplanetname() -> str:
    p = Path.home() / ".ipfs" / "swarm.key"
    try:
        return p.read_text().strip().split('\n')[-1]
    except Exception:
        return ""


def _token_for(email: str) -> str:
    """sha256(email:UPLANETNAME)[:16] — même algo que mailjet.sh bash."""
    return hashlib.sha256(f"{email}:{_uplanetname()}".encode()).hexdigest()[:16]


def _mailjet_path(email: str) -> Path:
    return settings.GAME_PATH / "nostr" / email / ".mailjet"


def _read_optout(email: str) -> dict:
    p = _mailjet_path(email)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _write_optout(email: str, channels: list[str], npub: str = "") -> None:
    p = _mailjet_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "email_channel": "email" in channels or "all" in channels,
        "nostr_channel": "nostr" in channels or "all" in channels,
        "channels": channels,
        "timestamp": int(time.time()),
    }
    if npub:
        data["npub"] = npub
    p.write_text(json.dumps(data, indent=2))
    logger.info("Mailjet optout %s → channels=%s npub=%s", email, channels, npub or "-")


# ─── CSS ─────────────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #050a12; color: #e0f0ff;
       font-family: 'Segoe UI', Arial, sans-serif; min-height: 100vh; }
.wrap { max-width: 560px; margin: 70px auto 40px; padding: 36px 28px;
        border: 1px solid rgba(0,245,255,0.15); border-radius: 6px;
        background: rgba(0,245,255,0.03); }
h1 { font-family: 'Courier New', monospace; color: #00f5ff; font-size: 1.5rem;
     letter-spacing: 4px; margin-bottom: 4px; }
.sub { font-family: 'Courier New', monospace; font-size: 0.62rem;
       color: rgba(255,255,255,0.3); letter-spacing: 3px; margin-bottom: 28px; }
p { color: rgba(255,255,255,0.65); font-size: 0.9rem; line-height: 1.75; margin-bottom: 14px; }
code { color: #00f5ff; background: rgba(0,245,255,0.08); padding: 1px 6px;
       border-radius: 3px; font-family: 'Courier New', monospace; font-size: 0.85em; }
.sep { border: none; border-top: 1px solid rgba(0,245,255,0.1); margin: 22px 0; }
/* ── Nostr bar ── */
#nostr-bar { position: fixed; top: 12px; right: 14px; z-index: 9000;
  display: flex; align-items: center; gap: 8px;
  background: rgba(0,0,0,0.6); backdrop-filter: blur(12px);
  border: 1px solid rgba(255,255,255,0.12); border-radius: 20px;
  padding: 5px 14px; font-size: 12px; color: rgba(255,255,255,0.9); }
#user-name-badge { display: none; color: #86efac; font-weight: 600; }
#btn-connect { background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.22);
  color: rgba(255,255,255,0.88); padding: 3px 10px; border-radius: 12px;
  cursor: pointer; font-size: 11px; font-weight: 500; transition: background .2s; }
#btn-connect:hover { background: rgba(255,255,255,0.2); }
/* ── NOSTR profile block ── */
.nostr-block { background: rgba(86,239,172,0.05); border: 1px solid rgba(86,239,172,0.18);
  border-radius: 4px; padding: 14px 16px; margin-bottom: 18px; }
.nostr-block .ntitle { font-family: 'Courier New', monospace; font-size: 0.62rem;
  color: #86efac; letter-spacing: 3px; margin-bottom: 8px; }
.nostr-block a { color: #86efac; text-decoration: none; font-size: 0.85rem;
  border-bottom: 1px dotted rgba(134,239,172,0.4); }
.nostr-block a:hover { background: rgba(86,239,172,0.08); }
/* ── channels ── */
.channel-grid { display: flex; flex-direction: column; gap: 10px; margin: 18px 0; }
.channel-card { display: flex; align-items: center; gap: 14px; padding: 13px 16px;
  border: 1px solid rgba(0,245,255,0.12); border-radius: 4px;
  cursor: pointer; transition: border-color .2s; }
.channel-card:hover { border-color: rgba(0,245,255,0.3); }
.channel-card input[type=checkbox] { accent-color: #00f5ff; width: 18px; height: 18px; flex-shrink: 0; }
.channel-card label { cursor: pointer; flex: 1; }
.ctitle { color: #fff; font-size: 0.88rem; font-weight: bold; display: block; }
.cdesc  { color: rgba(255,255,255,0.42); font-size: 0.77rem; margin-top: 2px; display: block; }
.warn { background: rgba(255,68,68,0.05); border-left: 3px solid #c0392b;
  padding: 12px 16px; border-radius: 0 4px 4px 0; margin: 18px 0; }
.warn p { color: rgba(255,110,110,0.85); font-size: 0.82rem; margin: 0; }
button[type=submit] { background: #c0392b; color: #fff; border: none; padding: 12px 32px;
  border-radius: 6px; font-size: 0.88rem; cursor: pointer;
  font-family: 'Courier New', monospace; letter-spacing: 1px; margin-top: 4px;
  transition: background .2s; }
button[type=submit]:hover { background: #e74c3c; }
.ok { background: rgba(0,255,136,0.06); border-left: 3px solid #00ff88;
  padding: 18px 20px; border-radius: 0 4px 4px 0; }
.ok h2 { font-family: 'Courier New', monospace; color: #00ff88; font-size: 1rem;
  letter-spacing: 2px; margin-bottom: 10px; }
footer { text-align: center; margin-top: 32px; font-family: 'Courier New', monospace;
  font-size: 0.62rem; color: rgba(255,255,255,0.18); }
"""

# ─── JS NOSTR (NIP-07 + nostr.bundle.js) ─────────────────────────────────────

_NOSTR_JS = """
var _npub = '';

function _setNpub(npub) {
  _npub = npub;
  document.getElementById('npub-hidden').value = npub;
  document.getElementById('conn-badge').textContent = '🟢';
  document.getElementById('btn-connect').style.display = 'none';
  var nb = document.getElementById('user-name-badge');
  nb.textContent = npub.slice(0,12) + '…' + npub.slice(-4);
  nb.style.display = '';
  _showProfileBlock(npub);
}

function _showProfileBlock(npub) {
  var blk = document.getElementById('nostr-block');
  if (!blk) return;
  var viewerUrl = 'https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr_profile_viewer.html?npub=' + encodeURIComponent(npub);
  document.getElementById('nostr-profile-link').href = viewerUrl;
  document.getElementById('nostr-npub-code').textContent = npub.slice(0,16) + '…';
  blk.style.display = '';
}

async function handleConnect() {
  var btn = document.getElementById('btn-connect');
  btn.textContent = '⏳…'; btn.disabled = true;
  try {
    if (window.nostr) {
      var hex = await window.nostr.getPublicKey();
      var npub = (window.NostrTools && window.NostrTools.nip19)
        ? window.NostrTools.nip19.npubEncode(hex)
        : 'npub1' + hex.slice(0, 20) + '…';
      _setNpub(npub);
    } else {
      alert('Aucune extension NOSTR détectée (Alby, nos2x, Flamingo…)');
    }
  } catch(e) { console.warn('NOSTR connect:', e); }
  finally { btn.textContent = '⚡ Se connecter'; btn.disabled = false; }
}

window.addEventListener('DOMContentLoaded', function() {
  // Pré-remplir si npub déjà sauvegardé
  var saved = document.getElementById('npub-hidden').value;
  if (saved && saved.startsWith('npub1')) { _showProfileBlock(saved); }
  // Cacher le bouton si pas d'extension
  if (!window.nostr) {
    document.getElementById('btn-connect').style.display = 'none';
    document.getElementById('conn-badge').title = 'Installez Alby ou nos2x pour vous connecter';
  }
});
"""


def _page_confirm(email: str, token: str, current: dict) -> str:
    ec = "checked" if current.get("email_channel") else ""
    nc = "checked" if current.get("nostr_channel") else ""
    saved_npub = current.get("npub", "")
    block_style = "" if saved_npub else "display:none"
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"

    return f"""<!DOCTYPE html><html lang="fr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Notifications UPlanet</title>
<script src="https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr.bundle.js"></script>
<script src="https://ipfs.copylaradio.com/ipns/copylaradio.com/feedback.js"></script>
<style>{_CSS}</style>
</head><body>

<!-- Barre NOSTR fixe -->
<div id="nostr-bar">
  <span id="conn-badge">🔴</span>
  <span id="user-name-badge"></span>
  <button id="btn-connect" onclick="handleConnect()">⚡ Se connecter</button>
</div>

<div class="wrap">
  <h1>ASTRO<span style="color:#00ff88;">PORT</span>.ONE</h1>
  <p class="sub">// PRÉFÉRENCES NOTIFICATIONS</p>
  <p>Notifications envoyées à <code>{email}</code> par votre station UPlanet.</p>

  <!-- Bloc NOSTR profile (visible si connecté ou npub sauvegardé) -->
  <div id="nostr-block" class="nostr-block" style="{block_style}">
    <div class="ntitle">// IDENTITÉ NOSTR</div>
    <p style="margin:0 0 8px;font-size:0.82rem;color:rgba(255,255,255,0.55);">
      Clé publique associée : <code id="nostr-npub-code">{saved_npub[:16] + "…" if saved_npub else ""}</code>
    </p>
    <a id="nostr-profile-link"
       href="https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr_profile_viewer.html{f'?npub={saved_npub}' if saved_npub else ''}">
      👤 Consulter mon MULTIPASS
    </a>
  </div>

  <hr class="sep">

  <form method="post" action="/mailjet">
    <input type="hidden" name="email" value="{email}">
    <input type="hidden" name="token" value="{token}">
    <input type="hidden" name="npub"  value="{saved_npub}" id="npub-hidden">
    <p style="color:rgba(255,255,255,0.5);font-size:0.82rem;margin-bottom:6px;">
      Cochez les canaux sur lesquels vous souhaitez
      <strong style="color:#e74c3c;">arrêter</strong> les notifications&nbsp;:
    </p>
    <div class="channel-grid">
      <div class="channel-card" onclick="this.querySelector('input').click()">
        <input type="checkbox" name="channels" value="email" id="ce" {ec}>
        <label for="ce">
          <span class="ctitle">📧 Email</span>
          <span class="cdesc">Invitations MULTIPASS, rappels contribution, actualités station</span>
        </label>
      </div>
      <div class="channel-card" onclick="this.querySelector('input').click()">
        <input type="checkbox" name="channels" value="nostr" id="cn" {nc}>
        <label for="cn">
          <span class="ctitle">💬 NOSTR DMs</span>
          <span class="cdesc">Messages directs depuis les relays coopératifs UPlanet</span>
        </label>
      </div>
    </div>
    <div class="warn">
      <p>⚠️ Vous ne recevrez plus de rappels pour créer votre MULTIPASS.
         Réversible via <a href="mailto:{captain}"
         style="color:#e88;">{captain}</a>.</p>
    </div>
    <button type="submit">Enregistrer mes préférences</button>
  </form>
</div>

<footer>ASTROPORT.ONE · AGPL-3.0</footer>
<script>{_NOSTR_JS}</script>
</body></html>"""


def _page_success(email: str, channels: list[str], npub: str = "") -> str:
    labels = {"email": "emails", "nostr": "DMs NOSTR"}
    desc = " et ".join(labels.get(c, c) for c in channels) if channels else "aucun canal"
    ipfs_viewer = "https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr_profile_viewer.html"
    viewer = f"{ipfs_viewer}?npub={npub}" if npub else ipfs_viewer
    return f"""<!DOCTYPE html><html lang="fr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Préférences enregistrées — UPlanet</title><style>{_CSS}</style></head><body>
<div class="wrap">
  <h1>ASTRO<span style="color:#00ff88;">PORT</span>.ONE</h1>
  <p class="sub">// PRÉFÉRENCES ENREGISTRÉES</p>
  <div class="ok">
    <h2>✅ Préférences mises à jour</h2>
    <p style="font-size:0.87rem;margin-bottom:10px;">
      <code>{email}</code> ne recevra plus de {desc}.
    </p>
    <p style="margin:0;font-size:0.85rem;">
      <a href="{viewer}" style="color:#86efac;">👤 Consulter mon MULTIPASS →</a><br><br>
      <a href="https://opencollective.com/monnaie-libre" style="color:#00ff88;">
        Rejoindre la coopérative →
      </a>
    </p>
  </div>
</div>
<footer>ASTROPORT.ONE · AGPL-3.0</footer>
</body></html>"""


def _page_error(msg: str) -> str:
    captain = settings.CAPTAINEMAIL or "support@qo-op.com"
    return f"""<!DOCTYPE html><html lang="fr"><head>
<meta charset="UTF-8"><title>Erreur — UPlanet</title><style>{_CSS}</style></head><body>
<div class="wrap">
  <h1>ASTRO<span style="color:#00ff88;">PORT</span>.ONE</h1>
  <p class="sub">// ERREUR</p>
  <p style="color:#e74c3c;">⚠️ {msg}</p>
  <p>Contactez <a href="mailto:{captain}" style="color:#00f5ff;">{captain}</a>
     pour obtenir un nouveau lien.</p>
</div>
<footer>ASTROPORT.ONE · AGPL-3.0</footer>
</body></html>"""


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/mailjet", response_class=HTMLResponse)
async def get_mailjet(
    email: str = Query(...),
    token: str = Query(...),
):
    if _token_for(email) != token:
        return HTMLResponse(_page_error("Lien invalide ou expiré."), status_code=403)
    current = _read_optout(email)
    return HTMLResponse(_page_confirm(email, token, current))


@router.post("/mailjet", response_class=HTMLResponse)
async def post_mailjet(
    email: str = Form(...),
    token: str = Form(...),
    channels: list[str] = Form(default=[]),
    npub: Optional[str] = Form(default=None),
):
    if _token_for(email) != token:
        return HTMLResponse(_page_error("Token invalide."), status_code=403)
    _write_optout(email, channels, npub or "")
    return HTMLResponse(_page_success(email, channels, npub or ""))
