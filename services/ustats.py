"""Carte de la station et de son essaim (remplace Astroport.ONE/Ustats.sh).

Deux usages :
- `python3 -m services.ustats` (depuis UPassport/) régénère `~/.zen/tmp/Ustats.json`
  dans un process COURT séparé, lancé détaché par `core/state.py` : jamais de
  fork depuis le process FastAPI (un fork d'un process de 1,7 Go à ~40 threads
  s'est interbloqué le 2026-10-23 et a figé l'API 5 h).
- `filter_area(data, lat, lon, deg)` : filtre géographique pur Python appliqué au
  cache complet, à la place d'un fichier Ustats_<lat>_<lon>_<deg>.json par requête.

Le format JSON est celui de Ustats.sh. Différence assumée : la liste NOSTR est
enfin remplie (le script appelait `process_nostr`, fonction définie nulle part,
donc toujours vide).
"""
import fcntl
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ustats")

HOME = Path.home()
ZEN = HOME / ".zen"
TMP = ZEN / "tmp"
CACHE_FILE = TMP / "Ustats.json"
LOCK_FILE = TMP / "Ustats.json.lock"
TOOLS = ZEN / "Astroport.ONE" / "tools"
ENV_VARS = ("IPFSNODEID", "uSPOT", "myLIBRA", "myRELAY", "myIPFS", "UPLANETG1PUB",
            "PAF", "NCARD", "ZCARD", "TODATE", "YESTERDATE")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


# --------------------------------------------------------------------------- utilitaires

def makecoord(value) -> str:
    """Même résultat que `makecoord` de my.sh : nombre à 2 décimales, "" si invalide."""
    if value is None or str(value).strip() == "":
        return ""
    s = str(value).strip()
    if not re.fullmatch(r"-?([0-9]+\.?[0-9]*|\.[0-9]+)", s):
        return ""
    return f"{float(s):.2f}"


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _last_line(path) -> str:
    lines = _read(path).splitlines()
    return lines[-1].strip() if lines else ""


def _first_last(paths) -> str:
    """Dernière ligne du dernier fichier existant (équivalent `cat a b | tail -n 1`)."""
    out = ""
    for p in sorted(paths):
        v = _last_line(p)
        if v:
            out = v
    return out


def _json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _num(s, default=0.0) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _fmt(x: float) -> str:
    """Valeur numérique comme `bc scale=1` / awk : sans zéros inutiles sauf une décimale."""
    return f"{x:.1f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


def load_env() -> dict:
    """Variables de my.sh (IPFSNODEID, myLIBRA, PAF…) : un seul `bash` source my.sh."""
    script = '. "%s/my.sh" >/dev/null 2>&1; for v in %s; do printf "%%s=%%s\\0" "$v" "${!v}"; done' % (
        TOOLS, " ".join(ENV_VARS))
    env = {}
    try:
        out = subprocess.run(["bash", "-c", script], capture_output=True, timeout=120).stdout
        for item in out.split(b"\0"):
            k, _, v = item.decode(errors="replace").partition("=")
            if k:
                env[k] = v
    except Exception as e:  # noqa: BLE001 - repli sur le 12345.json local
        log.warning("my.sh illisible : %s", e)
    if not env.get("IPFSNODEID"):
        env["IPFSNODEID"] = (_json(HOME / ".ipfs" / "config", {}) or {}).get("Identity", {}).get("PeerID", "")
    local = _json(TMP / env["IPFSNODEID"] / "12345.json", {}) or {}
    for k in ("uSPOT", "myRELAY", "myIPFS", "UPLANETG1PUB", "PAF", "NCARD", "ZCARD"):
        env.setdefault(k, "")
        if not env[k] and local.get(k) is not None:
            env[k] = str(local[k])
    return env


def _in_box(lat, lon, ulat, ulon, deg) -> bool:
    half = deg / 2
    return ulat - half <= lat <= ulat + half and ulon - half <= lon <= ulon + half


