"""
services/analysis.py — Où en sont les analyses d'un cloud, et par quel CHEMIN elles tournent.

Trois tâches portent sur chaque photo :
  faces     détection des visages (FaceID, InsightFace sur ComfyUI)
  enrich    date de prise de vue, lieu GPS, empreinte visuelle (doublons) — local, sans IA
  describe  description en français (modèle vision d'Ollama), modifiable à la main

MODE (choisi par le CAPITAINE de la station, `~/.zen/game/facecloud_analysis.json`) :
  auto    (défaut) accès DIRECT si la machine d'IA répond, sinon relais NOSTR
  direct  cette station parle elle-même à ComfyUI / Ollama (local, tunnel SSH ou P2P IPFS,
          cf. IA/services/comfyui.me.sh et ollama.me.sh) : ~1 photo/s (visages), pas de file
  relay   messages chiffrés NIP-44 entre stations (« nodeDM ») : la photo, sa clé et la tâche partent
          vers un Brain GPU qui renvoie le résultat — ≈ 1 minute par aller-retour, 6 en parallèle,
          file à durée de vie de 30 min (au-delà, le message expire)
`enrich` ne passe jamais par l'IA : il est toujours local.

Rien n'est importé ici hors bibliothèque standard : appelé aussi par les scripts CLI.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict, Optional

ZEN = Path.home() / ".zen"
CONFIG_FILE = ZEN / "game" / "facecloud_analysis.json"
MODES = ("auto", "direct", "relay")
COMFYUI_PORT = 8188
OLLAMA_PORT = 11434
INTERCOM = ZEN / "Astroport.ONE" / "tools" / "nostr_node_intercom.py"
NODE_SECRET = ZEN / "game" / "secret.nostr"

_probe_cache: Dict[int, tuple] = {}


def get_mode() -> str:
    try:
        mode = json.loads(CONFIG_FILE.read_text()).get("mode", "auto")
    except (OSError, ValueError, AttributeError):
        return "auto"
    return mode if mode in MODES else "auto"


def set_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"mode inconnu : {mode!r}")
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mode": mode, "set_at": int(time.time())}))
    os.replace(tmp, CONFIG_FILE)
    return mode


def port_open(port: int, ttl: float = 20.0) -> bool:
    """Le service écoute-t-il sur 127.0.0.1 (en local, ou au bout d'un tunnel déjà établi) ?
    Sonde rapide mémoïsée : `comfyui.me.sh` peut mettre 3 minutes à établir un tunnel, on ne le
    déclenche jamais depuis une simple lecture d'état."""
    hit = _probe_cache.get(port)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    ok = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            ok = True
    except OSError:
        pass
    _probe_cache[port] = (time.time(), ok)
    return ok


def relay_ready() -> bool:
    return INTERCOM.exists() and NODE_SECRET.exists()


def resolve(task: str, mode: Optional[str] = None) -> Dict[str, Any]:
    """{"configured", "effective", "direct_ready", "relay_ready", "why"} pour une tâche."""
    mode = mode or get_mode()
    port = OLLAMA_PORT if task == "describe" else COMFYUI_PORT
    direct, relay = port_open(port), relay_ready()
    if task == "enrich":
        return {"configured": mode, "effective": "local", "direct_ready": True, "relay_ready": False,
                "why": "Analyse locale (EXIF, empreinte) : aucune IA, rien ne quitte la station."}
    if mode == "direct":
        eff, why = "direct", ("La machine d'IA répond." if direct else
                              "Le capitaine impose l'accès direct, mais la machine d'IA ne répond pas encore : "
                              "la station tentera d'ouvrir le tunnel (jusqu'à 3 min) ou échouera.")
    elif mode == "relay":
        eff, why = "relay", ("Le capitaine impose le relais NOSTR (messages chiffrés vers un Brain)."
                             if relay else "Relais impossible : identité de la station ou canal inter-NODE introuvable.")
    else:
        if direct:
            eff, why = "direct", "Mode auto : la machine d'IA répond directement, aucun relais nécessaire."
        elif relay:
            eff, why = "relay", "Mode auto : pas d'accès direct à la machine d'IA, la station passe par le relais NOSTR."
        else:
            eff, why = "none", "Ni accès direct, ni relais : l'analyse ne peut pas démarrer."
    return {"configured": mode, "effective": eff, "direct_ready": direct, "relay_ready": relay, "why": why}


def refresh_info(nostr_dir: Path) -> Dict[str, Any]:
    """Heure du rafraîchissement quotidien du MULTIPASS (`NOSTRCARD.refresh.sh`).

    Chaque compte reçoit une heure aléatoire (`.refresh_time`, « HH:MM », étalée sur 00:00–20:12) ;
    le rafraîchissement a lieu UNE fois par jour, à la première exécution du script APRÈS cette heure
    (le démon de la station le lance au plus toutes les heures : compter jusqu'à 1 h de plus).
    C'est lui qui importe vos sources WebDAV et fait la sauvegarde FaceCloud du jour."""
    def read(name: str) -> str:
        try:
            return (nostr_dir / name).read_text().strip()
        except OSError:
            return ""
    t = read(".refresh_time")
    last = read(".todate")
    today = time.strftime("%Y%m%d")
    due = False
    if len(t) == 5 and t[2] == ":":
        due = time.strftime("%H:%M") >= t
    return {"time": t or None, "last": last or None, "done_today": last == today,
            "due": bool(t) and due and last != today, "today": today}
