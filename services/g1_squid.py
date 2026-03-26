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
    "wss://g1.p2p.legal/ws",
    "wss://rpc.duniter.org",
    "wss://g1.axiom-team.fr:443/ws/",
]

# Cache en mémoire : url_base → url_résolue (path /ws détecté automatiquement)
_rpc_url_cache: dict = {}

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


# Préfixe SS58 officiel du réseau Ğ1 Duniter v2
# Source : https://github.com/duniter/duniter-v2s — les adresses commencent par "g1"
_G1_SS58_PREFIX = 4450


def g1pub_to_ss58(g1pub: str) -> str:
    """
    Convertit une pubkey Ğ1 en format SS58 Duniter v2 (préfixe réseau 4450).

    Détection du format d'entrée :
      - Déjà SS58 Duniter v2 : commence par "g1" et longueur > 44 chars
        → retourné tel quel (les adresses v2 ont 48-50 chars)
      - Clé publique Ed25519 v1 : 32 bytes en Base58 Bitcoin (43-44 chars)
        → conversion vers SS58 avec préfixe 4450 (2 octets SCALE)
      - Autre format non reconnu → renvoyé tel quel

    Ne lève jamais d'exception.
    Implémentation identique à g1pub_to_ss58.py (Astroport.ONE/tools).
    """
    if not g1pub:
        return g1pub

    # Détection SS58 Duniter v2 : commence par "g1" et plus long qu'une pubkey v1
    if g1pub.startswith("g1") and len(g1pub) > 44:
        logger.debug("g1pub_to_ss58 : déjà SS58 Duniter v2 (%s…)", g1pub[:12])
        return g1pub

    try:
        raw = _b58decode(g1pub)
        if len(raw) != 32:
            # Format inconnu (ni v1 pur 32 bytes, ni déjà SS58 détecté ci-dessus)
            logger.debug(
                "g1pub_to_ss58 : %d bytes, format inconnu, renvoi brut (%s…)",
                len(raw), g1pub[:12],
            )
            return g1pub

        # Clé publique Ed25519 v1 (32 bytes) → SS58 avec préfixe 4450
        # Encodage SCALE 2-octets (préfixe >= 64)
        first  = ((_G1_SS58_PREFIX & 0xFC) >> 2) | 0x40
        second = (_G1_SS58_PREFIX >> 8) | ((_G1_SS58_PREFIX & 0x3) << 6)
        prefix_bytes = bytes([first, second])

        # Checksum SS58 : BLAKE2b-512(b"SS58PRE" + prefix + pubkey)[0:2]
        checksum = hashlib.blake2b(
            b"SS58PRE" + prefix_bytes + raw, digest_size=64
        ).digest()[:2]

        encoded = _b58encode(prefix_bytes + raw + checksum)
        logger.debug("g1pub_to_ss58 : %s… (v1→SS58) → %s…", g1pub[:12], encoded[:12])
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


async def _resolve_rpc_url(url: str) -> str:
    """
    Détecte automatiquement le bon path WebSocket d'un nœud Duniter RPC.

    Stratégie : tente une requête HTTP JSON-RPC (system_chain) sur les variantes
    https://host, https://host/ws, https://host/ws/ en ordre, puis
    retourne l'URL wss:// correspondante qui répond.
    Le résultat est mis en cache en mémoire (_rpc_url_cache) pour la durée
    de vie du processus — aucune re-probe inutile.
    """
    if url in _rpc_url_cache:
        return _rpc_url_cache[url]

    base = url.rstrip("/")
    # Génère toutes les variantes wss://, avec probe via https://
    variants_ws = [base]
    if not base.endswith("/ws"):
        variants_ws += [base + "/ws", base + "/ws/"]
    else:
        variants_ws += [base + "/"]

    _probe_payload = {"jsonrpc": "2.0", "method": "system_chain", "params": [], "id": 1}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for ws_url in variants_ws:
                # Convertit wss:// → https:// pour le probe HTTP
                http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")
                # Retire /ws ou /ws/ en fin pour l'endpoint HTTP JSON-RPC
                http_probe = http_url.rstrip("/")
                if http_probe.endswith("/ws"):
                    http_probe = http_probe[:-3]
                try:
                    resp = await client.post(http_probe, json=_probe_payload)
                    if resp.status_code == 200 and resp.json().get("result"):
                        _rpc_url_cache[url] = ws_url
                        logger.info("RPC URL résolue : %s → %s", url, ws_url)
                        return ws_url
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("_resolve_rpc_url probe échec pour %s : %s", url, exc)

    # Aucun ne répond via HTTP : conserver l'URL originale
    _rpc_url_cache[url] = url
    logger.debug("RPC URL non résolue, fallback original : %s", url)
    return url


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

