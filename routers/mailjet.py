"""
Route /mailjet — Gestion des opt-outs notifications UPlanet.
Auth NIP-42 (Schnorr BIP-340 pur Python) + détection roaming MULTIPASS.
Écrit ~/.zen/game/nostr/$email/.mailjet (JSON) comme marqueur.
Vérifié par Astroport.ONE/tools/mailjet.sh avant tout envoi.
"""
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

# ─── Challenges NIP-42 (mémoire locale, TTL 5 min) ────────────────────────────
_challenges: dict[str, float] = {}  # challenge_hex → expiry_timestamp

# ─── secp256k1 / BIP-340 Schnorr (pur Python) ────────────────────────────────

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _pt_add(P, Q):
    if P is None: return Q
    if Q is None: return P
    x1, y1 = P; x2, y2 = Q
    if x1 == x2:
        if y1 != y2: return None
        lam = 3 * x1 * x1 * pow(2 * y1, _P - 2, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    return x3, (lam * (x1 - x3) - y1) % _P


def _pt_mul(P, n):
    R = None
    for i in range(256):
        if (n >> i) & 1: R = _pt_add(R, P)
        P = _pt_add(P, P)
    return R


def _lift_x(x: int):
    if x >= _P: return None
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if pow(y, 2, _P) != y_sq: return None
    return x, (y if y % 2 == 0 else _P - y)


def _tagged_hash(tag: str, data: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + data).digest()


def _schnorr_verify(msg: bytes, pk32: bytes, sig64: bytes) -> bool:
    """Vérifie une signature Schnorr BIP-340."""
    if len(pk32) != 32 or len(sig64) != 64: return False
    P = _lift_x(int.from_bytes(pk32, "big"))
    if P is None: return False
    r = int.from_bytes(sig64[:32], "big")
    s = int.from_bytes(sig64[32:], "big")
    if r >= _P or s >= _N: return False
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", sig64[:32] + pk32 + msg), "big"
    ) % _N
    R = _pt_add(_pt_mul(_G, s), _pt_mul(P, _N - e))
    return R is not None and R[1] % 2 == 0 and R[0] == r


