#!/usr/bin/env python3
"""
webdav_import.py — Import quotidien de photos depuis des sources WebDAV
externes vers le cloud chiffré `.ucloud` d'un MULTIPASS (déclenche l'analyse
FaceID comme un PUT /dav/ normal, cf. services/cloud_storage.py::ingest_plaintext).

Source des identifiants/dossiers : `.ucloud/webdav_sources.json`, géré par
l'utilisateur depuis UPlanet/earth/ucloud.html (section « Importer depuis un
autre cloud ») via routers/cloud.py::add_webdav_source/list_webdav_sources.
Ce script ne configure rien — il consomme ce qui a déjà été enregistré.

Usage :
    python3 webdav_import.py <email> [--source-id ID] [--max N] [--dry-run] [--quiet]

Sans `--source-id` : traite TOUTES les sources configurées pour cet email
(cas du cron quotidien, cf. RUNTIME/NOSTRCARD.refresh.sh). Avec `--source-id` :
une seule (cas du bouton « Importer maintenant », routers/cloud.py
::sync_webdav_source_now).

Protection GPU — NE PAS SUPPRIMER SANS Y RÉFLÉCHIR À DEUX FOIS, mais lire
d'abord ce qui suit : la sérialisation des jobs FaceID elle-même (un seul à
la fois, flock GPU 300s) est déjà assurée PLUS LOIN dans la chaîne, par
`IA/generators/faceid.sh` côté Brain (cf. Astroport.ONE/IA/bro/bro_dm_daemon.sh)
— une fois le modèle chargé en mémoire GPU (ComfyUI « chaud »), traiter un
visage est rapide (quelques secondes), pas comparable à une génération vidéo.
`IMPORT_DELAY_SECONDS` n'existe donc PAS pour « laisser respirer le GPU »
(la file DM NOSTR le fait déjà) mais pour ne pas rafaler le relais NOSTR de
plusieurs dizaines d'events `vision_analysis_job` dans la même seconde.

Le vrai facteur limitant est ailleurs : le DM `vision_analysis_job` a un TTL
de 30 min, traité strictement en série — si le volume CUMULÉ de jobs envoyés
par TOUS les comptes de la station (webdav_import + envois manuels + uDRIVE…)
dans cette fenêtre dépasse ce que le GPU peut avaler en 30 min, les derniers
jobs expirent silencieusement, sans retry. Le nombre de photos que CE script
peut raisonnablement envoyer par passage dépend donc du nombre de MULTIPASS
hébergés sur CETTE station (`_station_multipass_count()`) — plus il y en a,
plus chacun doit se contenter d'une part modeste d'un budget quotidien
partagé (`DAILY_STATION_BUDGET`, station entière, toutes sources confondues)
— pas d'une constante arbitraire par compte. `--max` reste disponible pour
forcer une valeur (ex. import manuel ponctuel, cf. `--source-id`).

Rangement par date — `{dest_prefix}/YYYY/MM/<nom>` (cf. `_dated_dest()`),
PAS l'arborescence distante préservée (abandonné le 2026-10-04). L'année/mois
vient de la date de PRISE DE VUE EXIF quand la photo en porte une (lue sur
l'image téléchargée, avant toute normalisation), sinon du `getlastmodified`
PROPFIND du serveur distant (RFC 1123), sinon de la date du jour — avec
l'option --dry-run, rien n'est téléchargé : seule la date du serveur est connue — meilleur signal accessible sans
télécharger+décoder l'image rien que pour ça.

Idempotence à TROIS niveaux (cf. `_import_source`) :
1. Cache `(mtime, size)` distants dans `.ucloud/webdav_import_state_<id>.json`
   — volontairement PAS sous `~/.zen/tmp/` (purgé CHAQUE NUIT par
   `20h12.process.sh`, qui viderait ce cache à chaque passage).
2. Filet de sécurité SHA256 GLOBAL (`_hash_index()`) : si le cache est
   absent/périmé, le contenu téléchargé est cherché n'importe où dans
   `index.json` (pas seulement au chemin de destination calculé) — un PUT
   manuel antérieur, ou une autre source WebDAV pointant sur le même
   contenu, est ainsi reconnu comme déjà catalogué. Sans ce filet, une
   photo déjà analysée serait réimportée sous un nouveau chemin ET
   redéclencherait une analyse FaceID GPU inutile.
3. Désambiguïsation de nom : `ingest_plaintext(..., unique=True)` choisit, SOUS
   le verrou de l'index, un suffixe `-2`, `-3`… si le chemin daté est occupé
   par un AUTRE contenu (homonymie — IMG_0001.jpg d'appareils différents),
   au lieu d'écraser l'entrée existante (CID/clé orphelins).

Le filet SHA256 (niveau 2) compare le hash des octets REÇUS (`sha256_orig`),
pas celui de l'image stockée (ré-encodée si EXIF Orientation ≠ 1).

Un seul import à la fois PAR SOURCE (flock `.ucloud/.webdav_import_<id>.lock`),
progression sauvée tous les 50 fichiers, fichiers > `WEBDAV_MAX_DOWNLOAD`
(200 Mo) ignorés, parcours distant du plus RÉCENT au plus ancien.
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
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from services import cloud_storage, webdav_client  # noqa: E402
import faceid_reprocess  # noqa: E402  — analyse rapide (faceid.sh --batch)

# Garde-fous (cf. docstring du module — le facteur limitant est le nombre de
# comptes qui partagent la station, pas une constante par compte). Les deux
# sont overridables par variable d'environnement pour un ajustement admin
# sans toucher au code (même convention que ASTRO_PARALLEL_REFRESH dans
# RUNTIME/NOSTRCARD.refresh.sh).
DAILY_STATION_BUDGET = int(os.environ.get("WEBDAV_IMPORT_DAILY_BUDGET", "300"))
MIN_IMPORT_PER_RUN = 5
# Mode RAPIDE (ComfyUI joignable en direct, cf. faceid_reprocess.analyze_quick) :
# plus de file DM à TTL 30 min à protéger, l'analyse tourne à ~1 photo/s avec
# un verrou GPU pris PAR photo (partage équitable entre comptes). Le budget
# reste réparti entre les MULTIPASS de la station, mais 20× plus large.
DAILY_STATION_QUICK_BUDGET = int(os.environ.get("WEBDAV_IMPORT_QUICK_BUDGET", "6000"))
MIN_QUICK_IMPORT_PER_RUN = 200
# Photos commitées par lot (UNE lecture+écriture d'index par lot) : l'index est
# réécrit en entier à chaque commit (≈ 0,8 s à 50 000 photos, bien plus sur un
# RPi). En mode relais NOSTR on garde de petits lots (un job FaceID par photo).
BATCH_QUICK = int(os.environ.get("WEBDAV_IMPORT_BATCH", "25"))
BATCH_RELAY = 5
BATCH_MAX_BYTES = 96 * 1024 * 1024      # borne mémoire d'un lot
IMPORT_DELAY_SECONDS = float(os.environ.get("WEBDAV_IMPORT_DELAY_SECONDS", "2"))

def _log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def _station_multipass_count() -> int:
    """Nombre de comptes MULTIPASS hébergés sur CETTE station — même source
    que RUNTIME/NOSTRCARD.refresh.sh (`ls ~/.zen/game/nostr/ | grep "@"`).
    Sert à répartir équitablement DAILY_STATION_BUDGET : le budget GPU
    FaceID est partagé par toute la station, pas réservé à cette
    fonctionnalité — plus il y a de comptes, plus la part de chacun se
    réduit, à l'inverse d'une limite fixe qui ignorerait la charge réelle."""
    nostr_dir = cloud_storage.GAME_NOSTR_PATH
    if not nostr_dir.is_dir():
        return 1
    return max(1, sum(1 for p in nostr_dir.iterdir() if p.is_dir() and "@" in p.name))


def _default_max_import(quick: bool = False) -> int:
    if quick:
        return max(MIN_QUICK_IMPORT_PER_RUN, DAILY_STATION_QUICK_BUDGET // _station_multipass_count())
    return max(MIN_IMPORT_PER_RUN, DAILY_STATION_BUDGET // _station_multipass_count())


def _state_path(email: str, source_id: str) -> Path:
    """Cache d'idempotence (mtime/size distants déjà vus) — DANS `.ucloud/`,
    PAS sous `~/.zen/tmp/` : ce dernier est purgé CHAQUE NUIT par
    `20h12.process.sh` (sauf swarm/coucou/$IPFSNODEID), ce qui viderait ce
    cache à chaque passage et forcerait un nouveau téléchargement/hash de
    tout le dossier distant chaque jour. `.ucloud/` est déjà le répertoire
    persistant 0700 de cet utilisateur (dav_token, webdav_sources.json…)."""
    return cloud_storage.ucloud_dir(email) / f"webdav_import_state_{source_id}.json"


def _load_state(email: str, source_id: str) -> dict:
    try:
        return json.loads(_state_path(email, source_id).read_text())
    except Exception:
        return {}


def _save_state(email: str, source_id: str, state: dict) -> None:
    cloud_storage.ensure_ucloud_dir(email)
    p = _state_path(email, source_id)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def _guess_image_mime(name: str):
    import mimetypes
    if cloud_storage.is_junk_name(name):
        return None            # ._IMG.jpg, Thumbs.db… : jamais des photos
    mime, _ = mimetypes.guess_type(name)
    if mime == "image/svg+xml":
        return None            # du code, pas une photo (et PIL/FaceID ne le lisent pas)
    if mime and mime.startswith("image/"):
        return mime
    ext = Path(name).suffix.lower()
    return {".heic": "image/heic", ".heif": "image/heif"}.get(ext)


def _plausible_taken(ts) -> bool:
    """Date EXIF crédible : entre 1990 et demain (les appareils sans horloge écrivent 0000:00:00 ou 2000/1970)."""
    try:
        return 631152000 <= int(ts) <= time.time() + 86400
    except (TypeError, ValueError):
        return False


def _dated_dest(dest_prefix: str, entry: dict, taken_at=None) -> str:
    """`{dest_prefix}/YYYY/MM/<nom>` plutôt que l'ancien
    `{dest_prefix}/<arborescence distante préservée>` (abandonné le
    2026-10-04 — demande explicite d'un rangement par date, cohérent avec ce
    que fait tout gestionnaire de photos). La date vient du `getlastmodified`
    PROPFIND du serveur distant (RFC 1123, déjà disponible sans coût
    supplémentaire) — pas une vraie date de prise de vue EXIF, mais le
    meilleur signal accessible sans télécharger+décoder l'image rien que
    pour ça ; repli sur la date du jour si injustement absent/illisible.

    `taken_at` (secondes, heure locale de l'appareil, tel que lu par
    `cloud_storage._inspect_image`) PRIME quand il est plausible : l'image est
    alors rangée à sa vraie date de prise de vue, comme ses voisines envoyées
    depuis le téléphone."""
    from email.utils import parsedate_to_datetime
    mtime = entry.get("mtime")
    dt = None
    if taken_at is not None and _plausible_taken(taken_at):
        t = time.localtime(int(taken_at))
        return f"{dest_prefix}/{t.tm_year:04d}/{t.tm_mon:02d}/{Path(entry['path']).name}"
    if mtime:
        try:
            dt = parsedate_to_datetime(mtime)
        except (TypeError, ValueError):
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    basename = Path(entry["path"]).name
    return f"{dest_prefix}/{dt.year:04d}/{dt.month:02d}/{basename}"


def _hash_index(index: dict) -> dict:
    """`{sha256_plain: chemin}` pour TOUTES les entrées de l'index — permet
    de détecter qu'un contenu est déjà catalogué n'importe où (pas
    uniquement au chemin de destination calculé). Sans cette recherche
    globale, un contenu déjà présent sous un AUTRE chemin (PUT manuel
    antérieur, une autre source WebDAV qui pointe sur le même dossier
    distant) serait réimporté sous un nouveau chemin ET redéclencherait une
    analyse FaceID GPU — ce que le filet SHA256 "au même chemin de
    destination" (`_unique_dest`) ne peut, par construction, pas voir."""
    out = {}
    for path, e in (index.get("entries") or {}).items():
        if not isinstance(e, dict):
            continue
        # `sha256_orig` = octets tels que REÇUS : le hash de ce qu'on télécharge.
        # `sha256_plain` est celui de l'image STOCKÉE, qui diffère si elle a été
        # ré-encodée (rotation EXIF) — sans `sha256_orig`, toute photo tournée
        # était réimportée en `-2`, `-3`… dès que le cache mtime/size sautait.
        for key in ("sha256_orig", "sha256_plain"):
            if e.get(key):
                out.setdefault(e[key], path)
    return out


