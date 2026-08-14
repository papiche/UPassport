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
from services.admin_auth import check_admin_auth as _check_admin_auth, require_captain_signature
from services.coop_config import (
    COOP_CONFIG_SCHEMA, ALL_KEYS, is_sensitive, validate_value, CoopConfigError,
    coop_load_raw, coop_set_many, coop_delete,
)

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
    Auth UPLANETNAME ou signature NIP-98 du Capitaine."""
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
    body = await request.json()
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
    body = await request.json()
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
    cours. Lecture seule — auth UPLANETNAME ou signature NIP-98 du Capitaine."""
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
    if log and state.get("log"):
        log_path = Path(state["log"])
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                result["log_tail"] = "\n".join(lines[-80:])
            except Exception:
                result["log_tail"] = ""
    return JSONResponse(result)


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
    body = await request.json()
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
    plus jamais signalé au capitaine. Auth UPLANETNAME ou signature NIP-98
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
