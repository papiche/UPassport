#!/usr/bin/env python3
"""
ucloud_daily.py — Traitement QUOTIDIEN du `.ucloud` d'un MULTIPASS (lancé par RUNTIME/NOSTRCARD.refresh.sh).

    python3 ucloud_daily.py <email> [--only import,enrich,describe,embed,backup] [--describe-limit N] [-v|-vv]

Étapes, dans cet ordre, chacune dans son propre sous-processus (délai propre, échec NON bloquant pour les suivantes) :
  import    webdav_import.py       sources WebDAV externes → nouvelles photos (rangées à leur date de prise de vue EXIF)
  enrich    ucloud_dedupe.py scan  dates / lieux / empreintes des photos qui n'en ont pas (anciennes : les nouvelles
                                   les reçoivent à l'envoi) — seulement s'il en reste
  describe  ucloud_describe.py     description IA des photos SANS description, dans la limite d'un BUDGET GPU
  embed     ucloud_embed.py        vecteurs de recherche par le sens des descriptions nouvelles ou corrigées
  backup    ucloud_backup.py       sauvegarde chiffrée hors station, EN DERNIER : elle contient les descriptions du jour

Plusieurs MULTIPASS peuvent avoir leur heure quotidienne en même temps : les étapes qui sollicitent le GPU ou le CPU lourdement
(enrich, describe, embed) sont SÉRIALISÉES entre comptes par un verrou de station (`.ucloud_daily_heavy.lock`, attente
maximale FACECLOUD_DAILY_WAIT = 3 h, au-delà elles sont reprises le lendemain) ; import et sauvegarde, eux, ne l'attendent pas.
Ce programme tourne en processus AUTONOME (lancé par NOSTRCARD.refresh.sh avec `setsid`) : il ne retient jamais le rafraîchissement
des autres MULTIPASS ni les relances de `_12345.sh`.

Budget de descriptions : DESCRIBE_DAILY_STATION_BUDGET (2400 photos/jour pour TOUTE la station, `FACECLOUD_DESCRIBE_BUDGET`)
réparti entre les MULTIPASS hébergés, borné à [DESCRIBE_MIN, DESCRIBE_MAX] par compte : ce qui dépasse est repris le lendemain
(la sélection « sans description » le retrouve toute seule). L'étape est sautée si ni Ollama (accès direct) ni le relais NOSTR
ne sont disponibles. Une seule instance par compte (flock `.daily.lock`). Les messages vont sur stdout, une ligne par étape
(`ucloud_daily: [étape] …`) ; `-v`/`-vv` sont transmis aux scripts de description et de vectorisation.
"""
import sys as _sys
import os as _os

_venv_python = _os.path.expanduser("~/.astro/bin/python3")
if _os.path.exists(_venv_python) and _sys.executable != _venv_python:
    _os.execv(_venv_python, [_venv_python] + _sys.argv)
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
del _sys, _os

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from services import analysis, cloud_storage, ucloud_db  # noqa: E402

HERE = Path(__file__).resolve().parent
STEPS = ["import", "enrich", "describe", "embed", "backup"]
TIMEOUT = {"import": 3 * 3600, "enrich": 3600, "describe": 2 * 3600, "embed": 1800, "backup": 1200}
DESCRIBE_DAILY_STATION_BUDGET = int(os.environ.get("FACECLOUD_DESCRIBE_BUDGET", "2400"))
DESCRIBE_MIN, DESCRIBE_MAX = 30, 600
HEAVY = ("enrich", "describe", "embed")
HEAVY_WAIT = int(os.environ.get("FACECLOUD_DAILY_WAIT", str(3 * 3600)))


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] ucloud_daily: {msg}", flush=True)


def station_multipass_count() -> int:
    root = cloud_storage.GAME_NOSTR_PATH
    try:
        return max(1, sum(1 for d in root.iterdir() if d.is_dir() and "@" in d.name and not (d / ".roaming").exists()))
    except OSError:
        return 1


