"""
services/story_render.py — Rendu des scènes et personnages de vidéos IA (bouton « Générer »).

Chaque rendu est un job : un processus `generate_scene.sh` (ou `generate_character.sh`) détaché de la
requête HTTP, avec son répertoire de travail. Le suivi vient de `progress.json`, que ces scripts écrivent
à chaque étape (stage, plan en cours, plans terminés). Un seul job tourne à la fois (un GPU) ; les autres
attendent leur tour (statut `queued`).

    ~/.zen/workspace/scenes/renders/<auteur8>-<d>/src/      paquet déplié (SCENES_DIR du rendu)
    ~/.zen/workspace/scenes/renders/<auteur8>-<d>/work/     cache incrémental : un plan inchangé n'est pas recalculé
    ~/.zen/workspace/scenes/renders/<auteur8>-<d>/v/<cid>/<job>/   archive du rendu d'une version (vidéo, plans, voix)
    ~/.zen/workspace/scenes/library/jobs/<job>.json         état du job (survit à un redémarrage d'UPassport)

Les rendus d'un paquet coopératif sont publiés sur IPFS et annoncés dans l'événement Kind 30510
(`renders`), pour que les autres Capitaines voient et entendent le résultat ; ceux d'un paquet privé
restent sur la station (servis par l'API, jamais ajoutés à IPFS).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import settings

sys.path.insert(0, str(settings.TOOLS_PATH))
import story_asset  # noqa: E402

logger = logging.getLogger(__name__)

GEN_DIR = settings.TOOLS_PATH.parent / "IA" / "generators"
RENDERS_DIR = story_asset.SCENES / "renders"
JOBS_DIR = story_asset.LIBRARY / "jobs"
SAFE_NAME = re.compile(r"^(scene\.mp4|portrait\.png|voice\.wav|(shot|part|vo)_\d{2}\.(mp4|wav))$")
_lock = threading.Lock()
_pump_started = False


# ═════════════════════════════════════════════════════════════════════════════
# État des jobs
# ═════════════════════════════════════════════════════════════════════════════
def _job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise story_asset.AssetError("identifiant de job invalide")
    return JOBS_DIR / f"{job_id}.json"


def _read_job(job_id: str) -> Dict[str, Any]:
    p = _job_path(job_id)
    if not p.exists():
        raise story_asset.AssetError("job inconnu")
    return json.loads(p.read_text())


def _write_job(job: Dict[str, Any]) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    p = _job_path(job["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1))
    tmp.replace(p)


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # un zombie n'est plus un job en cours
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def _progress(job: Dict[str, Any]) -> Dict[str, Any]:
    p = Path(job["workdir"]) / "progress.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"stage": job["status"], "shot": -1, "shots": job.get("shots", 0), "message": "", "done": []}


def list_jobs(limit: int = 30) -> List[Dict[str, Any]]:
    _pump()
    jobs = []
    if JOBS_DIR.is_dir():
        for p in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            try:
                jobs.append(job_status(p.stem))
            except (OSError, ValueError, story_asset.AssetError):
                pass
    return jobs


def job_status(job_id: str) -> Dict[str, Any]:
    with _lock:  # une seule fin de job à la fois (le thread du pump peut la déclencher aussi)
        job = _read_job(job_id)
        if job["status"] == "running" and not _pid_alive(job.get("pid")):
            _finish(job)  # le process est terminé : archivage et statut final
            job = _read_job(job_id)
    prog = _progress(job)
    now = time.time()
    shots_total = prog.get("shots") or job.get("shots", 0)
    done = prog.get("done", [])
    eta = None
    if job["status"] == "running" and done and shots_total and len(done) < shots_total:
        per_shot = (now - job["started"]) / len(done)
        eta = int(per_shot * (shots_total - len(done)))
    work = Path(job["workdir"])
    shots = [{"index": i, "ready": i in done, "file": f"shot_{i:02d}.mp4"} for i in range(shots_total)] \
        if job["kind"] == "scene" else []
    log_tail = ""
    try:
        log_tail = Path(job["log"]).read_text(errors="replace")[-1500:]
    except OSError:
        pass
    return {"id": job["id"], "kind": job["kind"], "status": job["status"], "cid": job["cid"], "name": job["name"],
            "version": job.get("version"), "queued_at": job["queued"], "started": job.get("started"),
            "finished": job.get("finished"), "elapsed": int((job.get("finished") or now) - job["started"]) if job.get("started") else 0,
            "stage": prog.get("stage"), "message": prog.get("message", ""), "current": prog.get("shot", -1),
            "shots_total": shots_total, "shots_done": len(done), "shots": shots, "eta": eta,
            "has_scene": (work / "scene.mp4").exists() and job["status"] == "done",
            "render": job.get("render"), "error": job.get("error"), "log": log_tail}


# ═════════════════════════════════════════════════════════════════════════════
# Démarrage d'un rendu
# ═════════════════════════════════════════════════════════════════════════════
def _asset_key(entry: Dict[str, Any]) -> str:
    return f"{(entry.get('author') or story_asset.captain_hex() or 'local')[:8]}-{story_asset.slug(entry['d'])}"


def start_render(ref: str, regen: Optional[List[str]] = None) -> Dict[str, Any]:
    """Met en file le rendu d'une version (scène : vidéo ; personnage : portrait + voix)."""
    entry, manifest, files = story_asset.open_bundle(ref)
    kind = manifest["type"]
    key = _asset_key(entry)
    root = RENDERS_DIR / key
    src, work = root / "src", root / "work"
    if _asset_has_active_job(work):
        raise story_asset.AssetError("un rendu est déjà en cours pour cette scène : patientez qu'il se termine")
    if src.exists():
        shutil.rmtree(src)  # le paquet de CETTE version : ses chemins $SCENES_DIR doivent résoudre dedans
    for rel, data in files.items():
        story_asset.check_path(rel)
        dest = src / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    work.mkdir(parents=True, exist_ok=True)
    job: Dict[str, Any] = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "queued", "cid": entry["cid"],
                           "name": manifest["name"], "version": entry.get("version", 1), "scope": entry.get("scope", "private"),
                           "mine": not entry.get("foreign"), "queued": int(time.time()), "src": str(src), "workdir": str(work),
                           "root": str(root), "log": str(root / "job.log"), "regen": regen or []}
    if kind == "scene":
        if "storyboard.json" not in files:
            raise story_asset.AssetError("ce paquet n'a pas de storyboard")
        sb = json.loads(files["storyboard.json"])
        job["shots"] = len(sb.get("shots", []))
        job["cmd"] = [str(GEN_DIR / "generate_scene.sh"), "-w", str(work), str(src / "storyboard.json")]
    else:
        meta_path = next((p for p in files if p.endswith("character.json")), None)
        meta = json.loads(files[meta_path]) if meta_path else {}
        if not meta.get("look") and "cast/%s/portrait.png" % manifest["name"] not in files:
            raise story_asset.AssetError("décrivez d'abord l'apparence du personnage (champ « look »)")
        # les fichiers existants du paquet servent de point de départ ; « regen » force leur recalcul
        for f in ("portrait.png", "voice.wav"):
            have = src / "cast" / manifest["name"] / f
            if have.exists() and f.split(".")[0] not in (regen or []):
                shutil.copy(have, work / f)
            else:
                (work / f).unlink(missing_ok=True)
        job["shots"] = 2
        job["cmd"] = [str(GEN_DIR / "generate_character.sh"), "-w", str(work), "-S", str(zlib.crc32(manifest["name"].encode()) % 9999),
                      manifest["name"], meta.get("look", ""), meta.get("voice_design", ""), meta.get("voice_line", "")]
    # un ancien progress.json ferait croire à un rendu terminé
    (work / "progress.json").unlink(missing_ok=True)
    with _lock:
        _write_job(job)
    _ensure_pump()
    _pump()
    return job_status(job["id"])


