"""
services/roaming.py — Résolution de la station Home réelle d'un utilisateur roaming.

Extrait de routers/geo.py (aucune logique modifiée) pour être réutilisable depuis
plusieurs routers (geo.py::/api/myGPS, mailjet.py::/mailjet/auth) sans import
router → router.
"""

import json
from typing import Optional, Dict
from pathlib import Path

from core.config import settings


def _find_home_station_json(home_ipfsnodeid: Optional[str], user_email: Optional[str]) -> Optional[Path]:
    """Localise le `12345.json` mis en cache localement par le swarm P2P (Astroport.ONE)
    pour la home station d'un utilisateur roaming. Retourne None si non (encore)
    synchronisé dans le swarm local."""
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
            return direct

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
            return station_12345
    return None


def resolve_home_station(home_ipfsnodeid: Optional[str], user_email: Optional[str]) -> Dict[str, Optional[str]]:
    """Résout les URLs HTTP fiables (`uSPOT`, `myIPFS`) de la home station d'un
    utilisateur roaming, en lisant le `12345.json` que le swarm P2P a mis en cache
    localement dans `~/.zen/tmp/swarm/<peer>/`.

    IMPORTANT : `uSPOT`/`myIPFS` sont déjà calculés par `tools/my.sh` (via
    `myDomainName`/`zIp`, cf. `_12345.sh`) et sont les URLs effectivement utilisées
    partout ailleurs dans l'écosystème (Ustats.sh, common.js, uplanet-header.js).
    Le champ `hostname` du même JSON (`myHostName` = `hostname` OS + `myDomainName`)
    n'est que décoratif — sur une station sans `domainname`/`hostname -d` configuré
    (LAN, dev), `myDomainName` retombe sur `"localhost"`, produisant un hostname du
    style `nexus.localhost`, injoignable depuis un navigateur. Ne JAMAIS reconstruire
    une URL à partir de `hostname` : toujours lire `uSPOT`/`myIPFS` directement.

    Retourne `{"uSPOT": ..., "myIPFS": ...}` (valeurs `None` si absentes/non résolvable).
    """
    station_json = _find_home_station_json(home_ipfsnodeid, user_email)
    if not station_json:
        return {"uSPOT": None, "myIPFS": None}
    try:
        data = json.loads(station_json.read_text())
    except Exception:
        return {"uSPOT": None, "myIPFS": None}
    return {
        "uSPOT": data.get("uSPOT") or None,
        "myIPFS": data.get("myIPFS") or None,
    }


def resolve_home_http_url(home_ipfsnodeid: Optional[str], user_email: Optional[str]) -> Optional[str]:
    """URL HTTP API (uSPOT) de la home station — ou None si non résolvable.
    Enveloppe fine de resolve_home_station() pour les appelants qui n'ont besoin
    que de l'endpoint API (ex. mailjet.py::/mailjet/auth)."""
    return resolve_home_station(home_ipfsnodeid, user_email)["uSPOT"]