# --------------------------------------------------------------------------- sources

def _parse_exports(line: str) -> dict:
    """`export A=1 B=2 …` (dernière ligne d'un helper) → dict. Valeurs sans espace."""
    return dict(m.groups() for m in re.finditer(r"(\w+)=(\S*)", line.replace("export ", "", 1)))


def collect_players(env: dict) -> tuple:
    players, paying = [], 0
    names = sorted({p.parent.name for p in (ZEN / "game" / "players").glob("*@*.*/.player")})
    for email in names:
        cache = TMP / "players_cache" / f"{email}.ustats"
        line = _last_line(cache)
        if not line and EMAIL_RE.match(email):
            try:
                out = subprocess.run([str(TOOLS / "search_for_this_email_in_players.sh"), email],
                                     capture_output=True, text=True, timeout=180).stdout
                line = out.strip().splitlines()[-1] if out.strip() else ""
            except Exception as e:  # noqa: BLE001
                log.warning("player %s : %s", email, e)
        v = _parse_exports(line) if line.startswith("export") else {}
        myipfs = env.get("myIPFS", "")
        players.append({
            "ASTROPORT": myipfs + v.get("ASTROPORT", ""), "ASTROTW": myipfs + v.get("ASTROTW", ""),
            "ZEN": v.get("ZEN", ""), "LAT": v.get("LAT", ""), "LON": v.get("LON", ""),
            "ASTROG1": v.get("ASTROG1", ""), "ASTROMAIL": v.get("ASTROMAIL", ""),
            "ASTROFEED": myipfs + v.get("ASTROFEED", ""), "HEX": v.get("HEX", ""),
            "SOURCE": v.get("source", ""),
        })
        if _num(v.get("ZEN")) > 0:
            paying += 1
    return players, paying


def _nostr_record(email: str, env: dict):
    node = env["IPFSNODEID"]
    local = ZEN / "game" / "nostr" / email
    cache = TMP / node / "TW" / email
    hexv = _read(local / "HEX")
    if hexv:
        src, base = "LOCAL", local
    else:
        hexv = _read(cache / "HEX")
        if hexv:
            src, base = "CACHE", cache
        else:
            hits = sorted((TMP / "swarm").glob(f"*/TW/{email}/HEX"))
            if not hits:
                return None
            base = hits[0].parent
            hexv, src = _read(hits[0]), "SWARM"
    if not hexv:
        return None
    gps = _read(base / "GPS")
    lat = re.search(r"LAT=(-?[0-9]+\.?[0-9]*)", gps)
    lon = re.search(r"LON=(-?[0-9]+\.?[0-9]*)", gps)
    return {"source": src, "HEX": hexv, "LAT": lat.group(1) if lat else "0.00",
            "LON": lon.group(1) if lon else "0.00", "G1PUBNOSTR": _read(base / "G1PUBNOSTR")}


def _g1_coins(g1pub: str):
    if not g1pub:
        return None
    v = _read(TMP / "coucou" / f"{g1pub}.COINS")
    if v:
        return _num(v)
    try:
        out = subprocess.run([str(TOOLS / "G1check.sh"), g1pub], capture_output=True,
                             text=True, timeout=60).stdout.strip().splitlines()
        return _num(out[-1]) if out else None
    except Exception:  # noqa: BLE001
        return None


def collect_nostr(env: dict) -> tuple:
    node = env["IPFSNODEID"]
    emails = set()
    for pat in (f"{node}/TW/*/HEX", "swarm/*/TW/*/HEX"):
        emails.update(p.parent.name for p in TMP.glob(pat))
    # les MULTIPASS locaux sans cache TW compte aussi (cas courant d'une station seule)
    emails.update(p.name for p in (ZEN / "game" / "nostr").glob("*@*") if (p / "HEX").exists())
    out, paying = [], 0
    for email in sorted(emails):
        r = _nostr_record(email, env)
        if not r:
            continue
        coins = _g1_coins(r["G1PUBNOSTR"])
        zen = round((coins - 1) * 10, 1) if coins is not None else 0.0
        out.append({"EMAIL": email, "HEX": r["HEX"], "LAT": r["LAT"], "LON": r["LON"],
                    "G1PUBNOSTR": r["G1PUBNOSTR"], "ZEN": _fmt(zen), "SOURCE": r["source"]})
        if zen > 0:
            paying += 1
    return out, paying