def _asset_has_active_job(work: Path) -> bool:
    """Un job (scène entière ou plan seul) tourne-t-il déjà sur CE répertoire de travail ?
    Évite qu'un rendu de plan supprime des fichiers qu'un rendu complet est en train de lire
    ou d'écrire sur le même cache (et inversement)."""
    if not JOBS_DIR.is_dir():
        return False
    for p in JOBS_DIR.glob("*.json"):
        try:
            j = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if j.get("workdir") == str(work) and j.get("status") in ("queued", "running"):
            return True
    return False


def start_shot_render(ref: str, index: int) -> Dict[str, Any]:
    """Rend UN SEUL plan d'une scène déjà publiée, dans le même répertoire de travail qu'un
    rendu complet (les autres plans déjà calculés ne sont pas retouchés — cache partagé). La
    prise précédente de ce plan, si elle existe, est archivée sous v/<cid>/shots/<index>/ (jamais
    perdue) avant d'être remplacée."""
    entry, manifest, files = story_asset.open_bundle(ref)
    if manifest["type"] != "scene":
        raise story_asset.AssetError("seule une scène a des plans à générer séparément")
    if "storyboard.json" not in files:
        raise story_asset.AssetError("ce paquet n'a pas de storyboard")
    sb = json.loads(files["storyboard.json"])
    nb = len(sb.get("shots", []))
    if not (0 <= index < nb):
        raise story_asset.AssetError(f"plan {index} hors plage (0-{nb - 1})")
    key = _asset_key(entry)
    root = RENDERS_DIR / key
    src, work = root / "src", root / "work"
    if _asset_has_active_job(work):
        raise story_asset.AssetError("un rendu est déjà en cours pour cette scène : patientez qu'il se termine")
    if not src.exists():  # premier rendu de cette version (comme start_render)
        for rel, data in files.items():
            story_asset.check_path(rel)
            dest = src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    work.mkdir(parents=True, exist_ok=True)
    n = f"{index:02d}"
    old = work / f"shot_{n}.mp4"
    if old.exists():
        takes_dir = root / "v" / entry["cid"] / "shots" / n
        takes_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, takes_dir / f"{int(time.time())}.mp4")
    # Repart à neuf sur ce plan (et ses fichiers dérivés) : un clic sur « Générer ce plan »
    # demande explicitement une nouvelle prise, même si rien n'a changé dans le storyboard
    for pat in (f"shot_{n}.*", f"talk_{n}.*", f"screenclip_{n}.*", f"mix_{n}.*", f"part_{n}.*",
                f"vo_{n}.*", f"image_{n}.*", f"last_{n}.*", f"card_{n}.*", f"screen_{n}.*", f"title_{n}.txt",
                f"shot_{n}.sig"):
        for p in work.glob(pat):
            p.unlink(missing_ok=True)
    # Graine décalée à chaque nouvelle prise : même graine + mêmes paramètres = même plan, une relance sans changement
    # de prompt ne produirait qu'un doublon de la prise précédente. Première prise : graine d'origine (décalage 0).
    seed_shift = 7919 * len(shot_takes(entry["cid"], index))
    job: Dict[str, Any] = {"id": uuid.uuid4().hex[:12], "kind": "shot", "status": "queued", "cid": entry["cid"],
                           "name": manifest["name"], "version": entry.get("version", 1), "scope": entry.get("scope", "private"),
                           "mine": not entry.get("foreign"), "queued": int(time.time()), "src": str(src), "workdir": str(work),
                           "root": str(root), "log": str(root / "job.log"), "shots": 1, "shot_index": index, "seed_shift": seed_shift,
                           "cmd": [str(GEN_DIR / "generate_scene.sh"), "-w", str(work), "-i", str(index), str(src / "storyboard.json")]}
    (work / "progress.json").unlink(missing_ok=True)
    with _lock:
        _write_job(job)
    _ensure_pump()
    _pump()
    return job_status(job["id"])