def _verify_nostr_event(ev: dict) -> bool:
    """Vérifie l'ID (SHA-256) et la signature Schnorr d'un événement NOSTR."""
    try:
        serial = json.dumps(
            [0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
            separators=(",", ":"), ensure_ascii=False,
        )
        if ev.get("id") != hashlib.sha256(serial.encode()).hexdigest():
            return False
        return _schnorr_verify(
            bytes.fromhex(ev["id"]),
            bytes.fromhex(ev["pubkey"]),
            bytes.fromhex(ev["sig"]),
        )
    except Exception:
        return False


# ─── Helpers métier ───────────────────────────────────────────────────────────

def _uplanetname() -> str:
    try:
        return (Path.home() / ".ipfs" / "swarm.key").read_text().strip().split('\n')[-1]
    except Exception:
        return ""


def _relay_url() -> str:
    try:
        cap = settings.CAPTAINEMAIL or ""
        if "@" in cap:
            return f"wss://relay.{cap.split('@')[-1]}"
    except Exception:
        pass
    return "wss://relay.copylaradio.com"


def _token_for(email: str) -> str:
    """sha256(email:UPLANETNAME)[:16] — même algo que mailjet.sh bash."""
    return hashlib.sha256(f"{email}:{_uplanetname()}".encode()).hexdigest()[:16]


def _mailjet_path(email: str) -> Path:
    return settings.GAME_PATH / "nostr" / email / ".mailjet"


def _email_from_npub(npub: str) -> str | None:
    """Cherche l'email associé à un npub dans ~/.zen/game/nostr/*/NPUB."""
    for f in (settings.GAME_PATH / "nostr").glob("*/NPUB"):
        try:
            if f.read_text().strip() == npub:
                return f.parent.name
        except Exception:
            continue
    return None


def _check_roaming(npub: str, hex_pk: str = "") -> str:
    """
    Retourne 'local' | 'roaming' | 'unknown'.
    local   : MULTIPASS géré sur cette station.
    roaming : MULTIPASS présent dans le swarm mais pas localement.
    unknown : inconnu dans la constellation.
    """
    nostr_local = settings.GAME_PATH / "nostr"
    for f in nostr_local.glob("*/NPUB"):
        try:
            v = f.read_text().strip()
            if v == npub or (hex_pk and v == hex_pk):
                return "local"
        except Exception:
            continue
    swarm = Path.home() / ".zen" / "tmp" / "swarm"
    if swarm.exists():
        for f in swarm.glob("**/NPUB"):
            try:
                v = f.read_text().strip()
                if v == npub or (hex_pk and v == hex_pk):
                    return "roaming"
            except Exception:
                continue
    return "unknown"


def _npub_from_hex(hex_pk: str) -> str:
    """Convertit un hex pubkey en npub1… (bech32). Fallback : retourne hex."""
    try:
        from bech32 import bech32_encode, convertbits
        converted = convertbits(bytes.fromhex(hex_pk), 8, 5)
        if converted:
            return bech32_encode("npub", converted)
    except Exception:
        pass
    return hex_pk


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


# ─── CSS partagé ──────────────────────────────────────────────────────────────

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
.nostr-block { background: rgba(86,239,172,0.05); border: 1px solid rgba(86,239,172,0.18);
  border-radius: 4px; padding: 14px 16px; margin-bottom: 18px; }
.nostr-block .ntitle { font-family: 'Courier New', monospace; font-size: 0.62rem;
  color: #86efac; letter-spacing: 3px; margin-bottom: 8px; }
.nostr-block a { color: #86efac; text-decoration: none; font-size: 0.85rem;
  border-bottom: 1px dotted rgba(134,239,172,0.4); }
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
  font-family: 'Courier New', monospace; letter-spacing: 1px; margin-top: 4px; }
.ok { background: rgba(0,255,136,0.06); border-left: 3px solid #00ff88;
  padding: 18px 20px; border-radius: 0 4px 4px 0; }
.ok h2 { font-family: 'Courier New', monospace; color: #00ff88; font-size: 1rem;
  letter-spacing: 2px; margin-bottom: 10px; }
.box { background: rgba(0,245,255,0.03); border: 1px solid rgba(0,245,255,0.18);
  border-radius: 4px; padding: 20px 22px; margin-bottom: 18px; }
.box h3 { font-family: 'Courier New', monospace; color: #00f5ff; font-size: 0.9rem;
  letter-spacing: 2px; margin: 0 0 12px; }
.or-sep { text-align: center; color: rgba(255,255,255,0.2); font-family: 'Courier New', monospace;
  font-size: 0.72rem; letter-spacing: 3px; margin: 16px 0; }
.btn-main { display: block; width: 100%; background: rgba(0,245,255,0.12);
  color: #00f5ff; border: 1px solid rgba(0,245,255,0.3); padding: 12px 22px;
  border-radius: 6px; font-family: 'Courier New', monospace; font-size: 0.85rem;
  cursor: pointer; letter-spacing: 1px; transition: background .2s; text-align: center; }
.btn-main:hover { background: rgba(0,245,255,0.22); }
.btn-main:disabled { opacity: 0.4; cursor: not-allowed; }
input[type=email], input[type=text] { background: rgba(0,245,255,0.05);
  border: 1px solid rgba(0,245,255,0.2); border-radius: 4px; color: #e0f0ff;
  font-family: 'Courier New', monospace; font-size: 0.85rem; padding: 8px 12px;
  width: 100%; margin-bottom: 10px; }
input:focus { outline: none; border-color: rgba(0,245,255,0.5); }
.alert { border-radius: 4px; padding: 14px 16px; margin: 14px 0; font-size: 0.84rem; }
.alert-warn { background: rgba(255,165,0,0.08); border-left: 3px solid #e8d44d; color: #e8d44d; }
.alert-err  { background: rgba(231,76,60,0.08);  border-left: 3px solid #e74c3c; color: #e88; }
footer { text-align: center; margin-top: 32px; font-family: 'Courier New', monospace;
  font-size: 0.62rem; color: rgba(255,255,255,0.18); }
"""

# ─── JS NIP-42 partagé ────────────────────────────────────────────────────────

_NIP42_JS = """
var _challenge = null;
var _relay = null;

async function _fetchChallenge() {
  try {
    var r = await fetch('/mailjet/challenge');
    var d = await r.json();
    _challenge = d.challenge;
    _relay = d.relay;
  } catch(e) { console.warn('challenge fetch:', e); }
}

async function handleConnect() {
  var btn = document.getElementById('btn-nostr');
  var err = document.getElementById('nip42-err');
  if (err) err.style.display = 'none';
  if (!_challenge) await _fetchChallenge();
  if (!_challenge) {
    if (err) { err.textContent = 'Impossible de récupérer le challenge NIP-42.'; err.style.display = ''; }
    return;
  }
  btn.textContent = '⏳ Signature…'; btn.disabled = true;
  try {
    if (!window.nostr) throw new Error('Aucune extension NOSTR (Alby, nos2x, Flamingo…)');
    var unsigned = {
      kind: 22242,
      created_at: Math.floor(Date.now() / 1000),
      tags: [['relay', _relay || 'wss://relay.copylaradio.com'], ['challenge', _challenge]],
      content: 'Identification de passeport UPlanet',
    };
    var signed = await window.nostr.signEvent(unsigned);
    var res = await fetch('/mailjet/auth', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event: signed}),
    });
    var data = await res.json();
    if (data.redirect) { window.location = data.redirect; return; }
    if (data.status === 'roaming') {
      document.getElementById('roaming-block').style.display = '';
      document.getElementById('roaming-station').textContent = data.station || '(station inconnue)';
      document.getElementById('roaming-viewer').href = data.viewer || '#';
      btn.textContent = '⚡ Connexion NOSTR'; btn.disabled = false;
      return;
    }
    if (data.status === 'unknown') { window.location = data.viewer; return; }
    if (err) { err.textContent = data.message || 'Erreur NIP-42.'; err.style.display = ''; }
  } catch(e) {
    if (err) { err.textContent = e.message; err.style.display = ''; }
  }
  btn.textContent = '⚡ Connexion NOSTR'; btn.disabled = false;
}