def _umap_env(lat: str, lon: str, env: dict) -> dict:
    """Équivalent de getUMAP_ENV.sh (lecture de fichiers, aucune dérivation de clé)."""
    node, today, yest = env["IPFSNODEID"], env.get("TODATE", ""), env.get("YESTERDATE", "")
    slat, slon = lat[:-1], lon[:-1]
    rlat, rlon = lat.split(".")[0], lon.split(".")[0]
    mine, swarm = TMP / node / "UPLANET", [Path(p) for p in (TMP / "swarm").glob("*/UPLANET")]
    roots = [mine] + swarm

    def pick(rel: str, glob_ok=False):
        files = []
        for r in roots:
            files += sorted(r.glob(rel)) if glob_ok else ([r / rel] if (r / rel).exists() else [])
            if files and r == mine:
                break
        return _first_last(files) if files else ""

    base_u = f"__/_{rlat}_{rlon}/_{slat}_{slon}/_{lat}_{lon}"
    base_s = f"SECTORS/_{rlat}_{rlon}/_{slat}_{slon}"
    base_r = f"REGIONS/_{rlat}_{rlon}"

    def dated(base):
        return pick(f"{base}/ipfs.{today}") or pick(f"{base}/ipfs.{yest}")

    def ns(v):
        return f"/ipns/{v}" if v else "/ipns/"

    sec = f"__/_{rlat}_{rlon}/_{slat}_{slon}/_*_*"
    reg = f"__/_{rlat}_{rlon}/_*_*/_*_*"
    return {
        "UMAPROOT": dated(base_u), "SECTORROOT": dated(base_s), "REGIONROOT": dated(base_r),
        "UMAPHEX": pick(f"{base_u}/HEX"), "UMAPG1PUB": pick(f"{base_u}/G1PUB"),
        "UMAPIPNS": ns(pick(f"{base_u}/TODATENS")),
        "SECTORHEX": pick(f"{sec}/HEX_SECTOR", True), "SECTORG1PUB": pick(f"{sec}/SECTORG1PUB", True),
        "SECTORIPNS": ns(pick(f"{sec}/SECTORNS", True)),
        "REGIONHEX": pick(f"{reg}/HEX_REGION", True), "REGIONG1PUB": pick(f"{reg}/REGIONG1PUB", True),
        "REGIONIPNS": ns(pick(f"{reg}/REGIONNS", True)),
    }


def _umap_obj(lat: str, lon: str, env: dict) -> dict:
    e, myipfs = _umap_env(lat, lon, env), env.get("myIPFS", "")
    return {"LAT": lat, "LON": lon, "UMAPROOT": e["UMAPROOT"], "UMAPHEX": e["UMAPHEX"],
            "UMAPG1PUB": e["UMAPG1PUB"], "UMAPIPNS": myipfs + e["UMAPIPNS"],
            "SECTORROOT": e["SECTORROOT"], "SECTORHEX": e["SECTORHEX"],
            "SECTORG1PUB": e["SECTORG1PUB"], "SECTORIPNS": myipfs + e["SECTORIPNS"],
            "REGIONROOT": e["REGIONROOT"], "REGIONHEX": e["REGIONHEX"],
            "REGIONG1PUB": e["REGIONG1PUB"], "REGIONIPNS": myipfs + e["REGIONIPNS"]}


