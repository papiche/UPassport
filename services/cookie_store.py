"""Cookie encrypted storage — encrypt with natools.py seal + IPFS + NOSTR kind 31903 (Cookie Vault)."""

import os
import json
import asyncio
import logging
logger = logging.getLogger(__name__)
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import settings, ASTRO_PYTHON

NATOOLS    = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "natools.py"
NOSTR_SEND = settings.ZEN_PATH / "Astroport.ONE" / "tools" / "nostr_send_note.py"
MANIFEST   = ".cookie_manifest.json"


def get_user_pubkey(user_dir: Path) -> Optional[str]:
    """Extract G1 public key from user's .secret.dunikey."""
    for name in (".secret.dunikey", "secret.dunikey"):
        p = user_dir / name
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith("pub:"):
                    return line.split(":", 1)[1].strip()
    return None


def load_manifest(user_dir: Path) -> dict:
    p = user_dir / MANIFEST
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def save_manifest(user_dir: Path, manifest: dict):
    p = user_dir / MANIFEST
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    os.chmod(p, 0o600)


async def encrypt_and_pin(content: bytes, pubkey: str) -> Optional[str]:
    """Seal-encrypt cookie bytes with G1 pubkey, add to IPFS. Returns CID or None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        plain = Path(tmpdir) / "cookie.txt"
        enc   = Path(tmpdir) / "cookie.enc"
        plain.write_bytes(content)

        proc = await asyncio.create_subprocess_exec(
            ASTRO_PYTHON, str(NATOOLS), "encrypt",
            "-p", pubkey, "-i", str(plain), "-o", str(enc),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("natools encrypt timed out")
            return None
        if proc.returncode != 0 or not enc.exists():
            logger.warning(f"natools encrypt failed: {err.decode()[:200]}")
            return None

        proc2 = await asyncio.create_subprocess_exec(
            "ipfs", "add", "-q", str(enc),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("ipfs add timed out for cookie")
            return None
        if proc2.returncode != 0:
            return None
        return out.decode().strip() or None


async def decrypt_from_ipfs(cid: str, user_dir: Path) -> Optional[bytes]:
    """Download encrypted cookie from IPFS and decrypt with .secret.dunikey."""
    dunikey = user_dir / ".secret.dunikey"
    if not dunikey.exists():
        dunikey = user_dir / "secret.dunikey"
    if not dunikey.exists():
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        enc   = Path(tmpdir) / "cookie.enc"
        plain = Path(tmpdir) / "cookie.txt"

        proc = await asyncio.create_subprocess_exec(
            "ipfs", "get", "-o", str(enc), f"/ipfs/{cid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(f"ipfs get timed out for CID {cid[:20]}")
            return None
        if not enc.exists():
            return None

        proc2 = await asyncio.create_subprocess_exec(
            ASTRO_PYTHON, str(NATOOLS), "decrypt",
            "-f", "pubsec", "-k", str(dunikey),
            "-i", str(enc), "-o", str(plain),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc2.communicate(), timeout=15)
        except asyncio.TimeoutError:
            return None
        if proc2.returncode != 0:
            logger.warning(f"natools decrypt failed: {err.decode()[:200]}")
            return None
        return plain.read_bytes() if plain.exists() else None


async def publish_manifest_to_nostr(user_dir: Path, manifest: dict):
    """Publie le manifest cookie complet en NOSTR kind 31903 (Cookie Vault, d=cookies).

    Le contenu est le manifest entier {domain: {cid, uploaded_at, size}} : seules les
    références CID sont publiées, jamais le contenu des cookies (qui reste dans IPFS chiffré).
    Kind 31903 est dédié aux cookies dans le registre UPlanet NIP-101.
    Un seul event remplaçable par utilisateur couvre tous ses domaines.
    """
    nostr_key = user_dir / ".secret.nostr"
    if not nostr_key.exists():
        logger.debug(f"No .secret.nostr for {user_dir.name} — skip cookie manifest publish")
        return
    content = json.dumps(manifest, ensure_ascii=False)
    tags = json.dumps([
        ["d", "cookies"],
        ["t", "cookies"],
        ["t", "uplanet"],
    ])
    try:
        proc = await asyncio.create_subprocess_exec(
            ASTRO_PYTHON, str(NOSTR_SEND),
            "--keyfile", str(nostr_key),
            "--content", content,
            "--tags", tags,
            "--kind", "31903",
            "--relays", "ws://127.0.0.1:7777",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15)
        logger.info(f"Cookie manifest → NOSTR kind 31903 d=cookies ({len(manifest)} domaines)")
    except Exception as e:
        logger.warning(f"NOSTR kind 31903 cookie manifest publish failed: {e}")


async def store_cookie_encrypted(user_dir: Path, domain: str, content: bytes, private: bool = False) -> Optional[str]:
    """Full pipeline: encrypt → IPFS pin → manifest update → NOSTR kind 31903 (d=cookies).

    Returns CID on success, None if encryption/IPFS unavailable (non-fatal).
    Disk file (plain Netscape) is written by the caller before this function.
    """
    pubkey = get_user_pubkey(user_dir)
    if not pubkey:
        logger.info(f"No G1 pubkey for {user_dir.name} — cookie stored on disk only")
        return None

    cid = await encrypt_and_pin(content, pubkey)
    if not cid:
        logger.warning(f"encrypt_and_pin failed for {domain} — disk-only fallback")
        return None

    manifest = load_manifest(user_dir)
    manifest[domain] = {
        "cid": cid,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "size": len(content),
        "domain": domain,
    }
    save_manifest(user_dir, manifest)
    logger.info(f"Cookie {domain} encrypted → IPFS {cid[:20]}… manifest updated")

    # NOSTR publish est fire-and-forget — publie le manifest entier (kind 31903 d=cookies)
    asyncio.create_task(publish_manifest_to_nostr(user_dir, manifest))

    return cid