window.addEventListener('DOMContentLoaded', function() {
  _fetchChallenge();
  if (!window.nostr) {
    var btn = document.getElementById('btn-nostr');
    if (btn) { btn.textContent = '⚠️ Extension NOSTR non détectée'; btn.disabled = true; }
  }
});
"""

# ─── Nostr bar JS (pour les pages connectées) ─────────────────────────────────

_NOSTR_BAR_JS = """
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
  var url = 'https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr_profile_viewer.html?npub=' + encodeURIComponent(npub);
  document.getElementById('nostr-profile-link').href = url;
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
        ? window.NostrTools.nip19.npubEncode(hex) : 'npub1' + hex.slice(0,20) + '…';
      _setNpub(npub);
    } else {
      alert('Aucune extension NOSTR (Alby, nos2x, Flamingo…)');
    }
  } catch(e) { console.warn('NOSTR connect:', e); }
  finally { btn.textContent = '⚡ Se connecter'; btn.disabled = false; }
}

window.addEventListener('DOMContentLoaded', function() {
  var saved = document.getElementById('npub-hidden').value;
  if (saved && saved.startsWith('npub1')) _showProfileBlock(saved);
  if (!window.nostr) document.getElementById('btn-connect').style.display = 'none';
});
"""


# ─── Pages HTML ───────────────────────────────────────────────────────────────

def _page_landing() -> str:
    return f"""<!DOCTYPE html><html lang="fr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Préférences notifications — UPlanet</title>
<script src="https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr.bundle.js"></script>
<style>{_CSS}</style>
</head><body>

<div id="nostr-bar">
  <span>🛰️ UPlanet</span>
</div>

