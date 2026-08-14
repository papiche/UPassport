"""
services/roaming.py — Résolution de la station Home réelle d'un utilisateur roaming.

Extrait de routers/geo.py (aucune logique modifiée) pour être réutilisable depuis
plusieurs routers (geo.py::/api/myGPS, mailjet.py::/mailjet/auth) sans import
router → router.
"""

import json
from typing import Optional

from core.config import settings


def resolve_home_hostname(home_ipfsnodeid: Optional[str], user_email: Optional[str]) -> Optional[str]:
    """Résout le hostname HTTP réel (ex: "sagittarius.copylaradio.com") de la home
    station d'un utilisateur roaming, en lisant le 12345.json que le swarm P2P
    (Astroport.ONE) a mis en cache localement dans ~/.zen/tmp/swarm/<peer>/.

    NOSTRNS ne donne qu'une adresse IPFS de contenu (/ipns/k51...), pas l'endpoint
    HTTP UPassport de la station (u.<hostname>) qui sert réellement /earth/*.html —
    d'où ce scan séparé. Retourne None si la home station n'est pas (encore)
    synchronisée dans le swarm local.
    """
    swarm_dir = settings.ZEN_PATH / "tmp" / "swarm"
    if not swarm_dir.exists():
        return None
    try:
        station_dirs = [d for d in swarm_dir.iterdir() if d.is_dir()]
    except Exception:
        return None

    # 1. Match rapide : dossier swarm nommé exactement d'après le peer IPFS
    if home_ipfsnodeid:
        direct = swarm_dir / home_ipfsnodeid / "12345.json"
        if direct.exists():
            try:
                hostname = json.loads(direct.read_text()).get("hostname")
                if hostname:
                    return hostname
            except Exception:
                pass

    # 2. Fallback : scan des 12345.json, match par ipfsnodeid déclaré ou par TW/<email>
    for station_dir in station_dirs:
        station_12345 = station_dir / "12345.json"
        if not station_12345.exists():
            continue
        try:
            data = json.loads(station_12345.read_text())
        except Exception:
            continue
        matches_id    = home_ipfsnodeid and data.get("ipfsnodeid") == home_ipfsnodeid
        matches_email = user_email and (station_dir / "TW" / user_email).exists()
        if matches_id or matches_email:
            hostname = data.get("hostname")
            if hostname:
                return hostname
    return None


def resolve_home_http_url(home_ipfsnodeid: Optional[str], user_email: Optional[str]) -> Optional[str]:
    """URL HTTP UPassport (https://u.<hostname>) de la home station — ou None
    si non résolvable. Enveloppe fine de resolve_home_hostname()."""
    hostname = resolve_home_hostname(home_ipfsnodeid, user_email)
    return f"https://u.{hostname}" if hostname else None
