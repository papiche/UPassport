#!/usr/bin/env python3
"""
faceid_reprocess.py — Rattrapage FaceID SILENCIEUX des photos d'un cloud
FaceCloud (images jamais analysées, ou marquées `no_face` à tort).

Cas d'origine (2026-10-12) : faceid.sh renvoyait `faces: []` à côté de la
vraie sortie imbriquée `images[].faces` pour toute photo réduite avant
détection (> 1600 px, donc toute photo smartphone) — 1877 photos sur 2006
d'un import WebDAV marquées `no_face` malgré des visages bien visibles.

Fonctionnement :
  * sélectionne les images dont `faceid.status` ∈ --status (défaut :
    no_face, none = jamais analysée, pending/timeout = rattrapage interrompu) ;
    les copies reçues d'un ami (`origin: facecloud-share`) sont ignorées ;
  * pose `.ucloud/faceid_quiet_until` (stockage durable, PAS ~/.zen/tmp/) :
    satellite_face_matcher.py n'envoie alors ni notification « visage
    inconnu » ni message NIP-17 inter-stations (les copies locales vers un
    ami de la station restent actives — silencieuses par nature) ;
  * AUTO-CADENCÉ : au plus --parallel photos « en vol » (défaut 6) — chacune
    marquée `pending`, analyse déclenchée par le même chemin que tout PUT
    /dav/ (cloud_storage._trigger_faceid_analysis) ; une nouvelle n'est
    lancée que quand le matcher a écrit un résultat (`faces`/`no_face`).
    Le goulot mesuré (~60 s/photo) est la relève des DM par bro_dm_daemon,
    pas le GPU : quelques photos en vol suffisent, sans jamais approcher le
    TTL de 30 min de la file vision_analysis_job ;
  * publie sa progression dans `.ucloud/faceid_reprocess.json` (lue par
    GET /api/cloud/status → bandeau de progression de ucloud.html).

Deux modes :
  * --quick (recommandé, défaut de POST /api/cloud/faceid-reprocess) : un seul
    `faceid.sh --batch` parle DIRECTEMENT à ComfyUI (local, tunnel SSH ou P2P
    IPFS via comfyui.me.sh), résultat passé au matcher sans relais NOSTR —
    ~1,3 s par photo. Repli automatique sur le relais si ComfyUI est
    injoignable depuis cette station ;
  * relais NOSTR (défaut CLI, historique) : décrit ci-dessus.

Usage :
    faceid_reprocess.py <email> [--quick] [--status no_face,none,pending,timeout]
                        [--before ISO] [--wait 240] [--parallel 6] [--limit N] [--dry-run]

Une seule instance par compte (flock `.ucloud/.reprocess.lock`).
"""

import sys as _sys
import os as _os

_venv_python = _os.path.expanduser("~/.astro/bin/python3")
if _os.path.exists(_venv_python) and _sys.executable != _venv_python:
    _os.execv(_venv_python, [_venv_python] + _sys.argv)

_UPASSPORT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _UPASSPORT_DIR not in _sys.path:
    _sys.path.insert(0, _UPASSPORT_DIR)
del _sys, _os

import argparse
import fcntl
import json
import os
import queue as queue_mod
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from services import cloud_storage  # noqa: E402

POLL_SECONDS = 2.0


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _status_of(entry: dict) -> str:
    return ((entry.get("faceid") or {}).get("status")) or "none"


def _checked_before(entry: dict, before) -> bool:
    """Vrai si l'entrée n'a jamais été analysée, ou l'a été avant `before`."""
    if before is None:
        return True
    checked = (entry.get("faceid") or {}).get("checked_at")
    if not checked:
        return True
    try:
        return datetime.strptime(checked, "%Y-%m-%dT%H:%M:%S%z") < before
    except ValueError:
        return True


def _set_faceid(email: str, path: str, value: dict) -> None:
    # UNE ligne SQLite (l'ancien load_index/save_index réécrivait tout l'index
    # pour CHAQUE photo marquée « pending » puis « faces »).
    with cloud_storage.index_lock(email):
        cloud_storage.ucloud_db.patch_entry(
            cloud_storage.ucloud_dir(email), path, lambda e: e.update(faceid=value) or True)


