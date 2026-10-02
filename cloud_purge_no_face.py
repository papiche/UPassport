#!/usr/bin/env python3
"""
cloud_purge_no_face.py — Purge les entrées `.ucloud` sans visage détecté.

Depuis le 2026-10-02, `.ucloud` est réservé aux images contenant un visage
(cf. UPlanet/earth/CLAUDE.md) : le pipeline FaceID (satellite_face_matcher.py)
supprime désormais lui-même toute NOUVELLE photo où aucun visage n'est
détecté. Ce script traite l'ARRIÉRÉ — les entrées déjà cataloguées par
l'ancienne fonctionnalité « Objets & lieux détectés » (retirée le même jour),
reconnaissables à leur champ `scene` dans index.json (écrit uniquement quand
`satellite_face_matcher.py` ne trouvait aucun visage).

Usage :
    python3 cloud_purge_no_face.py [--email EMAIL] [--clean] [--quiet]

Sans --clean : dry-run, liste les candidats (email, chemin, scène identifiée)
sans rien supprimer. Avec --clean : supprime réellement — entrée d'index +
clé de keyring (même discipline qu'un DELETE DAV classique, cf.
services/cloud_storage.py::remove_subtree/prune_keyring) ; le blob IPFS n'est
pas dépin, seule la clé de déchiffrement disparaît.

Portée VOLONTAIREMENT CONSERVATRICE : seules les entrées portant un champ
`scene` (confirmation explicite « zéro visage » par l'ancien pipeline Ollama
vision) sont considérées pour suppression. Une entrée SANS `scene` et SANS
`tags` est ambiguë — visage détecté mais jamais partagé (tags n'est alimenté
que par un partage réciproque), ou photo jamais analysée — et n'est JAMAIS
supprimée automatiquement : un faux positif supprimerait une vraie photo de
visage.

`--list-ambiguous` lève partiellement cette ambiguïté SANS rien supprimer :
en croisant chaque entrée sans `scene`/`tags` avec le catalogue Qdrant
`faces_{hex}` du propriétaire (champ `source_path`, présent sur les points
catalogués depuis 2026-09-20), on distingue « visage catalogué mais jamais
partagé » (confirmé, ignoré) de « aucun visage connu pour cette photo »
(toujours ambigu, listé). Les entrées encore ambiguës après ce croisement
restent dans .ucloud ; les trancher avec certitude demanderait de
ré-exécuter l'analyse faciale dessus, hors de portée de ce script.
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
from pathlib import Path
from typing import Iterator, List, Set, Tuple

import httpx

from services import cloud_storage  # noqa: E402

_QDRANT_URL = "http://localhost:6333"


def _qdrant_headers() -> dict:
    """Même source que routers/mailjet.py::_qdrant_headers() — dupliqué ici
    plutôt qu'importé : ce script tourne hors FastAPI, importer le router
    chargerait toute l'app pour une seule fonction utilitaire."""
    try:
        for line in (Path.home() / ".zen" / "ai-company" / ".env").read_text().splitlines():
            if line.startswith("QDRANT_API_KEY="):
                return {"api-key": line.split("=", 1)[1].strip()}
    except Exception:
        pass
    return {}


def _known_face_paths(owner_hex: str) -> Set[str]:
    """Chemins `.ucloud` déjà couverts par au moins un visage catalogué dans
    Qdrant faces_{owner_hex} (champ `source_path`, absent sur les points
    pré-2026-09-20). Qdrant injoignable/collection absente → set vide, pas
    une erreur : ce script ne fait que lister, jamais supprimer sur la foi de
    ce croisement."""
    paths: Set[str] = set()
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{_QDRANT_URL}/collections/faces_{owner_hex}/points/scroll",
                headers=_qdrant_headers(),
                json={"limit": 2000, "with_payload": True, "with_vector": False},
            )
        if resp.status_code == 200:
            for point in resp.json().get("result", {}).get("points", []):
                source_path = (point.get("payload") or {}).get("source_path")
                if source_path:
                    paths.add(source_path)
    except Exception:
        pass
    return paths


