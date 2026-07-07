"""
utils/observability.py — Journal d'activité structuré (JSONL) pour UPassport.

Reproduit EXACTEMENT le schéma déjà en place ailleurs dans l'écosystème, pour que
BRO (l'agent IA) et le capitaine voient l'activité de l'API dans les mêmes flux/
digests JSONL — sans nouveau format, sans nouvelle source de vérité :

  - Niveau NODE/station : même fichier et même schéma que
    Astroport.ONE/IA/bro/bro_common_lib.sh::bro_log_event() et
    NIP-101/relay.writePolicy.plugin/filter/common.sh::nip101_log_event() —
    le "hub" unique ~/.zen/tmp/$IPFSNODEID/observability/node-activity.jsonl.
    Champ "script" = "upassport" pour distinguer la source dans le digest
    "NODE OBSERVABILITY DIGEST (24H)" de 20h12.process.sh.

  - Niveau BRO/utilisateur : même fichier et même schéma que
    Astroport.ONE/IA/observability.py::log_event() —
    ~/.zen/flashmem/<email>/observability/activity.jsonl. Permet au cycle RÊVE
    de BRO (memory_manager.reve_compress_slot) de voir un jour "cet utilisateur
    a fait telle action via l'API", pas seulement via le chat.

Les deux fonctions échouent TOUJOURS silencieusement : un problème
d'observabilité (disque plein, permissions, etc.) ne doit jamais faire planter
ni ralentir une requête de l'API UPassport.

Stdlib uniquement (os, json, time) — aucune nouvelle dépendance.
"""

import os
import json
import time
from pathlib import Path
from typing import Optional

ACTIVITY_RING_LIMIT = 200

# Cache mémoire process : le PeerID IPFS ne change jamais à chaud, inutile de
# relire ~/.ipfs/config à chaque évènement (potentiellement un par requête HTTP).
_ipfsnodeid_cache: Optional[str] = None


def get_ipfsnodeid() -> str:
    """Lit ~/.ipfs/config → .Identity.PeerID, exactement comme
    NIP-101/relay.writePolicy.plugin/filter/common.sh, pour rester cohérent
    avec la convention déjà en place partout ailleurs dans l'écosystème."""
    global _ipfsnodeid_cache
    if _ipfsnodeid_cache is not None:
        return _ipfsnodeid_cache
    node_id = "_local"
    try:
        config_path = Path.home() / ".ipfs" / "config"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            node_id = data.get("Identity", {}).get("PeerID") or "_local"
    except Exception:
        node_id = "_local"
    _ipfsnodeid_cache = node_id
    return node_id


def _trim_ring_buffer(path: Path, limit: int = ACTIVITY_RING_LIMIT) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > limit:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-limit:])
    except Exception:
        pass


def log_node_event(action: str, success: bool, category: Optional[str] = None,
                    latency_ms: Optional[float] = None, extra: Optional[dict] = None) -> None:
    """Niveau NODE/station. Append une ligne JSON dans le hub unique
    node-activity.jsonl (ring buffer 200 lignes), déjà alimenté par
    bro_log_event() (bash, Astroport.ONE) et nip101_log_event() (filtres
    relay strfry). Échoue toujours silencieusement."""
    if not action:
        return
    try:
        node_dir = Path.home() / ".zen" / "tmp" / get_ipfsnodeid() / "observability"
        path = node_dir / "node-activity.jsonl"
        os.makedirs(node_dir, exist_ok=True)
        event = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "script": "upassport",
            "action": action,
            "success": bool(success),
        }
        if category:
            event["category"] = category
        if latency_ms is not None:
            event["latency_ms"] = round(latency_ms, 1)
        if extra:
            event.update(extra)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        _trim_ring_buffer(path)
    except Exception:
        pass


def log_user_event(email: Optional[str], tool: str, action: str, success: bool,
                    latency_ms: Optional[float] = None, extra: Optional[dict] = None) -> None:
    """Niveau BRO/utilisateur. Même schéma que IA/observability.py::log_event(),
    même fichier (~/.zen/flashmem/<email>/observability/activity.jsonl).
    No-op silencieux si email/tool absent (ex: utilisateur roaming non résolu,
    échec d'authentification avant identification) — jamais d'exception."""
    if not email or "@" not in email or not tool:
        return
    try:
        user_dir = Path.home() / ".zen" / "flashmem" / email / "observability"
        path = user_dir / "activity.jsonl"
        os.makedirs(user_dir, exist_ok=True)
        event = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": tool,
            "action": action,
            "success": bool(success),
        }
        if latency_ms is not None:
            event["latency_ms"] = round(latency_ms, 1)
        if extra:
            event.update(extra)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        _trim_ring_buffer(path)
    except Exception:
        pass