async def _import_source(email: str, source: dict, max_import: int,
                          dry_run: bool, quiet: bool, quick: bool = False) -> dict:
    label = source.get("label") or source.get("url")
    remote_path = source.get("remote_path") or ""
    dest_prefix = (source.get("dest_prefix") or "/Photos/Import").rstrip("/")
    state = _load_state(email, source["id"])
    # Lus une fois ici (pas par fichier) : servent uniquement au filet de
    # sécurité SHA256 en lecture seule ci-dessous — ingest_plaintext() relit
    # et verrouille l'index lui-même pour l'écriture réelle, donc une légère
    # péremption de ces copies (autre PUT concurrent pendant la boucle) n'a
    # pour pire conséquence qu'un réimport rare, jamais une corruption.
    index_snapshot = cloud_storage.load_index(email)
    hash_index = _hash_index(index_snapshot)

    _log(f"[{label}] parcours du dossier distant (un PROPFIND par dossier : plusieurs minutes "
         f"possibles sur une grosse archive)…", quiet)
    _t0 = time.monotonic()
    try:
        entries = await webdav_client.walk_files(
            source["url"], source["username"], source["password"], remote_path,
            progress=lambda dirs, files: _log(
                f"[{label}] parcours : {dirs} dossiers lus, {files} fichiers vus ({int(time.monotonic() - _t0)} s)", quiet))
        _log(f"[{label}] parcours terminé : {len(entries)} fichiers en {int(time.monotonic() - _t0)} s — "
             f"téléchargement des nouveautés…", quiet)
    except webdav_client.WebdavError as exc:
        _log(f"ERROR:webdav_import:{label}: connexion impossible : {exc}", quiet)
        return {"at": int(time.time()), "imported": 0, "skipped": 0, "errors": 1,
                "error": str(exc)}

    imported = skipped = errors = 0
    new_paths: list = []          # chemins importés à CE passage (analyse rapide en lot)
    pending: list = []            # (entry, content_hash, base_dest, mime, plaintext) pas encore commités
    pending_hashes: set = set()
    pending_bytes = 0
    quota_hit = [False]           # espace du MULTIPASS atteint : on arrête, on ne supprime rien
    batch_max = BATCH_QUICK if quick else BATCH_RELAY

    async def flush() -> None:
        """Commit du lot en UNE écriture d'index (cf. cloud_storage.ingest_many)."""
        nonlocal imported, errors, pending, pending_hashes, pending_bytes
        if not pending:
            return
        batch, pending, pending_hashes, pending_bytes = pending, [], set(), 0
        try:
            # `unique=True` : l'homonymie (autre contenu au même chemin daté) est
            # résolue SOUS le verrou de l'index — plus d'écrasement possible
            # entre deux imports ou un import et un PUT simultanés.
            results = cloud_storage.ingest_many(email, [
                {"path": dest, "plaintext": data, "content_type": mime,
                 "trigger_faceid": not quick, "unique": True}
                for (_e, _h, dest, mime, data) in batch])
        except cloud_storage.storage_quota.QuotaExceeded as exc:
            errors += len(batch)
            quota_hit[0] = True
            _log(f"ERROR:webdav_import:{label}: espace plein ({exc}) — import arrêté, rien n'est supprimé", quiet)
            return
        except Exception as exc:
            errors += len(batch)
            _log(f"ERROR:webdav_import:{label}: lot de {len(batch)} en échec : {exc}", quiet)
            return
        for (e, h, _dest, _m, _d), res in zip(batch, results):
            dest = res["path"]
            index_snapshot.setdefault("entries", {})[dest] = {"sha256_orig": h}
            hash_index[h] = dest
            state[e["path"]] = {"mtime": e.get("mtime"), "size": e.get("size"), "dest_path": dest}
            imported += 1
            new_paths.append(cloud_storage.normalize_path(dest))
            _log(f"[{label}] importé {e['path']} → {dest}", quiet)
        # Progression sauvée à chaque lot : un crash / OOM en cours de passage
        # (déjà vu sur un import de masse) perdait TOUTE la progression.
        _save_state(email, source["id"], state)
        # Espacement délibéré en mode relais : chaque photo déclenche un job
        # FaceID distant (cf. docstring du module) — on les laisse trickle.
        if not quick:
            await asyncio.sleep(IMPORT_DELAY_SECONDS * len(batch))

    for entry in entries:
        if imported + len(pending) >= max_import:
            _log(f"[{label}] plafond atteint ({max_import}/passage) — "
                 f"le reste sera importé au prochain passage.", quiet)
            break

        if quota_hit[0]:
            break
        mime = _guess_image_mime(entry["name"])
        if not mime:
            continue   # pas une image — hors scope de l'import FaceCloud (documents, etc.)

        cache_key = entry["path"]
        cached = state.get(cache_key)
        if cached and cached.get("mtime") == entry.get("mtime") and cached.get("size") == entry.get("size"):
            skipped += 1
            continue

        if entry.get("size") == 0:
            # Fichier vide à la SOURCE (copie interrompue chez le propriétaire) : le cataloguer
            # donnait une « photo » de 0 octet, que le modèle ne peut pas lire et qui bloquait la file.
            skipped += 1
            if not dry_run:
                state[cache_key] = {"mtime": entry.get("mtime"), "size": 0, "empty": True}
            continue

        base_dest = cloud_storage.normalize_path(_dated_dest(dest_prefix, entry))
        if dry_run:
            _log(f"[{label}] (dry-run) importerait {entry['path']} → {base_dest} (date du serveur : l'EXIF n'est lue qu'au téléchargement)", quiet)
            imported += 1
            continue

        size = entry.get("size") or 0
        if size > webdav_client.MAX_DOWNLOAD_BYTES:
            errors += 1
            _log(f"ERROR:webdav_import:{label}:{entry['path']}: trop volumineux "
                 f"({size} octets > {webdav_client.MAX_DOWNLOAD_BYTES}), ignoré", quiet)
            state[cache_key] = {"mtime": entry.get("mtime"), "size": size, "too_big": True}
            continue

        try:
            plaintext = await webdav_client.download_file(
                source["url"], source["username"], source["password"], entry["path"])
        except Exception as exc:
            errors += 1
            _log(f"ERROR:webdav_import:{label}:{entry['path']}: {exc}", quiet)
            continue

        # Date de prise de vue EXIF (lue sur l'ORIGINAL téléchargé) : prime sur la date du serveur pour le rangement.
        taken = cloud_storage._inspect_image(plaintext).get("taken_at")
        if _plausible_taken(taken):
            exif_dest = cloud_storage.normalize_path(_dated_dest(dest_prefix, entry, taken))
            if exif_dest != base_dest:
                _log(f"[{label}] {entry['name']} : rangée à sa date de prise de vue "
                     f"({time.strftime('%Y-%m-%d', time.localtime(int(taken)))}) plutôt qu'à la date du serveur", quiet)
            base_dest = exif_dest

        content_hash = hashlib.sha256(plaintext).hexdigest()

        # Filet de sécurité SHA256, GLOBAL D'ABORD : ce contenu existe-t-il
        # DÉJÀ n'importe où dans l'index (un PUT manuel antérieur, une autre
        # source WebDAV pointant sur le même dossier distant) ? Si oui, c'est
        # déjà catalogué et déjà analysé — inutile de le réimporter sous un
        # nouveau chemin daté, et surtout pas question de redéclencher une
        # analyse FaceID GPU dessus.
        existing_path = hash_index.get(content_hash)
        if existing_path or content_hash in pending_hashes:
            state[cache_key] = {"mtime": entry.get("mtime"), "size": entry.get("size"),
                                 "dest_path": existing_path or base_dest}
            skipped += 1
            continue

        pending.append((entry, content_hash, base_dest, mime, plaintext))
        pending_hashes.add(content_hash)
        pending_bytes += len(plaintext)
        if len(pending) >= batch_max or pending_bytes >= BATCH_MAX_BYTES:
            await flush()

    await flush()

    if not dry_run:
        _save_state(email, source["id"], state)

    # Niveau de synchro calculé GRATUITEMENT ici — `entries` est déjà le
    # parcours complet du dossier distant qu'on vient de faire pour
    # l'import, pas la peine d'un second PROPFIND à la demande depuis l'UI
    # (plus de bouton "Vérifier la synchro" côté ucloud.html, trop lent :
    # un parcours récursif complet à chaque clic). `local_count` relit
    # index_snapshot, déjà à jour des imports de CE passage.
    remote_count = sum(1 for e in entries if _guess_image_mime(e["name"]))
    local_count = sum(
        1 for path, e in (index_snapshot.get("entries") or {}).items()
        if isinstance(e, dict) and path.startswith(dest_prefix + "/")
    )

    return {
        "at": int(time.time()), "imported": imported, "skipped": skipped, "errors": errors,
        "remote_count": remote_count, "remote_capped": len(entries) >= webdav_client.WALK_MAX_FILES,
        "local_count": local_count,
        "paths": new_paths,
        **({"error": "Espace épuisé : libérez de la place ou devenez parrain de l'infrastructure ẐEN pour continuer l'import."}
           if quota_hit[0] else {}),
    }