def _takes_dirs(entry: Dict[str, Any], index: int) -> List[Path]:
    """Dossiers de prises d'un plan : le catalogue courant (work/takes/NN, écrit par generate_scene.sh :
    fiches JSON + CID IPFS) puis l'ancien archivage (v/<cid>/shots/NN, sans fiche)."""
    root = RENDERS_DIR / _asset_key(entry)
    n = f"{index:02d}"
    return [root / "work" / "takes" / n, root / "v" / entry["cid"] / "shots" / n]


def shot_takes(ref: str, index: int) -> List[Dict[str, Any]]:
    """Toutes les prises d'un plan, de la plus récente à la plus ancienne : brouillon, normal, relance
    après changement de prompt… Chaque prise porte sa fiche (qualité, steps, prompt, CID IPFS) ;
    `current` marque celle qui correspond au plan actuellement utilisé pour l'assemblage."""
    entry = story_asset.find_entry(ref)
    cur_dir, legacy_dir = _takes_dirs(entry, index)
    work = cur_dir.parent.parent
    try:
        cur_sig = (work / f"shot_{index:02d}.sig").read_text().strip()
    except OSError:
        cur_sig = ""
    out: Dict[str, Dict[str, Any]] = {}
    if cur_dir.is_dir():
        for mp4 in cur_dir.glob("*.mp4"):
            meta: Dict[str, Any] = {}
            try:
                meta = json.loads(mp4.with_suffix(".json").read_text())
            except (OSError, ValueError):
                pass
            try:  # fichier « current » (prises importées d'un rendu CLI) : id de la prise utilisée au montage
                marked = (cur_dir / "current").read_text().strip() == mp4.stem
            except OSError:
                marked = False
            meta.update(id=mp4.stem, created=meta.get("created") or int(mp4.stat().st_mtime),
                        current=marked or bool(cur_sig and meta.get("sig") == cur_sig))
            out[mp4.stem] = meta
    if legacy_dir.is_dir():  # prises antérieures à la fiche : date seulement
        for mp4 in legacy_dir.glob("*.mp4"):
            out.setdefault(mp4.stem, {"id": mp4.stem, "created": int(mp4.stat().st_mtime), "legacy": True})
    return sorted(out.values(), key=lambda t: t["created"], reverse=True)


