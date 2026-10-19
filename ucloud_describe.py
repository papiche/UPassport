#!/usr/bin/env python3
"""
ucloud_describe.py — Décrit les photos d'un cloud FaceCloud avec le modèle vision d'Ollama.

    python3 ucloud_describe.py scan  <email> [--paths P…] [--mode auto|direct|relay] [--limit N] [--force] [--workers 1]
    python3 ucloud_describe.py --apply            (JSON d'un résultat de Brain sur stdin : mode relais)

Sans `--paths` : toutes les photos SANS description (`--redo` : celles décrites par l'IA avec un autre modèle que `analysis.DESCRIBE_MODEL`). Une description écrite ou corrigée à la main
(`desc.src == "user"`) n'est JAMAIS remplacée, même avec --force. Où ça tourne (choix du capitaine,
services/analysis.py) :
  direct  la photo est déchiffrée en mémoire et passée à `Astroport.ONE/IA/vision_describe.py --stdin`
          (Ollama local, tunnel SSH ou P2P IPFS) — une à la fois, le GPU est le goulot ;
  relay   un message NIP-44 `vision_analysis_job` (task=describe, photo = CID + clé) part vers un Brain ; sa
          réponse revient sur `vision_analysis_result` et `bro_dm_daemon.sh` appelle `--apply`. La file a une
          durée de vie de 30 min : on n'envoie que RELAY_BATCH photos par passage, à renouveler.
  auto    direct si Ollama répond, sinon relais.

Progression : `.ucloud/describe_scan.json` (running, total, done, failed, submitted, mode). Une seule instance
par compte (flock `.describe.lock`).
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
from typing import Callable, Dict, Optional

from services import analysis, cloud_storage, trace, ucloud_db  # noqa: E402

VISION = Path.home() / ".zen" / "Astroport.ONE" / "IA" / "vision_describe.py"
RELAY_BATCH = 60              # photos envoyées au Brain par passage (file à TTL 30 min)
RELAY_RESEND_AFTER = 35 * 60  # un job plus récent que ça est encore dans la file : on ne le renvoie pas


def progress_path(email: str) -> Path:
    return cloud_storage.ucloud_dir(email) / "describe_scan.json"


def _write_progress(email: str, data: dict) -> None:
    data["updated_at"] = int(time.time())
    p = progress_path(email)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)


def set_description(email: str, path: str, text: str, src: str = "ai", model: str = "", force: bool = False) -> str:
    return cloud_storage.set_photo_description(email, path, text, src, model)


def describe_direct(email: str, path: str, runner: Optional[Callable[[bytes], dict]] = None) -> dict:
    """Déchiffre en mémoire et décrit par l'accès direct. `runner` (tests) remplace vision_describe.py."""
    with trace.Timer() as t_dec:
        plain, mime = cloud_storage.read_plaintext(email, path)
    trace.log("describe", f"déchiffrement {mime or '?'} {len(plain) // 1024} Ko en {t_dec.s:.2f}s (clair en mémoire seulement)", 2)
    if runner:
        return runner(plain)
    if not VISION.exists():
        return {"error": f"{VISION} introuvable"}
    trace.log("describe", f"→ {sys.executable} {VISION.name} --stdin (suivi des sous-processus : FACECLOUD_VERBOSE={trace.level()})", 2)
    with trace.Timer() as t_vis:
        proc = subprocess.run([sys.executable, str(VISION), "--stdin"], input=plain, capture_output=True, timeout=420)
    err = proc.stderr.decode("utf-8", "replace")
    if trace.level() and err.strip():                 # le suivi de vision_describe.py / describe_image.py, même chronologie
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    trace.log("describe", f"vision_describe.py : code {proc.returncode} en {t_vis.s:.1f}s", 2)
    out = None
    for line in reversed(proc.stdout.decode("utf-8", "replace").strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    if out is None:
        out = {"error": proc.stderr.decode("utf-8", "replace")[-300:] or "pas de réponse"}
    if not out.get("description"):
        _check_source(plain)           # le modèle a échoué : est-ce la photo (illisible) ou lui ?
    return out


DESC_RETRY_DAYS = 7


class SourceUnreadable(Exception):
    """La PHOTO est inutilisable (fichier vide, JPEG corrompu, bloc IPFS invalide), pas le modèle."""


def _check_source(plain: bytes) -> None:
    if not plain:
        raise SourceUnreadable("fichier vide")
    try:
        from PIL import Image
        import io
        Image.open(io.BytesIO(plain))
    except ImportError:
        return
    except Exception as exc:
        raise SourceUnreadable(f"image illisible ({type(exc).__name__})")


def _is_source_error(exc: Exception) -> bool:
    """Bloc IPFS invalide ou manquant (réponse 5xx du démon) : permanent pour CETTE photo.
    Un démon injoignable ou un délai dépassé n'est PAS la photo : on ne la marque pas."""
    return isinstance(exc, SourceUnreadable) or (
        type(exc).__name__ == "IPFSError" and "HTTP 5" in str(exc))


def _mark_unreadable(udir, path: str, reason: str) -> None:
    """Sort la photo de la file (`describe_pending`) pendant DESC_RETRY_DAYS : sans ça, les mêmes
    photos cassées restaient en tête à chaque passage et coupaient le lot après 5 échecs."""
    ucloud_db.patch_entry(udir, path, lambda e: e.update(desc_fail={"at": int(time.time()), "reason": reason[:200]}) or True)


def submit_relay(email: str, path: str) -> bool:
    """Envoie la tâche au Brain (message NIP-44). La clé AES voyage dans le DM déjà chiffré (même contrat
    que le FaceID : le Brain déchiffre en mémoire et ne garde rien)."""
    entry = cloud_storage.db_entry(email, path) or {}
    key_hex = cloud_storage.get_key_hex(email, entry.get("cid", "")) if entry.get("cid") else None
    if not key_hex:
        return False
    hex_pub = cloud_storage.hex_for_email(email)
    trigger = cloud_storage.settings.TOOLS_PATH / "trigger_bro_vision_analysis.sh"
    if not hex_pub or not trigger.exists():
        return False
    env = dict(os.environ, UCLOUD_DECRYPTION_KEY=key_hex, UCLOUD_TASK="describe")
    trace.log("describe", f"relais : bash {trigger.name} {email} {hex_pub[:12]}… {path} cid={entry['cid'][:16]}… "
                          "(la clé AES voyage dans l'environnement, jamais affichée)", 2)
    try:
        with trace.Timer() as t:
            proc = subprocess.run(["bash", str(trigger), email, hex_pub, path, entry["cid"], ""], env=env,
                                  capture_output=True, timeout=120)
    except Exception as exc:
        trace.log("describe", f"relais : exception {type(exc).__name__}: {exc}")
        return False
    trace.log("describe", f"relais : code {proc.returncode} en {t.s:.1f}s"
              + (f" — {proc.stderr.decode('utf-8', 'replace')[-300:]}" if proc.returncode else ""), 2)
    if proc.returncode != 0:
        return False
    ucloud_db.patch_entry(cloud_storage.ucloud_dir(email), path, lambda e: e.update(desc_job=int(time.time())) or True)
    return True


def scan(email: str, paths: Optional[list] = None, mode: Optional[str] = None, limit: int = 0,
         force: bool = False, runner: Optional[Callable[[bytes], dict]] = None, redo: bool = False,
         verbose: int = 0) -> dict:
    if verbose:
        trace.set_level(max(trace.level(), int(verbose)))     # les sous-processus l'héritent : une seule chronologie
    verbose = trace.level()
    udir = cloud_storage.ensure_ucloud_dir(email)
    state = None
    described_cids = set()
    lock_fd = cloud_storage.try_lock_nowait(udir / ".describe.lock")
    if lock_fd is None:
        print("ERROR:describe: un passage est déjà en cours", file=sys.stderr)
        return {"status": "busy"}
    try:
        mode = mode or analysis.get_mode()
        route = analysis.resolve("describe", mode)
        eff = route["effective"]
        trace.log("describe", f"{email} : mode demandé={mode}, chemin={eff} (accès direct prêt={route.get('direct_ready')}, "
                              f"relais prêt={route.get('relay_ready')}) — {route.get('why')}")
        trace.log("describe", f"base {cloud_storage.ucloud_dir(email) / 'ucloud.db'}, modèle vision={analysis.DESCRIBE_MODEL}, "
                              f"Ollama 127.0.0.1:{analysis.OLLAMA_PORT}, options redo={redo} force={force} limit={limit or 'aucune'} "
                              f"paths={len(paths) if paths else 'toutes'}")
        if runner:
            eff = "direct"
        # redo : reprend les descriptions IA écrites par un AUTRE modèle (jamais celles saisies à la main).
        todo = paths if paths else (ucloud_db.describe_stale(udir, analysis.DESCRIBE_MODEL) if redo else ucloud_db.describe_pending(udir))
        trace.log("describe", f"sélection initiale : {len(todo)} photo(s) ({'refaire' if redo else 'sans description'})")
        dropped = {"écrite à la main / déjà décrite": 0, "envoyée au Brain récemment": 0}
        if not force and not redo:
            keep = []
            for p in todo:
                e = cloud_storage.db_entry(email, p) or {}
                d = e.get("desc") if isinstance(e.get("desc"), dict) else {}
                if d.get("text") and (d.get("src") == "user" or not paths):
                    dropped["écrite à la main / déjà décrite"] += 1
                    continue
                if eff == "relay" and e.get("desc_job") and time.time() - e["desc_job"] < RELAY_RESEND_AFTER:
                    dropped["envoyée au Brain récemment"] += 1
                    continue
                keep.append(p)
            todo = keep
            trace.log("describe", f"après filtre : {len(todo)} retenue(s), écartées : {dropped}")
        if limit:
            todo = todo[:limit]
        if eff == "relay":
            todo = todo[:RELAY_BATCH]
        state = {"running": True, "pid": os.getpid(), "mode": eff, "mode_requested": mode, "total": len(todo),
                 "done": 0, "failed": 0, "submitted": 0, "started_at": int(time.time())}
        if eff == "none":
            state.update(running=False, error="no_path")
            _write_progress(email, state)
            print("ERROR:describe: ni accès direct à Ollama, ni relais NOSTR", file=sys.stderr)
            return state
        _write_progress(email, state)
        if verbose:
            print(f"describe: {len(todo)} photo(s) à décrire — chemin {eff} (demandé : {mode}), modèle {analysis.DESCRIBE_MODEL}"
                  + (" [redo : descriptions IA d'un autre modèle]" if redo else ""), flush=True)
        t_start = time.time()
        for i, path in enumerate(todo, 1):
            t_photo = time.time()
            _e = cloud_storage.db_entry(email, path) or {}
            _d = _e.get("desc") if isinstance(_e.get("desc"), dict) else {}
            trace.log("describe", f"[{i}/{len(todo)}] {path} — {_e.get('mime', '?')}, {(_e.get('size_plain') or 0) // 1024} Ko, "
                                  f"cid={str(_e.get('cid', ''))[:16]}…, ancienne description : {_d.get('src') or 'aucune'}/{_d.get('model', '-')}", 2)
            try:
                if eff == "relay":
                    ok = submit_relay(email, path)
                    state["submitted" if ok else "failed"] += 1
                    if verbose:
                        print(f"[{i}/{len(todo)}] {path}  {'envoyée au Brain' if ok else 'ÉCHEC envoi'}", flush=True)
                else:
                    out = describe_direct(email, path, runner)
                    if out.get("description"):
                        r = set_description(email, path, out["description"], "ai", out.get("model", ""), force=force)
                        state["ok"] = state.get("ok", 0) + 1
                        if _e.get("cid"):
                            described_cids.add(_e["cid"])
                        if verbose:
                            print(f"[{i}/{len(todo)}] {path}  {time.time() - t_photo:.1f}s  {out.get('model', '?')}  ({r})\n    "
                                  + out["description"].replace("\n", "\n    "), flush=True)
                    else:
                        state["failed"] += 1
                        state["model_failed"] = state.get("model_failed", 0) + 1
                        print(f"WARN:describe:{path}: {out.get('error', '?')}", file=sys.stderr)
                        if verbose:
                            print(f"[{i}/{len(todo)}] {path}  ÉCHEC  {time.time() - t_photo:.1f}s", flush=True)
                        # seuls les échecs du MODÈLE comptent : une photo cassée n'est pas une panne
                        if state["model_failed"] >= 5 and not state.get("ok"):
                            state["error"] = "model_unavailable"
                            print("ERROR:describe: 5 échecs d'affilée — arrêt (modèle ou tunnel indisponible)", file=sys.stderr)
                            break
            except Exception as exc:
                state["failed"] += 1
                if eff != "relay" and _is_source_error(exc):
                    state["unreadable"] = state.get("unreadable", 0) + 1
                    _mark_unreadable(udir, path, str(exc))
                    print(f"WARN:describe:{path}: photo illisible, écartée {DESC_RETRY_DAYS} jours : {str(exc)[:160]}", file=sys.stderr)
                else:
                    state["model_failed"] = state.get("model_failed", 0) + 1
                    print(f"WARN:describe:{path}: {exc}", file=sys.stderr)
            state["done"] = i
            if eff != "relay" and not runner and (i == 1 or i % 10 == 0) and analysis.model_on_cpu(analysis.DESCRIBE_MODEL):
                state["error"] = "gpu_unavailable"
                print("ERROR:describe: le modèle tourne sur le CPU (size_vram=0) : GPU perdu ? Arrêt pour ne pas saturer le CPU "
                      "— corriger le GPU/redémarrer la machine d'IA, ou FACECLOUD_ALLOW_CPU=1 pour forcer", file=sys.stderr)
                break
            if i % 5 == 0 or i == len(todo):
                _write_progress(email, state)
                trace.log("describe", f"progression écrite : {i}/{len(todo)} (ok={state.get('ok', 0)}, échecs={state['failed']}) → describe_scan.json", 2)
            if i % 10 == 0 and i < len(todo):
                _dt = time.time() - t_start
                trace.log("describe", f"cadence {i / _dt * 60:.0f} photos/min — reste ≈ {(len(todo) - i) / max(i / _dt, 1e-9) / 60:.1f} min")
        state["running"] = False
        _write_progress(email, state)
        if verbose:
            dt = time.time() - t_start
            print(f"describe: terminé en {dt:.0f}s — {state.get('ok', 0)} décrite(s), {state['failed']} échec(s)"
                  + (f", {dt / max(1, state.get('ok', 0)):.1f}s/photo" if state.get('ok') else ""), flush=True)
        print(json.dumps(state))
    finally:
        if state and state.get("running"):              # Ctrl-C ou erreur : l'état ne doit pas rester « en cours »
            state.update(running=False, interrupted=True)
            _write_progress(email, state)
            print(f"describe: interrompu après {state.get('done', 0)}/{state.get('total', 0)} — ce qui est décrit est conservé", file=sys.stderr, flush=True)
        os.close(lock_fd)
    # Les descriptions toutes fraîches deviennent cherchables PAR LE SENS : seulement CELLES-LÀ (pas les 21 000 autres,
    # que la recherche ou `ucloud_embed.py scan` vectorisent à part). Best-effort : jamais bloquant, erreur visible.
    if described_cids and not runner and not state.get("interrupted"):
        try:
            import ucloud_embed
            trace.log("describe", f"vectorisation des {len(described_cids)} description(s) du passage (recherche par le sens)…")
            ucloud_embed.scan(email, only_cids=described_cids)
        except Exception as exc:
            print(f"WARN:describe: vectorisation des descriptions impossible : {exc}", file=sys.stderr)
    return state


def apply_result(message: dict) -> int:
    """Résultat d'un Brain (mode relais) : {email, path, task:'describe', status, description, model}."""
    email, path = str(message.get("email") or ""), str(message.get("path") or "")
    if not cloud_storage.hex_for_email(email) or not path:
        print("ERROR:describe:destinataire inconnu", file=sys.stderr)
        return 1
    if message.get("status") != "ok" or not message.get("description"):
        print(f"WARN:describe:{path}: Brain en échec ({message.get('error', '?')})", file=sys.stderr)
        return 1
    r = set_description(email, path, message["description"], "ai", str(message.get("model") or ""))
    print(json.dumps({"path": path, "result": r}))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--apply"]:
        return apply_result(json.load(sys.stdin))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["scan"])
    ap.add_argument("email")
    ap.add_argument("--paths", nargs="*")
    ap.add_argument("--mode", choices=analysis.MODES, default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", "-v", action="count", default=0,
                    help="-v : chaque étape et chaque photo, horodatées (stderr) + la description (stdout) ; -vv : TOUT — commandes, "
                         "chargement du modèle, jetons, réponses brutes, nettoyage — y compris les sous-processus "
                         "vision_describe.py et describe_image.py, dans la même chronologie")
    ap.add_argument("--redo", action="store_true", help="refaire les descriptions IA d'un autre modèle que le modèle courant")
    args = ap.parse_args()
    try:
        res = scan(args.email, args.paths, args.mode, args.limit, args.force, redo=args.redo, verbose=args.verbose)
    except KeyboardInterrupt:
        return 130
    if res.get("status") == "busy":
        return 75                                   # EX_TEMPFAIL : un autre passage tourne déjà, ce n'est pas une erreur
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