# Requête principale — schéma singulier Subquery/Duniter v2
_BALANCE_QUERY = """
query($a: String!) {
  account(id: $a) {
    id
    balance
    linkedAccount
  }
}
"""

# Requête alternative — schéma pluriel (autres déploiements Squid)
_BALANCE_QUERY_PLURAL = """
query($a: String!) {
  accounts(condition: {id: $a}) {
    nodes { id balance linkedAccount }
  }
}
"""

# Requête via linkedAccount (la pubkey originale peut être stockée comme linkedAccount)
_BALANCE_QUERY_LINKED = """
query($a: String!) {
  accounts(condition: {linkedAccount: $a}) {
    nodes { id balance linkedAccount }
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

    # Variantes de requêtes à essayer, dans l'ordre de priorité :
    # 1. account(id) singulier        → schéma standard Subquery Duniter v2
    # 2. accounts(condition) pluriel  → schéma alternatif
    # 3. accounts(linkedAccount)      → la pubkey v1 stockée comme linkedAccount
    # Pour chaque variante, on essaie avec ss58 ET la pubkey originale brute
    queries_to_try = [
        (_BALANCE_QUERY,        "account",      ss58,   "account(id=SS58)"),
        (_BALANCE_QUERY,        "account",      g1pub,  "account(id=v1)"),
        (_BALANCE_QUERY_PLURAL, "accounts_nodes", ss58, "accounts(cond.id=SS58)"),
        (_BALANCE_QUERY_PLURAL, "accounts_nodes", g1pub,"accounts(cond.id=v1)"),
        (_BALANCE_QUERY_LINKED, "accounts_nodes", g1pub,"accounts(linkedAccount=v1)"),
        (_BALANCE_QUERY_LINKED, "accounts_nodes", ss58, "accounts(linkedAccount=SS58)"),
    ]

    async with httpx.AsyncClient(timeout=15.0) as client:
        for url in get_squid_urls():
            squid_ok = False
            for query, result_key, param, variant_label in queries_to_try:
                try:
                    resp = await client.post(url, json={"query": query, "variables": {"a": param}})
                    if resp.status_code != 200:
                        continue
                    squid_ok = True
                    data = resp.json()

                    # Extraction selon le type de requête
                    if result_key == "account":
                        account = (data.get("data") or {}).get("account")
                        if account is not None:
                            raw_balance = int(account.get("balance") or 0)
                            logger.info(
                                "G1balance squid OK [%s] pour %s… : %d centimes via %s",
                                variant_label, g1pub[:12], raw_balance, url,
                            )
                            return {"balances": {"pending": 0, "blockchain": raw_balance, "total": raw_balance}}
                    else:  # accounts_nodes
                        nodes = ((data.get("data") or {}).get("accounts") or {}).get("nodes") or []
                        if nodes:
                            raw_balance = int((nodes[0].get("balance") or 0))
                            logger.info(
                                "G1balance squid OK [%s] pour %s… : %d centimes via %s",
                                variant_label, g1pub[:12], raw_balance, url,
                            )
                            return {"balances": {"pending": 0, "blockchain": raw_balance, "total": raw_balance}}

                    # Log diagnostic : le Squid répond mais le compte est absent
                    logger.warning(
                        "G1balance squid [%s] null pour %s… (param=%s…) via %s — réponse: %s",
                        variant_label, g1pub[:12], param[:16], url,
                        json.dumps(data)[:300],
                    )
                except Exception as exc:
                    logger.debug("G1balance squid [%s] exception sur %s : %s", variant_label, url, exc)

            if squid_ok:
                # Le Squid a répondu sur toutes les variantes mais aucune n'a retourné de balance →
                # inutile d'essayer les autres Squids pour ce compte
                break

    # ── Fallback gcli puis G1check.sh ─────────────────────────────────────────
    logger.warning(
        "G1balance : Squid ne contient pas le compte %s…, fallback gcli/G1check.sh",
        g1pub[:12],
    )
    return await _get_g1_balance_gcli_fallback(g1pub, ss58)


async def _get_g1_balance_gcli_fallback(g1pub: str, ss58: str) -> dict:
    """
    Fallback : appelle gcli via subprocess asyncio si le Squid est indisponible.
    Utilise le cache duniter_nodes.json pour choisir le meilleur nœud RPC.
    Détecte automatiquement le bon path /ws / /ws/ via _resolve_rpc_url()
    (probe HTTP JSON-RPC mis en cache en mémoire).
    """
    _empty = {"balances": {"pending": 0, "blockchain": 0, "total": 0}}
    rpc_nodes = get_rpc_nodes()

    for node in rpc_nodes:
        try:
            # Résolution automatique du path /ws ou /ws/ (mis en cache)
            resolved = await _resolve_rpc_url(node)
            proc = await asyncio.create_subprocess_exec(
                "gcli", "--no-password",
                "-a", ss58,
                "-u", resolved,
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
            logger.warning("gcli introuvable — balance indisponible pour %s…, fallback G1check.sh", g1pub[:12])
            break
        except Exception as exc:
            logger.debug("gcli échec sur %s : %s", node, exc)

    # ── Super-fallback : G1check.sh (toujours disponible, fetch parallèle RPC) ─
    logger.warning(
        "G1balance : gcli et Squid échoués pour %s…, super-fallback G1check.sh", g1pub[:12]
    )
    return await _get_g1_balance_g1check_fallback(g1pub)


async def _get_g1_balance_g1check_fallback(g1pub: str) -> dict:
    """
    Super-fallback : appelle G1check.sh (fetch parallèle RPC multi-nœuds).
    G1check.sh retourne le solde en Ğ1 sur la dernière ligne (ex: "881.00").
    Convertit en centimes pour uniformiser avec le reste du service.
    """
    _empty = {"balances": {"pending": 0, "blockchain": 0, "total": 0}}
    try:
        from core.config import settings
        g1check = settings.TOOLS_PATH / "G1check.sh"
        if not g1check.exists():
            logger.error("G1check.sh introuvable : %s", g1check)
            return _empty

        proc = await asyncio.create_subprocess_exec(
            str(g1check), g1pub,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("G1check.sh timeout pour %s…", g1pub[:12])
            return _empty

        # G1check.sh écrit la valeur numérique seule sur la dernière ligne
        last_line = stdout.decode().strip().splitlines()[-1].split()[0]
        g1_value = float(last_line)
        centimes = int(round(g1_value * 100))
        logger.info(
            "G1balance G1check.sh OK pour %s… : %.2f Ğ1 = %d centimes",
            g1pub[:12], g1_value, centimes,
        )
        return {
            "balances": {
                "pending": 0,
                "blockchain": centimes,
                "total": centimes,
            }
        }
    except Exception as exc:
        logger.error("G1balance G1check.sh échec pour %s… : %s", g1pub[:12], exc)
        return _empty
