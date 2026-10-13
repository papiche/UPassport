"""
services/storage_quota.py — Espace disque d'un MULTIPASS : mesure, offre, refus propre.

Deux offres (cf. Astroport.ONE/RUNTIME/ZEN.ECONOMY.sh, NCARD / ZCARD) :
  MULTIPASS   10 Go   NCARD = 1 Ẑ / semaine
  ZENCARD    128 Go   NCARD + ZCARD = 1 + 4 = 5 Ẑ / semaine      (dossier ~/.zen/game/players/<email>)

L'espace utilisé = le cloud chiffré `.ucloud` (somme des tailles de l'index SQLite) + le dossier public
`APP/uDRIVE`. Surcharge par le capitaine : octets dans `<multipass>/.storage_quota`.

Règle : au-delà du forfait, l'utilisateur entre en PÉRIODE DE GRÂCE (une lettre l'invite à devenir parrain de
l'infrastructure ẐEN, sans lui dire jusqu'où il peut aller) ; les NOUVEAUX fichiers ne sont refusés qu'au-delà de
GRACE_FACTOR × forfait (2 par défaut, `STORAGE_GRACE_FACTOR`). On ne supprime jamais rien. Les petits fichiers
(< SMALL_FREE : carnet d'adresses, descriptions, réglages) sont toujours acceptés — un compte plein doit
pouvoir continuer à se synchroniser et à libérer de la place.

Bibliothèque standard + ucloud_db seulement : appelé aussi par les scripts CLI (`ucloud_usage.py`).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

GO = 1000 ** 3
SMALL_FREE = 256 * 1024
SPONSOR_URL = os.environ.get("OC_URL_PARRAIN") or "https://opencollective.com/monnaie-libre/contribute"
USAGE_FILE = ".storage_usage.json"
_CACHE_TTL = 60                      # secondes : le total `.ucloud` (une requête SQL)
_UDRIVE_TTL = 600                    # secondes : le parcours du dossier public

_cache: Dict[str, Any] = {}


class QuotaExceeded(RuntimeError):
    """Écriture refusée : le MULTIPASS a atteint son espace."""

    def __init__(self, used: int, quota: int, incoming: int):
        self.used, self.quota, self.incoming = used, quota, incoming
        super().__init__(f"ERROR:quota:{used} + {incoming} octets > {quota} octets autorisés")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def offers() -> Dict[str, Dict[str, Any]]:
    ncard, zcard = _num("NCARD", 1), _num("ZCARD", 4)
    return {
        "multipass": {"label": "MULTIPASS", "quota": 10 * GO, "zen_week": ncard},
        "zencard": {"label": "ZenCard", "quota": 128 * GO, "zen_week": ncard + zcard},
    }


def grace_factor() -> float:
    return max(1.0, _num("STORAGE_GRACE_FACTOR", 2.0))


def tier_for(nostr_dir: Path) -> str:
    """`zencard` si le MULTIPASS a aussi une ZenCard sur cette station (même test que ZEN.ECONOMY.sh)."""
    card = nostr_dir.parent.parent / "players" / nostr_dir.name / "secret.dunikey"
    return "zencard" if card.is_file() else "multipass"


def quota_bytes(nostr_dir: Path) -> int:
    try:
        override = int((nostr_dir / ".storage_quota").read_text().strip())
        if override > 0:
            return override
    except (OSError, ValueError):
        pass
    return int(offers()[tier_for(nostr_dir)]["quota"])


def hard_limit(nostr_dir: Path) -> int:
    """Seuil de refus : forfait × facteur de grâce. N'est JAMAIS montré à l'utilisateur."""
    return int(quota_bytes(nostr_dir) * grace_factor())


def _tree_bytes(root: Path) -> int:
    total, stack = 0, [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def usage(nostr_dir: Path, fresh: bool = False) -> Dict[str, int]:
    """{ucloud, udrive, used} en octets. Mémoïsé (60 s / 10 min) : appelé à chaque envoi."""
    from services import ucloud_db
    key, now = str(nostr_dir), time.time()
    hit = _cache.get(key)
    if hit and not fresh and now - hit["at_u"] < _CACHE_TTL:
        return hit["val"]
    udir = nostr_dir / ".ucloud"
    cloud = int(ucloud_db.stats(udir)["bytes"]) if ucloud_db.exists(udir) else 0
    if hit and not fresh and now - hit["at_d"] < _UDRIVE_TTL:
        drive, at_d = hit["val"]["udrive"], hit["at_d"]
    else:
        drive, at_d = _tree_bytes(nostr_dir / "APP" / "uDRIVE"), now
    val = {"ucloud": cloud, "udrive": drive, "used": cloud + drive}
    _cache[key] = {"at_u": now, "at_d": at_d, "val": val}
    return val


def bump(nostr_dir: Path, nbytes: int) -> None:
    """Compte tout de suite des octets qui viennent d'être écrits (le total mémoïsé ne rattrape qu'à 60 s)."""
    hit = _cache.get(str(nostr_dir))
    if hit:
        hit["val"] = {**hit["val"], "ucloud": hit["val"]["ucloud"] + nbytes, "used": hit["val"]["used"] + nbytes}


def forget(nostr_dir: Path) -> None:
    _cache.pop(str(nostr_dir), None)


def status(nostr_dir: Path, fresh: bool = False) -> Dict[str, Any]:
    tier, quota = tier_for(nostr_dir), quota_bytes(nostr_dir)
    use = usage(nostr_dir, fresh)
    offer = offers()
    pct = round(100.0 * use["used"] / quota, 1) if quota else 0.0
    out = {"tier": tier, "label": offer[tier]["label"], "quota": quota, "zen_week": offer[tier]["zen_week"],
           "pct": pct, "over": use["used"] > quota,                 # période de grâce (lettre envoyée)
           "full": use["used"] >= hard_limit(nostr_dir),            # refus des nouveaux fichiers
           "sponsor_url": SPONSOR_URL, **use,
           "overridden": (nostr_dir / ".storage_quota").is_file()}
    if tier == "multipass":
        up = offer["zencard"]
        out["upgrade"] = {"label": up["label"], "quota": up["quota"], "zen_week": up["zen_week"]}
    return out


def check(nostr_dir: Path, incoming: int) -> None:
    """Lève QuotaExceeded si `incoming` octets dépassent le seuil de refus (sauf petits fichiers)."""
    if incoming < SMALL_FREE:
        return
    limit = hard_limit(nostr_dir)
    used = usage(nostr_dir)["used"]
    if used + incoming > limit:
        raise QuotaExceeded(used, quota_bytes(nostr_dir), incoming)


def record(nostr_dir: Path) -> Dict[str, Any]:
    """Mesure à neuf et l'écrit dans `<multipass>/.storage_usage.json` (lu par ZEN.ECONOMY.sh)."""
    st = status(nostr_dir, fresh=True)
    st["at"] = int(time.time())
    tmp = nostr_dir / f".{USAGE_FILE}.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, nostr_dir / USAGE_FILE)
    return st


def human(n: float) -> str:
    return f"{n / GO:.1f} Go" if n >= GO / 10 else f"{n / 1e6:.0f} Mo"