def _write_progress(email: str, progress: dict) -> None:
    progress["updated_at"] = int(time.time())
    path = cloud_storage.ucloud_dir(email) / "faceid_reprocess.json"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(progress), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _set_quiet_until(email: str, until: int) -> None:
    path = cloud_storage.ucloud_dir(email) / "faceid_quiet_until"
    path.write_text(str(int(until)), encoding="utf-8")
    os.chmod(path, 0o600)


FACEID_SH = Path.home() / ".zen" / "Astroport.ONE" / "IA" / "generators" / "faceid.sh"
MATCHER = Path.home() / ".zen" / "Astroport.ONE" / "IA" / "bro" / "satellite_face_matcher.py"
QUICK_LOOKAHEAD = 6          # photos déchiffrées d'avance (fichiers clairs 0600, dossier privé)


def _run_quick(email: str, queue: list, record, quiet, durations: list) -> list:
    """Mode RAPIDE : un seul `faceid.sh --batch` (ComfyUI local, tunnel SSH ou
    P2P IPFS via comfyui.me.sh, connexion vérifiée UNE fois), résultat passé
    directement à satellite_face_matcher.py — exactement ce que fait
    bro_dm_daemon.sh à réception d'un vision_analysis_result, mais sans
    l'aller-retour DM NOSTR (~60 s par photo, relève du daemon). Mesuré :
    ~1,3 s par photo contre ~10 s.

    Les clairs ne vivent que le temps de l'analyse, dans un dossier 0700 sous
    /tmp (PAS ~/.zen/tmp/, purgé chaque nuit en plein rattrapage), supprimé
    à la fin quoi qu'il arrive. Retourne les chemins NON traités (ComfyUI
    injoignable) pour repli sur le relais NOSTR."""
    if not FACEID_SH.exists() or not MATCHER.exists():
        _log(f"ERROR: {FACEID_SH} ou {MATCHER} introuvable")
        return queue
    owner_hex = cloud_storage.hex_for_email(email)
    tmpdir = tempfile.mkdtemp(prefix="faceid_quick_")
    proc = subprocess.Popen(["bash", str(FACEID_SH), "--batch"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    slots = threading.Semaphore(QUICK_LOOKAHEAD)
    sent: "queue_mod.Queue" = queue_mod.Queue()      # (path, fichier clair, cid, t0) dans l'ordre d'envoi
    remaining = list(queue)

    def feeder() -> None:
        try:
            for i, path in enumerate(queue):
                slots.acquire()
                try:
                    entry = cloud_storage.db_entry(email, path) or {}
                    plain, _mime = cloud_storage.read_plaintext(email, path)
                except Exception as exc:
                    _log(f"ERROR: {path} illisible ({exc}) — ignorée")
                    slots.release()
                    continue
                local = os.path.join(tmpdir, f"{i:06d}{os.path.splitext(path)[1] or '.jpg'}")
                fd = os.open(local, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(plain)
                _set_faceid(email, path, {"status": "pending",
                                          "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
                sent.put((path, local, entry.get("cid", ""), time.time()))
                proc.stdin.write(local + "\n")
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            sent.put(None)
            try:
                proc.stdin.close()
            except OSError:
                pass

    def match(path: str, cid: str, faces: list, t0: float) -> None:
        payload = json.dumps({"email": email, "owner_hex": owner_hex, "path": path,
                              "ipfs_link": cid, "status": "ok", "faces": faces})
        try:
            subprocess.run([sys.executable, str(MATCHER), email, owner_hex, path, cid],
                           input=payload, text=True, capture_output=True, timeout=300)
        except Exception as exc:
            _log(f"WARN: matcher {path} : {exc}")
        status = _status_of(cloud_storage.db_entry(email, path) or {})
        if status == "pending":            # matcher en échec : à reprendre
            status = "timeout"
            _set_faceid(email, path, {"status": "timeout",
                                      "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        record(path, status, time.time() - t0)

    threading.Thread(target=feeder, daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=3)      # le matcher (Qdrant, partage) en parallèle
    try:
        while True:
            item = sent.get()
            if item is None:
                break
            path, local, cid, t0 = item
            line = proc.stdout.readline()
            try:
                os.unlink(local)
            except OSError:
                pass
            slots.release()
            if not line:
                _log("ERROR: faceid.sh --batch s'est arrêté")
                break
            try:
                res = json.loads(line)
            except ValueError:
                res = {"ok": False, "error": "bad_json"}
            if res.get("fatal"):
                _log(f"ERROR: {res['fatal']}")
                _set_faceid(email, path, {"status": "timeout"})
                break
            remaining.remove(path)
            if res.get("ok"):
                pool.submit(match, path, cid, res.get("faces") or [], t0)
            else:
                _set_faceid(email, path, {"status": "timeout", "error": res.get("error", ""),
                                          "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
                record(path, "timeout", time.time() - t0)
            per = (sum(durations[-30:]) / len(durations[-30:])) if durations else 5.0
            quiet(len(remaining), per)
    finally:
        pool.shutdown(wait=True)
        proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
    return remaining


COMFYUI_ME = Path.home() / ".zen" / "Astroport.ONE" / "IA" / "services" / "comfyui.me.sh"


def comfyui_reachable() -> bool:
    """ComfyUI joignable depuis CETTE station (local, tunnel SSH ou P2P IPFS) ?
    Même vérification que faceid.sh — établit le tunnel au besoin."""
    if not COMFYUI_ME.exists():
        return False
    try:
        return subprocess.run(["bash", str(COMFYUI_ME)], capture_output=True,
                              timeout=180).returncode == 0
    except Exception:
        return False


def analyze_quick(email: str, paths: list) -> tuple:
    """Analyse FaceID RAPIDE et SILENCIEUSE d'une liste de chemins du cloud de
    `email` (cf. _run_quick) — point d'entrée réutilisé par webdav_import.py
    juste après un import. Retourne (chemins non traités, compteurs par
    statut) ; les chemins non traités (ComfyUI injoignable) sont à confier
    au relais NOSTR par l'appelant."""
    counts: dict = {}
    durations: list = []
    lock = threading.Lock()

    def record(path: str, status: str, seconds: float) -> None:
        with lock:
            durations.append(seconds)
            counts[status] = counts.get(status, 0) + 1

    def quiet(remaining: int, per_photo: float) -> None:
        _set_quiet_until(email, time.time() + per_photo * remaining + 900)

    try:
        remaining = _run_quick(email, list(paths), record, quiet, durations)
    finally:
        _set_quiet_until(email, time.time() + 600)
    return remaining, counts


def _run_nostr(email: str, queue: list, args, record, quiet, durations: list) -> None:
    """Mode RELAIS (historique) : chaque photo part en DM vision_analysis_job
    vers le Brain, au plus --parallel en vol ; marche même sans ComfyUI
    joignable directement depuis cette station."""
    inflight: dict = {}          # path -> t0
    queue = list(queue)

    def _launch(path: str) -> None:
        entry = cloud_storage.db_entry(email, path)
        if not entry or not entry.get("cid"):
            return
        key_hex = cloud_storage.get_key_hex(email, entry["cid"])
        if not key_hex:
            _log(f"ERROR: clé absente pour {path} — ignorée")
            return
        _set_faceid(email, path, {"status": "pending",
                                  "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        cloud_storage._trigger_faceid_analysis(email, path, entry["cid"], key_hex)
        inflight[path] = time.time()

    while queue or inflight:
        avg = (sum(durations[-30:]) / len(durations[-30:])) if durations else 60.0
        quiet(len(queue) + len(inflight), avg / max(1, args.parallel))
        while queue and len(inflight) < args.parallel:
            _launch(queue.pop(0))
        time.sleep(POLL_SECONDS)
        entries = cloud_storage.load_index(email)["entries"]
        for path, t0 in list(inflight.items()):
            status = _status_of(entries.get(path) or {})
            if status == "pending" and time.time() - t0 < args.wait:
                continue
            if status == "pending":
                status = "timeout"
                _set_faceid(email, path, {"status": "timeout",
                                          "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            del inflight[path]
            record(path, status, time.time() - t0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Rattrapage FaceID silencieux")
    ap.add_argument("email")
    ap.add_argument("--status", default="no_face,none,pending,timeout")
    ap.add_argument("--wait", type=float, default=240.0,
                    help="attente max du résultat d'UNE photo (s)")
    ap.add_argument("--parallel", type=int, default=6,
                    help="photos en vol simultanément (la file GPU reste sérielle)")
    ap.add_argument("--quick", action="store_true",
                    help="(historique) équivaut à --mode auto : ComfyUI en direct, repli sur le relais")
    ap.add_argument("--mode", choices=("auto", "direct", "relay"), default=None,
                    help="auto : direct si ComfyUI répond, sinon relais NOSTR ; direct : ComfyUI seulement, sans repli ; "
                         "relay : messages NIP-44 vers un Brain. Défaut : le choix du capitaine "
                         "(services/analysis.py), sinon auto avec --quick et relay sans")
    ap.add_argument("--before", default="",
                    help="ne reprendre que les photos analysées AVANT cette date ISO (ex. 2026-10-12T16:20) — "
                         "évite de refaire celles déjà analysées par un rattrapage précédent")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    email = args.email
    if args.mode is None:
        if args.quick:
            from services import analysis
            args.mode = analysis.get_mode()
        else:
            args.mode = "relay"
    args.quick = args.mode in ("auto", "direct")
    wanted = {s.strip() for s in args.status.split(",") if s.strip()}
    before = datetime.fromisoformat(args.before).astimezone() if args.before else None

    if not cloud_storage.hex_for_email(email):
        print(f"ERROR:faceid_reprocess:{email}: MULTIPASS sans HEX sur cette station", file=sys.stderr)
        return 1

    lock_fd = os.open(str(cloud_storage.ucloud_dir(email) / ".reprocess.lock"),
                      os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"ERROR:faceid_reprocess:{email}: un rattrapage est déjà en cours", file=sys.stderr)
        return 1

    idx = cloud_storage.load_index(email)
    todo = sorted(
        path for path, e in idx["entries"].items()
        if isinstance(e, dict) and e.get("type") == "file"
        and str(e.get("mime", "")).startswith("image/")
        and e.get("origin") != "facecloud-share"
        and _status_of(e) in wanted
        and _checked_before(e, before)
    )
    if args.limit:
        todo = todo[: args.limit]
    _log(f"{email} : {len(todo)} image(s) à réanalyser (statuts {sorted(wanted)})")
    if args.dry_run or not todo:
        return 0

    progress = {"pid": os.getpid(), "running": True, "total": len(todo), "done": 0,
                "faces": 0, "no_face": 0, "timeout": 0, "started_at": int(time.time()),
                "mode": "quick" if args.quick else "nostr", "mode_requested": args.mode}
    durations: list = []
    counter = {"done": 0}
    record_lock = threading.Lock()

    def _record(path: str, status: str, seconds: float) -> None:
        with record_lock:
            durations.append(seconds)
            counter["done"] += 1
            progress["done"] = counter["done"]
            progress[status] = progress.get(status, 0) + 1
            _write_progress(email, progress)
            _log(f"{counter['done']}/{len(todo)} {status:8s} {seconds:5.1f}s  {path}")

    def _quiet(remaining: int, per_photo: float) -> None:
        # Fenêtre silencieuse prolongée à chaque tour : reste valide jusqu'à la
        # fin estimée, sans jamais rester posée indéfiniment si le script
        # meurt (elle expire d'elle-même).
        _set_quiet_until(email, time.time() + per_photo * remaining + 900)

    try:
        queue = list(todo)
        if args.quick:
            queue = _run_quick(email, queue, _record, _quiet, durations)
            if queue and args.mode == "direct":
                # Le capitaine a imposé l'accès direct : pas de repli silencieux sur le relais.
                progress["error"] = "direct_unavailable"
                _log(f"ERROR: accès direct impossible (ComfyUI injoignable) — {len(queue)} photo(s) non traitées "
                     "(le capitaine a choisi le mode « direct »)")
                queue = []
            elif queue:
                progress["mode"] = "nostr"
                _log(f"mode rapide indisponible — {len(queue)} photo(s) passent par le relais NOSTR")
        if queue:
            _run_nostr(email, queue, args, _record, _quiet, durations)
    finally:
        progress["running"] = False
        _write_progress(email, progress)
        # Petite marge pour les derniers résultats encore en vol, puis la
        # fenêtre silencieuse expire d'elle-même.
        _set_quiet_until(email, time.time() + 600)
        _log(f"terminé : {progress}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
