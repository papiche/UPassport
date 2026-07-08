"""
services/memory_status.py — État des mémoires (fichiers + Qdrant) d'un MULTIPASS.

Utilisé par :
  - routers/nostr.py   (vue admin "BRO mémoire" — tous les comptes de la station)
  - routers/mailjet.py  (vue self-service — le compte du visiteur uniquement)

N'entretient AUCUNE nouvelle source de vérité : lit les mêmes fichiers/collections
que Astroport.ONE/IA/memory_manager.py, bro/identity.py, bro/love_handler.sh —
jamais de duplication. Les appels Qdrant passent par memory_manager.py en
sous-processus (mêmes creds/URL Qdrant que le reste de BRO, une seule logique
d'auth Qdrant dans tout le projet).
"""

import json
import subprocess
import sys
from pathlib import Path

from core.config import settings, ASTRO_PYTHON

_IA_PATH    = settings.ZEN_PATH / "Astroport.ONE" / "IA"
_MEMORY_MGR = _IA_PATH / "memory_manager.py"

sys.path.insert(0, str(_IA_PATH))
try:
    from bro.identity import _IDENTITY_TEMPLATES  # template exact de .Preferences.md vide
except Exception:
    _IDENTITY_TEMPLATES = {}

# Registre des slots réservés — cf. Astroport.ONE/IA/bro/rag.py (13, 14) et
# memory_manager.py (15). 0-12 : contexte géo/conversation (short_memory.py).
CONVERSATION_SLOTS = list(range(0, 13))
BRO_MEMORY_SLOT    = 13
PERSONA_SLOT       = 14
LOVE_SLOT          = 15
ALL_SLOTS          = CONVERSATION_SLOTS + [BRO_MEMORY_SLOT, PERSONA_SLOT, LOVE_SLOT]

RESET_SCOPES = ("conversations", "bro_memory", "persona", "love", "identity_learned", "all")

_MASTODON_SCRAPER = _IA_PATH / "scrapers" / "mastodon" / "scraper_mastodon.py"


def list_multipass_emails() -> list[str]:
    """Scanne ~/.zen/game/nostr/*/ — un compte MULTIPASS par dossier possédant
    un .secret.nostr (même pattern que routers/identity.py::_find_email_by_npub)."""
    root = settings.GAME_PATH / "nostr"
    if not root.is_dir():
        return []
    return sorted(p.parent.name for p in root.glob("*/.secret.nostr") if p.parent.name)


def _user_dir(email: str) -> Path:
    return settings.GAME_PATH / "nostr" / email


def _flashmem_dir(email: str) -> Path:
    return settings.ZEN_PATH / "flashmem" / email


def _file_stat(path: Path) -> dict:
    try:
        st = path.stat()
        return {"exists": True, "size_bytes": st.st_size, "modified_at": int(st.st_mtime)}
    except Exception:
        return {"exists": False, "size_bytes": 0, "modified_at": None}