def find_ambiguous(email: str) -> List[str]:
    """Images ni confirmées « avec visage » (ni `tags`, ni point Qdrant
    référençant leur chemin) ni confirmées « sans visage » (`scene`) —
    jamais supprimées ici, juste remontées pour décision humaine."""
    idx = cloud_storage.load_index(email)
    owner_hex = cloud_storage.hex_for_email(email)
    known_paths = _known_face_paths(owner_hex) if owner_hex else set()
    out = []
    for path, entry in (idx.get("entries") or {}).items():
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        if not (entry.get("mime") or "").startswith("image/"):
            continue
        if isinstance(entry.get("scene"), dict):
            continue   # confirmé « sans visage » (candidat de find_candidates)
        if entry.get("tags"):
            continue   # visage déjà partagé => visage confirmé
        if path in known_paths:
            continue   # visage catalogué (même non partagé) => confirmé
        out.append(path)
    return out


def _iter_emails() -> Iterator[str]:
    nostr_dir = cloud_storage.GAME_NOSTR_PATH
    if not nostr_dir.is_dir():
        return
    for child in sorted(nostr_dir.iterdir()):
        if child.is_dir() and (child / ".ucloud" / "index.json").exists():
            yield child.name


def find_candidates(email: str) -> List[Tuple[str, dict]]:
    """Entrées de index.json portant un champ `scene` (zéro visage confirmé
    par l'ancien pipeline) — voir docstring du module pour la portée exacte."""
    idx = cloud_storage.load_index(email)
    return [
        (path, entry)
        for path, entry in (idx.get("entries") or {}).items()
        if isinstance(entry, dict) and isinstance(entry.get("scene"), dict)
    ]


def purge_user(email: str, clean: bool, quiet: bool) -> int:
    candidates = find_candidates(email)
    if not candidates:
        return 0

    if not quiet:
        tag = "CLEAN" if clean else "DRY-RUN"
        for path, entry in candidates:
            scene = entry.get("scene") or {}
            print(f"[{tag}] {email} {path} — scene="
                  f"{scene.get('type')}/{scene.get('category')} "
                  f"({scene.get('name') or '?'})")

    if clean:
        with cloud_storage.index_lock(email):
            idx = cloud_storage.load_index(email)
            keyring = cloud_storage.load_keyring(email)
            for path, _entry in candidates:
                cloud_storage.remove_subtree(idx, path)
            keyring = cloud_storage.prune_keyring(idx, keyring)
            cloud_storage.save_keyring(email, keyring)
            cloud_storage.save_index(email, idx)

    return len(candidates)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Purge les entrées .ucloud sans visage (champ `scene`).")
    parser.add_argument("--email", help="Un seul MULTIPASS (sinon : tous ceux hébergés ici)")
    parser.add_argument("--clean", action="store_true",
                         help="Supprime réellement (sinon dry-run, par défaut)")
    parser.add_argument("--quiet", action="store_true",
                         help="N'affiche que le total par email, pas chaque entrée")
    parser.add_argument("--list-ambiguous", action="store_true",
                         help="Liste en plus les entrées ni confirmées avec visage "
                              "ni sans (voir docstring) — jamais supprimées")
    args = parser.parse_args()

    emails = [args.email] if args.email else list(_iter_emails())
    if not emails:
        print("Aucun MULTIPASS avec .ucloud trouvé sur cette station.")
        return 0

    total = 0
    for email in emails:
        n = purge_user(email, clean=args.clean, quiet=args.quiet)
        if n:
            total += n
            print(f"{'Supprimé' if args.clean else 'À supprimer'} : {n} entrée(s) pour {email}")

    if args.list_ambiguous:
        ambiguous_total = 0
        for email in emails:
            paths = find_ambiguous(email)
            ambiguous_total += len(paths)
            if not paths:
                continue
            if not args.quiet:
                for path in paths:
                    print(f"[AMBIGU] {email} {path} — ni visage ni scène confirmés")
            print(f"Ambigu(ës) : {len(paths)} entrée(s) pour {email}")
        print(f"\nTotal ambigu : {ambiguous_total} entrée(s) — jamais supprimées automatiquement.")

    action = "supprimée(s)" if args.clean else "trouvée(s) (dry-run — relancez avec --clean)"
    print(f"\nTotal : {total} entrée(s) {action}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
