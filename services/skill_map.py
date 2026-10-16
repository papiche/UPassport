"""
services/skill_map.py — « Qui d'autre maîtrise ça dans le coin ? » : savoir-faire déclarés, retrouvables par territoire.

Deux événements par savoir-faire déclaré :
  1. le CERTIFICAT, kind 30503 signé par la clé du MULTIPASS (WoTx² : `d=PERMIT_<SKILL>_X<niveau>`, `t=<skill>`, niveau = x1 auto-proclamé,
     x2+ quand d'autres le signent). C'est la SEULE source de vérité : n'importe qui peut le relire et vérifier la signature.
  2. l'ANNUAIRE DU COIN, kind 30078 signé par la clé de la case géographique (UMAP 0,01° ≈ 1 km, SECTOR 0,1°, REGION 1°), une case par
     niveau de visibilité choisi : `d=skillmap:<skill>:<hex du membre>`, `t=<skill>`, `p=<hex du membre>`, `a=30503:<hex>:<d du certificat>`.
     Les clés de case dérivent de la clé secrète de la coopérative (`~/.ipfs/swarm.key`) et des coordonnées (même dérivation que
     NOSTR.UMAP.refresh.sh) : seules les stations de la coopérative peuvent les signer, tout le monde peut les LIRE (clé publique de la case).
     L'annuaire n'est qu'un index : un membre n'apparaît dans les résultats que si son propre certificat existe, est valide et non expiré.

Visibilité (choisie par le membre, jamais imposée) : `umap` publie dans les trois cases (précis à ~1 km), `sector` dans SECTOR + REGION
(~11 km, défaut), `region` dans REGION seulement (~111 km), `none` retire tout l'annuaire (le certificat reste).
Toutes les fonctions sont SYNCHRONES (asyncio.to_thread) ; les pannes de relais lèvent RelayUnavailable (jamais « personne » à tort).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services import relay_io

logger = logging.getLogger(__name__)

ZEN = Path.home() / ".zen"
KEYGEN = ZEN / "Astroport.ONE" / "tools" / "keygen"
GAME_NOSTR = ZEN / "game" / "nostr"
LEVELS = ("umap", "sector", "region")                         # du plus précis au plus large
VISIBILITY = ("none", "region", "sector", "umap")
DEFAULT_VISIBILITY = "sector"
EXPIRE_DAYS = {1: 260, 2: 780, 3: 2600}                       # cycles tzolkin, comme tools/emit_skill.sh
D_PREFIX = "skillmap:"
_ZERO_SECRET = "0" * 64
_pub_cache: Dict[str, str] = {}


# ── noms ─────────────────────────────────────────────────────────────────────
def skill_norm(raw: str) -> str:
    """« Astroport Install » → astroport-install (même règle que tools/emit_skill.sh)."""
    s = re.sub(r"[^a-z0-9]", "-", str(raw or "").lower())
    return re.sub(r"-+", "-", s).strip("-")[:60]


def permit_key(skill: str, level: int) -> str:
    return "PERMIT_" + skill.replace("-", "_").upper() + f"_X{int(level)}"


def cell_names(lat: float, lon: float) -> Dict[str, str]:
    """Noms des trois cases (tels que NOSTR.UMAP.refresh.sh / getUMAP_ENV.sh : coordonnées à 2 décimales, secteur = 1 décimale tronquée,
    région = partie entière) : {'umap': '_43.92_2.14', 'sector': '_43.9_2.1', 'region': '_43_2'}."""
    la, lo = f"{float(lat):.2f}", f"{float(lon):.2f}"
    la, lo = ("0.00" if la == "-0.00" else la), ("0.00" if lo == "-0.00" else lo)
    return {"umap": f"_{la}_{lo}", "sector": f"_{la[:-1]}_{lo[:-1]}", "region": f"_{la.split('.')[0]}_{lo.split('.')[0]}"}


def swarm_secret() -> str:
    try:
        lines = [x.strip() for x in (Path.home() / ".ipfs" / "swarm.key").read_text().splitlines() if x.strip()]
        return lines[-1] if lines else _ZERO_SECRET
    except OSError:
        return _ZERO_SECRET


def cell_salts(level: str, name: str) -> Tuple[str, str]:
    """(sel, poivre) de keygen pour la case — comme les scripts bash : UMAP = `${UPLANETNAME}${LAT}` et `${UPLANETNAME}${LON}` (deux valeurs
    distinctes), SECTOR et REGION = `${UPLANETNAME}${nom}` deux fois (nom = « _43.9_2.1 », « _43_2 »)."""
    secret = swarm_secret()
    if level == "umap":
        _, lat, lon = name.split("_")                              # '_43.92_2.14'
        return secret + lat, secret + lon
    return secret + name, secret + name


def cell_nsec(level: str, name: str) -> str:
    """nsec de la case (keygen -t nostr, comme les scripts bash). Lève RuntimeError si keygen échoue : jamais de clé inventée."""
    salt, pepper = cell_salts(level, name)
    proc = subprocess.run([sys.executable, str(KEYGEN), "-t", "nostr", salt, pepper, "-s"], capture_output=True, text=True, timeout=60)
    nsec = proc.stdout.strip()
    if proc.returncode != 0 or not nsec.startswith("nsec1"):
        raise RuntimeError(f"keygen a échoué pour la case {name} : {(proc.stderr or proc.stdout).strip()[-200:]}")
    return nsec


def cell_pub(level: str, name: str) -> str:
    """Clé publique (hex) de la case, mémorisée (la dérivation coûte ~0,2 s)."""
    key = f"{level}{name}"
    if key not in _pub_cache:
        import nostr_sdk
        _pub_cache[key] = nostr_sdk.Keys.parse(cell_nsec(level, name)).public_key().to_hex()
    return _pub_cache[key]


def member_keys(email: str):
    import nostr_sdk
    m = re.search(r"NSEC=([^;\s]+)", (GAME_NOSTR / email / ".secret.nostr").read_text())
    if not m:
        raise RuntimeError("NSEC du MULTIPASS introuvable")
    return nostr_sdk.Keys.parse(m.group(1))


def member_gps(email: str) -> Optional[Tuple[float, float]]:
    """Position du MULTIPASS (fichier GPS : « LAT=43.92; LON=2.14; ») ou None."""
    try:
        raw = (GAME_NOSTR / email / "GPS").read_text()
        return float(re.search(r"LAT=(-?[\d.]+)", raw).group(1)), float(re.search(r"LON=(-?[\d.]+)", raw).group(1))
    except (OSError, AttributeError, ValueError):
        return None


# ── événements ───────────────────────────────────────────────────────────────
def _sign(keys, kind: int, content: str, tags: List[List[str]]) -> Dict[str, Any]:
    import nostr_sdk
    b = nostr_sdk.EventBuilder(nostr_sdk.Kind(kind), content).tags([nostr_sdk.Tag.parse(t) for t in tags])
    return json.loads(b.finalize(keys).as_json())


def _expire(level: int) -> int:
    return int(time.time()) + EXPIRE_DAYS.get(level, 260 * max(1, level)) * 86400


def levels_for(visibility: str) -> List[str]:
    """Cases où publier : le niveau choisi ET les plus larges (un voisin qui cherche à l'échelle de la région doit aussi te trouver)."""
    if visibility not in VISIBILITY or visibility == "none":
        return []
    return list(LEVELS[LEVELS.index(visibility):])


def certificate_event(keys, skill: str, level: int, description: str = "", icon: str = "") -> Dict[str, Any]:
    pk = permit_key(skill, level)
    tags = [["d", pk], ["l", pk, "permit_type"], ["level", str(level)], ["t", skill], ["t", "auto_proclaimed"], ["expiration", str(_expire(level))]]
    if description:
        tags.append(["summary", description[:300]])
    content = {"skill": skill, "level": level, "auto_proclaimed": True, "attested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if icon:
        content["icon"] = icon[:8]
    if description:
        content["description"] = description[:300]
    return _sign(keys, 30503, json.dumps(content, ensure_ascii=False), tags)


def directory_event(cell_keys, member_hex: str, skill: str, level: int, cert_d: str) -> Dict[str, Any]:
    tags = [["d", f"{D_PREFIX}{skill}:{member_hex}"], ["l", "skillmap"], ["t", skill], ["p", member_hex], ["level", str(level)],
            ["a", f"30503:{member_hex}:{cert_d}"], ["expiration", str(_expire(level))]]
    return _sign(cell_keys, 30078, "", tags)


def delete_event(cell_keys, cell_hex: str, d_tag: str) -> Dict[str, Any]:
    return _sign(cell_keys, 5, "retrait du savoir-faire", [["a", f"30078:{cell_hex}:{d_tag}"]])


def _cell_keys(level: str, name: str):
    import nostr_sdk
    return nostr_sdk.Keys.parse(cell_nsec(level, name))


# ── API métier ───────────────────────────────────────────────────────────────
def declare(email: str, skill: str, level: int = 1, description: str = "", icon: str = "", visibility: str = DEFAULT_VISIBILITY,
            lat: Optional[float] = None, lon: Optional[float] = None) -> Dict[str, Any]:
    """Déclare (ou met à jour) un savoir-faire : certificat du membre + annuaire des cases choisies. Retire de l'annuaire les cases
    plus fines que `visibility`. Rend {skill, level, visibility, cells, relays, ...}. Lève ValueError (saisie), RuntimeError (clés/relais)."""
    skill = skill_norm(skill)
    if len(skill) < 2:
        raise ValueError("nom de savoir-faire trop court")
    if visibility not in VISIBILITY:
        raise ValueError("visibilité inconnue")
    level = max(1, min(int(level or 1), 9))
    keys = member_keys(email)
    member_hex = keys.public_key().to_hex()
    cert = certificate_event(keys, skill, level, description, icon)
    events = [cert]
    names: Dict[str, str] = {}
    if lat is None or lon is None:
        pos = member_gps(email)
        if pos:
            lat, lon = pos
    if lat is not None and lon is not None:
        names = cell_names(lat, lon)
    elif visibility != "none":
        raise ValueError("position inconnue : indiquez où vous êtes (lat, lon) pour apparaître dans le coin")
    d = f"{D_PREFIX}{skill}:{member_hex}"
    wanted = levels_for(visibility)
    for lv in LEVELS:                                           # publie dans les cases voulues, RETIRE des autres (visibilité réduite ou nulle)
        if not names:
            break
        keys_c = _cell_keys(lv, names[lv])
        cell_hex = keys_c.public_key().to_hex()
        events.append(directory_event(keys_c, member_hex, skill, level, cert_d=permit_key(skill, level)) if lv in wanted else delete_event(keys_c, cell_hex, d))
    ok = relay_io.publish(events)
    if not ok:
        raise RuntimeError("aucun relais n'a accepté la déclaration")
    return {"skill": skill, "level": level, "visibility": visibility, "cells": {lv: names.get(lv) for lv in wanted}, "relays": ok, "id": cert["id"]}


def retract(email: str, skill: str, lat: Optional[float] = None, lon: Optional[float] = None) -> Dict[str, Any]:
    """Retire le savoir-faire : annuaire des trois cases (kind 5 signé par la case) et certificat (kind 5 signé par le membre)."""
    skill = skill_norm(skill)
    keys = member_keys(email)
    member_hex = keys.public_key().to_hex()
    mine = [e for e in relay_io.query({"kinds": [30503], "authors": [member_hex], "#t": [skill]}, strict=True)]
    events = [_sign(keys, 5, "retrait du savoir-faire", [["a", f"30503:{member_hex}:{e_d}"] for e_d in
                                                        {t[1] for e in mine for t in e.get("tags", []) if t[0] == "d"}])] if mine else []
    if lat is None or lon is None:
        pos = member_gps(email)
        lat, lon = pos if pos else (None, None)
    if lat is not None:
        d = f"{D_PREFIX}{skill}:{member_hex}"
        for lv, name in cell_names(lat, lon).items():
            ck = _cell_keys(lv, name)
            events.append(delete_event(ck, ck.public_key().to_hex(), d))
    ok = relay_io.publish(events) if events else []
    return {"skill": skill, "removed": len(events), "relays": ok}


def _tag(event: Dict[str, Any], name: str) -> str:
    return next((t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == name), "")


def _alive(event: Dict[str, Any], now: Optional[int] = None) -> bool:
    exp = _tag(event, "expiration")
    return not (exp.isdigit() and int(exp) < (now or time.time()))


def skills_of(hex_key: str) -> List[Dict[str, Any]]:
    """Savoir-faire déclarés par un MULTIPASS (ses certificats 30503 non expirés), le plus haut niveau par savoir-faire."""
    best: Dict[str, Dict[str, Any]] = {}
    for e in relay_io.query({"kinds": [30503], "authors": [hex_key], "limit": 200}, strict=True):
        tags_t = [t[1] for t in e.get("tags", []) if t[0] == "t" and t[1] != "auto_proclaimed"]
        skill = tags_t[0] if tags_t else ""
        lvl = int(_tag(e, "level") or 1) if (_tag(e, "level") or "1").isdigit() else 1
        if not skill or not _alive(e):
            continue
        if skill not in best or lvl > best[skill]["level"]:
            try:
                c = json.loads(e.get("content") or "{}")
            except ValueError:
                c = {}
            best[skill] = {"skill": skill, "level": lvl, "icon": c.get("icon", ""), "description": c.get("description", ""), "created_at": e["created_at"]}
    return sorted(best.values(), key=lambda s: (-s["level"], s["skill"]))


def nearby(skill: str, lat: float, lon: float, scope: str = "sector", min_level: int = 1, exclude_hex: str = "") -> List[Dict[str, Any]]:
    """Qui d'autre maîtrise `skill` dans la case `scope` (umap|sector|region) de (lat, lon), à niveau ≥ `min_level` ?
    [{hex, level, scope, since}] — chaque annonce de l'annuaire est VÉRIFIÉE contre le certificat du membre (signé par lui, valide) ;
    le niveau retenu est celui du certificat, jamais celui annoncé par l'annuaire. Lève relay_io.RelayUnavailable si aucun relais ne répond."""
    skill = skill_norm(skill)
    if scope not in LEVELS:
        raise ValueError("portée inconnue")
    cell = cell_pub(scope, cell_names(lat, lon)[scope])
    claims = [e for e in relay_io.query({"kinds": [30078], "authors": [cell], "#t": [skill], "#l": ["skillmap"], "limit": 500}, strict=True) if _alive(e)]
    members = sorted({_tag(e, "p") for e in claims if re.fullmatch(r"[0-9a-f]{64}", _tag(e, "p")) and _tag(e, "p") != exclude_hex})
    if not members:
        return []
    certs = relay_io.query({"kinds": [30503], "authors": members, "#t": [skill], "limit": 500}, strict=True)
    level_of: Dict[str, int] = {}
    for c in certs:
        if not _alive(c):
            continue
        lv = int(_tag(c, "level")) if _tag(c, "level").isdigit() else 1
        level_of[c["pubkey"]] = max(lv, level_of.get(c["pubkey"], 0))
    out = []
    for m in members:
        lv = level_of.get(m)
        if lv and lv >= min_level:
            since = min(e["created_at"] for e in claims if _tag(e, "p") == m)
            out.append({"hex": m, "level": lv, "scope": scope, "since": since})
    return sorted(out, key=lambda r: (-r["level"], r["since"]))


def my_visibility(member_hex: str, lat: float, lon: float) -> Dict[str, str]:
    """Pour chaque savoir-faire annoncé dans l'annuaire des cases de (lat, lon) : sa visibilité effective (la case la plus fine où il figure)."""
    out: Dict[str, str] = {}
    for lv in LEVELS:                                                    # du plus fin au plus large : le premier trouvé gagne
        cell = cell_pub(lv, cell_names(lat, lon)[lv])
        for e in relay_io.query({"kinds": [30078], "authors": [cell], "#p": [member_hex], "#l": ["skillmap"], "limit": 200}, strict=True):
            if _alive(e) and _tag(e, "t"):
                out.setdefault(_tag(e, "t"), lv)
    return out
