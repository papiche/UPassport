#!/usr/bin/env python3
"""
cloud_import.py — Import en masse d'un répertoire local vers le cloud chiffré
d'un MULTIPASS (mêmes chemin/chiffrement/déclenchement FaceID que PUT /dav/).

Cas d'usage visé : recopier une arborescence de photos déjà existante (import
initial d'archives, ou synchronisation périodique en cron d'un dossier partagé
NextCloud) vers le cloud chiffré — sans passer par le montage DAV ni par un
navigateur/extension NOSTR (ce script tourne directement sur la station,
authentification implicite par l'accès filesystem local à ~/.zen/game/nostr/).

Usage :
    cloud_import.py <email> <répertoire> [--dest-prefix /Photos/Import]
                     [--dry-run] [--quiet]

    # Exemple cron (toutes les 30 min, dossier NextCloud d'un membre) :
    */30 * * * * python3 ~/.zen/UPassport/cloud_import.py \\
        alice@example.com /home/alice/Nextcloud/Photos --dest-prefix /Photos/NextCloud

Idempotence : un cache local (~/.zen/tmp/cloud_import/<email>.json) retient
{mtime, size, dest_path} par fichier source déjà importé — un fichier inchangé
est ignoré SANS relire l'index chiffré ni recalculer de hash (essentiel pour
un cron répété sur ~30 ans d'archives). Si le cache est absent/perdu, on
retombe sur l'index chiffré lui-même (`sha256_plain`) : un fichier dont le
contenu correspond déjà à l'entrée existante à ce chemin n'est PAS ré-importé
(pas de nouvelle clé, pas de nouveau CID, pas de second job FaceID pour la
même photo).

Chaque image importée déclenche l'analyse FaceID exactement comme un PUT DAV
normal (cf. services/cloud_storage.py::ingest_plaintext), avec le MÊME seuil
de correspondance (0.82) que le flux interactif — voir cloud.html pour le
rapprochement manuel des visages qui ne matchent pas automatiquement (ex :
la même personne à des âges différents).
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
import hashlib
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

from services import cloud_storage  # noqa: E402

# Extensions traitées même quand mimetypes ne les reconnaît pas (HEIC/HEIF
# notamment, courant sur les exports iPhone — souvent absent des tables
# mimetypes système selon la distribution).
_EXTRA_IMAGE_EXT = {".heic": "image/heic", ".heif": "image/heif"}

_STATE_DIR = Path.home() / ".zen" / "tmp" / "cloud_import"


def _log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def _guess_image_mime(path: Path):
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("image/"):
        return mime
    return _EXTRA_IMAGE_EXT.get(path.suffix.lower())


def _state_path(email: str) -> Path:
    safe = hashlib.sha256(email.encode()).hexdigest()[:16]
    return _STATE_DIR / f"{safe}.json"


def _load_state(email: str) -> dict:
    p = _state_path(email)
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_state(email: str, state: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _state_path(email)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, p)


def _dest_path(dest_prefix: str, root: Path, src: Path) -> str:
    rel = src.relative_to(root).as_posix()
    return cloud_storage.normalize_path(f"{dest_prefix.rstrip('/')}/{rel}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importe un répertoire local dans le cloud chiffré d'un MULTIPASS.")
    parser.add_argument("email", help="MULTIPASS propriétaire (doit déjà exister sur cette station)")
    parser.add_argument("directory", help="Répertoire local à importer (parcouru récursivement)")
    parser.add_argument("--dest-prefix", default="/Photos/Import",
                         help="Préfixe des chemins dans le cloud chiffré (défaut: /Photos/Import)")
    parser.add_argument("--dry-run", action="store_true",
                         help="N'écrit rien — affiche juste ce qui serait importé/ignoré")
    parser.add_argument("--quiet", action="store_true", help="Seul le résumé final est affiché")
    args = parser.parse_args()

    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR:cloud_import:répertoire introuvable: {root}", file=sys.stderr)
        return 1

    hex_pubkey = cloud_storage.hex_for_email(args.email)
    if not hex_pubkey:
        print(f"ERROR:cloud_import:aucun MULTIPASS '{args.email}' sur cette station "
              f"(~/.zen/game/nostr/{args.email}/.secret.nostr introuvable)", file=sys.stderr)
        return 1

    state = _load_state(args.email)
    imported = skipped_cache = skipped_index = errors = 0

    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in sorted(filenames):
            src = Path(dirpath) / fname
            mime = _guess_image_mime(src)
            if not mime:
                continue

            try:
                st = src.stat()
            except OSError as exc:
                _log(f"⚠️  {src} illisible : {exc}", args.quiet)
                errors += 1
                continue

            key = str(src)
            cached = state.get(key)
            if cached and cached.get("mtime") == st.st_mtime and cached.get("size") == st.st_size:
                skipped_cache += 1
                continue

            dest = _dest_path(args.dest_prefix, root, src)

            try:
                plaintext = src.read_bytes()
            except OSError as exc:
                _log(f"⚠️  {src} illisible : {exc}", args.quiet)
                errors += 1
                continue

            # Repli si le cache local est absent/perdu : ne pas ré-importer
            # (nouvelle clé, nouveau CID, nouveau job FaceID) un fichier déjà
            # présent à ce chemin avec le même contenu.
            existing = cloud_storage.get_entry(cloud_storage.load_index(args.email), dest)
            if existing and existing.get("sha256_plain") == hashlib.sha256(plaintext).hexdigest():
                skipped_index += 1
                state[key] = {"mtime": st.st_mtime, "size": st.st_size, "dest_path": dest}
                continue

            if args.dry_run:
                _log(f"[dry-run] importerait {src} → {dest} ({mime}, {st.st_size} octets)", args.quiet)
                imported += 1
                continue

            try:
                cloud_storage.ingest_plaintext(args.email, dest, plaintext, content_type=mime)
                state[key] = {"mtime": st.st_mtime, "size": st.st_size, "dest_path": dest}
                imported += 1
                _log(f"✅ {src} → {dest}", args.quiet)
            except Exception as exc:
                _log(f"❌ {src} : {exc}", args.quiet)
                errors += 1

            # Sauvegarde incrémentale : un import de plusieurs dizaines de
            # milliers de fichiers interrompu (Ctrl-C, cron tué) ne reperd pas
            # tout le travail déjà fait.
            if not args.dry_run and (imported % 50 == 0):
                _save_state(args.email, state)

    if not args.dry_run:
        _save_state(args.email, state)

    print(f"Importés: {imported}  Ignorés (cache): {skipped_cache}  "
          f"Ignorés (déjà dans le cloud): {skipped_index}  Erreurs: {errors}")
    return 1 if errors and not imported else 0


if __name__ == "__main__":
    sys.exit(main())