def describe_limit(station_count: int, override: int = 0) -> int:
    """Part de ce MULTIPASS dans le budget quotidien de la station."""
    if override > 0:
        return override
    return max(DESCRIBE_MIN, min(DESCRIBE_MAX, DESCRIBE_DAILY_STATION_BUDGET // max(1, station_count)))


def plan(email: str, only=None, describe_override: int = 0) -> list:
    """[(étape, arguments | None, raison)] : ce qu'il y a à faire MAINTENANT. `None` = étape sautée ; la raison dit
    pourquoi on la fait (ou pourquoi on la saute)."""
    from services import ucloud_embed
    udir = cloud_storage.ensure_ucloud_dir(email)
    c = ucloud_db.analysis_counts(udir)
    images = c["images"]
    out = []

    sources = udir / "webdav_sources.json"
    if sources.is_file() and sources.stat().st_size > 2:
        out.append(("import", ["webdav_import.py", email], "sources WebDAV configurées : nouvelles photos du cloud distant"))
    else:
        out.append(("import", None, "aucune source WebDAV configurée"))

    enrich_pending = max(0, images - c["hashed"])
    out.append(("enrich", ["ucloud_dedupe.py", "scan", email] if enrich_pending else None,
                f"{enrich_pending} photo(s) sans empreinte/date/lieu" if enrich_pending
                else f"toutes les photos ont déjà date, lieu et empreinte ({images})"))

    desc_pending = max(0, images - c["described"] - c["unreadable"])
    route = analysis.resolve("describe")
    if not desc_pending:
        out.append(("describe", None, "toutes les photos sont décrites"))
    elif route["effective"] == "none":
        out.append(("describe", None, "ni Ollama en accès direct ni relais NOSTR : " + route["why"]))
    else:
        lim = describe_limit(station_multipass_count(), describe_override)
        out.append(("describe", ["ucloud_describe.py", "scan", email, "--limit", str(lim)],
                    f"{desc_pending} photo(s) sans description, {min(lim, desc_pending)} aujourd'hui (budget), chemin {route['effective']}"))

    embed_pending = ucloud_embed.counts(udir)["pending"]
    out.append(("embed", ["ucloud_embed.py", "scan", email] if embed_pending else None,
                f"{embed_pending} description(s) à vectoriser" if embed_pending else "tous les vecteurs sont à jour"))

    has_cloud = ucloud_db.exists(udir) or (udir / "index.json").is_file()
    out.append(("backup", ["ucloud_backup.py", "snapshot", email] if has_cloud else None,
                "archive chiffrée hors station (avec les descriptions du jour)" if has_cloud else "pas de cloud à sauvegarder"))
    return [(s_, a_, r_) for s_, a_, r_ in out if only is None or s_ in only]


def heavy_lock_path() -> Path:
    return cloud_storage.GAME_NOSTR_PATH.parent / ".ucloud_daily_heavy.lock"


def acquire_heavy(email: str, wait: int):
    """Verrou de station pour les étapes lourdes. Rend un descripteur ouvert, ou None si l'attente est dépassée."""
    path = heavy_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline, announced = time.time() + wait, False
    while True:
        fd = cloud_storage.try_lock_nowait(path)
        if fd is not None:
            return fd
        if not announced:
            say(f"{email} : un autre MULTIPASS occupe les étapes lourdes (GPU) — attente (3 h au plus, import et sauvegarde ne attendent pas)")
            announced = True
        if time.time() >= deadline:
            return None
        time.sleep(min(30, max(0.05, deadline - time.time())))


def run(email: str, only=None, describe_override: int = 0, verbose: int = 0) -> dict:
    udir = cloud_storage.ensure_ucloud_dir(email)
    lock_fd = cloud_storage.try_lock_nowait(udir / ".daily.lock")
    if lock_fd is None:
        say(f"{email} : un traitement quotidien est déjà en cours — rien à faire")
        return {"status": "busy"}
    results = {}
    heavy_fd, heavy_given_up = None, False
    try:
        t_all = time.time()
        steps = plan(email, only, describe_override)
        last_heavy = max((i for i, (st, a, _r) in enumerate(steps) if st in HEAVY and a), default=-1)
        for idx, (step, args, why) in enumerate(steps):
            if args is None:
                say(f"[{step}] sauté — {why}")
                results[step] = "skip"
                continue
            if step in HEAVY:
                if heavy_given_up:
                    results[step] = "attente"
                    say(f"[{step}] reporté à demain — le verrou des étapes lourdes n'a pas été obtenu")
                    continue
                if heavy_fd is None:
                    heavy_fd = acquire_heavy(email, HEAVY_WAIT)
                    if heavy_fd is None:
                        heavy_given_up = True
                        results[step] = "attente"
                        say(f"[{step}] reporté à demain — verrou des étapes lourdes non obtenu en {HEAVY_WAIT}s")
                        continue
            if step in ("describe", "embed") and verbose:
                args = args + ["-" + "v" * verbose]
            say(f"[{step}] début — {why}")
            t0 = time.time()
            try:
                proc = subprocess.run([sys.executable, str(HERE / args[0]), *args[1:]], timeout=TIMEOUT[step])
                code = proc.returncode
            except subprocess.TimeoutExpired:
                code = -1
                say(f"[{step}] délai dépassé ({TIMEOUT[step]}s) — arrêté, les suivantes continuent")
            except OSError as exc:
                code = -2
                say(f"[{step}] impossible à lancer : {exc}")
            if code == 75:
                results[step] = "occupé"
                say(f"[{step}] déjà en cours ailleurs (lancé à la main ?) — sauté, repris demain")
            else:
                results[step] = "ok" if code == 0 else f"échec({code})"
                say(f"[{step}] {'terminé' if code == 0 else 'ÉCHEC code ' + str(code)} en {time.time() - t0:.0f}s")
            if heavy_fd is not None and idx >= last_heavy:        # dernière étape lourde faite : on libère les autres comptes
                os.close(heavy_fd)
                heavy_fd = None
        say(f"{email} : terminé en {time.time() - t_all:.0f}s — " + ", ".join(f"{k}={v}" for k, v in results.items()))
        return {"status": "ok", "steps": results}
    finally:
        if heavy_fd is not None:
            os.close(heavy_fd)
        os.close(lock_fd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("email")
    ap.add_argument("--only", help="étapes séparées par des virgules : " + ",".join(STEPS))
    ap.add_argument("--describe-limit", type=int, default=0, help="remplace le budget quotidien de descriptions")
    ap.add_argument("--verbose", "-v", action="count", default=0)
    ap.add_argument("--dry-run", action="store_true", help="affiche le plan (étapes, raisons, budget) sans rien lancer")
    args = ap.parse_args()
    only = None
    if args.only:
        only = [x.strip() for x in args.only.split(",") if x.strip()]
        bad = [x for x in only if x not in STEPS]
        if bad:
            print(f"ERROR:ucloud_daily:étape(s) inconnue(s) {bad} (valides : {STEPS})", file=sys.stderr)
            return 2
    if not cloud_storage.hex_for_email(args.email):
        print(f"ERROR:ucloud_daily:aucun MULTIPASS '{args.email}' sur cette station", file=sys.stderr)
        return 1
    if args.dry_run:
        for step, a, why in plan(args.email, only, args.describe_limit):
            say(f"[{step}] {'À FAIRE' if a else 'sauté'} — {why}" + (f" : {' '.join(a)}" if a else ""))
        return 0
    try:
        res = run(args.email, only, args.describe_limit, args.verbose)
    except KeyboardInterrupt:
        say("interrompu — l'étape en cours a été arrêtée, ce qui est fait est conservé")
        return 130
    return 0 if res.get("status") in ("ok", "busy") and not any(str(v).startswith("échec") for v in (res.get("steps") or {}).values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
