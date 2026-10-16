"""
services/relay_io.py — Lire et publier des événements NOSTR sur les relais de la station (WebSocket synchrone, sans état).

`query(filter)` : événements dédoublonnés par id sur tous les relais joignables ; `strict=True` lève RelayUnavailable si AUCUN relais
n'a répondu (« pas de résultat » ne veut alors rien dire). `publish(raw_events)` : rend les relais qui ont TOUT accepté ; les refus
sont journalisés avec leur motif (jamais silencieux). Toutes les fonctions sont SYNCHRONES : à appeler via asyncio.to_thread.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

RELAYS = ["ws://127.0.0.1:7777", "wss://relay.copylaradio.com"]


class RelayUnavailable(RuntimeError):
    pass


def query(filt: Dict[str, Any], timeout: float = 8.0, strict: bool = False, relays: List[str] = None) -> List[Dict[str, Any]]:
    from websockets.sync.client import connect
    found: Dict[str, Dict[str, Any]] = {}
    answered = 0
    for relay in (relays or RELAYS):
        try:
            with connect(relay, open_timeout=timeout, close_timeout=3) as ws:
                ws.send(json.dumps(["REQ", "rio", filt]))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=timeout))
                    if msg[0] == "EVENT":
                        found[msg[2]["id"]] = msg[2]
                    elif msg[0] in ("EOSE", "CLOSED"):
                        answered += 1
                        break
        except Exception as exc:
            logger.warning("relay_io: %s injoignable (%s)", relay, exc)
    if strict and not answered:
        raise RelayUnavailable("aucun relais NOSTR ne répond")
    return list(found.values())


def publish(raw_events: List[Dict[str, Any]], relays: List[str] = None) -> List[str]:
    from websockets.sync.client import connect
    ok = []
    for relay in (relays or RELAYS):
        try:
            with connect(relay, open_timeout=10, close_timeout=5) as ws:
                accepted = 0
                for ev in raw_events:
                    ws.send(json.dumps(["EVENT", ev]))
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        msg = json.loads(ws.recv(timeout=10))
                        if msg[0] == "OK":
                            if msg[2]:
                                accepted += 1
                            else:
                                logger.warning("relay_io: %s refuse kind %s : %s", relay, ev.get("kind"), msg[3] if len(msg) > 3 else "")
                            break
                if accepted == len(raw_events):
                    ok.append(relay)
        except Exception as exc:
            logger.warning("relay_io: %s injoignable (%s)", relay, exc)
    return ok
