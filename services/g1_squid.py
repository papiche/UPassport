"""
services/g1_squid.py
────────────────────
Service Python natif pour interroger l'indexeur Squid Duniter v2.
Remplace les appels shell à G1history.sh et G1balance.sh par des
requêtes httpx asynchrones directes — 10× plus rapides (pas de fork OS).

Gestion des nœuds
-----------------
1. Lit ~/.zen/tmp/duniter_nodes.json (généré par duniter_getnode.sh, TTL 1h)
   → liste Squid déjà health-checkée et triée par latence croissante.
2. Si le cache est absent / expiré → utilise settings.SQUID_FALLBACKS.

API publique
------------
  g1pub_to_ss58(g1pub)                   → str  (SS58, synchrone)
  get_squid_urls()                       → list[str]  (ordre latence)
  get_g1_history_native(g1pub, limit)    → dict {"history": [...]}
  get_g1_balance_native(g1pub)           → dict {"balances": {...}}
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────
_CACHE_FILE = Path.home() / ".zen" / "tmp" / "duniter_nodes.json"
_CACHE_TTL = 3600        # secondes (identique à duniter_getnode.sh)
_PICK_TOP_N = 3          # nombre de meilleurs nœuds dans lesquels piocher (load balancing)

_BOOTSTRAP_SQUIDS = [
    "https://squid.g1.gyroi.de/v1/graphql",
    "https://squid.g1.coinduf.eu/v1/graphql",
    "https://g1-squid.axiom-team.fr/v1/graphql",
    "https://indexer.duniter.org/v1/graphql",
    "https://g1-squid.cgeek.fr/v1/graphql",
]
_BOOTSTRAP_RPC = [
    "wss://g1.1000i100.fr/ws",
    "wss://g1-v2s.cgeek.fr",
    "wss://g1.coinduf.eu",
    "wss://g1.gyroi.de",
    "wss://rpc.duniter.org",
]

# ── Alphabet Base58 Bitcoin (identique à Substrate / Duniter) ─────────────────
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    alphabet = _B58_ALPHABET
    n = 0
    for c in s.encode():
        n = n * 58 + alphabet.index(c)
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + result


def _b58encode(b: bytes) -> str:
    alphabet = _B58_ALPHABET
    n = int.from_bytes(b, "big")
    result = []
    while n:
        n, rem = divmod(n, 58)
        result.append(alphabet[rem: rem + 1])
    result.reverse()
    pad = len(b) - len(b.lstrip(b"\x00"))
    return (b"1" * pad + b"".join(result)).decode()


def g1pub_to_ss58(g1pub: str, network_prefix: int = 42) -> str:
    """
    Convertit une pubkey Ğ1 v1 (Base58 Bitcoin, 32 octets Ed25519)
    en format SS58 Substrate (préfixe réseau 42 pour Ğ1).

    Si la pubkey est déjà SS58 (raw décodé ≠ 32 octets), elle est renvoyée telle quelle.
    Ne lève jamais d'exception.
    """
    if not g1pub:
        return g1pub
    try:
        raw = _b58decode(g1pub)
        if len(raw) != 32:
            # Clé déjà en SS58 ou format inconnu
            return g1pub

        # Encodage SCALE du préfixe réseau
        if network_prefix < 64:
            prefix_bytes = bytes([network_prefix])
        else:
            first = ((network_prefix & 0xFC) >> 2) | 0x40
            second = (network_prefix >> 8) | ((network_prefix & 0x3) << 6)
            prefix_bytes = bytes([first, second])

        # Checksum SS58 : BLAKE2b-512(b"SS58PRE" + prefix + pubkey)[0:2]
        checksum = hashlib.blake2b(
            b"SS58PRE" + prefix_bytes + raw, digest_size=64
        ).digest()[:2]

        encoded = _b58encode(prefix_bytes + raw + checksum)
        logger.debug("g1pub_to_ss58 : %s… → %s…", g1pub[:12], encoded[:12])
        return encoded
    except Exception as exc:
        logger.debug("g1pub_to_ss58 échec (%s), renvoi brut : %s", exc, g1pub[:16])
        return g1pub


# ── Lecture du cache duniter_getnode.sh ──────────────────────────────────────

def _load_node_cache() -> Optional[dict]:
    """
    Lit ~/.zen/tmp/duniter_nodes.json produit par duniter_getnode.sh.
    Format : {"timestamp": <unix>, "rpc": [...], "squid": [{"url":..., "latency":...}, ...]}
    Retourne None si absent ou expiré (TTL = 1h).
    """
    try:
        if not _CACHE_FILE.exists():
            return None
        with open(_CACHE_FILE, "r") as fh:
            data = json.load(fh)
        ts = int(data.get("timestamp", 0))
        if (int(time.time()) - ts) > _CACHE_TTL:
            logger.debug("Cache duniter_nodes.json expiré (%ds)", int(time.time()) - ts)
            return None
        return data
    except Exception as exc:
        logger.debug("Erreur lecture cache duniter_nodes.json : %s", exc)
        return None


def get_squid_urls() -> List[str]:
    """
    Retourne la liste des URLs Squid ordonnées par latence croissante.

    Priorité :
      1. ~/.zen/tmp/duniter_nodes.json (.squid[].url, trié par latence)
      2. settings.SQUID_FALLBACKS (hardcodés dans config.py)
      3. _BOOTSTRAP_SQUIDS (liste interne)

    Pick aléatoire parmi les _PICK_TOP_N meilleurs pour le load-balancing
    (reproduit le comportement de pick_node de duniter_getnode.sh).
    """
    # 1. Cache duniter_getnode.sh
    cache = _load_node_cache()
    if cache:
        nodes = cache.get("squid", [])
        if nodes:
            # Trier par latence (déjà trié par le script, mais on s'assure)
            nodes_sorted = sorted(nodes, key=lambda n: n.get("latency", 9999))
            top = [n["url"] for n in nodes_sorted if n.get("url")]
            # Load-balancing : on mélange les _PICK_TOP_N meilleurs
            best = top[:_PICK_TOP_N]
            rest = top[_PICK_TOP_N:]
            random.shuffle(best)
            result = best + rest
            logger.debug("Squids depuis cache (%d) : %s…", len(result), result[0][:40] if result else "")
            return result

    # 2. Settings
    try:
        from core.config import settings
        cfg_urls = [settings.SQUID_URL] + list(settings.SQUID_FALLBACKS)
        cfg_urls = list(dict.fromkeys(cfg_urls))  # déduplique, préserve l'ordre
        if cfg_urls:
            logger.debug("Squids depuis settings (%d)", len(cfg_urls))
            return cfg_urls
    except Exception:
        pass

    # 3. Bootstrap interne
    logger.debug("Squids bootstrap (%d)", len(_BOOTSTRAP_SQUIDS))
    return list(_BOOTSTRAP_SQUIDS)


def get_rpc_nodes() -> List[str]:
    """
    Retourne la liste des nœuds RPC Duniter ordonnés par latence.

    Priorité :
      1. ~/.zen/tmp/duniter_nodes.json (.rpc[].url, trié par latence)
      2. settings.G1_WS_NODE + settings.G1_RPC_FALLBACKS (config.py)
      3. _BOOTSTRAP_RPC (liste interne)
    """
    cache = _load_node_cache()
    if cache:
        nodes = cache.get("rpc", [])
        if nodes:
            nodes_sorted = sorted(nodes, key=lambda n: n.get("latency", 9999))
            top = [n["url"] for n in nodes_sorted if n.get("url")]
            if top:
                logger.debug("RPC depuis cache (%d) : %s…", len(top), top[0][:40])
                return top

    try:
        from core.config import settings
        primary = getattr(settings, "G1_WS_NODE", _BOOTSTRAP_RPC[0])
        fallbacks = list(getattr(settings, "G1_RPC_FALLBACKS", _BOOTSTRAP_RPC))
        # Déduplique en préservant l'ordre (primary en tête)
        all_nodes = list(dict.fromkeys([primary] + fallbacks))
        logger.debug("RPC depuis settings (%d)", len(all_nodes))
        return all_nodes
    except Exception:
        return list(_BOOTSTRAP_RPC)


# ── Requêtes GraphQL ──────────────────────────────────────────────────────────

_HISTORY_QUERY = """
query($a: String!, $n: Int!) {
  received: transfers(condition: {toId: $a}, orderBy: BLOCK_NUMBER_DESC, first: $n) {
    nodes { fromId toId amount timestamp blockNumber comment { remark } }
  }
  sent: transfers(condition: {fromId: $a}, orderBy: BLOCK_NUMBER_DESC, first: $n) {
    nodes { fromId toId amount timestamp blockNumber comment { remark } }
  }
}
"""

_BALANCE_QUERY = """
query($a: String!) {
  account(id: $a) {
    id
    balance
    linkedAccount
  }
}
"""


def _parse_history(raw: dict, g1pub: str) -> dict:
    """
    Transforme la réponse Squid en format compatible G1history.sh :
      {"history": [{"Date", "Amounts Ğ1", "Issuers/Recipients",
                    "Reference", "blockNumber", "direction"}, ...]}
    Montants en Ğ1 (centimes/100). Positif = reçu, négatif = envoyé.
    """
    data = raw.get("data", {})
    received_nodes = (data.get("received") or {}).get("nodes") or []
    sent_nodes = (data.get("sent") or {}).get("nodes") or []

    history = []
    for node in received_nodes:
        history.append({
            "Date": node.get("timestamp", ""),
            "Amounts Ğ1": (node.get("amount") or 0) / 100,
            "Issuers/Recipients": node.get("fromId", ""),
            "Reference": (node.get("comment") or {}).get("remark", "") or "",
            "blockNumber": node.get("blockNumber", 0),
            "direction": "received",
        })
    for node in sent_nodes:
        history.append({
            "Date": node.get("timestamp", ""),
            "Amounts Ğ1": -((node.get("amount") or 0) / 100),
            "Issuers/Recipients": node.get("toId", ""),
            "Reference": (node.get("comment") or {}).get("remark", "") or "",
            "blockNumber": node.get("blockNumber", 0),
            "direction": "sent",
        })
    # Tri descendant par blockNumber (identique à G1history.sh)
    history.sort(key=lambda x: x["blockNumber"], reverse=True)
    return {"history": history}


async def get_g1_history_native(g1pub: str, limit: int = 50) -> dict:
    """
    Équivalent Python de G1history.sh.
    Interroge directement le Squid Duniter v2 via httpx (pas de processus OS).

    Utilise le cache duniter_nodes.json pour sélectionner le Squid le plus rapide,
    avec load-balancing parmi les _PICK_TOP_N meilleurs nœuds.

    Retourne {"history": [...]} ou {"history": []} si tous les Squids échouent.
    """
    if not g1pub:
        return {"history": []}

    ss58 = g1pub_to_ss58(g1pub)
    payload = {
        "query": _HISTORY_QUERY,
        "variables": {"a": ss58, "n": limit},
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        for url in get_squid_urls():
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    # Vérifie que la réponse est valide (clé "received" présente)
                    if (data.get("data") or {}).get("received") is not None:
                        logger.info(
                            "G1history natif OK pour %s… via %s", g1pub[:12], url
                        )
                        return _parse_history(data, g1pub)
                    logger.debug("Réponse Squid incomplète depuis %s", url)
            except Exception as exc:
                logger.debug("G1history échec sur %s : %s", url, exc)

    logger.warning("G1history natif : aucun Squid disponible pour %s…", g1pub[:12])
    return {"history": []}


async def get_g1_balance_native(g1pub: str) -> dict:
    """
    Équivalent Python de G1balance.sh.
    Lit la balance en centimes depuis le Squid Duniter v2 via httpx.

    Utilise le cache duniter_nodes.json pour sélectionner le Squid le plus rapide.
    Fallback automatique vers gcli subprocess si tous les Squids échouent.

    Retourne {"balances": {"pending": 0, "blockchain": <centimes>, "total": <centimes>}}.
    """
    _empty = {"balances": {"pending": 0, "blockchain": 0, "total": 0}}
    if not g1pub:
        return _empty

    ss58 = g1pub_to_ss58(g1pub)
    payload = {"query": _BALANCE_QUERY, "variables": {"a": ss58}}

    async with httpx.AsyncClient(timeout=15.0) as client:
        for url in get_squid_urls():
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    account = (data.get("data") or {}).get("account")
                    if account is not None:
                        raw_balance = int(account.get("balance") or 0)
                        logger.info(
                            "G1balance natif squid OK pour %s… : %d centimes via %s",
                            g1pub[:12], raw_balance, url,
                        )
                        return {
                            "balances": {
                                "pending": 0,
                                "blockchain": raw_balance,
                                "total": raw_balance,
                            }
                        }
            except Exception as exc:
                logger.debug("G1balance squid échec sur %s : %s", url, exc)

    # ── Fallback gcli (source de vérité on-chain) ─────────────────────────────
    logger.info(
        "G1balance natif : tous les Squids indisponibles pour %s…, fallback gcli",
        g1pub[:12],
    )
    return await _get_g1_balance_gcli_fallback(g1pub, ss58)


async def _get_g1_balance_gcli_fallback(g1pub: str, ss58: str) -> dict:
    """
    Fallback : appelle gcli via subprocess asyncio si le Squid est indisponible.
    Utilise le cache duniter_nodes.json pour choisir le meilleur nœud RPC,
    reproduisant la logique de G1balance.sh (gcli account balance -o json).
    """
    _empty = {"balances": {"pending": 0, "blockchain": 0, "total": 0}}
    rpc_nodes = get_rpc_nodes()

    for node in rpc_nodes:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gcli", "--no-password",
                "-a", ss58,
                "-u", node,
                "-o", "json",
                "account", "balance",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
            except asyncio.TimeoutError:
                proc.kill()
                logger.debug("gcli timeout sur %s", node)
                continue

            text = stdout.decode().strip()
            if text:
                parsed = json.loads(text)
                raw = int(parsed.get("total_balance") or 0)
                if raw > 0:
                    logger.info(
                        "G1balance gcli OK pour %s… via %s : %d centimes",
                        g1pub[:12], node, raw,
                    )
                    return {
                        "balances": {
                            "pending": 0,
                            "blockchain": raw,
                            "total": raw,
                        }
                    }
        except FileNotFoundError:
            logger.warning("gcli introuvable — balance indisponible pour %s…", g1pub[:12])
            break
        except Exception as exc:
            logger.debug("gcli échec sur %s : %s", node, exc)

    logger.error("G1balance : tous les nœuds ont échoué pour %s…", g1pub[:12])
    return _empty