def shot_take_file(ref: str, index: int, take: str) -> Path:
    if not re.fullmatch(r"\d{1,12}", take):
        raise story_asset.AssetError("identifiant de prise invalide")
    entry = story_asset.find_entry(ref)
    for d in _takes_dirs(entry, index):
        p = d / f"{take}.mp4"
        if p.is_file():
            return p
    raise story_asset.AssetError("prise introuvable")


def _ensure_pump() -> None:
    global _pump_started
    if _pump_started:
        return
    _pump_started = True

    def loop():
        while True:
            time.sleep(3)
            try:
                _pump()
            except Exception:  # pragma: no cover - ne jamais tuer le thread
                logger.exception("story: pump")

    threading.Thread(target=loop, name="story-render-pump", daemon=True).start()


def _pump() -> None:
    """Lance le plus ancien job en attente si aucun ne tourne ; termine ceux dont le process a disparu."""
    with _lock:
        if not JOBS_DIR.is_dir():
            return
        jobs = []
        for p in JOBS_DIR.glob("*.json"):
            try:
                jobs.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                pass
        running = [j for j in jobs if j["status"] == "running"]
        for j in running:
            if not _pid_alive(j.get("pid")):
                _finish(j)
        if any(j["status"] == "running" and _pid_alive(j.get("pid")) for j in jobs):
            return
        queued = sorted((j for j in jobs if j["status"] == "queued"), key=lambda j: j["queued"])
        if queued:
            _launch(queued[0])


def _launch(job: Dict[str, Any]) -> None:
    env = dict(os.environ, SCENES_DIR=job["src"], CAST_BANK=str(Path(job["root"]) / "bank"))
    if job.get("seed_shift"):
        env["SHOT_SEED_SHIFT"] = str(job["seed_shift"])
    if job["scope"] != "coop" or not job["mine"]:
        env["STORY_NO_IPFS"] = "1"  # rendu privé : la vidéo ne part pas sur IPFS
    log = open(job["log"], "ab")
    try:
        proc = subprocess.Popen(job["cmd"], cwd=GEN_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)  # survit au redémarrage d'UPassport
    except OSError as exc:
        job.update(status="failed", error=str(exc), finished=int(time.time()))
        _write_job(job)
        return
    job.update(status="running", pid=proc.pid, started=int(time.time()))
    _write_job(job)
    threading.Thread(target=proc.wait, daemon=True).start()  # récolte le zombie


def cancel(job_id: str) -> Dict[str, Any]:
    job = _read_job(job_id)
    if job["status"] == "queued":
        job.update(status="cancelled", finished=int(time.time()))
    elif job["status"] == "running" and _pid_alive(job.get("pid")):
        try:
            os.killpg(os.getpgid(job["pid"]), signal.SIGTERM)
        except OSError:
            pass
        job.update(status="cancelled", finished=int(time.time()))
    _write_job(job)
    return job_status(job_id)


# ═════════════════════════════════════════════════════════════════════════════
# Fin de job : archivage, enregistrement du rendu, nouvelle version du personnage
# ═════════════════════════════════════════════════════════════════════════════
def _duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return round(float(p.stdout.strip()), 1)
    except ValueError:
        return 0.0


