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
vient du `getlastmodified` PROPFIND du serveur distant (RFC 1123), pas d'une
vraie date de prise de vue EXIF — meilleur signal accessible sans
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
3. Désambiguïsation de nom (`_unique_dest()`) : si le chemin daté calculé
   est déjà occupé par un AUTRE contenu (homonymie — plus probable
   maintenant qu'un seul mois de photos partage le même dossier qu'avant),
   un suffixe `-2`, `-3`… est ajouté plutôt que d'écraser silencieusement
   l'entrée existante (son CID/clé deviendraient orphelins, invisibles).
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

# Garde-fous (cf. docstring du module — le facteur limitant est le nombre de
# comptes qui partagent la station, pas une constante par compte). Les deux
# sont overridables par variable d'environnement pour un ajustement admin
# sans toucher au code (même convention que ASTRO_PARALLEL_REFRESH dans
# RUNTIME/NOSTRCARD.refresh.sh).
DAILY_STATION_BUDGET = int(os.environ.get("WEBDAV_IMPORT_DAILY_BUDGET", "300"))
MIN_IMPORT_PER_RUN = 5
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


def _default_max_import() -> int:
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
    mime, _ = mimetypes.guess_type(name)
    if mime and mime.startswith("image/"):
        return mime
    ext = Path(name).suffix.lower()
    return {".heic": "image/heic", ".heif": "image/heif"}.get(ext)


def _dated_dest(dest_prefix: str, entry: dict) -> str:
    """`{dest_prefix}/YYYY/MM/<nom>` plutôt que l'ancien
    `{dest_prefix}/<arborescence distante préservée>` (abandonné le
    2026-10-04 — demande explicite d'un rangement par date, cohérent avec ce
    que fait tout gestionnaire de photos). La date vient du `getlastmodified`
    PROPFIND du serveur distant (RFC 1123, déjà disponible sans coût
    supplémentaire) — pas une vraie date de prise de vue EXIF, mais le
    meilleur signal accessible sans télécharger+décoder l'image rien que
    pour ça ; repli sur la date du jour si injustement absent/illisible."""
    from email.utils import parsedate_to_datetime
    mtime = entry.get("mtime")
    dt = None
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
        if isinstance(e, dict) and e.get("sha256_plain"):
            out.setdefault(e["sha256_plain"], path)
    return out


def _unique_dest(index: dict, dest: str, content_hash: str) -> str:
    """`dest` si libre OU déjà occupé par ce MÊME contenu (sha256) ; sinon
    `<nom>-2.<ext>`, `-3`, … jusqu'à trouver un chemin libre. Le classement
    par date (cf. `_dated_dest`) réduit l'espace de noms à un seul mois —
    deux photos distinctes partageant un nom généré en série (IMG_0001.jpg…)
    y sont bien plus probables que sur un `/Photos/` plat ; sans ce
    garde-fou, `ingest_plaintext()` écraserait silencieusement la première
    entrée (son CID/clé deviennent orphelins, invisibles, non récupérables)."""
    existing = cloud_storage.get_entry(index, dest)
    if existing is None or existing.get("sha256_plain") == content_hash:
        return dest
    stem, dot, ext = dest.rpartition(".")
    stem = stem if dot else dest
    ext = f".{ext}" if dot else ""
    n = 2
    while True:
        candidate = f"{stem}-{n}{ext}"
        existing = cloud_storage.get_entry(index, candidate)
        if existing is None or existing.get("sha256_plain") == content_hash:
            return candidate
        n += 1