def _json_message_count(path: Path) -> int:
    """Nombre de messages dans un fichier slot*.json ({"messages": [...]})
    ou dans un array JSON simple (love/memories.json, love/dialog.json)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, dict):
        return len(data.get("messages", []))
    if isinstance(data, list):
        return len(data)
    return 0


def _count_lines(path: Path) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def _preferences_line_count(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip().startswith("- "))
    except Exception:
        return 0


def _qdrant_slot_counts(email: str, slots: list) -> dict:
    if not _MEMORY_MGR.exists():
        return {}
    try:
        result = subprocess.run(
            [ASTRO_PYTHON, str(_MEMORY_MGR), "slot-counts",
             "--user-id", email, "--slots", *[str(s) for s in slots]],
            capture_output=True, text=True, timeout=20,
        )
        return json.loads(result.stdout.strip() or "{}")
    except Exception:
        return {}


def _delete_qdrant_slots(email: str, slots: list) -> None:
    if not _MEMORY_MGR.exists():
        return
    for s in slots:
        try:
            subprocess.run(
                [ASTRO_PYTHON, str(_MEMORY_MGR), "delete-slot",
                 "--user-id", email, "--slot", str(s)],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass


def get_memory_status(email: str) -> dict:
    """État complet des mémoires d'un MULTIPASS — fichiers locaux + Qdrant.
    Best-effort : un module indisponible (Qdrant down) donne juste des
    compteurs à 0/indisponible, jamais une exception qui casserait la page.
    Appel bloquant (I/O disque + un sous-processus) — à exécuter via
    asyncio.to_thread() depuis un handler FastAPI."""
    user_dir     = _user_dir(email)
    fm_dir       = _flashmem_dir(email)
    love_dir     = fm_dir / "love"
    identity_dir = user_dir / "identity"

    bro_memory_file = fm_dir / f"slot{BRO_MEMORY_SLOT}.json"
    persona_file    = fm_dir / f"slot{PERSONA_SLOT}.json"

    conversations = {
        "slots_present": [s for s in CONVERSATION_SLOTS if (fm_dir / f"slot{s}.json").exists()],
        "message_count": sum(_json_message_count(fm_dir / f"slot{s}.json") for s in CONVERSATION_SLOTS),
        "size_bytes": sum(_file_stat(fm_dir / f"slot{s}.json")["size_bytes"] for s in CONVERSATION_SLOTS),
    }

    love = {
        "memories_count": _json_message_count(love_dir / "memories.json"),
        "dialog_count":   _json_message_count(love_dir / "dialog.json"),
        "matches_count":  _json_message_count(love_dir / "matches.json"),
        "has_profile":    (love_dir / "profile.json").exists(),
        "size_bytes": sum(_file_stat(love_dir / f)["size_bytes"]
                          for f in ("memories.json", "dialog.json", "matches.json", "profile.json")),
    }

    identity = {
        "core_bytes":       _file_stat(identity_dir / ".Core.md")["size_bytes"],
        "style_bytes":      _file_stat(identity_dir / ".Style.md")["size_bytes"],
        "rules_bytes":      _file_stat(identity_dir / ".Rules.md")["size_bytes"],
        "objectifs_bytes":  _file_stat(identity_dir / ".Objectifs.md")["size_bytes"],
        "preferences_lines": _preferences_line_count(identity_dir / ".Preferences.md"),
        "preferences_history_entries": _count_lines(identity_dir / ".Preferences.history.jsonl"),
    }

    cookie_manifest = {}
    manifest_path = user_dir / ".cookie_manifest.json"
    if manifest_path.exists():
        try:
            cookie_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            cookie_manifest = {}
    cookie_domains = sorted(d for d in cookie_manifest if not d.startswith("_"))

    qdrant_counts = _qdrant_slot_counts(email, ALL_SLOTS)
    qdrant_available = bool(qdrant_counts)

    def _qc(slot: int) -> int:
        return qdrant_counts.get(str(slot), 0)

    # Compteurs Qdrant injectés directement dans chaque catégorie — évite au
    # gabarit HTML (Jinja) de devoir sommer/indexer le dict "slots" bruts.
    conversations["qdrant_points"] = sum(_qc(s) for s in CONVERSATION_SLOTS)

    return {
        "email": email,
        "conversations": conversations,
        "bro_memory": {**_file_stat(bro_memory_file), "message_count": _json_message_count(bro_memory_file),
                       "qdrant_points": _qc(BRO_MEMORY_SLOT)},
        "persona":    {**_file_stat(persona_file), "message_count": _json_message_count(persona_file),
                       "qdrant_points": _qc(PERSONA_SLOT)},
        "love": {**love, "qdrant_points": _qc(LOVE_SLOT)},
        "identity": identity,
        "cookies": {"domains": cookie_domains, "count": len(cookie_domains)},
        "can_regenerate_from_mastodon": has_mastodon_cookie(email),
        "qdrant": {
            "available": qdrant_available,
            "slots": qdrant_counts,
            "total_points": sum(qdrant_counts.values()) if qdrant_counts else 0,
        },
    }


def reset_memory(email: str, scope: str) -> dict:
    """Réinitialise un périmètre de mémoire (fichiers + Qdrant). Best-effort :
    chaque suppression individuelle est isolée, une erreur n'empêche pas les
    autres. Ne touche JAMAIS identity/.Core.md, .Style.md, .Rules.md,
    .Objectifs.md (rédigés par l'utilisateur, jamais par un reset automatique)
    ni love/profile.json (déclaré explicitement via #love_profile, pas un
    journal). Appel bloquant — à exécuter via asyncio.to_thread()."""
    if scope not in RESET_SCOPES:
        raise ValueError(f"scope inconnu : {scope}")

    user_dir     = _user_dir(email)
    fm_dir       = _flashmem_dir(email)
    love_dir     = fm_dir / "love"
    identity_dir = user_dir / "identity"

    deleted: list[str] = []

    def _rm(path: Path):
        try:
            if path.exists():
                path.unlink()
                deleted.append(str(path))
        except Exception:
            pass

    if scope in ("conversations", "all"):
        for s in CONVERSATION_SLOTS:
            _rm(fm_dir / f"slot{s}.json")
        _delete_qdrant_slots(email, CONVERSATION_SLOTS)

    if scope in ("bro_memory", "all"):
        _rm(fm_dir / f"slot{BRO_MEMORY_SLOT}.json")
        _delete_qdrant_slots(email, [BRO_MEMORY_SLOT])

    if scope in ("persona", "all"):
        _rm(fm_dir / f"slot{PERSONA_SLOT}.json")
        _delete_qdrant_slots(email, [PERSONA_SLOT])

    if scope in ("love", "all"):
        _rm(love_dir / "memories.json")
        _rm(love_dir / "dialog.json")
        _rm(love_dir / "matches.json")
        _delete_qdrant_slots(email, [LOVE_SLOT])

    if scope in ("identity_learned", "all"):
        prefs_path = identity_dir / ".Preferences.md"
        default_prefs = _IDENTITY_TEMPLATES.get(".Preferences.md", "")
        try:
            if prefs_path.exists() and default_prefs:
                prefs_path.write_text(default_prefs, encoding="utf-8")
                deleted.append(f"{prefs_path} (réinitialisé au template)")
        except Exception:
            pass
        _rm(identity_dir / ".Preferences.history.jsonl")
        _rm(identity_dir / ".social_learned_seen.json")

    return {"email": email, "scope": scope, "deleted": deleted}


def has_mastodon_cookie(email: str) -> bool:
    """Utilisé pour proposer (ou non) le bouton 'Régénérer depuis Mastodon'."""
    return (_user_dir(email) / ".mastodon.social.cookie").exists()


def regenerate_lifeos_from_mastodon(email: str) -> dict:
    """Déclenche une extraction ciblée des propres posts Mastodon (Playwright,
    ~15-30s) et réapprend immédiatement le profil LifeOS (force=True) — action
    manuelle explicite (bouton 'Régénérer depuis Mastodon'), par opposition au
    run quotidien passif de scraper_mastodon.py qui, lui, attend l'accumulation
    quotidienne (cf. bro_watch_core.learn_personality_from_posts). Nécessite un
    cookie mastodon.social déjà déposé. Appel bloquant (sous-processus
    Playwright) — à exécuter via asyncio.to_thread()."""
    cookie_file = _user_dir(email) / ".mastodon.social.cookie"
    if not cookie_file.exists():
        return {"ok": False, "error": "Aucun cookie mastodon.social déposé pour ce compte."}
    if not _MASTODON_SCRAPER.exists():
        return {"ok": False, "error": "scraper_mastodon.py introuvable."}

    seen_file = _user_dir(email) / ".mastodon.social.seen.json"
    try:
        result = subprocess.run(
            [ASTRO_PYTHON, str(_MASTODON_SCRAPER),
             "--player", email, "--cookie-file", str(cookie_file),
             "--instance", "mastodon.social", "--seen-file", str(seen_file),
             "--profile-only"],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Délai dépassé (90s) — le cookie est peut-être expiré."}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout or "échec inconnu").strip()[:300]}

    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except Exception:
        payload = {}
    return {"ok": True, **payload}
