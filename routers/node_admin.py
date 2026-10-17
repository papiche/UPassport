"""
routers/node_admin.py — Administration de la configuration NODE (station) par
le capitaine, en whitelist stricte (cf. services/coop_config.py). Les
endpoints ARBOR (statut + déclenchement à distance) partagent ce même
router/schéma — voir plus bas.

Préfixe /api/nostr/admin/* conservé volontairement (même convention d'auth
NIP-98/UPLANETNAME déjà consommée par UPlanet/earth/nostr_admin.html —
apiFetch/buildAdminNip98Header ne changent pas) bien que ce router soit un
module séparé de routers/nostr.py (déjà volumineux, domaine NOSTR pur).
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core.config import settings
from services.admin_auth import check_admin_auth as _check_admin_auth, require_captain_signature, get_node_and_captain_hex
from services.coop_config import (
    COOP_CONFIG_SCHEMA, ALL_KEYS, is_sensitive, validate_value, CoopConfigError,
    coop_load_raw, coop_set_many, coop_delete,
)
from utils.security import safe_json_body

router = APIRouter()
logger = logging.getLogger(__name__)

_ENCRYPTED_RE = re.compile(r"^[0-9a-f]{32}:")

# ── ARBOR — statut + déclenchement à distance ───────────────────────────────
REPO_ROOT = settings.ZEN_PATH / "Astroport.ONE"
_IA_PATH = REPO_ROOT / "IA"
ARBOR_RUN_SH = _IA_PATH / "arbor_run.sh"
ARBOR_STATE_FILE = settings.ZEN_PATH / "tmp" / "arbor_run.state.json"
ARBOR_AUDIT_FILE = settings.ZEN_PATH / "flashmem" / "arbor_audit.jsonl"
_ARBOR_MODES = ("mine-requests", "observe-love", "explore", "apply", "forge", "forge-scraper")
_ARBOR_MODEL_RE = re.compile(r"^[A-Za-z0-9_./:-]{1,80}$")
_ARBOR_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,30}$")
_ARBOR_DOMAIN_RE = re.compile(r"^[a-z0-9.-]{1,80}$")
ARBOR_NEED_MAX_CHARS = 500
ARBOR_BATON_SH = _IA_PATH / "arbor_baton.sh"
ARBOR_BATON_ELIGIBILITY_SH = _IA_PATH / "arbor_baton_eligibility.sh"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# ── Purge NOSTR des comptes "étrangers" ─────────────────────────────────────
PURGE_SCRIPT = REPO_ROOT / "admin" / "system" / "purge_nostr_strangers.sh"
PURGE_LOCK_FILE = settings.ZEN_PATH / "tmp" / "purge_nostr_strangers.pid"
PURGE_LOG_FILE = settings.ZEN_PATH / "tmp" / "purge_nostr_strangers.log"

# ── Destruction MULTIPASS (nostr_DESTROY_TW.sh) ─────────────────────────────
DESTROY_TW_SCRIPT = REPO_ROOT / "tools" / "nostr_DESTROY_TW.sh"
DESTROY_LOCK_FILE = settings.ZEN_PATH / "tmp" / "nostr_destroy_tw.pid"
DESTROY_LOG_FILE = settings.ZEN_PATH / "tmp" / "nostr_destroy_tw.log"
_DESTROY_REASONS = ("INSOLVENCY", "INTRUSION", "INCOMPATIBLE_KEY")

# ── Synastrie planétaire (indépendante du Kin) — cf. RUNTIME/KIN.news.sh §8 ─
SYNASTRY_TRIGGER_SCRIPT = REPO_ROOT / "RUNTIME" / "KIN.news.sh"
SYNASTRY_LOCK_FILE = settings.ZEN_PATH / "tmp" / "kin_synastry_trigger.pid"
SYNASTRY_LOG_FILE = settings.ZEN_PATH / "tmp" / "kin_synastry_trigger.log"

sys.path.insert(0, str(_IA_PATH))
try:
    import arbor_config as _arbor_config  # stdlib seule — jamais bro.*/question.py (trop lourd pour ce process)
except Exception:
    _arbor_config = None
try:
    # bro.media reste raisonnablement léger (bro._shared/bro.nostr/bro.watch_store,
    # pas de chaîne question.py/ollama) — importable au niveau module comme arbor_config.
    from bro.media import list_station_scrapers as _list_station_scrapers
    from bro.media import _available_scraper_domains
except Exception:
    _list_station_scrapers = None
    _available_scraper_domains = None


def _cookies_without_scraper() -> list:
    """Domaines pour lesquels au moins un compte local a déposé un cookie
    réel (fichier en clair présent, cf. bro.media._available_scraper_domains)
    mais dont AUCUN scraper n'existe encore sur cette station — le "smart
    contract" documenté (dépôt cookie → notification capitaine) reste alors
    en attente indéfiniment tant que personne (capitaine ou ARBOR) ne code le
    scraper. Trié par ancienneté de notification décroissante (le plus
    ancien signalé en premier)."""
    if not (_list_station_scrapers and _available_scraper_domains):
        return []
    from services.memory_status import list_multipass_emails
    covered = {s["domain"] for s in _list_station_scrapers()}
    out = []
    for email in list_multipass_emails():
        for domain in _available_scraper_domains(email):
            if domain in covered:
                continue
            notified_since_days = None
            marker = settings.GAME_PATH / "nostr" / email / f".{domain}_notified"
            if marker.is_file():
                try:
                    notified_since_days = int((time.time() - marker.stat().st_mtime) // 86400)
                except Exception:
                    pass
            out.append({"email": email, "domain": domain, "notified_since_days": notified_since_days})
    out.sort(key=lambda e: e["notified_since_days"] if e["notified_since_days"] is not None else -1, reverse=True)
    return out


def _claude_cli_status() -> dict:
    """État du CLI claude — nécessaire au mode ARBOR 'forge' (génération de
    code, cf. arbor_tool_forge.py::claude_available()/_ask_claude). Lecture
    disque uniquement (aucun appel réseau) — reflète la même logique que
    claude.vscodium.setup.sh::cmd_status() : ~/.claude est un symlink vers
    ~/.claude-{slug}/, authentifié si .credentials* ou .claude.json non vide
    y sont présents. L'authentification RÉELLE n'est vérifiée qu'à l'exécution
    (une session expirée échouera à l'appel, message clair dans le log).

    shutil.which() seul respecte le PATH du process COURANT — insuffisant ici
    car ce process tourne typiquement sous un service systemd dont le PATH
    est restreint (constaté en pratique : ~/.local/bin absent, là où `claude`
    est installé). arbor_run.sh ajoute ~/.local/bin à son propre PATH avant
    d'exécuter arbor_tool_forge.py — ce contrôle vérifie explicitement le même
    emplacement pour que le statut affiché reflète ce que le run réel verra."""
    available = (
        shutil.which("claude") is not None
        or (Path.home() / ".local" / "bin" / "claude").exists()
    )
    claude_dir = Path.home() / ".claude"
    authenticated = False
    account = None
    target = None
    try:
        if claude_dir.is_symlink():
            target = Path(os.readlink(claude_dir))
            if target.name.startswith(".claude-"):
                account = target.name[len(".claude-"):]
        elif claude_dir.is_dir():
            target = claude_dir
        if target and target.is_dir():
            has_creds = any(target.glob(".credentials*"))
            claude_json = target / ".claude.json"
            authenticated = has_creds or (claude_json.is_file() and claude_json.stat().st_size > 0)
    except Exception:
        pass
    return {"available": available, "authenticated": authenticated, "account": account}


def _serialize_schema(raw_config: dict, only_category: Optional[str] = None) -> list:
    """Sérialise COOP_CONFIG_SCHEMA + l'état courant (raw_config, déjà chargé
    en un seul coop_load_raw()) — valeur masquée INCONDITIONNELLEMENT si la
    clé est sensible, même si elle est stockée en clair (cas d'un ancien
    --no-encrypt) : dans ce cas encrypted=False signale juste l'anomalie,
    sans jamais exposer la valeur elle-même."""
    categories = []
    for cat in COOP_CONFIG_SCHEMA:
        if only_category and cat.id != only_category:
            continue
        keys_out = []
        for k in cat.keys:
            raw_value = raw_config.get(k.key)
            defined = raw_value is not None and raw_value != ""
            sensitive = is_sensitive(k.key)
            encrypted = bool(defined and isinstance(raw_value, str) and _ENCRYPTED_RE.match(raw_value))
            keys_out.append({
                "key": k.key, "label": k.label, "help": k.help, "kind": k.kind,
                "choices": list(k.choices), "unit": k.unit, "placeholder": k.placeholder,
                "readonly": k.readonly, "sensitive": sensitive, "defined": defined,
                "encrypted": encrypted if sensitive else None,
                "value": "" if sensitive else (raw_value or ""),
            })
        categories.append({"id": cat.id, "icon": cat.icon, "title": cat.title,
                            "note": cat.note, "keys": keys_out})
    return categories


@router.get("/api/nostr/admin/node_config")
async def get_node_config(request: Request, uplanetname: Optional[str] = None):
    """Config coopérative en whitelist stricte — valeurs masquées si sensibles.
    Auth signature NIP-98 du Capitaine."""
    await _check_admin_auth(request, uplanetname)
    raw = await coop_load_raw()
    return JSONResponse({"categories": _serialize_schema(raw)})


@router.post("/api/nostr/admin/node_config")
async def post_node_config(request: Request):
    """Écrit une ou plusieurs clés whitelistées. Body JSON :
    {uplanetname?, key, value} ou {uplanetname?, entries:[{key,value},...]}
    (le singulier est normalisé vers entries — un seul chemin de code).
    Auth : require_captain_signature (NIP-98 exclusif) si le lot contient une
    clé sensible, sinon check_admin_auth standard (UPLANETNAME accepté)."""
    body = await safe_json_body(request)
    uplanetname = body.get("uplanetname", "")
    entries = body.get("entries")
    if entries is None:
        key = body.get("key", "")
        entries = [{"key": key, "value": body.get("value")}] if key else []
    if not entries:
        raise HTTPException(status_code=400, detail="Aucune entrée à écrire.")

    unknown = [e.get("key") for e in entries if e.get("key") not in ALL_KEYS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Clé(s) hors whitelist : {', '.join(map(str, unknown))}.",
        )

    readonly = [e["key"] for e in entries if ALL_KEYS[e["key"]].readonly]
    if readonly:
        raise HTTPException(status_code=400, detail=f"Clé(s) en lecture seule : {', '.join(readonly)}.")

    pubkey = None
    if any(is_sensitive(e["key"]) for e in entries):
        pubkey = await require_captain_signature(request)
    else:
        await _check_admin_auth(request, uplanetname)

    try:
        validated = [(e["key"], validate_value(ALL_KEYS[e["key"]], e.get("value"))) for e in entries]
    except CoopConfigError as ex:
        raise HTTPException(status_code=400, detail=str(ex))

    try:
        await coop_set_many(validated)
    except CoopConfigError as ex:
        raise HTTPException(status_code=500, detail=str(ex))

    keys_written = [k for k, _ in validated]
    logger.info(f"Admin node_config set: {keys_written} (captain={(pubkey or 'uplanetname')[:16]})")
    return JSONResponse({"ok": True, "keys": keys_written})


@router.post("/api/nostr/admin/node_config/delete")
async def post_node_config_delete(request: Request):
    """Supprime une clé whitelistée — require_captain_signature UNIQUEMENT
    (jamais le repli UPLANETNAME) : une suppression est plus difficile à
    auditer qu'une écriture, réservée à une preuve de clé privée individuelle."""
    body = await safe_json_body(request)
    key = body.get("key", "")
    if key not in ALL_KEYS:
        raise HTTPException(status_code=400, detail=f"Clé hors whitelist : {key}")
    if ALL_KEYS[key].readonly:
        raise HTTPException(status_code=400, detail=f"Clé en lecture seule : {key}")

    pubkey = await require_captain_signature(request)
    try:
        await coop_delete(key)
    except CoopConfigError as ex:
        raise HTTPException(status_code=500, detail=str(ex))

    logger.info(f"Admin node_config delete: {key} (captain={pubkey[:16]})")
    return JSONResponse({"ok": True, "key": key})


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_audit_tail(path: Path, limit: int = 20) -> list:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _arbor_branches() -> list:
    """Branches arbor/* en attente de revue — distingue un worktree encore
    présent (chemin /tmp, disparaît au reboot) d'une branche persistante sans
    worktree associé, et signale si une branche est déjà mergée dans master
    (proposée au nettoyage plutôt qu'à la revue)."""
    if not REPO_ROOT.is_dir():
        return []
    try:
        refs = subprocess.run(
            ["git", "for-each-ref", "--sort=-committerdate",
             "--format=%(refname:short)%09%(committerdate:unix)%09%(subject)", "refs/heads/arbor/"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15,
        )
        worktrees = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return []

    worktree_branches = set()
    for block in (worktrees.stdout or "").split("\n\n"):
        if "branch refs/heads/" in block:
            b = block.split("branch refs/heads/", 1)[1].splitlines()[0].strip()
            worktree_branches.add(b)

    branches = []
    for line in (refs.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, committed_at, subject = parts
        try:
            merged = subprocess.run(
                ["git", "merge-base", "--is-ancestor", name, "master"],
                cwd=str(REPO_ROOT), capture_output=True, timeout=10,
            ).returncode == 0
        except Exception:
            merged = False
        try:
            ahead_proc = subprocess.run(
                ["git", "rev-list", "--count", f"master..{name}"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10,
            )
            ahead = ahead_proc.stdout.strip() or "?"
        except Exception:
            ahead = "?"
        branches.append({
            "branch": name,
            "committed_at": int(committed_at) if committed_at.isdigit() else None,
            "subject": subject,
            "worktree_present": name in worktree_branches,
            "already_merged": merged,
            "commits_ahead": ahead,
            "review_command": f"cd {REPO_ROOT} && git diff master...{name}",
        })
    return branches


@router.get("/api/nostr/admin/arbor_status")
async def get_arbor_status(request: Request, uplanetname: Optional[str] = None, log: int = 0):
    """Statut ARBOR : seuils effectifs (même moteur de rendu que ⚙️ NODE),
    branches en attente de revue, derniers runs (audit), état du run en
    cours. Lecture seule — auth signature NIP-98 du Capitaine."""
    await _check_admin_auth(request, uplanetname)
    raw = await coop_load_raw()
    state = _read_json_file(ARBOR_STATE_FILE)
    audit = _read_audit_tail(ARBOR_AUDIT_FILE, limit=20)
    branches = _arbor_branches()

    result = {
        "thresholds": _serialize_schema(raw, only_category="arbor"),
        "remote_trigger_enabled": str(raw.get("ARBOR_REMOTE_TRIGGER_ENABLED", "")).strip().lower() == "true",
        "cooldown_min": raw.get("ARBOR_TRIGGER_COOLDOWN_MIN") or "30",
        "state": state,
        "audit": list(reversed(audit)),
        "branches": branches,
        "claude_cli": _claude_cli_status(),
        "cookies_without_scraper": _cookies_without_scraper(),
    }
    # Journal du dernier run (en cours OU terminé) toujours renvoyé : l'UI
    # l'affiche en direct pendant le run et garde le diagnostic après un échec.
    if state.get("log"):
        log_path = Path(state["log"])
        result["log_tail"] = ""
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                result["log_tail"] = "\n".join(lines[-200:])
                result["log_lines"] = len(lines)
            except Exception:
                pass
    return JSONResponse(result)


# ── STATION — état en direct + journaux (lecture seule) ─────────────────────
# Complète 12345.json (instantané mis en cache par _12345.sh) par des mesures
# prises à l'appel : services systemd, disque, mémoire, charge. Aucune action
# n'est exposée ici (pas de restart HTTP) : la console affiche la commande à copier.
_STATION_UNITS = ("astroport", "upassport", "ipfs", "strfry", "docker")
_STATION_FILE_LOGS = {
    "file:54321": ("UPassport (54321.log)", "54321.log"),
    "file:IA": ("IA (IA.log)", "IA.log"),
    "file:multipass": ("MULTIPASS refresh", "MULTIPASS.refresh.log"),
    "file:bro_dm": ("BRO DM daemon", "bro_dm_daemon.log"),
    "file:mailjet": ("Mailjet", "mailjet.log"),
    "file:purge": ("Purge étrangers", "purge_nostr_strangers.log"),
    "file:destroy": ("Destruction MULTIPASS", "nostr_destroy_tw.log"),
    "file:synastry": ("Synastrie", "kin_synastry_trigger.log"),
}
STATION_LOG_MAX_LINES = 1000


def _station_own_json() -> dict:
    cands = [p for p in (settings.ZEN_PATH / "tmp").glob("*/12345.json") if p.is_file()]
    if not cands:
        return {}
    data = _read_json_file(max(cands, key=lambda p: p.stat().st_mtime))
    data.pop("boots", None)
    return data


def _station_swarm() -> list:
    out = []
    swarm_dir = settings.ZEN_PATH / "tmp" / "swarm"
    if not swarm_dir.is_dir():
        return out
    for f in sorted(swarm_dir.glob("*/12345.json")):
        d = _read_json_file(f)
        cap = d.get("capacities") or {}
        out.append({
            "id": f.parent.name, "hostname": d.get("hostname"), "captain": d.get("captain"),
            "city": d.get("IPCity"), "power_score": cap.get("power_score"), "tier": cap.get("provider_tier"),
            "arbor_eligible": cap.get("arbor_eligible"), "date": d.get("date"), "created": d.get("created"),
            "astroport": d.get("astroport"),
        })
    return out


def _station_live() -> dict:
    services = []
    for unit in _STATION_UNITS:
        try:
            r = subprocess.run(["systemctl", "show", f"{unit}.service", "-p", "LoadState", "-p", "ActiveState",
                                "-p", "SubState", "-p", "ActiveEnterTimestamp"],
                               capture_output=True, text=True, timeout=5)
            kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        except Exception as e:
            kv = {"LoadState": "error", "ActiveState": str(e)[:60]}
        if kv.get("LoadState") == "not-found":
            continue
        services.append({"unit": unit, "active": kv.get("ActiveState") == "active", "state": kv.get("ActiveState"),
                         "sub": kv.get("SubState"), "since": kv.get("ActiveEnterTimestamp", "")})
    disks = []
    for mount in ("/", str(Path.home() / ".ipfs")):
        try:
            du = shutil.disk_usage(mount)
            disks.append({"mount": mount, "total": du.total, "used": du.used, "free": du.free,
                          "pct": round(du.used * 100 / du.total, 1)})
        except Exception:
            pass
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                mem[k] = int(v.strip().split()[0]) * 1024
    except Exception:
        pass
    try:
        load = list(os.getloadavg())
    except Exception:
        load = []
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        uptime = None
    return {"services": services, "disks": disks, "mem": mem, "load": load, "cpu_count": os.cpu_count(),
            "uptime_s": uptime, "now": int(time.time())}


def _station_log_sources() -> list:
    tmp = settings.ZEN_PATH / "tmp"
    src = [{"key": f"journal:{u}", "label": f"journal · {u}", "kind": "journal"} for u in _STATION_UNITS]
    for key, (label, fname) in _STATION_FILE_LOGS.items():
        f = tmp / fname
        if f.is_file():
            src.append({"key": key, "label": label, "kind": "file", "size": f.stat().st_size})
    runs = sorted(tmp.glob("arbor_run.*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if runs:
        src.append({"key": "file:arbor_last", "label": "ARBOR · dernier run", "kind": "file", "size": runs[0].stat().st_size})
    return src


def _station_read_log(source: str, lines: int, level: str) -> dict:
    lines = max(10, min(int(lines), STATION_LOG_MAX_LINES))
    tmp = settings.ZEN_PATH / "tmp"
    if source.startswith("journal:"):
        unit = source.split(":", 1)[1]
        if unit not in _STATION_UNITS:
            raise HTTPException(status_code=400, detail="Source inconnue.")
        cmd = ["journalctl", "-u", f"{unit}.service", "-n", str(lines), "--no-pager", "-o", "short-iso"]
        if level == "warn":
            cmd += ["-p", "warning"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0 and not r.stdout:
            raise HTTPException(status_code=500, detail=(r.stderr or "journalctl indisponible").strip()[:300])
        return {"source": source, "text": r.stdout}
    if source == "file:arbor_last":
        runs = sorted(tmp.glob("arbor_run.*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = runs[0] if runs else None
    elif source in _STATION_FILE_LOGS:
        path = tmp / _STATION_FILE_LOGS[source][1]
    else:
        raise HTTPException(status_code=400, detail="Source inconnue.")
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Journal absent.")
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - 400_000))
        chunk = f.read().decode("utf-8", errors="replace")
    out = chunk.splitlines()[-lines:]
    if level == "warn":
        out = [l for l in out if re.search(r"error|warn|erreur|échec|fail|❌|⚠️|traceback", l, re.I)]
    return {"source": source, "text": "\n".join(out)}


@router.get("/api/nostr/admin/station_live")
async def get_station_live(request: Request, uplanetname: Optional[str] = None):
    """État de la station pour la console Capitaine : 12345.json (instantané),
    mesures en direct (services systemd, disque, mémoire, charge), essaim connu
    et sources de journaux disponibles. Lecture seule — NIP-98."""
    await _check_admin_auth(request, uplanetname)
    live = await asyncio.to_thread(_station_live)
    return JSONResponse({
        "station": _station_own_json(), "live": live,
        "swarm": _station_swarm(), "log_sources": _station_log_sources(),
    })


@router.get("/api/nostr/admin/station_log")
async def get_station_log(request: Request, source: str, lines: int = 200, level: str = "all",
                          uplanetname: Optional[str] = None):
    """Fin de journal (lecture seule, sources en liste blanche : unités systemd
    de la station et journaux ~/.zen/tmp connus). level=warn : erreurs/alertes."""
    await _check_admin_auth(request, uplanetname)
    return JSONResponse(await asyncio.to_thread(_station_read_log, source, lines, level if level in ("all", "warn") else "all"))


# ── TUNNELS IPFS P2P (DRAGONS & TUNNELS) ────────────────────────────────────
# Lecture : `ipfs p2p ls` (sens déterminé par la 2e colonne : /p2p/<soi> =
# service PUBLIÉ par cette station ; /ip4|/ip6/… = tunnel CLIENT vers un autre
# nœud), ~/.zen/tunnels/enabled (tunnels persistants, watchdog 20h12) et les
# x_<service>.sh du swarm (services proposés par les autres stations).
# Écriture : UNIQUEMENT via astrosystemctl (jamais de commande ipfs libre),
# arguments validés (service/nœud en liste blanche), NIP-98 Capitaine exclusif.
_TUNNEL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
_TUNNEL_NODE_RE = re.compile(r"^12D3KooW[A-Za-z0-9]{30,60}$")
_TUNNEL_ACTIONS = ("connect", "enable", "disable", "close", "share", "hide")
ASTROSYSTEMCTL_SH = REPO_ROOT / "admin" / "system" / "astrosystemctl.sh"
TUNNELS_ENABLED_DIR = Path.home() / ".zen" / "tunnels" / "enabled"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _tunnel_env():
    env = dict(os.environ)
    env["PATH"] = f"{Path.home()}/.local/bin:/usr/local/bin:" + env.get("PATH", "")
    return env


def _own_node_id() -> str:
    own = _station_own_json()
    return own.get("ipfsnodeid") or ""


def _p2p_ls() -> list:
    """[{proto, a, b}] — 3 colonnes de `ipfs p2p ls`. Erreur → exception HTTP 503."""
    try:
        r = subprocess.run(["ipfs", "p2p", "ls"], capture_output=True, text=True, timeout=10, env=_tunnel_env())
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"ipfs injoignable : {e}")
    if r.returncode != 0:
        raise HTTPException(status_code=503, detail=(r.stderr or "ipfs p2p ls a échoué").strip()[:300])
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("/x/"):
            out.append({"proto": parts[0], "a": parts[1], "b": parts[2]})
    return out


def _private_services() -> list:
    env = REPO_ROOT / ".env"
    try:
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r'^DRAGON_PRIVATE_SERVICES=["\']?([^"\']*)', line)
            if m:
                return m.group(1).split()
    except Exception:
        pass
    return []


def _tcp_open(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.6):
            return True
    except Exception:
        return False


def _tunnels_state() -> dict:
    own = _own_node_id()
    private = _private_services()
    published, consumed = {}, {}
    for e in _p2p_ls():
        slug_node = e["proto"][3:]
        if e["a"].startswith("/p2p/"):
            slug = slug_node.rsplit("-12D3", 1)[0]
            m = re.search(r"tcp/(\d+)", e["b"])
            published[e["proto"]] = {"slug": slug, "proto": e["proto"], "port": int(m.group(1)) if m else None,
                                     "private": slug in private}
        else:
            remote = e["b"].replace("/p2p/", "")
            slug = slug_node[: -(len(remote) + 1)] if slug_node.endswith("-" + remote) else slug_node
            m = re.search(r"tcp/(\d+)", e["a"])
            c = consumed.setdefault(e["proto"], {"slug": slug, "proto": e["proto"], "remote": remote, "lports": []})
            if m:
                c["lports"].append(int(m.group(1)))
    for c in consumed.values():
        c["lports"] = sorted(set(c["lports"]))
        c["reachable"] = any(_tcp_open(p) for p in c["lports"])
    persistent = []
    if TUNNELS_ENABLED_DIR.is_dir():
        for f in sorted(TUNNELS_ENABLED_DIR.glob("x_*.sh")):
            m = re.match(r"^x_(.+?)[_.](12D3KooW[A-Za-z0-9]+)\.sh$", f.name)
            if m:
                persistent.append({"service": m.group(1), "node_id": m.group(2), "file": f.name})
    consumed_keys = {(c["slug"], c["remote"]) for c in consumed.values()}
    persistent_keys = {(x["service"], x["node_id"]) for x in persistent}
    available = []
    swarm_dir = settings.ZEN_PATH / "tmp" / "swarm"
    if swarm_dir.is_dir():
        for nd in sorted(swarm_dir.iterdir()):
            if not nd.is_dir() or not _TUNNEL_NODE_RE.match(nd.name):
                continue
            j = _read_json_file(nd / "12345.json")
            cap = j.get("capacities") or {}
            for xs in sorted(nd.glob("x_*.sh")):
                svc = xs.stem[2:]
                try:
                    pm = re.search(r'NATIVE_PORT="(\d+)"', xs.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    pm = None
                available.append({
                    "service": svc, "node_id": nd.name, "hostname": j.get("hostname"), "captain": j.get("captain"),
                    "power_score": cap.get("power_score"), "tier": cap.get("provider_tier"), "port": int(pm.group(1)) if pm else None,
                    "connected": (svc, nd.name) in consumed_keys, "persistent": (svc, nd.name) in persistent_keys,
                })
    return {"own_id": own, "published": list(published.values()), "consumed": list(consumed.values()),
            "persistent": persistent, "available": available, "private": private}


def _tunnel_action(action: str, service: str, node_id: str) -> dict:
    env = _tunnel_env()
    if action == "close":
        # tunnel client non persistant : protocole exact lu dans `ipfs p2p ls`, jamais fourni tel quel
        target = f"/x/{service}-{node_id}"
        if not any(e["proto"] == target and not e["a"].startswith("/p2p/") for e in _p2p_ls()):
            raise HTTPException(status_code=404, detail="Tunnel client introuvable.")
        r = subprocess.run(["ipfs", "p2p", "close", "-p", target], capture_output=True, text=True, timeout=20, env=env)
        return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()[-600:]}
    if action in ("connect", "enable", "disable"):
        arg = service if action == "disable" else f"{service}@{node_id}"
        cmd = [str(ASTROSYSTEMCTL_SH), action, arg]
    else:  # share / hide : DRAGON_PRIVATE_SERVICES
        cmd = [str(ASTROSYSTEMCTL_SH), "local", action, service]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env, stdin=subprocess.DEVNULL)
    out = _ANSI_RE.sub("", (r.stdout + r.stderr)).strip()
    failed = r.returncode != 0 or out.lstrip().startswith("❌") or "\n❌" in out
    return {"ok": not failed, "output": out[-1500:]}


@router.get("/api/nostr/admin/tunnels")
async def get_tunnels(request: Request, uplanetname: Optional[str] = None):
    """Tunnels IPFS P2P de la station : services publiés, tunnels clients ouverts
    (avec test TCP du port local), tunnels persistants (watchdog) et services
    disponibles chez les autres stations. Lecture seule — NIP-98."""
    await _check_admin_auth(request, uplanetname)
    return JSONResponse(await asyncio.to_thread(_tunnels_state))


@router.post("/api/nostr/admin/tunnel_action")
async def post_tunnel_action(request: Request):
    """Pilote un tunnel via astrosystemctl. Body : {action, service, node_id?}.
    connect/enable (service+node_id du swarm), disable/close, share/hide (publication
    du service local au swarm). NIP-98 Capitaine EXCLUSIF."""
    body = await safe_json_body(request)
    action = (body.get("action") or "").strip()
    service = (body.get("service") or "").strip()
    node_id = (body.get("node_id") or "").strip()
    pubkey = await require_captain_signature(request)
    if action not in _TUNNEL_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action invalide (attendu : {', '.join(_TUNNEL_ACTIONS)})")
    if not _TUNNEL_SLUG_RE.match(service):
        raise HTTPException(status_code=400, detail="service invalide.")
    if action in ("connect", "enable", "close"):
        if not _TUNNEL_NODE_RE.match(node_id):
            raise HTTPException(status_code=400, detail="node_id invalide.")
        if action != "close" and not (settings.ZEN_PATH / "tmp" / "swarm" / node_id).is_dir():
            raise HTTPException(status_code=404, detail="Nœud absent du swarm.")
    if not ASTROSYSTEMCTL_SH.is_file():
        raise HTTPException(status_code=500, detail="astrosystemctl.sh introuvable sur cette station.")
    result = await asyncio.to_thread(_tunnel_action, action, service, node_id)
    _arbor_audit("tunnel", mode=f"tunnel-{action}", service=service, node_id=node_id, rc=0 if result["ok"] else 1, captain_hex=pubkey)
    logger.info(f"Admin tunnel_action: {action} {service} {node_id[:14]} ok={result['ok']} captain={pubkey[:16]}")
    return JSONResponse(result, status_code=200 if result["ok"] else 422)


# ── DOCKER — conteneurs et piles (compose) : voir, arrêter, démarrer ─────────
# Lecture : `docker ps -a` regroupé par projet compose (une « pile » = Dify, Open WebUI…) et
# consommation (`docker stats`, à la demande). Écriture : stop | start | restart d'UN conteneur ou
# d'UNE pile, NIP-98 Capitaine exclusif ; la cible doit exister dans `docker ps -a` (jamais de nom
# ou de commande libre) ; aucune suppression, aucun `docker run` : rien n'est détruit, tout se redémarre.
_DOCKER_ACTIONS = ("stop", "start", "restart")
_DOCKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
_DOCKER_FMT = "{{.ID}}|{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}|{{.Ports}}|{{.Label \"com.docker.compose.project\"}}|{{.Label \"com.docker.compose.service\"}}|{{.Label \"com.docker.compose.project.working_dir\"}}"


def _docker_app(c: dict) -> dict:
    """Application (« pile ») d'un conteneur : le projet compose seul est trompeur — ici Dify ET le Qdrant
    d'Astroport partagent le projet « docker » ; arrêter ce projet entier couperait la mémoire de BRO."""
    name, image, project = c["name"], c["image"], c["project"]
    if name == "astroport-qdrant" or ("qdrant" in image and project):
        return {"app": "qdrant", "label": "Qdrant — mémoire vectorielle", "critical": True,
                "impact": "Mémoire de BRO, recherche sémantique et reconnaissance des visages de FaceCloud"}
    workdir = c.get("workdir", "")
    if project == "dify" or "/dify/" in workdir + "/" or (project == "docker" and name.startswith("docker-")):
        return {"app": "dify", "label": "Dify — workflows IA", "critical": False,
                "impact": "Les workflows Dify (aucun autre service de la station n'en dépend)"}
    if project == "ai-company" or "open-webui" in image:
        return {"app": "open-webui", "label": "Open WebUI — chat IA", "critical": False,
                "impact": "L'interface de chat Open WebUI"}
    if "bgutil" in name or "bgutil" in image:
        return {"app": "bgutil", "label": "Jetons YouTube (yt-dlp)", "critical": False,
                "impact": "Les téléchargements YouTube de la station"}
    key = project or name
    return {"app": key, "label": key, "critical": False, "impact": ""}


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, env=_tunnel_env())


def _docker_list() -> list:
    r = _docker("ps", "-a", "--format", _DOCKER_FMT, timeout=20)
    if r.returncode != 0:
        raise HTTPException(status_code=503, detail=("Docker indisponible : " + (r.stderr or "").strip())[:300])
    out = []
    for line in r.stdout.splitlines():
        f = line.split("|")
        if len(f) < 8:
            continue
        c = {"id": f[0], "name": f[1], "image": f[2], "state": f[3], "status": f[4], "ports": f[5],
             "project": f[6], "service": f[7], "workdir": f[8] if len(f) > 8 else ""}
        c.update(_docker_app(c))
        out.append(c)
    return out


def _docker_stats() -> dict:
    r = _docker("stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}", timeout=40)
    out = {}
    for line in r.stdout.splitlines():
        f = line.split("|")
        if len(f) == 4:
            out[f[0]] = {"cpu": f[1], "mem": f[2].split("/")[0].strip(), "mem_pct": f[3]}
    return out


def _docker_act(action: str, kind: str, target: str) -> dict:
    items = _docker_list()
    if kind == "app":
        ids = [c["id"] for c in items if c["app"] == target]
    else:
        ids = [c["id"] for c in items if c["name"] == target]
    if not ids:
        raise HTTPException(status_code=404, detail="Cible introuvable parmi les conteneurs Docker.")
    # on n'agit que sur ce qui a un sens : arrêter ce qui tourne, démarrer ce qui est arrêté
    byid = {c["id"]: c for c in items}
    if action == "stop":
        ids = [i for i in ids if byid[i]["state"] == "running"]
    elif action == "start":
        # « created » = jamais démarré (ex. conteneur de modèle) : on ne le démarre pas par un geste de pile
        ids = [i for i in ids if byid[i]["state"] in ("exited", "paused", "dead")]
    if not ids:
        return {"ok": True, "changed": 0, "output": "Rien à faire : déjà dans l'état demandé."}
    args = [action] + (["-t", "20"] if action in ("stop", "restart") else []) + ids
    r = _docker(*args, timeout=180)
    return {"ok": r.returncode == 0, "changed": len(ids), "output": ((r.stdout or "") + (r.stderr or "")).strip()[-600:]}


@router.get("/api/nostr/admin/docker_list")
async def get_docker_list(request: Request, uplanetname: Optional[str] = None):
    """Conteneurs Docker de la station (lecture seule), avec leur projet compose. NIP-98 Capitaine."""
    await _check_admin_auth(request, uplanetname)
    return JSONResponse({"containers": await asyncio.to_thread(_docker_list)})


@router.get("/api/nostr/admin/docker_stats")
async def get_docker_stats(request: Request, uplanetname: Optional[str] = None):
    """Consommation instantanée (CPU, mémoire) par conteneur actif — ~2 s : appelée à la demande."""
    await _check_admin_auth(request, uplanetname)
    return JSONResponse({"stats": await asyncio.to_thread(_docker_stats)})


@router.post("/api/nostr/admin/docker_action")
async def post_docker_action(request: Request):
    """Body : {action: stop|start|restart, kind: "container"|"app", target}. NIP-98 Capitaine EXCLUSIF.
    `app` = application calculée par _docker_app (Dify, Open WebUI, Qdrant…), jamais le projet compose brut."""
    body = await safe_json_body(request)
    action = (body.get("action") or "").strip()
    kind = (body.get("kind") or "container").strip()
    target = (body.get("target") or "").strip()
    pubkey = await require_captain_signature(request)
    if action not in _DOCKER_ACTIONS or kind not in ("container", "app") or not _DOCKER_NAME_RE.match(target):
        raise HTTPException(status_code=400, detail="action, type ou cible invalide.")
    result = await asyncio.to_thread(_docker_act, action, kind, target)
    _arbor_audit("docker", mode=f"docker-{action}", kind=kind, target=target, rc=0 if result["ok"] else 1, captain_hex=pubkey)
    logger.info(f"Admin docker_action: {action} {kind} {target} changed={result['changed']} ok={result['ok']} captain={pubkey[:16]}")
    return JSONResponse(result, status_code=200 if result["ok"] else 422)


# ── QDRANT — cartographie des collections (lecture seule) ───────────────────
# Vue d'ensemble (collections, familles, propriétaires MULTIPASS) et projection
# 2D (ACP) d'un échantillon de vecteurs pour la représentation graphique de
# nostr_admin. CONFIDENTIALITÉ : les collections personnelles (memory_*, faces_*)
# contiennent des conversations et des visages de membres — le Capitaine voit leur
# structure, leur volume et la forme du nuage de points, JAMAIS leur contenu.
_QDRANT_BASE = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
_QDRANT_PRIVATE_PREFIXES = ("memory_", "faces_")
_QDRANT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
QDRANT_PROJECTION_MAX = 500


def _qdrant_headers() -> dict:
    try:
        for line in (Path.home() / ".zen" / "ai-company" / ".env").read_text().splitlines():
            if line.startswith("QDRANT_API_KEY="):
                return {"api-key": line.split("=", 1)[1].strip()}
    except Exception:
        pass
    return {}


def _qdrant_get(path: str):
    import httpx
    try:
        r = httpx.get(_QDRANT_BASE + path, headers=_qdrant_headers(), timeout=15)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Qdrant injoignable : {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Qdrant a répondu {r.status_code}")
    return r.json()


def _qdrant_family(name: str) -> str:
    for pre, fam in (("memory_", "memory"), ("faces_", "faces")):
        if name.startswith(pre):
            return fam
    if name.startswith("veille_"):
        return "veille"
    return "system"


def _hex_owners() -> dict:
    """prefixe/hex NOSTR → email, depuis ~/.zen/game/nostr/*/HEX (comptes de cette station)."""
    owners = {}
    base = settings.GAME_PATH / "nostr"
    if base.is_dir():
        for d in base.glob("*@*"):
            try:
                h = (d / "HEX").read_text().strip()
            except Exception:
                continue
            if len(h) == 64:
                owners[h] = d.name
    return owners


def _qdrant_overview() -> dict:
    cols = _qdrant_get("/collections")["result"]["collections"]
    owners = _hex_owners()
    out = []
    for c in cols:
        name = c["name"]
        try:
            i = _qdrant_get(f"/collections/{name}")["result"]
        except HTTPException:
            out.append({"name": name, "family": _qdrant_family(name), "error": True})
            continue
        vec = i["config"]["params"]["vectors"]
        dim, dist = (vec.get("size"), vec.get("distance")) if "size" in vec else (None, None)
        if dim is None and isinstance(vec, dict):  # vecteurs nommés
            first = next(iter(vec.values()), {})
            dim, dist = first.get("size"), first.get("distance")
        owner_hex = owner_email = None
        suffix = name.split("_", 1)[1] if "_" in name else ""
        if _qdrant_family(name) in ("memory", "faces") and re.fullmatch(r"[0-9a-f]{16,64}", suffix):
            for h, em in owners.items():
                if h.startswith(suffix):
                    owner_hex, owner_email = h, em
                    break
        out.append({
            "name": name, "family": _qdrant_family(name), "points": i.get("points_count") or 0,
            "indexed": i.get("indexed_vectors_count") or 0, "segments": i.get("segments_count"),
            "status": i.get("status"), "dim": dim, "distance": dist,
            "private": name.startswith(_QDRANT_PRIVATE_PREFIXES), "owner_email": owner_email,
            "payload_keys": sorted((i.get("payload_schema") or {}).keys()),
        })
    try:
        version = _qdrant_get("/").get("version")
    except HTTPException:
        version = None
    return {"version": version, "collections": out, "total_points": sum(c.get("points", 0) for c in out)}


def _qdrant_project(name: str, limit: int, mine_hex: str = "") -> dict:
    import httpx
    import numpy as np
    names = {c["name"] for c in _qdrant_get("/collections")["result"]["collections"]}
    if name not in names or not _QDRANT_NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="Collection inconnue.")
    private = name.startswith(_QDRANT_PRIVATE_PREFIXES)
    limit = max(10, min(int(limit), QDRANT_PROJECTION_MAX))
    pts, offset = [], None
    while len(pts) < limit:
        body = {"limit": min(250, limit - len(pts)), "with_vector": True, "with_payload": not private}
        if offset is not None:
            body["offset"] = offset
        try:
            r = httpx.post(f"{_QDRANT_BASE}/collections/{name}/points/scroll", headers=_qdrant_headers(), json=body, timeout=30)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Qdrant injoignable : {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Qdrant a répondu {r.status_code}")
        res = r.json()["result"]
        pts += res.get("points", [])
        offset = res.get("next_page_offset")
        if offset is None:
            break
    mine_ids = set()
    if mine_hex and name == f"faces_{mine_hex}":
        # les visages du Capitaine lui-même : toujours présents dans le nuage, repérés par leur identifiant
        flt = {"must": [{"key": "pubkey", "match": {"value": mine_hex}}]}
        try:
            r = httpx.post(f"{_QDRANT_BASE}/collections/{name}/points/scroll", headers=_qdrant_headers(), timeout=30,
                           json={"limit": 150, "with_vector": True, "with_payload": False, "filter": flt})
            if r.status_code == 200:
                have = {str(p["id"]) for p in pts}
                for p in r.json()["result"].get("points", []):
                    mine_ids.add(str(p["id"]))
                    if str(p["id"]) not in have:
                        pts.append(p)
        except Exception:
            pass
        # ceux déjà tirés au hasard dans l'échantillon de base peuvent aussi être « moi » : un seul passage filtré
        try:
            ids = [p["id"] for p in pts]
            r = httpx.post(f"{_QDRANT_BASE}/collections/{name}/points/scroll", headers=_qdrant_headers(), timeout=30,
                           json={"limit": len(ids) or 1, "with_vector": False, "with_payload": False,
                                 "filter": {"must": [flt["must"][0], {"has_id": ids}]}})
            if r.status_code == 200:
                mine_ids |= {str(p["id"]) for p in r.json()["result"].get("points", [])}
        except Exception:
            pass
    vecs = [p["vector"] for p in pts if isinstance(p.get("vector"), list)]
    if len(vecs) < 3:
        return {"name": name, "private": private, "points": [], "variance": [0, 0], "sampled": len(vecs)}
    X = np.asarray(vecs, dtype=np.float32)
    X -= X.mean(axis=0)
    _, S, Vt = np.linalg.svd(X, full_matrices=False)
    xy = X @ Vt[:2].T
    centroid_xy = None
    if mine_ids:
        mv = np.asarray([p["vector"] for p in pts if str(p["id"]) in mine_ids and isinstance(p.get("vector"), list)], dtype=np.float32)
        if len(mv):
            centroid_xy = ((mv.mean(axis=0) - np.asarray(vecs, dtype=np.float32).mean(axis=0)) @ Vt[:2].T)
    var = (S ** 2) / max(1e-9, (S ** 2).sum())
    mx = float(np.abs(xy).max()) or 1.0
    xy = xy / mx
    if centroid_xy is not None:
        centroid_xy = centroid_xy / mx
    out = []
    for p, (x, y) in zip([p for p in pts if isinstance(p.get("vector"), list)], xy):
        item = {"id": str(p["id"]), "x": round(float(x), 4), "y": round(float(y), 4)}
        if str(p["id"]) in mine_ids:
            item["mine"] = True
        if not private:
            pl = p.get("payload") or {}
            item["label"] = next((str(pl[k])[:90] for k in ("title", "name", "source", "path", "file", "text", "content") if pl.get(k)), "")
            item["payload"] = {k: (v if isinstance(v, (int, float, bool)) else str(v)[:120]) for k, v in list(pl.items())[:8]}
        out.append(item)
    res = {"name": name, "private": private, "points": out, "variance": [round(float(var[0]), 3), round(float(var[1]), 3)], "sampled": len(out),
           "mine_count": len(mine_ids)}
    if centroid_xy is not None:
        res["centroid"] = {"x": round(float(centroid_xy[0]), 4), "y": round(float(centroid_xy[1]), 4)}
    return res


def _qdrant_my_face(hex_pub: str) -> dict:
    """Signature faciale du Capitaine CONNECTÉ : visages de SA clé dans SA collection faces_<hex>
    (jamais ceux des autres). Vecteur moyen (centroïde normalisé), cohésion et quelques vecteurs."""
    import httpx
    import numpy as np
    if not re.fullmatch(r"[0-9a-f]{64}", hex_pub or ""):
        raise HTTPException(status_code=400, detail="clé invalide.")
    name = f"faces_{hex_pub}"
    names = {c["name"] for c in _qdrant_get("/collections")["result"]["collections"]}
    if name not in names:
        return {"collection": name, "count": 0, "vectors": []}
    flt = {"must": [{"key": "pubkey", "match": {"value": hex_pub}}]}
    cnt = httpx.post(f"{_QDRANT_BASE}/collections/{name}/points/count", headers=_qdrant_headers(), json={"filter": flt, "exact": True}, timeout=20)
    total = cnt.json()["result"]["count"] if cnt.status_code == 200 else 0
    r = httpx.post(f"{_QDRANT_BASE}/collections/{name}/points/scroll", headers=_qdrant_headers(), timeout=30,
                   json={"limit": 300, "with_vector": True, "with_payload": ["timestamp"], "filter": flt})
    pts = r.json()["result"].get("points", []) if r.status_code == 200 else []
    vecs = [p["vector"] for p in pts if isinstance(p.get("vector"), list)]
    if not vecs:
        return {"collection": name, "count": total, "vectors": []}
    M = np.asarray(vecs, dtype=np.float32)
    c = M.mean(axis=0)
    norm = float(np.linalg.norm(c)) or 1.0
    cn = c / norm
    cos = (M / np.maximum(np.linalg.norm(M, axis=1, keepdims=True), 1e-9)) @ cn
    order = np.argsort(-cos)
    pick = [int(order[0]), int(order[len(order) // 2]), int(order[-1])] if len(order) >= 3 else list(range(len(order)))
    return {
        "collection": name, "count": total, "sampled": len(vecs), "dim": int(M.shape[1]),
        "centroid": [round(float(x), 4) for x in cn],
        "cohesion": round(float(cos.mean()), 3), "worst": round(float(cos.min()), 3),
        "vectors": [{"label": lab, "cos": round(float(cos[i]), 3), "timestamp": (pts[i].get("payload") or {}).get("timestamp", ""),
                     "vector": [round(float(x), 4) for x in M[i]]} for lab, i in zip(("le plus typique", "médian", "le plus atypique"), pick)],
    }


@router.get("/api/nostr/admin/qdrant_overview")
async def get_qdrant_overview(request: Request, uplanetname: Optional[str] = None):
    """Collections Qdrant (familles, volumes, configuration, propriétaire MULTIPASS
    des collections personnelles). Lecture seule — NIP-98 Capitaine."""
    await _check_admin_auth(request, uplanetname)
    return JSONResponse(await asyncio.to_thread(_qdrant_overview))


@router.get("/api/nostr/admin/qdrant_projection")
async def get_qdrant_projection(request: Request, name: str, limit: int = 300, uplanetname: Optional[str] = None):
    """Projection 2D (ACP) d'un échantillon de vecteurs. Collections personnelles
    (memory_*, faces_*) : coordonnées seules, aucun contenu. NIP-98 Capitaine."""
    pub = await require_captain_signature(request)
    return JSONResponse(await asyncio.to_thread(_qdrant_project, name, limit, pub.lower()))


@router.get("/api/nostr/admin/qdrant_my_face")
async def get_qdrant_my_face(request: Request):
    """Signature faciale (vecteur moyen, cohésion, quelques vecteurs) du Capitaine connecté : uniquement
    SA collection faces_<sa clé> et SES visages. NIP-98 Capitaine."""
    pub = await require_captain_signature(request)
    return JSONResponse(await asyncio.to_thread(_qdrant_my_face, pub.lower()))


# ── ARBOR — relecture / fusion / nettoyage des branches arbor/* ─────────────
# Seul endroit où une branche arbor/* peut être mergée : geste explicite du
# Capitaine (NIP-98 EXCLUSIF) après relecture du diff — arbor_run.sh et les
# scripts de forge, eux, ne mergent JAMAIS. Gardes : branche arbor/* existante,
# dépôt sur master, aucun fichier SUIVI modifié (le dépôt est le répertoire de
# travail du capitaine : un merge ne doit jamais s'y mêler à des modifications
# non commitées), conflit → merge --abort, jamais d'état à moitié fusionné.
_ARBOR_BRANCH_RE = re.compile(r"^arbor/[A-Za-z0-9._/-]{1,100}$")
_ARBOR_MAIN_BRANCH = "master"
ARBOR_DIFF_MAX_BYTES = 200_000


def _git(repo, *args, timeout=30):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=timeout)


def _arbor_check_branch(repo, branch):
    if not branch or not _ARBOR_BRANCH_RE.match(branch) or ".." in branch:
        raise HTTPException(status_code=400, detail="Nom de branche invalide (arbor/… attendu).")
    r = _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if r.returncode != 0:
        raise HTTPException(status_code=404, detail=f"Branche introuvable : {branch}")


def arbor_branch_diff(repo, branch):
    _arbor_check_branch(repo, branch)
    rng = f"{_ARBOR_MAIN_BRANCH}...{branch}"
    log = _git(repo, "log", "--format=%h%x09%an%x09%ad%x09%s", "--date=iso", f"{_ARBOR_MAIN_BRANCH}..{branch}")
    stat = _git(repo, "diff", "--stat", "--no-color", rng)
    patch = _git(repo, "diff", "--no-color", rng, timeout=60)
    text = patch.stdout or ""
    truncated = len(text.encode("utf-8", "replace")) > ARBOR_DIFF_MAX_BYTES
    if truncated:
        text = text.encode("utf-8", "replace")[:ARBOR_DIFF_MAX_BYTES].decode("utf-8", "ignore")
    merged = _git(repo, "merge-base", "--is-ancestor", branch, _ARBOR_MAIN_BRANCH).returncode == 0
    commits = []
    for line in (log.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return {"branch": branch, "already_merged": merged, "commits": commits,
            "stat": stat.stdout or "", "diff": text, "truncated": truncated}


def arbor_branch_merge(repo, branch):
    _arbor_check_branch(repo, branch)
    cur = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != _ARBOR_MAIN_BRANCH:
        raise HTTPException(status_code=409, detail=f"Le dépôt est sur « {cur} », pas sur {_ARBOR_MAIN_BRANCH} — fusion refusée.")
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        n = len(dirty.splitlines())
        raise HTTPException(status_code=409, detail=f"{n} fichier(s) suivi(s) modifié(s) dans le dépôt — commitez ou rangez-les avant de fusionner.")
    if _git(repo, "merge-base", "--is-ancestor", branch, _ARBOR_MAIN_BRANCH).returncode == 0:
        raise HTTPException(status_code=409, detail="Branche déjà fusionnée.")
    r = _git(repo, "merge", "--no-ff", "--no-edit", branch, timeout=120)
    if r.returncode != 0:
        _git(repo, "merge", "--abort")
        raise HTTPException(status_code=409, detail="Conflit ou échec de fusion (annulée, dépôt inchangé) : "
                            + ((r.stdout + r.stderr).strip()[-400:] or "?"))
    return {"merged": branch, "head": _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()}


def arbor_branch_delete(repo, branch):
    _arbor_check_branch(repo, branch)
    if _git(repo, "merge-base", "--is-ancestor", branch, _ARBOR_MAIN_BRANCH).returncode != 0:
        raise HTTPException(status_code=409, detail="Branche non fusionnée — suppression refusée (travail perdu).")
    wt = _git(repo, "worktree", "list", "--porcelain").stdout or ""
    for block in wt.split("\n\n"):
        if f"branch refs/heads/{branch}" in block:
            path = block.splitlines()[0].replace("worktree ", "", 1).strip()
            _git(repo, "worktree", "remove", "--force", path)
    r = _git(repo, "branch", "-d", branch)
    if r.returncode != 0:
        raise HTTPException(status_code=409, detail=(r.stderr or r.stdout).strip()[-300:] or "Suppression impossible.")
    return {"deleted": branch}


def _arbor_audit(event, **fields):
    try:
        import datetime
        ARBOR_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"event": event, "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
        with open(ARBOR_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


@router.get("/api/nostr/admin/arbor_branch_diff")
async def get_arbor_branch_diff(request: Request, branch: str, uplanetname: Optional[str] = None):
    """Relecture en LECTURE SEULE d'une branche arbor/* : commits, stat, diff
    (tronqué à 200 Ko). Auth NIP-98 du Capitaine."""
    await _check_admin_auth(request, uplanetname)
    return JSONResponse(await asyncio.to_thread(arbor_branch_diff, REPO_ROOT, branch))


@router.post("/api/nostr/admin/arbor_branch_merge")
async def post_arbor_branch_merge(request: Request):
    """Fusionne une branche arbor/* dans master — geste explicite du Capitaine
    (NIP-98 EXCLUSIF) après relecture. Body : {branch, confirm} ; `confirm`
    doit répéter la branche (confirmation tapée côté UI)."""
    body = await safe_json_body(request)
    branch = (body.get("branch") or "").strip()
    pubkey = await require_captain_signature(request)
    if (body.get("confirm") or "").strip() != branch:
        raise HTTPException(status_code=400, detail="Confirmation absente ou différente de la branche.")
    result = await asyncio.to_thread(arbor_branch_merge, REPO_ROOT, branch)
    _arbor_audit("merge", mode="merge", branch=branch, rc=0, captain_hex=pubkey)
    logger.info(f"Admin arbor_branch_merge: {branch} → {_ARBOR_MAIN_BRANCH} ({result['head']}) captain={pubkey[:16]}")
    return JSONResponse(result)


@router.post("/api/nostr/admin/arbor_branch_delete")
async def post_arbor_branch_delete(request: Request):
    """Supprime une branche arbor/* DÉJÀ fusionnée (+ son worktree). NIP-98 Capitaine EXCLUSIF."""
    body = await safe_json_body(request)
    branch = (body.get("branch") or "").strip()
    pubkey = await require_captain_signature(request)
    result = await asyncio.to_thread(arbor_branch_delete, REPO_ROOT, branch)
    _arbor_audit("cleanup", mode="cleanup", branch=branch, rc=0, captain_hex=pubkey)
    logger.info(f"Admin arbor_branch_delete: {branch} captain={pubkey[:16]}")
    return JSONResponse(result)


@router.get("/api/nostr/admin/arbor_run_state")
async def get_arbor_run_state(request: Request, uplanetname: Optional[str] = None):
    """Suivi LÉGER d'un run (état + fin du journal) pour le rafraîchissement toutes les
    3 s de l'interface : arbor_status coûte 0,2 à 0,7 s (config coopérative, cookies, git)
    et n'a aucune raison d'être rappelé à chaque tick. NIP-98 Capitaine."""
    await _check_admin_auth(request, uplanetname)
    state = _read_json_file(ARBOR_STATE_FILE)
    out = {"state": state}
    if state.get("log"):
        lp = Path(state["log"])
        out["log_tail"] = ""
        if lp.is_file():
            try:
                size = lp.stat().st_size
                with open(lp, "rb") as f:
                    f.seek(max(0, size - 60_000))
                    lines = f.read().decode("utf-8", errors="replace").splitlines()
                out["log_tail"] = "\n".join(lines[-200:])
                out["log_lines"] = len(lines)
            except Exception:
                pass
    return JSONResponse(out)


@router.post("/api/nostr/admin/arbor_trigger")
async def post_arbor_trigger(request: Request):
    """Déclenche un run ARBOR à distance — capitaine-only, NIP-98 EXCLUSIF
    (jamais le repli UPLANETNAME : le secret coopératif partagé du swarm
    n'est pas une preuve d'autorité individuelle suffisante pour cette
    action). Body JSON : {mode, model?, need?, slug?}. Aucun argument libre
    au-delà de "mode" (whitelist stricte), "model" (regex + liste candidats
    si définie), "need"/"slug" (mode forge uniquement, bornés). Ne produit
    JAMAIS de merge — voir arbor_run.sh et arbor_self_improve.py::
    _create_worktree / arbor_tool_forge.py::forge_tool (worktree + branche
    isolée uniquement)."""
    body = await safe_json_body(request)
    mode = body.get("mode", "")
    model = (body.get("model") or "").strip() or None
    need = (body.get("need") or "").strip() or None
    slug = (body.get("slug") or "").strip() or None
    owner_email = (body.get("owner_email") or "").strip() or None
    domain = (body.get("domain") or "").strip() or None
    url = (body.get("url") or "").strip() or None

    pubkey = await require_captain_signature(request)

    if not ARBOR_RUN_SH.is_file():
        raise HTTPException(status_code=500, detail="arbor_run.sh introuvable sur cette station.")

    raw = await coop_load_raw()
    if str(raw.get("ARBOR_REMOTE_TRIGGER_ENABLED", "")).strip().lower() != "true":
        raise HTTPException(
            status_code=403,
            detail="Déclenchement distant désactivé — activez ARBOR_REMOTE_TRIGGER_ENABLED dans ⚙️ NODE.",
        )

    if mode not in _ARBOR_MODES:
        raise HTTPException(status_code=400, detail=f"mode invalide (attendu : {', '.join(_ARBOR_MODES)})")

    if model:
        if not _ARBOR_MODEL_RE.match(model):
            raise HTTPException(status_code=400, detail="Nom de modèle invalide.")
        allowed = _arbor_config.get_list("ARBOR_CANDIDATE_MODELS", []) if _arbor_config else []
        if allowed and model not in allowed:
            raise HTTPException(status_code=400, detail=f"Modèle hors liste candidats : {', '.join(allowed)}")

    if mode in ("forge", "forge-scraper"):
        # Génération de code par Claude CLI — nécessite que le capitaine ait
        # configuré/authentifié son compte au préalable (claude.vscodium.
        # setup.sh setup/migrate). Vérifié ICI (message HTTP clair, avant de
        # lancer un run qui échouerait silencieusement) plutôt que de laisser
        # arbor_tool_forge.py::claude_available() être le seul signal.
        cli = _claude_cli_status()
        if not cli["available"]:
            raise HTTPException(
                status_code=400,
                detail="Claude CLI introuvable sur cette station — installez-le (npm install -g "
                       "@anthropic-ai/claude-code) puis configurez un compte : "
                       "bash Astroport.ONE/claude.vscodium.setup.sh setup",
            )
        if not cli["authenticated"]:
            raise HTTPException(
                status_code=400,
                detail="Claude CLI installé mais aucun compte authentifié détecté — lancez : "
                       "bash Astroport.ONE/claude.vscodium.setup.sh setup   (ou migrate si vous avez "
                       "déjà une session VSCodium configurée).",
            )

    if mode == "forge":
        if not need or len(need) < 5:
            raise HTTPException(status_code=400, detail="Le mode forge nécessite un champ « need » d'au moins 5 caractères.")
        if len(need) > ARBOR_NEED_MAX_CHARS:
            raise HTTPException(status_code=400, detail=f"« need » trop long (max {ARBOR_NEED_MAX_CHARS} caractères).")
        if any(c in need for c in ("\n", "\r", "\x00")):
            raise HTTPException(status_code=400, detail="« need » : retours à la ligne non autorisés.")
        if slug and not _ARBOR_SLUG_RE.match(slug):
            raise HTTPException(status_code=400, detail="« slug » invalide (minuscules/chiffres/tirets, ≤30 caractères).")
        owner_email = None; domain = None; url = None
    elif mode == "forge-scraper":
        # Défense en profondeur : owner_email doit être un compte MULTIPASS
        # réel de cette station, et domain doit correspondre à un cookie
        # RÉELLEMENT déposé et RÉELLEMENT sans scraper à l'instant présent —
        # jamais un couple arbitraire fourni par le client.
        from services.memory_status import list_multipass_emails
        if not owner_email or owner_email not in list_multipass_emails():
            raise HTTPException(status_code=400, detail="owner_email invalide ou introuvable sur cette station.")
        if not domain or not _ARBOR_DOMAIN_RE.match(domain):
            raise HTTPException(status_code=400, detail="domain invalide.")
        pending = _cookies_without_scraper()
        if not any(p["email"] == owner_email and p["domain"] == domain for p in pending):
            raise HTTPException(
                status_code=400,
                detail=f"Aucun cookie {domain!r} sans scraper trouvé pour {owner_email!r} — "
                       "rechargez le panneau ARBOR, la situation a peut-être changé.",
            )
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="url invalide (http(s):// attendu).")
        need = None; slug = None
    else:
        need = None; slug = None; owner_email = None; domain = None; url = None

    cooldown_min = int(raw.get("ARBOR_TRIGGER_COOLDOWN_MIN") or 30)
    state = _read_json_file(ARBOR_STATE_FILE)
    if state.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail=f"Un run ARBOR est déjà en cours (mode={state.get('mode')}, démarré à {state.get('started_at')}).",
        )
    finished_at = state.get("finished_at")
    if finished_at:
        try:
            import datetime
            last = datetime.datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            remaining = cooldown_min * 60 - (time.time() - last.timestamp())
            if remaining > 0:
                raise HTTPException(status_code=429, detail=f"Cooldown en cours — réessayez dans {int(remaining)}s.")
        except HTTPException:
            raise
        except Exception:
            pass

    args = [str(ARBOR_RUN_SH), "--mode", mode, "--origin", "http", "--captain-hex", pubkey]
    if model:
        args += ["--model", model]
    if need:
        args += ["--need", need]
    if slug:
        args += ["--slug", slug]
    if owner_email:
        args += ["--owner-email", owner_email]
    if domain:
        args += ["--domain", domain]
    if url:
        args += ["--url", url]
    try:
        subprocess.Popen(
            # Liste d'arguments (jamais shell=True) : "need" en texte libre
            # (espaces, ponctuation) est passé tel quel à execve, sans jamais
            # transiter par un interpréteur shell — aucun risque d'injection.
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(REPO_ROOT),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec du lancement : {e}")

    logger.info(f"Admin arbor_trigger: mode={mode} model={model} need={'oui' if need else 'non'} "
                f"owner_email={owner_email} domain={domain} (captain={pubkey[:16]})")
    return JSONResponse({
        "status": "started", "mode": mode,
        "note": "Aucun merge automatique — la branche produite (si apply/forge/forge-scraper) reste à relire.",
    })


@router.get("/api/nostr/admin/arbor_mined_preview")
async def get_arbor_mined_preview(request: Request, uplanetname: Optional[str] = None):
    """Prévisualisation en LECTURE SEULE des patterns de besoins récurrents
    détectés dans le corpus multi-utilisateurs (embeddings Ollama, un appel
    par entrée non déjà vue — coûteux, à la demande uniquement, jamais
    auto-chargé). persist=False : n'écrit RIEN dans bro_tool_requests_mined.
    json — rejouable sans jamais "consommer" un pattern qui ne serait alors
    plus jamais signalé au capitaine. Auth signature NIP-98
    du Capitaine (lecture seule, pas besoin de require_captain_signature)."""
    await _check_admin_auth(request, uplanetname)
    # Import LAZY, dans le handler uniquement — arbor_self_improve.py importe
    # bro_watch_core → question.py → ollama, une chaîne bien plus lourde que
    # arbor_config/bro.media déjà importés au niveau module de ce fichier.
    # Ne jamais payer ce coût au démarrage de FastAPI pour un endpoint dont
    # l'usage reste occasionnel (bouton "Actualiser" manuel).
    import arbor_self_improve
    reports = await asyncio.to_thread(arbor_self_improve.mine_tool_requests, False, True)
    return JSONResponse({"clusters": reports})


# ── ARBOR — bâton de parole (cycle automatique quotidien) ──────────────────
def _swarm_arbor_stations() -> list:
    """Stations swarm connues (hors soi-même) ayant déclaré arbor_eligible=true
    dans leur 12345.json (cf. heartbox_analysis.sh) — alimente le sélecteur de
    cible pour une passation de bâton. Lecture seule, aucun appel réseau."""
    out = []
    own_hex, _ = get_node_and_captain_hex()
    swarm_dir = settings.ZEN_PATH / "tmp" / "swarm"
    if not swarm_dir.is_dir():
        return out
    for json_file in swarm_dir.glob("*/12345.json"):
        data = _read_json_file(json_file)
        node_hex = str(data.get("NODEHEX") or "").lower()
        if not node_hex or node_hex == own_hex:
            continue
        if not data.get("capacities", {}).get("arbor_eligible"):
            continue
        out.append({
            "hex": node_hex,
            "hostname": data.get("hostname") or node_hex[:12],
            "captain": data.get("captain") or "",
        })
    return out


async def _run_arbor_baton_sh(*args: str, timeout: float = 30.0) -> dict:
    if not ARBOR_BATON_SH.is_file():
        raise HTTPException(status_code=500, detail="arbor_baton.sh introuvable sur cette station.")
    try:
        proc = await asyncio.create_subprocess_exec(
            str(ARBOR_BATON_SH), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(_IA_PATH),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout arbor_baton.sh (réseau lent ?).")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec arbor_baton.sh : {e}")
    try:
        return json.loads(stdout.decode().strip() or "{}")
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500,
            detail=(stderr.decode(errors="replace") or stdout.decode(errors="replace") or "Sortie invalide")[:300],
        )


@router.get("/api/nostr/admin/arbor_baton_status")
async def get_arbor_baton_status(request: Request, uplanetname: Optional[str] = None):
    """Statut du bâton de parole ARBOR (cf. IA/arbor_baton.sh — gouverne
    UNIQUEMENT le cycle automatique quotidien mine-requests, jamais le
    déclenchement manuel) + liste des stations swarm éligibles pour une
    passation. Lecture seule : auth NIP-98 Capitaine."""
    await _check_admin_auth(request, uplanetname)
    state = await _run_arbor_baton_sh("status")
    state["swarm_eligible_stations"] = _swarm_arbor_stations()
    return JSONResponse(state)


@router.post("/api/nostr/admin/arbor_baton_handoff")
async def post_arbor_baton_handoff(request: Request):
    """Passe le bâton à une autre station — capitaine EXCLUSIF (NIP-98).
    arbor_baton.sh handoff revérifie lui-même (défense en profondeur) que
    CETTE station le détient actuellement et que la cible est une station
    swarm connue et éligible."""
    body = await safe_json_body(request)
    target_hex = (body.get("target_hex") or "").strip().lower()
    pubkey = await require_captain_signature(request)
    if not _HEX64_RE.match(target_hex):
        raise HTTPException(status_code=400, detail="target_hex invalide (64 caractères hex attendus).")

    result = await _run_arbor_baton_sh("handoff", target_hex, timeout=60.0)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Échec de la passation.")
    logger.info(f"Admin arbor_baton_handoff: target={target_hex[:16]} (captain={pubkey[:16]})")
    return JSONResponse(result)


@router.post("/api/nostr/admin/arbor_baton_eligibility_check")
async def post_arbor_baton_eligibility_check(request: Request):
    """Relance la vérification RÉSEAU d'éligibilité (git push --dry-run sur
    ARBOR_GIT_REMOTE_NAME) — capitaine EXCLUSIF : déclenche un appel réseau,
    jamais automatique via le seul repli UPLANETNAME."""
    pubkey = await require_captain_signature(request)
    if not ARBOR_BATON_ELIGIBILITY_SH.is_file():
        raise HTTPException(status_code=500, detail="arbor_baton_eligibility.sh introuvable sur cette station.")
    try:
        proc = await asyncio.create_subprocess_exec(
            str(ARBOR_BATON_ELIGIBILITY_SH), "--force",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(_IA_PATH),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout lors de la vérification d'éligibilité (réseau lent ?).")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec : {e}")
    try:
        result = json.loads(stdout.decode().strip() or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Sortie invalide de arbor_baton_eligibility.sh")
    logger.info(f"Admin arbor_baton_eligibility_check: eligible={result.get('eligible')} (captain={pubkey[:16]})")
    return JSONResponse(result)


@router.post("/api/nostr/admin/arbor_chat")
async def post_arbor_chat(request: Request):
    """Dialogue conversationnel avec Arbor — capitaine EXCLUSIF (NIP-98).
    Body : {message, history: [{role, content}]}. Le contexte (bâton,
    branches en attente, derniers runs, CLI Claude) est reconstruit ICI,
    jamais fourni par le client. Ne déclenche RIEN : une action proposée doit
    être validée explicitement via POST arbor_trigger (même whitelist, même
    garde) — ce endpoint ne fait que dialoguer."""
    body = await safe_json_body(request)
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not message:
        raise HTTPException(status_code=400, detail="message manquant.")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="message trop long (max 2000 caractères).")
    if not isinstance(history, list) or len(history) > 40:
        raise HTTPException(status_code=400, detail="history invalide (liste de 40 entrées maximum).")

    await require_captain_signature(request)

    raw = await coop_load_raw()
    audit = _read_audit_tail(ARBOR_AUDIT_FILE, limit=5)
    try:
        baton_status = await _run_arbor_baton_sh("status", timeout=15.0)
    except HTTPException:
        baton_status = {}

    context = {
        "baton": {k: baton_status.get(k) for k in ("holder", "vacant", "is_local_holder", "timeout_hours")},
        "branches_awaiting_review": [
            {"branch": b["branch"], "subject": b["subject"], "commits_ahead": b["commits_ahead"]}
            for b in _arbor_branches() if not b["already_merged"]
        ][:10],
        "recent_runs": [
            {"mode": a.get("mode"), "rc": a.get("rc"), "finished_at": a.get("finished_at")} for a in audit
        ],
        "remote_trigger_enabled": str(raw.get("ARBOR_REMOTE_TRIGGER_ENABLED", "")).strip().lower() == "true",
        "claude_cli": _claude_cli_status(),
    }

    # Import LAZY — même raison que arbor_mined_preview (arbor_chat importe
    # arbor_tool_forge, qui importe bro_watch_core/arbor_self_improve, chaîne
    # bien plus lourde que ce module à lui seul).
    import arbor_chat
    try:
        result = await asyncio.to_thread(arbor_chat.chat_turn, message, history, context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec dialogue Arbor : {e}")
    return JSONResponse(result)


# ── Jobs bash destructifs en arrière-plan (purge NOSTR, destruction MULTIPASS) ──
# Deux instances du même pattern : verrou PID + log fichier + Popen détaché,
# cf. arbor_trigger plus haut pour le précédent qui a établi cette convention.
def _bg_job_is_running(lock_file: Path) -> Optional[int]:
    """PID du job en cours si le process est vivant, sinon None. Pas de
    nettoyage explicite du fichier de verrou à la fin d'un run : la prochaine
    vérification (os.kill(pid, 0) échoue sur un process mort) s'auto-corrige,
    inutile de faire signaler sa propre fin par le script bash."""
    try:
        pid = int(lock_file.read_text().strip())
    except Exception:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def _bg_job_log_tail(log_file: Path, limit: int = 40) -> str:
    if not log_file.is_file():
        return ""
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-limit:])


def _bg_job_launch(script: Path, args: list, log_file: Path, lock_file: Path, header: str) -> int:
    """Lance `script args...` en arrière-plan, stdout+stderr redirigés vers
    log_file (écrasé, précédé de `header`), PID écrit dans lock_file. Retourne
    le PID. Ne vérifie PAS lock_file/is_running — à l'appelant de le faire
    avant (fenêtre de course négligeable : actions admin rares, jamais deux
    clics simultanés en pratique)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_file, "w", encoding="utf-8")
    try:
        log_fh.write(header)
        log_fh.flush()
        proc = subprocess.Popen(
            [str(script)] + args,
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=str(script.parent),
        )
    finally:
        log_fh.close()
    lock_file.write_text(str(proc.pid))
    return proc.pid


# ── Purge NOSTR des comptes "étrangers" ─────────────────────────────────────
@router.get("/api/nostr/admin/purge_strangers")
async def get_purge_strangers(request: Request, uplanetname: Optional[str] = None):
    """Analyse des comptes NOSTR "étrangers" (ni MULTIPASS connu, ni NODE
    actif, ni DID+abonnement coopératif valide) — délègue à
    purge_nostr_strangers.sh --list-json (lecture seule : quelques scans
    strfry FIXES, jamais un par auteur). Si une purge --clean est en cours,
    ne relance pas l'analyse (le relay est déjà occupé par les suppressions)
    et renvoie juste l'état + le tail du log. Lecture seule : auth
    NIP-98 Capitaine suffit."""
    await _check_admin_auth(request, uplanetname)
    running_pid = _bg_job_is_running(PURGE_LOCK_FILE)
    log_tail = _bg_job_log_tail(PURGE_LOG_FILE)
    if running_pid:
        return JSONResponse({"running": True, "pid": running_pid, "log_tail": log_tail, "authors": [], "candidate_count": None})

    if not PURGE_SCRIPT.is_file():
        raise HTTPException(status_code=500, detail="purge_nostr_strangers.sh introuvable")
    try:
        proc = subprocess.run(
            [str(PURGE_SCRIPT), "--list-json"],
            capture_output=True, text=True, timeout=60, cwd=str(PURGE_SCRIPT.parent),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Timeout purge_nostr_strangers.sh --list-json")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Erreur analyse : {(proc.stderr or proc.stdout)[:500] or 'code ' + str(proc.returncode)}")
    try:
        data = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail=f"Sortie invalide : {proc.stdout[:300]}")
    return JSONResponse({"running": False, "log_tail": log_tail, **data})


@router.post("/api/nostr/admin/purge_strangers/clean")
async def post_purge_strangers_clean(request: Request):
    """Lance purge_nostr_strangers.sh --clean en arrière-plan — suppression
    IRRÉVERSIBLE des événements NOSTR des comptes candidats (cf. --list-json
    purge_candidate=true : MULTIPASS inconnu ou NODE disparu depuis >48h).
    NIP-98 Capitaine EXCLUSIF, jamais le repli UPLANETNAME : le secret
    coopératif partagé du swarm n'est pas une preuve d'autorité individuelle
    suffisante pour déclencher une purge destructive et non annulable (même
    garde que arbor_trigger)."""
    pubkey = await require_captain_signature(request)

    if _bg_job_is_running(PURGE_LOCK_FILE):
        raise HTTPException(status_code=409, detail="Une purge est déjà en cours.")
    if not PURGE_SCRIPT.is_file():
        raise HTTPException(status_code=500, detail="purge_nostr_strangers.sh introuvable")

    header = f"=== Purge lancée par {pubkey[:16]}… — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n"
    try:
        pid = _bg_job_launch(PURGE_SCRIPT, ["--clean"], PURGE_LOG_FILE, PURGE_LOCK_FILE, header)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec du lancement : {e}")

    logger.info(f"Admin purge_strangers --clean lancé (pid={pid}, captain={pubkey[:16]})")
    return JSONResponse({"status": "started", "pid": pid})


# ── Destruction MULTIPASS (nostr_DESTROY_TW.sh) ─────────────────────────────
@router.get("/api/nostr/admin/destroy_multipass/status")
async def get_destroy_multipass_status(request: Request, uplanetname: Optional[str] = None):
    """Statut du dernier lancement de nostr_DESTROY_TW.sh (running + tail du
    log) — permet à nostr_admin.html de suivre la progression (export backup
    chiffré, cashback G1, envoi email) sans bloquer sur une requête synchrone
    de plusieurs dizaines de secondes. Lecture seule : auth NIP-98 Capitaine."""
    await _check_admin_auth(request, uplanetname)
    pid = _bg_job_is_running(DESTROY_LOCK_FILE)
    return JSONResponse({"running": bool(pid), "pid": pid, "log_tail": _bg_job_log_tail(DESTROY_LOG_FILE)})


@router.post("/api/nostr/admin/destroy_multipass")
async def post_destroy_multipass(request: Request):
    """Lance nostr_DESTROY_TW.sh en arrière-plan pour UN compte MULTIPASS —
    désactivation IRRÉVERSIBLE (export chiffré + cashback G1 vers le wallet
    primal + suppression de ~/.zen/game/nostr/<email>) permettant une
    migration ultérieure de relay/capitaine via le backup. NIP-98 Capitaine
    EXCLUSIF (même garde que purge_strangers/clean et arbor_trigger) : action
    destructive ciblant un compte nommé, jamais déclenchable via le secret
    coopératif partagé UPLANETNAME seul.

    email doit être un MULTIPASS RÉEL de cette station (jamais une valeur
    arbitraire fournie par le client — même principe de défense en profondeur
    que arbor_trigger::forge-scraper) ; reason est une whitelist stricte (le
    script en dérive le texte des emails envoyés au joueur et au Capitaine)."""
    body = await request.json()
    email = (body.get("email") or "").strip()
    reason = (body.get("reason") or "INSOLVENCY").strip()

    pubkey = await require_captain_signature(request)

    from services.memory_status import list_multipass_emails
    if not email or email not in list_multipass_emails():
        raise HTTPException(status_code=400, detail="email invalide ou introuvable sur cette station.")
    if reason not in _DESTROY_REASONS:
        raise HTTPException(status_code=400, detail=f"reason invalide (attendu : {', '.join(_DESTROY_REASONS)})")

    if _bg_job_is_running(DESTROY_LOCK_FILE):
        raise HTTPException(status_code=409, detail="Une destruction est déjà en cours sur cette station.")
    if not DESTROY_TW_SCRIPT.is_file():
        raise HTTPException(status_code=500, detail="nostr_DESTROY_TW.sh introuvable")

    header = (f"=== Destruction MULTIPASS {email} ({reason}) lancée par "
              f"{pubkey[:16]}… — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n")
    try:
        pid = _bg_job_launch(DESTROY_TW_SCRIPT, [email, reason], DESTROY_LOG_FILE, DESTROY_LOCK_FILE, header)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec du lancement : {e}")

    logger.warning(f"Admin destroy_multipass lancé : email={email} reason={reason} pid={pid} captain={pubkey[:16]}")
    return JSONResponse({"status": "started", "pid": pid, "email": email})


# ── Synastrie planétaire (indépendante du Kin) ──────────────────────────────
def _synastry_stats_file() -> Path:
    from utils.observability import get_ipfsnodeid
    return settings.ZEN_PATH / "tmp" / get_ipfsnodeid() / "kin_synastry_stats.json"


@router.get("/api/nostr/admin/synastry_stats")
async def get_synastry_stats(request: Request, uplanetname: Optional[str] = None):
    """Stats du dernier run de la section 8 (Constellations Synastriques) de
    KIN.news.sh — membres avec date de naissance, paires scannées, matches
    trouvés. Lecture seule : auth NIP-98 Capitaine."""
    await _check_admin_auth(request, uplanetname)
    running_pid = _bg_job_is_running(SYNASTRY_LOCK_FILE)
    log_tail = _bg_job_log_tail(SYNASTRY_LOG_FILE)
    stats_file = _synastry_stats_file()
    if not stats_file.is_file():
        return JSONResponse({"never_run": True, "running": bool(running_pid), "log_tail": log_tail})
    try:
        data = json.loads(stats_file.read_text())
    except Exception:
        return JSONResponse({"never_run": True, "running": bool(running_pid), "log_tail": log_tail})
    data["running"] = bool(running_pid)
    data["log_tail"] = log_tail
    return JSONResponse(data)


@router.post("/api/nostr/admin/synastry_trigger")
async def post_synastry_trigger(request: Request):
    """Lance KIN.news.sh --synastry-only --force en arrière-plan — NIP-98
    Capitaine EXCLUSIF (même garde que arbor_trigger/purge_strangers/clean) :
    ce déclenchement envoie des emails à des membres, une action visible
    plutôt qu'une simple lecture, pas déclenchable via le secret coopératif
    partagé UPLANETNAME seul."""
    pubkey = await require_captain_signature(request)

    if _bg_job_is_running(SYNASTRY_LOCK_FILE):
        raise HTTPException(status_code=409, detail="Un run Constellations Synastriques est déjà en cours.")
    if not SYNASTRY_TRIGGER_SCRIPT.is_file():
        raise HTTPException(status_code=500, detail="KIN.news.sh introuvable")

    header = (f"=== Synastrie déclenchée par {pubkey[:16]}… — "
              f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n")
    try:
        pid = _bg_job_launch(SYNASTRY_TRIGGER_SCRIPT, ["--synastry-only", "--force"],
                              SYNASTRY_LOG_FILE, SYNASTRY_LOCK_FILE, header)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec du lancement : {e}")

    logger.info(f"Admin synastry_trigger lancé (pid={pid}, captain={pubkey[:16]})")
    return JSONResponse({"status": "started", "pid": pid})
