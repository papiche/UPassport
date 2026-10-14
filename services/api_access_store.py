"""API Access Vault — identifiants d'API officielle (Mastodon, Telegram...) par
MULTIPASS, chiffrés avec la clé G1 propre de l'utilisateur + IPFS + NOSTR
kind 31904 (distinct du Cookie Vault kind 31903 : sémantique différente —
un cookie de session de navigateur n'est pas une clé d'API officielle, cf.
NIP-101/KIND_REGISTRY.md). Réutilise le même mécanisme de chiffrement que
services/cookie_store.py (natools.py seal + pin IPFS) — le contenu n'est
qu'un petit JSON de champs, pas un fichier cookies.txt.
"""

import json
import asyncio
import logging
logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Optional

from core.config import settings, ASTRO_PYTHON
from services.cookie_store import get_user_pubkey, encrypt_and_pin, decrypt_from_ipfs

NOSTR_SEND = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "nostr_send_note.py"
MANIFEST   = ".api_access_manifest.json"
KIND       = 31904

## Schéma des providers — ajouter un provider = ajouter une entrée ici,
## rien d'autre à toucher côté backend (router + UI lisent ce dict).
API_PROVIDERS: dict = {
    "mastodon": {
        "label": "Mastodon",
        "icon": "🐘",
        "fields": [
            {"key": "instance_url", "label": "Instance", "placeholder": "https://mastodon.social", "secret": False},
            {"key": "access_token", "label": "Token d'accès", "placeholder": "", "secret": True},
        ],
        "howto": "Sur votre instance Mastodon : Préférences → Développement → Nouvelle application. "
                 "Portée (scopes) : write:statuses read:notifications. Copiez le « token d'accès » généré "
                 "(pas la clé cliente/secrète).",
    },
    "telegram": {
        "label": "Telegram",
        "icon": "✈️",
        "fields": [
            {"key": "bot_token", "label": "Token du bot", "placeholder": "123456:ABC-...", "secret": True},
            {"key": "channel_id", "label": "ID canal (optionnel, pour annoncer)", "placeholder": "@mon_canal", "secret": False},
        ],
        "howto": "Ouvrez une conversation avec @BotFather sur Telegram, envoyez /newbot, suivez les "
                 "instructions, copiez le token fourni. Le canal/chat_id n'est nécessaire que si vous "
                 "voulez aussi publier des annonces (sinon laissez vide — la conversation privée avec "
                 "le bot fonctionne sans).",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "icon": "🧠",
        "fields": [
            {"key": "api_key", "label": "Clé API", "placeholder": "sk-ant-...", "secret": True},
            {"key": "model", "label": "Modèle (optionnel)", "placeholder": "claude-sonnet-5", "secret": False},
        ],
        "howto": "console.anthropic.com → API Keys → Create Key. Optionnel : BRO utilise par défaut "
                 "l'Ollama gratuit fourni par les stations DRAGON de la constellation — configurez "
                 "votre propre clé seulement si vous voulez vos questions traitées par ce modèle précis.",
    },
    "gemini": {
        "label": "Google Gemini",
        "icon": "✨",
        "fields": [
            {"key": "api_key", "label": "Clé API", "placeholder": "AIzaSy...", "secret": True},
            {"key": "model", "label": "Modèle (optionnel)", "placeholder": "gemini-2.0-flash", "secret": False},
        ],
        "howto": "aistudio.google.com/apikey → Create API key. Optionnel : BRO utilise par défaut "
                 "l'Ollama gratuit fourni par les stations DRAGON de la constellation — configurez "
                 "votre propre clé seulement si vous voulez vos questions traitées par ce modèle précis.",
    },
}


def load_manifest(user_dir: Path) -> dict:
    p = user_dir / MANIFEST
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def save_manifest(user_dir: Path, manifest: dict):
    import os
    p = user_dir / MANIFEST
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    os.chmod(p, 0o600)


async def get_provider_config(user_dir: Path, provider: str) -> Optional[dict]:
    """Déchiffre et retourne {field: value} pour ce provider, ou None si absent/échec."""
    manifest = load_manifest(user_dir)
    info = manifest.get(provider)
    if not info or not info.get("cid"):
        return None
    content = await decrypt_from_ipfs(info["cid"], user_dir)
    if not content:
        return None
    try:
        return json.loads(content.decode("utf-8"))
    except Exception:
        logger.warning(f"api_access_store: JSON invalide déchiffré pour provider={provider}")
        return None


async def set_provider_config(user_dir: Path, provider: str, values: dict) -> bool:
    """Chiffre + pin + met à jour le manifest + republie en NOSTR. Retourne True si OK."""
    pubkey = get_user_pubkey(user_dir)
    if not pubkey:
        logger.warning(f"api_access_store: pas de .secret.dunikey pour {user_dir.name}")
        return False
    content = json.dumps(values, ensure_ascii=False).encode("utf-8")
    cid = await encrypt_and_pin(content, pubkey)
    if not cid:
        return False

    manifest = load_manifest(user_dir)
    from datetime import datetime, timezone
    manifest[provider] = {"cid": cid, "updated_at": datetime.now(timezone.utc).isoformat()}
    save_manifest(user_dir, manifest)
    asyncio.create_task(publish_manifest_to_nostr(user_dir, manifest))
    return True


async def delete_provider_config(user_dir: Path, provider: str) -> Optional[str]:
    """Retire le provider du manifest, republie, retourne le CID à dépingler (ou None)."""
    manifest = load_manifest(user_dir)
    info = manifest.pop(provider, None)
    if info is None:
        return None
    save_manifest(user_dir, manifest)
    asyncio.create_task(publish_manifest_to_nostr(user_dir, manifest))
    return info.get("cid")


async def publish_manifest_to_nostr(user_dir: Path, manifest: dict):
    """Publie le manifest API Access complet en NOSTR kind 31904 (d=api_access).
    Seules les références CID sont publiées (jamais les tokens en clair), chiffrées
    IPFS avec la clé G1 propre de l'utilisateur — même discipline que le Cookie Vault."""
    nostr_key = user_dir / ".secret.nostr"
    if not nostr_key.exists():
        logger.debug(f"No .secret.nostr for {user_dir.name} — skip api_access manifest publish")
        return
    content = json.dumps(manifest, ensure_ascii=False)
    tags = json.dumps([
        ["d", "api_access"],
        ["t", "api_access"],
    ])
    try:
        proc = await asyncio.create_subprocess_exec(
            ASTRO_PYTHON, str(NOSTR_SEND),
            "--keyfile", str(nostr_key),
            "--content", content,
            "--tags", tags,
            "--kind", str(KIND),
            "--relays", "ws://127.0.0.1:7777",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode != 0:
            logger.warning(f"api_access manifest publish failed: {err.decode()[:200]}")
        else:
            logger.info(f"API access manifest → NOSTR kind {KIND} d=api_access ({len(manifest)} providers)")
    except Exception as e:
        logger.warning(f"NOSTR kind {KIND} api_access manifest publish failed: {e}")