def collect_umaps(env: dict) -> list:
    node = env["IPFSNODEID"]
    names = set()
    for pat in (f"{node}/UPLANET/__/_*_*/_*.?_*.?/*", "swarm/*/UPLANET/__/_*_*/_*.?_*.?/*"):
        names.update(p.name for p in TMP.glob(pat))
    out = []
    for n in sorted(names):
        parts = n.split("_")
        if len(parts) < 3:
            continue
        out.append(_umap_obj(parts[1], parts[2], env))
    return out


SWARM_KEYS = ("ipfsnodeid", "hostname", "IPCity", "captain", "captainHEX", "STATION_LAT",
              "STATION_LON", "g1station", "g1swarm", "myIPFS", "myAPI", "myRELAY", "uSPOT",
              "NODEZEN", "PAF", "NCARD", "ZCARD", "BILAN", "uptime", "boots")


def collect_swarm(env: dict) -> list:
    out = []
    for f in sorted((TMP / "swarm").rglob("12345.json")):
        d = _json(f)
        if not isinstance(d, dict):
            continue
        nid = d.get("ipfsnodeid")
        if not nid or nid == env["IPFSNODEID"]:
            continue
        cap, eco = d.get("capacities") or {}, d.get("economy") or {}
        card = {k: d.get(k) for k in SWARM_KEYS}
        card["capacities"] = {
            "power_score": cap.get("power_score") or 0, "nostr_slots": cap.get("nostr_slots") or 0,
            "zencard_slots": cap.get("zencard_slots") or 0, "gpu": cap.get("gpu") or {},
            "provider_ready": cap.get("provider_ready") or False,
            "storage_ready": cap.get("storage_ready") or False}
        card["economy"] = {"multipass_count": eco.get("multipass_count") or 0,
                           "zencard_count": eco.get("zencard_count") or 0,
                           "captain_remuneration": eco.get("captain_remuneration") or 0}
        card["services"] = d.get("services") or {}
        out.append(card)
    return out


def revenue(env: dict) -> tuple:
    """(G1, ZEN) depuis le cache de G1revenue.sh (rafraîchi par lui, 1 h) ; script en repli."""
    path = TMP / "G1revenue_all.json"
    d = _json(path)
    if not isinstance(d, dict) or "total_revenue_zen" not in d:
        try:
            out = subprocess.run([str(TOOLS / "G1revenue.sh")], capture_output=True, text=True,
                                 timeout=300).stdout
            d = json.loads(out)
        except Exception:  # noqa: BLE001
            d = None
    if isinstance(d, dict) and "total_revenue_zen" in d:
        return d.get("total_revenue_g1") or 0, d.get("total_revenue_zen") or 0
    coins = _g1_coins(env.get("UPLANETG1PUB", ""))
    return (coins if coins is not None else 0), round(((coins or 0) - 1) * 10, 1)


# --------------------------------------------------------------------------- assemblage

def build() -> dict:
    start = time.time()
    env = load_env()
    players, twcount = collect_players(env)
    nostr, nostrcount = collect_nostr(env)
    umaps = collect_umaps(env)
    swarm = collect_swarm(env)
    coins, zen = revenue(env)

    def ival(k, default):
        v = env.get(k, "")
        return int(_num(v, default)) if v != "" else default

    paf, ncard, zcard = ival("PAF", 14), ival("NCARD", 1), ival("ZCARD", 4)
    node = env["IPFSNODEID"]
    data = {
        "version": "54321.0",
        "DATE": datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y"),
        "uSPOT": env.get("uSPOT", ""),
        "g1station": f'{env.get("myLIBRA", "")}/ipns/{node}',
        "PAF": str(paf), "NCARD": str(ncard), "ZCARD": str(zcard),
        "myRELAY": env.get("myRELAY", ""), "IPFSNODEID": node, "myIPFS": env.get("myIPFS", ""),
        "UPLANETG1PUB": env.get("UPLANETG1PUB", ""), "G1": str(coins), "ZEN": str(zen),
        "BILAN": str(nostrcount * ncard + twcount * zcard - paf),
        "SWARM": swarm, "NOSTR": nostr, "PLAYERs": players, "UMAPs": umaps,
    }
    local = _json(TMP / node / "12345.json", {}) or {}
    if local:
        data.update({
            "capacities": local.get("capacities") or {}, "economy": local.get("economy") or {},
            "hostname": local.get("hostname") or "", "STATION_LAT": str(local.get("STATION_LAT") or ""),
            "STATION_LON": str(local.get("STATION_LON") or ""), "uptime": local.get("uptime") or "",
            "boots": local.get("boots") or []})
    data["GENERATION_TIME"] = str(int(time.time() - start))
    return data