<div class="wrap">
  <h1>ASTRO<span style="color:#00ff88;">PORT</span>.ONE</h1>
  <p class="sub">// PRÉFÉRENCES NOTIFICATIONS</p>
  <p>Gérez vos préférences de notification UPlanet.</p>

  <!-- Bloc roaming (masqué par défaut) -->
  <div id="roaming-block" class="alert alert-warn" style="display:none;">
    <strong>⚠️ Roaming détecté</strong><br>
    Votre MULTIPASS est géré par la station <strong id="roaming-station"></strong>.
    Gérez vos préférences depuis cette station ou consultez votre profil :<br>
    <a id="roaming-viewer" href="#" style="color:#e8d44d;">👤 Voir mon MULTIPASS →</a>
  </div>

  <!-- Option 1 : NOSTR NIP-42 -->
  <div class="box">
    <h3>🔑 MULTIPASS NOSTR</h3>
    <p style="color:rgba(255,255,255,0.55);font-size:0.85rem;margin-bottom:16px;">
      Prouvez votre identité via votre clé NOSTR (NIP-42).<br>
      La station vérifie que vous êtes bien le propriétaire de votre MULTIPASS.
    </p>
    <button id="btn-nostr" class="btn-main" onclick="handleConnect()">⚡ Connexion NOSTR</button>
    <div id="nip42-err" class="alert alert-err" style="display:none;margin-top:12px;"></div>
  </div>

  <div class="or-sep">─── OU ───</div>

  <!-- Option 2 : Email + Token (lien email) -->
  <div class="box">
    <h3>📧 Lien email (token)</h3>
    <p style="color:rgba(255,255,255,0.55);font-size:0.85rem;margin-bottom:14px;">
      Collez le token reçu dans votre email d'invitation UPlanet.
    </p>
    <form method="get" action="/mailjet">
      <input type="email" name="email" placeholder="votre@email.com" required>
      <input type="text" name="token" placeholder="token — 16 caractères hex" required
             pattern="[0-9a-f]{{16}}" title="16 caractères hexadécimaux">
      <button type="submit" class="btn-main" style="background:rgba(0,255,136,0.12);color:#00ff88;border-color:rgba(0,255,136,0.3);">
        Accéder à mes préférences →
      </button>
    </form>
  </div>
</div>

<footer>ASTROPORT.ONE · AGPL-3.0</footer>
<script src="https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr.bundle.js"></script>
<script>{_NIP42_JS}</script>
</body></html>"""


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

<div id="nostr-bar">
  <span id="conn-badge">🔴</span>
  <span id="user-name-badge"></span>
  <button id="btn-connect" onclick="handleConnect()">⚡ Se connecter</button>
</div>

<div class="wrap">
  <h1>ASTRO<span style="color:#00ff88;">PORT</span>.ONE</h1>
  <p class="sub">// PRÉFÉRENCES NOTIFICATIONS</p>
  <p>Notifications envoyées à <code>{email}</code> par votre station UPlanet.</p>

  <div id="nostr-block" class="nostr-block" style="{block_style}">
    <div class="ntitle">// IDENTITÉ NOSTR</div>
    <p style="margin:0 0 8px;font-size:0.82rem;color:rgba(255,255,255,0.55);">
      Clé publique : <code id="nostr-npub-code">{saved_npub[:16] + "…" if saved_npub else ""}</code>
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
      Cochez les canaux à <strong style="color:#e74c3c;">désactiver</strong>&nbsp;:
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
         Réversible via <a href="mailto:{captain}" style="color:#e88;">{captain}</a>.</p>
    </div>
    <button type="submit">Enregistrer mes préférences</button>
  </form>
</div>

<footer>ASTROPORT.ONE · AGPL-3.0</footer>
<script>{_NOSTR_BAR_JS}</script>
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
      <a href="https://opencollective.com/monnaie-libre" style="color:#00ff88;">Rejoindre la coopérative →</a>
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
  <p><a href="/mailjet" style="color:rgba(0,245,255,0.6);">← Retour</a></p>
</div>
<footer>ASTROPORT.ONE · AGPL-3.0</footer>
</body></html>"""


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/mailjet/challenge")
async def get_challenge():
    """Génère un challenge NIP-42 (TTL 5 min, usage unique)."""
    # Purge challenges expirés
    now = time.time()
    expired = [k for k, v in _challenges.items() if v < now]
    for k in expired:
        del _challenges[k]

    challenge = secrets.token_hex(16)
    _challenges[challenge] = now + 300
    return JSONResponse({"challenge": challenge, "relay": _relay_url()})