async def _analyze_imported(email: str, paths: list, quiet: bool) -> dict:
    """Analyse FaceID rapide et silencieuse des photos tout juste importées
    (faceid_reprocess.analyze_quick). Ce que le lot n'a pas pu traiter
    (ComfyUI tombé en route) repart par le relais NOSTR, comme avant."""
    remaining, counts = await asyncio.to_thread(faceid_reprocess.analyze_quick, email, paths)
    _log(f"webdav_import: analyse rapide {counts} — {len(remaining)} à confier au relais NOSTR", quiet)
    for path in remaining:
        entry = cloud_storage.db_entry(email, path) or {}
        key_hex = cloud_storage.get_key_hex(email, entry.get("cid", "")) if entry.get("cid") else None
        if key_hex:
            cloud_storage._trigger_faceid_analysis(email, path, entry["cid"], key_hex)
            await asyncio.sleep(IMPORT_DELAY_SECONDS)
    counts["relayed"] = len(remaining)
    return counts


async def _main_async(args) -> int:
    hex_pubkey = cloud_storage.hex_for_email(args.email)
    if not hex_pubkey:
        print(f"ERROR:webdav_import:aucun MULTIPASS '{args.email}' sur cette station",
              file=sys.stderr)
        return 1

    sources = cloud_storage.load_webdav_sources(args.email)
    if args.source_id:
        sources = [s for s in sources if s.get("id") == args.source_id]
        if not sources:
            print(f"ERROR:webdav_import:source '{args.source_id}' introuvable pour "
                  f"{args.email}", file=sys.stderr)
            return 1
    if not sources:
        _log(f"webdav_import: aucune source configurée pour {args.email}", args.quiet)
        return 0

    # Mode rapide si ComfyUI est joignable en direct depuis cette station
    # (local, tunnel SSH ou P2P IPFS) : l'analyse se fait en un lot après
    # chaque source, sans relais NOSTR. Sinon, comportement historique.
    quick = (not args.dry_run and not args.no_quick
             and faceid_reprocess.comfyui_reachable())
    _log(f"webdav_import: analyse {'RAPIDE (ComfyUI direct)' if quick else 'par relais NOSTR'}", args.quiet)

    if args.max is not None:
        per_source_max = args.max   # override explicite : s'applique tel quel à chaque source
    else:
        # Budget de CE compte (part équitable du budget station, cf.
        # _default_max_import) réparti entre ses propres sources — un compte
        # avec 3 sources ne doit pas tripler sa part au prétexte d'en avoir
        # configuré plusieurs.
        account_budget = _default_max_import(quick)
        per_source_max = max(1, account_budget // len(sources))
        _log(f"webdav_import: budget auto {account_budget} photo(s)/jour pour "
             f"{args.email} ({_station_multipass_count()} MULTIPASS sur la "
             f"station) → {per_source_max}/source", args.quiet)

    total_imported = total_skipped = total_errors = 0
    for source in sources:
        # Une seule instance par source (cron + « Importer maintenant » répété) :
        # deux passages parallèles téléchargeaient et chiffraient les mêmes fichiers.
        lock_fd = cloud_storage.try_lock_nowait(
            cloud_storage.webdav_import_lock_path(args.email, source["id"]))
        if lock_fd is None:
            _log(f"webdav_import: source '{source.get('label')}' déjà en cours d'import — ignorée", args.quiet)
            continue
        try:
            summary = await _import_source(args.email, source, per_source_max, args.dry_run,
                                           args.quiet, quick)
        finally:
            os.close(lock_fd)
        new_paths = summary.pop("paths", [])
        if quick and new_paths:
            summary["analysis"] = await _analyze_imported(args.email, new_paths, args.quiet)
        total_imported += summary["imported"]
        total_skipped += summary["skipped"]
        total_errors += summary["errors"]
        if not args.dry_run:
            cloud_storage.record_webdav_sync(args.email, source["id"], summary)

    print(f"webdav_import {args.email} : {total_imported} importée(s), "
          f"{total_skipped} ignorée(s), {total_errors} erreur(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importe des photos depuis les sources WebDAV externes configurées.")
    parser.add_argument("email", help="MULTIPASS propriétaire")
    parser.add_argument("--source-id", help="Une seule source (sinon : toutes)")
    parser.add_argument("--max", type=int, default=None,
                         help="Photos max importées par source à ce passage (défaut : calculé "
                              "automatiquement — DAILY_STATION_BUDGET / nb de MULTIPASS de la "
                              "station / nb de sources de ce compte, cf. _default_max_import())")
    parser.add_argument("--dry-run", action="store_true",
                         help="N'écrit rien — affiche juste ce qui serait importé/ignoré")
    parser.add_argument("--quiet", action="store_true", help="Seul le résumé final est affiché")
    parser.add_argument("--no-quick", action="store_true",
                         help="Forcer l'analyse par relais NOSTR (une photo = un DM vers le Brain)")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