def filter_area(data: dict, lat, lon, deg) -> dict:
    """Vue géographique du cache complet : même sortie que Ustats.sh <lat> <lon> <deg>."""
    ulat, ulon, udeg = makecoord(lat), makecoord(lon), makecoord(deg)
    if not (ulat and ulon):
        return data
    flat, flon = float(ulat), float(ulon)
    out = dict(data)

    def keep(item):
        if not udeg:
            return True
        return _in_box(_num(item.get("LAT")), _num(item.get("LON")), flat, flon, float(udeg))

    for key in ("PLAYERs", "NOSTR", "UMAPs"):
        out[key] = [i for i in data.get(key, []) if keep(i)]
    umaps = data.get("UMAPs", [])
    ranked = sorted(umaps, key=lambda u: haversine_km(flat, flon, _num(u["LAT"]), _num(u["LON"])))[:4]
    out["CLOSEST_UMAPs"] = [dict(u, DISTANCE_KM=str(haversine_km(flat, flon, _num(u["LAT"]), _num(u["LON"]))))
                            for u in ranked]
    out["CENTER"] = {"LAT": ulat, "LON": ulon, "DEG": udeg}
    return out


def write_cache(data: dict) -> None:
    tmp = CACHE_FILE.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, CACHE_FILE)


def generate() -> int:
    """Régénère le cache complet sous verrou ; 75 = déjà en cours."""
    TMP.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("génération déjà en cours")
            return 75
        write_cache(build())
    return 0


def main(argv=None) -> int:
    """CLI compatible Ustats.sh : `[lat lon deg] [--refresh] [--stdout]`.

    Dernière ligne affichée = chemin du JSON (cache complet, ou fichier filtré
    Ustats_<lat>_<lon>_<deg>.json si des coordonnées sont données). Un cache
    présent est servi tel quel ; passé 1 h, une régénération détachée est lancée."""
    argv = sys.argv[1:] if argv is None else argv
    flags = {a for a in argv if a.startswith("--")}
    pos = [a for a in argv if not a.startswith("--")]
    if "--stdout" in flags:
        print(json.dumps(build(), indent=2, ensure_ascii=False))
        return 0
    fresh_needed = "--refresh" in flags or not CACHE_FILE.exists() or _json(CACHE_FILE) is None
    if fresh_needed:
        if generate() == 75:
            for _ in range(120):  # un autre process génère : attendre son résultat
                time.sleep(1)
                if _json(CACHE_FILE) is not None:
                    break
    elif time.time() - CACHE_FILE.stat().st_mtime > 3600:
        subprocess.Popen([sys.executable, "-m", "services.ustats", "--refresh"],
                         cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    data = _json(CACHE_FILE)
    if data is None:
        print('{"error": "Cache Ustats invalide"}')
        return 1
    path = CACHE_FILE
    lat, lon, deg = (pos + ["", "", ""])[:3]
    if makecoord(lat) and makecoord(lon):
        path = TMP / f"Ustats_{makecoord(lat)}_{makecoord(lon)}_{makecoord(deg)}.json"
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(filter_area(data, lat, lon, deg), indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    print(path)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    sys.exit(main())