@router.post("/mailjet/auth")
async def post_mailjet_auth(request: Request):
    """
    Vérifie l'événement NIP-42 signé, détecte le roaming, redirige.
    Réponse JSON : {redirect, status, viewer, station, message}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "JSON invalide."}, status_code=400)

    ev = body.get("event", {})

    # 1. Vérification Schnorr BIP-340
    if not _verify_nostr_event(ev):
        return JSONResponse({"status": "error", "message": "Signature NIP-42 invalide."}, status_code=403)

    # 2. Kind 22242 obligatoire
    if ev.get("kind") != 22242:
        return JSONResponse({"status": "error", "message": "Kind d'événement invalide (attendu 22242)."}, status_code=400)

    # 3. Challenge présent et valide
    challenge = next((t[1] for t in ev.get("tags", []) if t[0] == "challenge"), None)
    if not challenge or challenge not in _challenges:
        return JSONResponse({"status": "error", "message": "Challenge expiré ou invalide."}, status_code=403)
    if _challenges[challenge] < time.time():
        del _challenges[challenge]
        return JSONResponse({"status": "error", "message": "Challenge expiré."}, status_code=403)
    del _challenges[challenge]  # usage unique

    # 4. Obtenir npub depuis hex pubkey
    hex_pk = ev.get("pubkey", "")
    npub = _npub_from_hex(hex_pk)

    ipfs_viewer = "https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr_profile_viewer.html"
    viewer_url = f"{ipfs_viewer}?npub={npub}"

    # 5. Détection roaming
    state = _check_roaming(npub, hex_pk)
    logger.info("NIP-42 auth npub=%s… state=%s", npub[:16], state)

    if state == "local":
        # MULTIPASS géré ici : on redirige vers les préférences mailjet
        email = _email_from_npub(npub) or _email_from_npub(hex_pk)
        if email:
            token = _token_for(email)
            return JSONResponse({"status": "ok", "redirect": f"/mailjet?email={email}&token={token}"})
        # npub local mais email introuvable (incohérence) → profil viewer
        return JSONResponse({"status": "ok", "redirect": viewer_url})

    if state == "roaming":
        # MULTIPASS dans le swarm mais pas ici → refus, rediriger vers profil
        return JSONResponse({
            "status": "roaming",
            "viewer": viewer_url,
            "station": "un autre nœud UPlanet",
            "message": "Votre MULTIPASS est géré par une autre station. Gérez vos préférences depuis là-bas.",
        })

    # unknown : pas de MULTIPASS → profil viewer (pour créer son compte)
    return JSONResponse({"status": "unknown", "viewer": viewer_url})


@router.get("/mailjet", response_class=HTMLResponse)
async def get_mailjet(
    email: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
):
    # Pas de paramètres → page d'accueil NIP-42
    if not email:
        return HTMLResponse(_page_landing())
    expected = _token_for(email)
    # Token fourni mais invalide → erreur
    if token and token != expected:
        return HTMLResponse(_page_error("Lien invalide ou expiré."), status_code=403)
    # Email seul (accès local) ou token correct → préférences
    current = _read_optout(email)
    return HTMLResponse(_page_confirm(email, expected, current))


@router.post("/mailjet", response_class=HTMLResponse)
async def post_mailjet(
    email: Optional[str] = Form(None),
    token: Optional[str] = Form(None),
    channels: list[str] = Form(default=[]),
    npub: Optional[str] = Form(default=None),
):
    # npub seul (landing page → NOSTR connect sans NIP-42 complet) → profile viewer
    if not email and npub:
        ipfs_viewer = "https://ipfs.copylaradio.com/ipns/copylaradio.com/nostr_profile_viewer.html"
        return RedirectResponse(f"{ipfs_viewer}?npub={npub}", status_code=303)

    # Soumission formulaire préférences (email + token + channels)
    if not email or not token:
        return HTMLResponse(_page_error("Paramètres manquants."), status_code=400)
    if _token_for(email) != token:
        return HTMLResponse(_page_error("Token invalide."), status_code=403)
    _write_optout(email, channels, npub or "")
    return HTMLResponse(_page_success(email, channels, npub or ""))