async def _import_source(email: str, source: dict, max_import: int,
                          dry_run: bool, quiet: bool) -> dict:
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

    try:
        entries = await webdav_client.walk_files(
            source["url"], source["username"], source["password"], remote_path)
    except webdav_client.WebdavError as exc:
        _log(f"ERROR:webdav_import:{label}: connexion impossible : {exc}", quiet)
        return {"at": int(time.time()), "imported": 0, "skipped": 0, "errors": 1,
                "error": str(exc)}

    imported = skipped = errors = 0
    for entry in entries:
        if imported >= max_import:
            _log(f"[{label}] plafond atteint ({max_import}/passage) — "
                 f"le reste sera importé au prochain passage.", quiet)
            break

        mime = _guess_image_mime(entry["name"])
        if not mime:
            continue   # pas une image — hors scope de l'import FaceCloud (documents, etc.)

        cache_key = entry["path"]
        cached = state.get(cache_key)
        if cached and cached.get("mtime") == entry.get("mtime") and cached.get("size") == entry.get("size"):
            skipped += 1
            continue

        base_dest = cloud_storage.normalize_path(_dated_dest(dest_prefix, entry))
        if dry_run:
            _log(f"[{label}] (dry-run) importerait {entry['path']} → {base_dest}", quiet)
            imported += 1
            continue

        try:
            plaintext = await webdav_client.download_file(
                source["url"], source["username"], source["password"], entry["path"])
        except Exception as exc:
            errors += 1
            _log(f"ERROR:webdav_import:{label}:{entry['path']}: {exc}", quiet)
            continue

        content_hash = hashlib.sha256(plaintext).hexdigest()

        # Filet de sécurité SHA256, GLOBAL D'ABORD : ce contenu existe-t-il
        # DÉJÀ n'importe où dans l'index (un PUT manuel antérieur, une autre
        # source WebDAV pointant sur le même dossier distant) ? Si oui, c'est
        # déjà catalogué et déjà analysé — inutile de le réimporter sous un
        # nouveau chemin daté, et surtout pas question de redéclencher une
        # analyse FaceID GPU dessus.
        existing_path = hash_index.get(content_hash)
        if existing_path:
            state[cache_key] = {"mtime": entry.get("mtime"), "size": entry.get("size"),
                                 "dest_path": existing_path}
            skipped += 1
            continue

        # Sinon : _unique_dest() ne fait plus que la désambiguïsation de nom
        # (homonymie au chemin daté calculé — un AUTRE contenu y est déjà,
        # plus probable désormais qu'un seul mois de photos partage le même
        # dossier). index_snapshot/hash_index sont mis à jour au fil de la
        # boucle (ci-dessous) pour voir aussi les collisions survenues
        # PENDANT ce même passage, pas seulement avant.
        dest = _unique_dest(index_snapshot, base_dest, content_hash)

        try:
            cloud_storage.ingest_plaintext(email, dest, plaintext, content_type=mime)
        except Exception as exc:
            errors += 1
            _log(f"ERROR:webdav_import:{label}:{entry['path']}: {exc}", quiet)
            continue

        index_snapshot.setdefault("entries", {})[dest] = {"sha256_plain": content_hash}
        hash_index[content_hash] = dest
        state[cache_key] = {"mtime": entry.get("mtime"), "size": entry.get("size"), "dest_path": dest}
        imported += 1
        _log(f"[{label}] importé {entry['path']} → {dest}", quiet)

        # Espacement délibéré : chaque ingest_plaintext() déclenche un job
        # FaceID distant (cf. docstring du module) — on les laisse trickle
        # plutôt que de les tirer tous d'un coup.
        if imported < max_import:
            await asyncio.sleep(IMPORT_DELAY_SECONDS)

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
        "remote_count": remote_count, "remote_capped": len(entries) >= 2000,
        "local_count": local_count,
    }


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

    if args.max is not None:
        per_source_max = args.max   # override explicite : s'applique tel quel à chaque source
    else:
        # Budget de CE compte (part équitable du budget station, cf.
        # _default_max_import) réparti entre ses propres sources — un compte
        # avec 3 sources ne doit pas tripler sa part au prétexte d'en avoir
        # configuré plusieurs.
        account_budget = _default_max_import()
        per_source_max = max(1, account_budget // len(sources))
        _log(f"webdav_import: budget auto {account_budget} photo(s)/jour pour "
             f"{args.email} ({_station_multipass_count()} MULTIPASS sur la "
             f"station) → {per_source_max}/source", args.quiet)

    total_imported = total_skipped = total_errors = 0
    for source in sources:
        summary = await _import_source(args.email, source, per_source_max, args.dry_run, args.quiet)
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
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