def _finish(job: Dict[str, Any]) -> None:
    prog = _progress(job)
    ok = prog.get("stage") == "done"
    work = Path(job["workdir"])
    job["finished"] = int(time.time())
    if not ok:
        job.update(status="failed", error=prog.get("message") or "le rendu s'est arrêté avant la fin")
        _write_job(job)
        return
    try:
        if job["kind"] == "scene":
            job["render"] = _archive_scene(job, work)
        elif job["kind"] == "character":
            job["render"] = _apply_character(job, work)
        else:
            job["render"] = _finish_shot(job, work)
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001 - l'erreur est montrée à l'utilisateur
        logger.exception("story: fin de job %s", job["id"])
        job.update(status="failed", error=str(exc))
    _write_job(job)


def _archive_scene(job: Dict[str, Any], work: Path) -> Dict[str, Any]:
    final = work / "scene.mp4"
    if not final.exists():
        raise story_asset.AssetError("scene.mp4 absent après le rendu")
    arch = Path(job["root"]) / "v" / job["cid"] / job["id"]
    arch.mkdir(parents=True, exist_ok=True)
    names = ["scene.mp4"] + sorted(p.name for p in work.glob("part_*.mp4")) + sorted(p.name for p in work.glob("vo_*.wav"))
    for n in names[1:]:
        try:
            os.link(work / n, arch / n)  # lien physique : les plans réutilisés ne sont stockés qu'une fois
        except OSError:
            shutil.copy2(work / n, arch / n)
    shutil.copy2(final, arch / "scene.mp4")  # copie : le rendu suivant remplace work/scene.mp4
    public = job["scope"] == "coop" and job["mine"]
    rec = {"id": job["id"], "created": int(time.time()), "duration": _duration(arch / "scene.mp4"),
           "shots": [n for n in names if n.startswith("part_")], "voices": [n for n in names if n.startswith("vo_")],
           "public": public, "dir": str(arch)}
    if public:  # CID IPFS des plans finis (écrits par generate_scene.sh) : partageables avec la scène
        try:
            plans = json.loads((work / "plans.json").read_text())
            rec["plans"] = [{"file": n, "cid": plans[n]} for n in rec["shots"] if plans.get(n)]
        except (OSError, ValueError):
            pass
    if public:
        p = subprocess.run(["ipfs", "add", "-q", str(arch / "scene.mp4")], capture_output=True, text=True)
        rec["mp4_cid"] = p.stdout.strip().splitlines()[-1] if p.returncode == 0 and p.stdout.strip() else None
    story_asset.add_render(job["cid"], rec)
    if public and rec.get("mp4_cid"):
        entry = story_asset.load_keyring().get(job["cid"])
        if entry:
            story_asset.announce(entry)  # les autres Capitaines voient et entendent ce rendu
    return {k: v for k, v in rec.items() if k != "dir"}


def _finish_shot(job: Dict[str, Any], work: Path) -> Dict[str, Any]:
    idx = job["shot_index"]
    name = f"shot_{idx:02d}.mp4"
    if not (work / name).exists():
        raise story_asset.AssetError(f"{name} absent après le rendu")
    return {"index": idx, "file": name, "duration": _duration(work / name)}


def _apply_character(job: Dict[str, Any], work: Path) -> Dict[str, Any]:
    if not job["mine"]:
        raise story_asset.AssetError("personnage d'un autre Capitaine : copiez-le d'abord dans votre bibliothèque")
    entry, manifest, _files = story_asset.open_bundle(job["cid"])
    entry = story_asset.latest_of(entry)
    changes = {}
    for f in ("portrait.png", "voice.wav"):
        if (work / f).exists():
            changes[f"cast/{manifest['name']}/{f}"] = (work / f).read_bytes()
    new = story_asset.new_version(entry["cid"], changes, reshare=True)
    return {"new_cid": new["cid"], "version": new["version"]}


# ═════════════════════════════════════════════════════════════════════════════
# Fichiers servis à l'interface
# ═════════════════════════════════════════════════════════════════════════════
def job_file(job_id: str, name: str) -> Path:
    if not SAFE_NAME.match(name):
        raise story_asset.AssetError("nom de fichier refusé")
    job = _read_job(job_id)
    p = Path(job["workdir"]) / name
    if not p.is_file():
        raise story_asset.AssetError("fichier pas encore disponible")
    return p


def render_file(version_cid: str, render_id: str, name: str) -> Path:
    if not SAFE_NAME.match(name):
        raise story_asset.AssetError("nom de fichier refusé")
    for rec in story_asset.renders_index().get(version_cid, []):
        if rec["id"] == render_id and rec.get("dir"):
            p = Path(rec["dir"]) / name
            if p.is_file():
                return p
    raise story_asset.AssetError("rendu introuvable")
