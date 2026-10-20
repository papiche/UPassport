#!/usr/bin/env python3
"""
ucloud_backup.py — Sauvegarde et restauration du cloud FaceCloud d'un MULTIPASS.

UNE seule implémentation pour trois usages :
  * sauvegarde quotidienne hors station (RUNTIME/NOSTRCARD.refresh.sh) ;
  * export dans l'archive de nostr_DESTROY_TW.sh (mêmes noms de fichiers
    qu'avant : ucloud_index.json, ucloud_keyring.json, qdrant_faces.json…) ;
  * restauration (FaceCloud, nostr_RESTORE_TW.sh).

Contenu du « paquet » (`export_bundle`) — tout ce qui permet de RETROUVER et
DÉCHIFFRER le cloud ; les fichiers eux-mêmes restent sur IPFS, déjà chiffrés :
    ucloud_index.json       chemins ↔ CID (photos ET fiches de contact .vcf)
    ucloud_keyring.json     clé AES-256 de chaque CID
    webdav_sources.json     sources photo externes (identifiants inclus)
    carddav_sources.json    carnets externes (identifiants inclus)
    webdav_import_state_*.json  ce qui a déjà été importé (évite tout réimport)
    face_invites/*.json     invitations « Envoyer ses photos » en attente
    qdrant_faces.json       catalogue de visages (vecteurs + noms + liens)
    qdrant_lifeos.json      mémoire personnelle de BRO (collection memory_<hex16> :
                            souvenirs ET souvenirs oubliés — texte effacé, empreinte
                            gardée pour que BRO ne les réapprenne pas —, vecteurs compris)
    settings/               face_ref_sharing/face_ref_given/blocked_senders (.ucloud) et
                            .mailjet. TRANSITOIRE : ces options ont vocation à vivre dans
                            NOSTR (kind 31903, un d-tag par usage) et à suivre le MULTIPASS
                            seules. Jamais .storage_quota : décision du Capitaine de la station
    uDRIVE_manifest.json    manifeste du uDRIVE (final_cid : de quoi le ré-épingler)
    account_state.tgz       ÉTAT du compte : tous ses fichiers cachés (marqueurs .account_created, .lastpayment,
                            .n2_genesis_minted…, cookies, préférences) + identity/ (biographie de BRO), SANS les
                            secrets re-dérivables du QR (voir ACCOUNT_STATE_EXCLUDE). Déménagement : + `.pass`.
    nostr_events.jsonl      (déménagement seulement) événements écrits par le compte + messages privés reçus
    zencard_state.tgz       (déménagement seulement) la ZenCard : tout `players/<email>` (TiddlyWiki, secret.june,
                            parts sociales U.SOCIETY…). Contient des SECRETS (clé de la ZenCard, SALT/PEPPER) : voyage
                            uniquement dans le paquet chiffré pour la station destinataire, jamais en sauvegarde quotidienne.
Non inclus, reconstruits seuls : résumé et clé du carnet (contacts.enc), cache
de vignettes, jeton WebDAV (à recréer : il donne accès au disque).

Sauvegarde hors station (`snapshot`) : le paquet est compressé, chiffré
AES-256-GCM avec une clé NEUVE, ajouté et épinglé sur IPFS ; UN SEUL event
NOSTR kind 30078 `d=facecloud:backup`, contenu chiffré NIP-44 pour soi-même
({cid, key, sha, at, counts}), est (re)publié — remplacé à chaque nouvelle
version, l'ancienne archive est désépinglée. Rien n'est republié si le paquet
n'a pas changé. Choix délibéré (2026-10-12) : pas un event par contact ni par
fichier — un seul pointeur, peu de métadonnées visibles, un seul event à
remplacer ou supprimer.

Usage :
    ucloud_backup.py snapshot <email>
    ucloud_backup.py export   <email> <dossier>
    ucloud_backup.py restore  <email> (--from-relay | --bundle <dossier>) [--qdrant]
    ucloud_backup.py pin      <email> [--gateway URL]   épingle tous les CID de l'index (+ le uDRIVE) ; avec --gateway, les
                                                        rapatrie par la PASSERELLE HTTP de l'ancienne station (changement
                                                        de constellation : le swarm IPFS d'ici ne joint pas l'autre)
    ucloud_backup.py zencard-export <email> <dossier>    CONTENU de la ZenCard (TiddlyWiki + chaîne de versions, flux, historiques de
                                                         paiements) dans <dossier>/zencard_content.tgz — AUCUN secret ; archive de départ
    ucloud_backup.py zencard-import <email> <tgz> [--zone Z]   le restaure dans la ZenCard (recréée avec ses identifiants d'origine) ;
                                                         avec --zone, ses liens vers la passerelle publique d'origine sont
                                                         réécrits vers la zone DNS de CETTE station (paramètre coopératif OVH_ZONE)
    ucloud_backup.py zencard-links  <email> <zone>       réécrit seulement ces liens (idempotent) : tout
                                                         <sous-domaine.>copylaradio.com → <sous-domaine.><zone>
    ucloud_backup.py status   <email>
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
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from services import cloud_storage, gateway_fetch, ucloud_db  # noqa: E402

D_TAG = "facecloud:backup"
RELAYS = ["ws://127.0.0.1:7777", "wss://relay.copylaradio.com"]
QDRANT_URL = "http://localhost:6333"

# Réglages du compte portés par le paquet : (dossier, nom). « ucloud » = .ucloud/,
# « user » = dossier du MULTIPASS. Jamais dav_token (il donne accès au disque et
# l'URL DAV change avec la station : réenrôlement par FaceCloud), jamais
# .storage_quota (le quota est décidé par le Capitaine de CHAQUE station : dans un
# paquet signé par la personne, elle pourrait se l'attribuer).
SETTINGS_FILES = [("ucloud", "face_ref_sharing.json"), ("ucloud", "face_ref_given.json"),
                  ("ucloud", "blocked_senders.json"), ("user", ".mailjet")]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _user_dir(email: str) -> Path:
    return cloud_storage.GAME_NOSTR_PATH / email


def _qdrant_headers() -> Dict[str, str]:
    try:
        for line in (Path.home() / ".zen" / "ai-company" / ".env").read_text().splitlines():
            if line.startswith("QDRANT_API_KEY="):
                return {"api-key": line.split("=", 1)[1].strip().strip('"')}
    except OSError:
        pass
    return {}


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


# ═════════════════════════════════════════════════════════════════════════════
# Paquet
# ═════════════════════════════════════════════════════════════════════════════
# État du compte : tout fichier ou dossier CACHÉ du dossier du MULTIPASS (marqueurs, cookies, préférences) plus
# `identity/`, SAUF — jamais emportés, car ils se re-dérivent du QR + PASS (check_ssss.sh / derive_account_keys) ou
# sont propres à la station : les secrets, le zine/la carte (nsec, salt, pepper, QR des parts SSSS), les parts SSSS,
# le .keyformula (recalculé), l'état de déménagement, .refresh_time (réattribué), .nip42_auth_*, .ucloud (son propre
# paquet), les caches. L'archive ne doit JAMAIS donner à qui possède le nsec de quoi dériver le wallet.
ACCOUNT_STATE_EXCLUDE = (".secret*", ".multipass.json", ".keyformula", ".ssss*", "._SSSSQR.png", ".nostr.zine.html",
                         ".MULTIPASS.CARD.png", ".welcome.html", ".roaming", ".migrat*", ".nip42_auth_*",
                         ".refresh_time", ".ucloud", ".face_invites", ".pass", "thumbcache")
# Le PASS (4 chiffres) ouvre la récupération par email+PASS : il ne voyage que dans le paquet de DÉMÉNAGEMENT
# (chiffré pour la station destinataire), jamais dans la sauvegarde quotidienne lisible avec le nsec.
ACCOUNT_STATE_MAX_BYTES = 64 * 1024 * 1024
STRFRY_DIR = Path.home() / ".zen" / "strfry"


def _excluded(name: str, for_migration: bool) -> bool:
    import fnmatch
    for pat in ACCOUNT_STATE_EXCLUDE:
        if pat == ".pass" and for_migration:
            continue
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def export_account_state(email: str, out_dir: Path, for_migration: bool = False) -> int:
    """Écrit account_state.tgz dans `out_dir` ; retourne le nombre de fichiers. Les liens symboliques sont ignorés."""
    udir = _user_dir(email)
    buf = io.BytesIO()
    n = total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in sorted(udir.iterdir()):
            if not (entry.name.startswith(".") or entry.name == "identity") or _excluded(entry.name, for_migration):
                continue
            if entry.is_symlink():
                continue
            files = [entry] if entry.is_file() else [f for f in sorted(entry.rglob("*")) if f.is_file() and not f.is_symlink()]
            for f in files:
                total += f.stat().st_size
                if total > ACCOUNT_STATE_MAX_BYTES:
                    raise RuntimeError(f"état du compte > {ACCOUNT_STATE_MAX_BYTES // (1024 * 1024)} Mo : "
                                       "un fichier caché anormalement gros, exporter à la main")
                tar.add(str(f), arcname=str(f.relative_to(udir)))
                n += 1
    if n:
        _write_private(out_dir / "account_state.tgz", buf.getvalue())
    return n


def import_account_state(email: str, tgz: Path, for_migration: bool = False) -> int:
    """Extrait account_state.tgz dans le dossier du compte. Défense en profondeur : un membre exclu, un chemin
    hors du dossier ou un lien est ignoré même si l'archive en contient. Les fichiers sont remis en 0600.
    Restauration normale = FUSION (un fichier déjà présent n'est jamais écrasé) ; déménagement = l'état du compte
    d'origine fait foi (le dossier ne contient alors que ce que le roaming a dérivé du QR)."""
    udir = _user_dir(email)
    n = 0
    with tarfile.open(str(tgz), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if (not m.isfile() or m.name.startswith("/") or ".." in parts or not parts
                    or not (parts[0].startswith(".") or parts[0] == "identity")
                    or _excluded(parts[0], for_migration)):
                continue
            if not for_migration and (udir / m.name).exists():
                continue
            tar.extract(m, str(udir), filter="data")
            os.chmod(udir / m.name, 0o600)
            n += 1
    return n


ZENCARD_MAX_BYTES = 256 * 1024 * 1024


def _players_dir(email: str) -> Path:
    return _user_dir(email).parent.parent / "players" / email


def export_zencard_state(email: str, out_dir: Path) -> int:
    """zencard_state.tgz : TOUT le dossier players/<email> (fichiers seulement, liens ignorés). 0 si pas de ZenCard.
    Au-delà de ZENCARD_MAX_BYTES : erreur visible (un TiddlyWiki anormalement gros s'exporte à la main)."""
    pdir = _players_dir(email)
    if not pdir.is_dir():
        return 0
    buf = io.BytesIO()
    n = total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted(pdir.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            total += f.stat().st_size
            if total > ZENCARD_MAX_BYTES:
                raise RuntimeError(f"ZenCard > {ZENCARD_MAX_BYTES // (1024 * 1024)} Mo : à exporter à la main")
            tar.add(str(f), arcname=str(f.relative_to(pdir)))
            n += 1
    if n:
        _write_private(out_dir / "zencard_state.tgz", buf.getvalue())
    return n


def import_zencard_state(email: str, tgz: Path) -> int:
    """Restaure players/<email>. REFUSE si une ZenCard DIFFÉRENTE existe déjà ici (jamais écrasée : c'est une clé
    et des parts sociales) ; la même (.g1pub identique) est mise à jour. Chemins hors dossier et liens ignorés.
    Recrée ensuite les liens U.SOCIETY* du dossier MULTIPASS."""
    pdir = _players_dir(email)
    with tarfile.open(str(tgz), mode="r:gz") as tar:
        members = [m for m in tar.getmembers()
                   if m.isfile() and not m.name.startswith("/") and ".." not in Path(m.name).parts]
        new_g1 = ""
        for m in members:
            if m.name == ".g1pub":
                new_g1 = tar.extractfile(m).read().decode().strip()
        if pdir.is_dir() and any(pdir.iterdir()):
            cur = ""
            try:
                cur = (pdir / ".g1pub").read_text().strip()
            except OSError:
                pass
            if not cur or cur != new_g1:
                raise RuntimeError(f"une ZenCard différente existe déjà pour {email} sur cette station "
                                   f"({cur[:10] or 'sans .g1pub'}… ≠ {new_g1[:10] or 'archive sans .g1pub'}…) : jamais écrasée")
        pdir.mkdir(parents=True, exist_ok=True)
        os.chmod(pdir, 0o755)
        for m in members:
            tar.extract(m, str(pdir), filter="data")
            if Path(m.name).name.startswith("secret"):
                os.chmod(pdir / m.name, 0o600)
    udir = _user_dir(email)
    for name in ("U.SOCIETY", "U.SOCIETY.end"):
        target, link = pdir / name, udir / name
        if target.exists() and udir.is_dir() and not (link.exists() or link.is_symlink()):
            os.symlink(str(target), str(link))
    return len(members)


# CONTENU de la ZenCard (≠ zencard_state.tgz, qui est le dossier ENTIER, secrets compris, du seul déménagement « même
# clé ») : ce que la personne a ÉCRIT — son TiddlyWiki et la chaîne de ses versions (ipfs/moa), le flux, les historiques de
# paiements (G1PalPay). Ni clé (secret.*), ni PASS, ni QR/avatars (régénérés), ni U.SOCIETY (le statut de sociétaire
# appartient à la coopérative d'ORIGINE : il ne passe pas dans une autre). La clé de la ZenCard étant la même sur toutes les
# UPlanets (clé CAPEX, crédentiel brut), ce contenu se restaure tel quel, y compris dans une autre constellation.
ZENCARD_CONTENT_DIRS = ("ipfs", "G1PalPay")
ZENCARD_OWNER_MARK = "ipfs/G1SSB/_g1.pubkey"       # écrit par VISA.new.sh : la clé Ğ1 de la ZenCard
FRESH_ROOT = Path(os.environ.get("ZENCARD_FRESH_ROOT") or (Path.home() / ".zen" / "tmp"))


# copylaradio.com = domaine de UPlanet ORIGIN. Embarqué sur une autre UPlanet, le contenu restauré ne doit plus renvoyer vers
# lui : TOUT lien portant ce domaine (nu ou avec n'importe quels sous-domaines : ipfs., vdo., relay., u.…) passe à la zone DNS
# de CETTE station — paramètre coopératif OVH_ZONE (celui du DNSLink, admin/system/ovh.me.sh). Les adresses e-mail
# (x@copylaradio.com) et les domaines qui ne font que COMMENCER par lui (copylaradio.com.evil.org) ne sont pas des liens.
COOP_OLD_DOMAIN = "copylaradio.com"
ZONE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_OLD_LINK_RE = re.compile(rb"(?<![A-Za-z0-9.@-])((?:[A-Za-z0-9-]+\.)*)" + re.escape(COOP_OLD_DOMAIN.encode())
                          + rb"(?![A-Za-z0-9-]|\.[A-Za-z0-9])")


def coop_zone() -> str:
    """Domaine de CETTE UPlanet Ẑen (UPLANET_DOMAIN de tools/my.sh = OVH_ZONE) ; « » sur ORIGIN ou si absent."""
    try:
        p = subprocess.run(["bash", "-c", f'. "{Path.home()}/.zen/Astroport.ONE/tools/my.sh" >/dev/null 2>&1; printf %s "$UPLANET_DOMAIN"'],
                           capture_output=True, text=True, timeout=120)
        return p.stdout.strip().lower() if p.returncode == 0 else ""
    except Exception:
        return ""


def rewrite_zencard_links(email: str, zone: str) -> Dict[str, Any]:
    """Remplace le domaine d'origine par `zone` dans le contenu texte de la ZenCard (ipfs/ : TiddlyWiki, flux RSS…, et
    G1PalPay/), liens ET titres de tiddlers qui les citent. Les `.chain.*` (empreintes de versions) ne sont jamais touchés,
    ni les fichiers binaires, ni les liens symboliques. Octets préservés (aucun réencodage), droits conservés, écriture
    atomique, idempotent. Zone invalide : erreur (jamais un nom bricolé). Zone = domaine d'origine : rien à faire.
    Retourne {zone, links, files}."""
    zone = (zone or "").strip().lower()
    if not ZONE_RE.fullmatch(zone):
        raise ValueError(f"zone DNS invalide : {zone[:40]!r}")
    out: Dict[str, Any] = {"zone": zone, "links": 0, "files": 0}
    if zone == COOP_OLD_DOMAIN:
        return out
    base = _players_dir(email)
    for sub in ZENCARD_CONTENT_DIRS:
        root = base / sub
        if not root.is_dir() or root.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                f = Path(dirpath) / name
                if name.startswith(".chain") or f.is_symlink() or not f.is_file():
                    continue
                data = f.read_bytes()
                if b"\0" in data or COOP_OLD_DOMAIN.encode() not in data:
                    continue
                n = [0]

                def sub_(m):
                    n[0] += 1
                    return m.group(1) + zone.encode()
                new = _OLD_LINK_RE.sub(sub_, data)
                if new != data:
                    tmp = f.with_name(f.name + ".tmp")
                    tmp.write_bytes(new)
                    os.chmod(tmp, f.stat().st_mode & 0o7777)
                    os.replace(tmp, f)
                    out["links"] += n[0]
                    out["files"] += 1
    return out


def export_zencard_content(email: str, out_dir: Path) -> int:
    """zencard_content.tgz ; 0 s'il n'y a pas de ZenCard. Erreur visible au-delà de ZENCARD_MAX_BYTES (jamais tronqué)."""
    pdir = _players_dir(email)
    if not pdir.is_dir():
        return 0
    buf = io.BytesIO()
    n = total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for d in ZENCARD_CONTENT_DIRS:
            root = pdir / d
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if not f.is_file() or f.is_symlink():
                    continue
                total += f.stat().st_size
                if total > ZENCARD_MAX_BYTES:
                    raise RuntimeError(f"contenu de la ZenCard > {ZENCARD_MAX_BYTES // (1024 * 1024)} Mo : à exporter à la main")
                tar.add(str(f), arcname=str(f.relative_to(pdir)))
                n += 1
    if n:
        _write_private(out_dir / "zencard_content.tgz", buf.getvalue())
    return n


def import_zencard_content(email: str, tgz: Path, zone: str = "") -> Dict[str, Any]:
    """Restaure le contenu dans la ZenCard DÉJÀ recréée ici (VISA.new.sh, mêmes identifiants). Refuse : pas de ZenCard
    ici ; archive sans marqueur de propriétaire ; TiddlyWiki d'UNE AUTRE carte. Le contenu tout neuf créé par VISA est
    mis de côté (jamais détruit sur l'instant : ~/.zen/tmp/zencard_fresh_…, purgé à 20h12) puis remplacé par l'ancien."""
    import time
    pdir = _players_dir(email)
    if not pdir.is_dir():
        raise RuntimeError(f"aucune ZenCard pour {email} sur cette station : la recréer d'abord (VISA.new.sh)")
    with tarfile.open(str(tgz), mode="r:gz") as tar:
        members = [m for m in tar.getmembers()
                   if m.isfile() and not m.name.startswith("/") and ".." not in Path(m.name).parts
                   and Path(m.name).parts and Path(m.name).parts[0] in ZENCARD_CONTENT_DIRS]
        mark = next((m for m in members if m.name == ZENCARD_OWNER_MARK), None)
        if mark is None:
            raise RuntimeError("archive sans marqueur de propriétaire (ipfs/G1SSB/_g1.pubkey) : contenu refusé")
        theirs = tar.extractfile(mark).read().decode().strip()
        try:
            mine = (pdir / ".g1pub").read_text().strip()
        except OSError:
            raise RuntimeError("ZenCard d'ici sans .g1pub : contenu refusé")
        if not theirs or theirs != mine:
            raise RuntimeError(f"ce contenu est celui d'une AUTRE ZenCard ({theirs[:10]}… ≠ {mine[:10]}…) : refusé")
        staging = Path(tempfile.mkdtemp(prefix=".zc_restore_", dir=str(pdir)))
        try:
            for m in members:
                tar.extract(m, str(staging), filter="data")
            fresh = FRESH_ROOT / f"zencard_fresh_{email}_{time.strftime('%Y%m%d%H%M%S')}"
            moved = []
            for d in ZENCARD_CONTENT_DIRS:
                if not (staging / d).is_dir():
                    continue
                if (pdir / d).exists():
                    fresh.mkdir(parents=True, exist_ok=True)
                    os.chmod(fresh, 0o700)
                    shutil.move(str(pdir / d), str(fresh / d))
                    moved.append(d)
                shutil.move(str(staging / d), str(pdir / d))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    res: Dict[str, Any] = {"files": len(members), "fresh_moved_to": str(fresh) if moved else ""}
    if zone:                       # le contenu est en place : un échec de réécriture ne le met jamais en cause
        try:
            res["links_rewritten"] = rewrite_zencard_links(email, zone)
        except Exception as exc:
            res["links_error"] = str(exc)
            _log(f"WARN: liens du TiddlyWiki non réécrits ({exc})")
    return res


def export_nostr_events(hex_pub: str, out_file: Path) -> int:
    """Événements écrits par le compte + messages privés (kind 4, giftwraps 1059) qui lui sont adressés, lus dans
    la base strfry LOCALE de l'ancienne station. Erreur visible si strfry est illisible (jamais un export vide muet)."""
    strfry = STRFRY_DIR / "strfry"
    if not strfry.exists():
        raise RuntimeError(f"strfry introuvable ({strfry})")
    n = 0
    with open(out_file, "wb") as fh:
        for flt in ({"authors": [hex_pub]}, {"#p": [hex_pub], "kinds": [4, 1059]}):
            p = subprocess.run([str(strfry), "scan", json.dumps(flt)], cwd=str(STRFRY_DIR),
                               capture_output=True, timeout=600)
            if p.returncode != 0:
                raise RuntimeError(f"strfry scan a échoué : {p.stderr.decode()[-200:]}")
            for line in p.stdout.splitlines():
                if line.strip().startswith(b"{"):
                    fh.write(line + b"\n")
                    n += 1
    os.chmod(out_file, 0o600)
    return n


def import_nostr_events(jsonl: Path) -> int:
    """strfry import AVEC vérification des signatures (jamais --no-verify) ; compte les lignes présentées."""
    strfry = STRFRY_DIR / "strfry"
    if not strfry.exists():
        raise RuntimeError(f"strfry introuvable ({strfry})")
    n = sum(1 for _ in open(jsonl, "rb"))
    with open(jsonl, "rb") as fh:
        p = subprocess.run([str(strfry), "import"], cwd=str(STRFRY_DIR), stdin=fh, capture_output=True, timeout=1800)
    if p.returncode != 0:
        raise RuntimeError(f"strfry import a échoué : {p.stderr.decode()[-200:]}")
    return n


def memory_collection(email: str) -> Optional[str]:
    """Nom de la collection mémoire (memory_<16 premiers car. du HEX>, comme
    Astroport.ONE/IA/memory_manager.py::_user_hex) ; None sans HEX."""
    hex_pub = cloud_storage.hex_for_email(email)
    return f"memory_{hex_pub[:16]}" if hex_pub else None


def _settings_src(email: str, where: str, name: str) -> Path:
    return (cloud_storage.ucloud_dir(email) if where == "ucloud" else _user_dir(email)) / name


def export_bundle(email: str, out_dir: Path, for_migration: bool = False) -> Dict[str, Any]:
    """Écrit le paquet dans `out_dir` (noms compatibles nostr_RESTORE_TW.sh). `for_migration` : déménagement
    entre stations — ajoute le `.pass` à l'état du compte et les événements NOSTR (jamais dans la sauvegarde
    quotidienne). Un composant qui échoue est listé dans counts["errors"], jamais avalé."""
    out_dir.mkdir(parents=True, exist_ok=True)
    udir = cloud_storage.ucloud_dir(email)
    counts: Dict[str, Any] = {}
    # L'index et le keyring vivent dans SQLite (ucloud.db) : exportés au format JSON
    # historique, aux noms attendus par nostr_RESTORE_TW.sh.
    if ucloud_db.exists(udir):
        ucloud_db.export_json(udir, out_dir, "ucloud_index.json", "ucloud_keyring.json")
    pairs = [("webdav_sources.json", "webdav_sources.json"), ("carddav_sources.json", "carddav_sources.json")]
    for src, dst in pairs:
        if (udir / src).is_file():
            shutil.copyfile(udir / src, out_dir / dst)
            os.chmod(out_dir / dst, 0o600)
    for state in udir.glob("webdav_import_state_*.json"):
        shutil.copyfile(state, out_dir / state.name)
    invites = _user_dir(email) / ".face_invites"
    if invites.is_dir():
        (out_dir / "face_invites").mkdir(exist_ok=True)
        for f in invites.glob("*.json"):
            shutil.copyfile(f, out_dir / "face_invites" / f.name)
    try:
        counts["files"] = ucloud_db.stats(udir)["files"]
    except (OSError, ValueError, KeyError, RuntimeError):
        counts["files"] = 0

    hex_pub = cloud_storage.hex_for_email(email)
    if hex_pub:
        points = _qdrant_export(f"faces_{hex_pub}")
        if points is not None:
            (out_dir / "qdrant_faces.json").write_text(json.dumps(points))
            counts["faces"] = len(points)
    mem = memory_collection(email)
    if mem:
        points = _qdrant_export(mem)
        if points is not None:
            (out_dir / "qdrant_lifeos.json").write_text(json.dumps(points))
            counts["memories"] = len(points)
    for where, name in SETTINGS_FILES:
        src = _settings_src(email, where, name)
        if src.is_file():
            (out_dir / "settings").mkdir(exist_ok=True)
            shutil.copyfile(src, out_dir / "settings" / name)
            counts["settings"] = counts.get("settings", 0) + 1
    manifest = _user_dir(email) / "APP" / "uDRIVE" / "manifest.json"
    if manifest.is_file():
        shutil.copyfile(manifest, out_dir / "uDRIVE_manifest.json")
        counts["udrive"] = 1
    errors: List[str] = []
    try:
        n_state = export_account_state(email, out_dir, for_migration)
        if n_state:
            counts["state_files"] = n_state
    except Exception as exc:
        errors.append(f"state: {exc}")
    if for_migration:
        try:
            n_zc = export_zencard_state(email, out_dir)
            if n_zc:
                counts["zencard_files"] = n_zc
        except Exception as exc:
            errors.append(f"zencard: {exc}")
    if for_migration and hex_pub:
        try:
            counts["nostr_events"] = export_nostr_events(hex_pub, out_dir / "nostr_events.jsonl")
        except Exception as exc:
            errors.append(f"nostr_events: {exc}")
    if errors:
        counts["errors"] = errors
    # Tout le paquet en 0600 / 0700 : noms, identifiants de sources et clés
    # (copyfile ne recopie pas les droits, le umask donnerait 0664).
    for f in out_dir.rglob("*"):
        os.chmod(f, 0o700 if f.is_dir() else 0o600)
    return counts


def _qdrant_export(collection: str) -> Optional[List[Dict[str, Any]]]:
    points: List[Dict[str, Any]] = []
    offset = None
    try:
        with httpx.Client(timeout=60) as client:
            while True:
                body = {"limit": 1000, "with_payload": True, "with_vector": True}
                if offset is not None:
                    body["offset"] = offset
                r = client.post(f"{QDRANT_URL}/collections/{collection}/points/scroll",
                                headers=_qdrant_headers(), json=body)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                res = r.json()["result"]
                points.extend(res.get("points", []))
                offset = res.get("next_page_offset")
                if offset is None:
                    return points
    except Exception as exc:
        _log(f"WARN: export Qdrant {collection} impossible ({exc})")
        return None


def apply_bundle(email: str, bundle: Path, with_qdrant: bool = True,
                 overwrite_settings: bool = False, for_migration: bool = False) -> Dict[str, Any]:
    """Restaure un paquet en FUSION : ce qui manque ici est ajouté, rien de
    local n'est écrasé (une restauration ne détruit jamais une photo ajoutée
    depuis). Retourne les compteurs ; `errors` liste ce qui n'a pas pu être
    restauré (jamais d'échec silencieux).

    `overwrite_settings` (déménagement) : les réglages du paquet remplacent ceux
    d'un compte tout neuf, dont les valeurs sont celles par défaut."""
    udir = cloud_storage.ensure_ucloud_dir(email)
    counts: Dict[str, Any] = {"files": 0, "keys": 0, "sources": 0, "faces": 0,
                              "memories": 0, "settings": 0, "state_files": 0, "events": 0, "zencard_files": 0,
                              "errors": []}
    idx_file, key_file = bundle / "ucloud_index.json", bundle / "ucloud_keyring.json"
    if idx_file.is_file():
        remote_idx = json.loads(idx_file.read_text())
        remote_keys = json.loads(key_file.read_text()) if key_file.is_file() else {}
        with cloud_storage.index_lock(email):
            idx = cloud_storage.load_index(email)
            keyring = cloud_storage.load_keyring(email)
            for path, entry in remote_idx.get("entries", {}).items():
                if path not in idx["entries"]:
                    idx["entries"][path] = entry
                    if isinstance(entry, dict) and entry.get("type") == "file":
                        counts["files"] += 1
            for cid, k in remote_keys.items():
                if cid not in keyring:
                    keyring[cid] = k
                    counts["keys"] += 1
            cloud_storage.save_keyring(email, keyring)
            cloud_storage.save_index(email, idx)
    for name in ("webdav_sources.json", "carddav_sources.json"):
        src = bundle / name
        if src.is_file() and not (udir / name).exists():
            shutil.copyfile(src, udir / name)
            os.chmod(udir / name, 0o600)
            counts["sources"] += 1
    for state in bundle.glob("webdav_import_state_*.json"):
        if not (udir / state.name).exists():
            shutil.copyfile(state, udir / state.name)
            os.chmod(udir / state.name, 0o600)
    inv = bundle / "face_invites"
    if inv.is_dir():
        dest = _user_dir(email) / ".face_invites"
        dest.mkdir(parents=True, exist_ok=True)
        for f in inv.glob("*.json"):
            if not (dest / f.name).exists():
                shutil.copyfile(f, dest / f.name)
    # État du compte (marqueurs, cookies, préférences, identity/) : le dossier du compte existe (MULTIPASS créé
    # ou hydraté par roaming avant l'import) ; les secrets sont exclus à l'export ET refusés ici.
    if (bundle / "account_state.tgz").is_file():
        try:
            counts["state_files"] = import_account_state(email, bundle / "account_state.tgz", for_migration)
        except Exception as exc:
            counts["errors"].append(f"state: {exc}")
            _log(f"ERROR: restauration de l'état du compte impossible ({exc})")
    if for_migration and (bundle / "zencard_state.tgz").is_file():
        try:
            counts["zencard_files"] = import_zencard_state(email, bundle / "zencard_state.tgz")
        except Exception as exc:
            counts["errors"].append(f"zencard: {exc}")
            _log(f"ERROR: restauration de la ZenCard impossible ({exc})")
    if for_migration and (bundle / "nostr_events.jsonl").is_file():
        try:
            counts["events"] = import_nostr_events(bundle / "nostr_events.jsonl")
        except Exception as exc:
            counts["errors"].append(f"nostr_events: {exc}")
            _log(f"ERROR: import des événements NOSTR impossible ({exc})")
    sdir = bundle / "settings"
    if sdir.is_dir():
        for where, name in SETTINGS_FILES:
            src = sdir / name
            dst = _settings_src(email, where, name)
            if src.is_file() and (overwrite_settings or not dst.exists()):
                _write_private(dst, src.read_bytes())
                counts["settings"] += 1
    hex_pub = cloud_storage.hex_for_email(email)
    if with_qdrant and hex_pub:
        for fname, collection, key, size in (
                ("qdrant_faces.json", f"faces_{hex_pub}", "faces", 512),
                ("qdrant_lifeos.json", memory_collection(email), "memories", None)):
            if not (bundle / fname).is_file():
                continue
            try:
                counts[key] = _qdrant_restore(collection, json.loads((bundle / fname).read_text()), size)
            except Exception as exc:
                counts["errors"].append(f"{key}: {exc}")
                _log(f"ERROR: restauration {collection} impossible ({exc})")
    return counts


def _qdrant_restore(collection: str, points: List[Dict[str, Any]], size: Optional[int] = None) -> int:
    """Ré-insère les points ABSENTS de la collection (fusion). `size` = dimension
    des vecteurs ; sans elle, lue dans le premier point (la mémoire dépend du
    modèle d'embedding de la station d'origine). Une collection existante d'une
    autre dimension LÈVE : deux modèles ne se mélangent jamais."""
    if not points:
        return 0
    first = next((p["vector"] for p in points if isinstance(p.get("vector"), list)), None)
    size = size or (len(first) if first else None)
    if not size:
        raise ValueError(f"{collection}: aucun vecteur dans le paquet")
    headers = _qdrant_headers()
    with httpx.Client(timeout=60) as client:
        cur = client.get(f"{QDRANT_URL}/collections/{collection}", headers=headers)
        if cur.status_code == 200:
            have = (((cur.json().get("result") or {}).get("config") or {}).get("params") or {}).get("vectors", {}).get("size")
            if have and have != size:
                raise RuntimeError(f"{collection}: vecteurs de {have} dimensions ici, {size} dans le paquet "
                                   "(modèle d'embedding différent — ne rien mélanger, ré-embarquer)")
        else:
            r = client.put(f"{QDRANT_URL}/collections/{collection}", headers=headers,
                           json={"vectors": {"size": size, "distance": "Cosine"}})
            r.raise_for_status()
        existing = set()
        offset = None
        while True:
            body = {"limit": 1000, "with_payload": False, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            r = client.post(f"{QDRANT_URL}/collections/{collection}/points/scroll", headers=headers, json=body)
            if r.status_code != 200:
                break
            res = r.json()["result"]
            existing.update(str(p["id"]) for p in res.get("points", []))
            offset = res.get("next_page_offset")
            if offset is None:
                break
        todo = [{"id": p["id"], "vector": p["vector"], "payload": p.get("payload") or {}}
                for p in points if str(p.get("id")) not in existing and isinstance(p.get("vector"), list)]
        for i in range(0, len(todo), 256):
            client.put(f"{QDRANT_URL}/collections/{collection}/points?wait=true", headers=headers,
                       json={"points": todo[i:i + 256]}).raise_for_status()
    return len(todo)


def _fetch_one(cid: str, gateway: Optional[str]) -> bool:
    """Rapatrie UN CID : par la passerelle (CAR, CID préservé, blocs vérifiés) si `gateway`, sinon par le swarm local."""
    try:
        if gateway:
            if subprocess.run(["ipfs", "pin", "ls", "--type=recursive", cid], capture_output=True, timeout=60).returncode == 0:
                return True
            gateway_fetch.fetch_dag(cid, gateway)
            return True
        return subprocess.run(["ipfs", "pin", "add", "--progress=false", cid], capture_output=True, timeout=600).returncode == 0
    except Exception as exc:                # GatewayError comprise : consignée, jamais avalée
        _log(f"ERROR: {cid[:16]}… : {exc}")
        return False


def pin_all(email: str, gateway: Optional[str] = None) -> Dict[str, Any]:
    """Épingle tous les CID de l'index sur CETTE station (prise de garde après restauration : sans ça, les fichiers
    ne vivent que tant que l'ancienne station les garde épinglés). `gateway` : changement de CONSTELLATION — l'IPFS
    d'ici ne joint pas l'ancienne station, on passe par sa passerelle HTTP (même CID, donc l'index et le trousseau
    de clés restent valables)."""
    idx = cloud_storage.load_index(email)
    cids = sorted({e["cid"] for e in idx["entries"].values() if isinstance(e, dict) and e.get("cid")})
    if gateway:
        gateway = gateway_fetch.normalize_gateway(gateway)        # refuse d'emblée une passerelle inacceptable
        from concurrent.futures import ThreadPoolExecutor
        # Démon en ligne : l'API IPFS gère la concurrence (4 en parallèle). Hors ligne (IPFS_ARGS = --offline, tests),
        # le dépôt est verrouillé en exclusivité par chaque commande : un seul à la fois.
        with ThreadPoolExecutor(max_workers=1 if gateway_fetch.IPFS_ARGS else 4) as ex:
            results = list(ex.map(lambda c: _fetch_one(c, gateway), cids))
    else:
        results = [_fetch_one(c, None) for c in cids]
    ok = sum(1 for r in results if r)
    res: Dict[str, Any] = {"pinned": ok, "failed": len(results) - ok, "total": len(cids)}
    res.update(pin_udrive(email, gateway))
    return res


def pin_udrive(email: str, gateway: Optional[str] = None) -> Dict[str, Any]:
    """Épingle (récursivement) le uDRIVE du manifeste restauré : sans cela le dossier n'existe que tant que
    l'ancienne station le garde. Même option `gateway` que pin_all."""
    manifest = _user_dir(email) / "APP" / "uDRIVE" / "manifest.json"
    try:
        cid = json.loads(manifest.read_text()).get("final_cid") or ""
    except (OSError, ValueError):
        return {}
    if not cid:
        return {}
    return {"udrive_cid": cid, "udrive_pinned": _fetch_one(cid, gateway)}


# ═════════════════════════════════════════════════════════════════════════════
# Sauvegarde hors station : archive chiffrée sur IPFS + 1 event NOSTR
# ═════════════════════════════════════════════════════════════════════════════
def _keys(email: str):
    import nostr_sdk
    raw = (_user_dir(email) / ".secret.nostr").read_text()
    m = re.search(r"NSEC=([^;\s]+)", raw)
    if not m:
        raise RuntimeError("NSEC introuvable")
    return nostr_sdk.Keys.parse(m.group(1))


def _state_path(email: str) -> Path:
    return cloud_storage.ucloud_dir(email) / "backup.json"


def status(email: str) -> Dict[str, Any]:
    try:
        return json.loads(_state_path(email).read_text())
    except (OSError, ValueError):
        return {}


def dir_sha(root: Path) -> str:
    """Empreinte du CONTENU d'un paquet (noms relatifs + octets, ordre trié)."""
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(root)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def export_encrypted(email: str, for_migration: bool = False) -> Dict[str, Any]:
    """Paquet complet, chiffré AES-256-GCM avec une clé NEUVE, ajouté et épinglé sur
    IPFS. Retourne {cid, key, sha, counts, size}. Aucun événement NOSTR (≠ snapshot) :
    sert au déménagement entre stations, où le CID et la clé voyagent dans un message
    inter-NODE chiffré."""
    data, sha, counts = _tar_bundle(email, for_migration)
    key_hex = os.urandom(32).hex()
    payload, _iv = cloud_storage.uenc_codec.encrypt_aes256gcm(data, key_hex)
    cid = cloud_storage.ipfs_add_bytes(payload, filename="multipass-bundle.uenc", pin=True)
    return {"cid": cid, "key": key_hex, "sha": sha, "counts": counts, "size": len(payload)}


def import_encrypted(email: str, cid: str, key_hex: str, sha: str = "",
                     with_qdrant: bool = True, overwrite_settings: bool = False,
                     for_migration: bool = False) -> Dict[str, Any]:
    """Inverse de export_encrypted : récupère, déchiffre, CONTRÔLE l'empreinte (si
    fournie — une archive altérée est refusée avant tout import), puis applique en fusion."""
    blob = cloud_storage.ipfs_cat(cid, max_bytes=500 * 1024 * 1024)
    data = cloud_storage.uenc_codec.decrypt_aes256gcm(blob, key_hex)
    tmp = Path(tempfile.mkdtemp(prefix="ucloud_restore_"))
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")
        if sha and dir_sha(tmp / "bundle") != sha:
            raise ValueError("empreinte du paquet différente de celle annoncée — archive altérée, rien importé")
        return apply_bundle(email, tmp / "bundle", with_qdrant, overwrite_settings, for_migration)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tar_bundle(email: str, for_migration: bool = False) -> tuple:
    tmp = Path(tempfile.mkdtemp(prefix="ucloud_bundle_"))
    try:
        counts = export_bundle(email, tmp, for_migration)
        # Empreinte STABLE du contenu (sans dates d'archive) : rien à republier si rien n'a changé
        sha = dir_sha(tmp)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(str(tmp), arcname="bundle")
        return buf.getvalue(), sha, counts
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _publish(keys, content: str) -> List[str]:
    import nostr_sdk
    from websockets.sync.client import connect
    ev = nostr_sdk.EventBuilder(nostr_sdk.Kind(30078), content).tags(
        [nostr_sdk.Tag.parse(["d", D_TAG])]).finalize(keys)
    raw = ev.as_json()
    ok = []
    for relay in RELAYS:
        try:
            with connect(relay, open_timeout=10, close_timeout=5) as ws:
                ws.send(json.dumps(["EVENT", json.loads(raw)]))
                deadline = time.time() + 10
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=10))
                    if msg[0] == "OK":
                        if msg[2]:
                            ok.append(relay)
                        else:
                            _log(f"WARN: {relay} a refusé la sauvegarde : {msg[3] if len(msg) > 3 else ''}")
                        break
        except Exception as exc:
            _log(f"WARN: {relay} injoignable ({exc})")
    return ok


def snapshot(email: str, force: bool = False) -> Dict[str, Any]:
    udir = cloud_storage.ucloud_dir(email)
    if not (ucloud_db.exists(udir) or (udir / "index.json").exists()):
        return {"status": "empty"}
    data, sha, counts = _tar_bundle(email)
    prev = status(email)
    if not force and prev.get("sha") == sha and prev.get("relays"):
        return {"status": "unchanged", **prev}
    import nostr_sdk
    key_hex = os.urandom(32).hex()
    payload, _iv = cloud_storage.uenc_codec.encrypt_aes256gcm(data, key_hex)
    cid = cloud_storage.ipfs_add_bytes(payload, filename="facecloud-backup.uenc", pin=True)
    keys = _keys(email)
    content = nostr_sdk.nip44_encrypt(keys.secret_key(), keys.public_key(), json.dumps({
        "v": 1, "cid": cid, "key": key_hex, "sha": sha, "at": int(time.time()), "counts": counts,
    }), nostr_sdk.Nip44Version.V2)
    relays = _publish(keys, content)
    if not relays:
        subprocess.run(["ipfs", "pin", "rm", cid], capture_output=True, timeout=60)
        return {"status": "error", "reason": "aucun relais n'a accepté la sauvegarde"}
    if prev.get("cid") and prev["cid"] != cid:
        subprocess.run(["ipfs", "pin", "rm", prev["cid"]], capture_output=True, timeout=60)
    state = {"cid": cid, "sha": sha, "at": int(time.time()), "counts": counts, "relays": relays,
             "size": len(payload)}
    _write_private(_state_path(email), json.dumps(state).encode())
    return {"status": "saved", **state}


def fetch_pointer(email: str) -> Optional[Dict[str, Any]]:
    """Dernier pointeur de sauvegarde publié (déchiffré), depuis les relais."""
    import nostr_sdk
    from websockets.sync.client import connect
    keys = _keys(email)
    best = None
    for relay in RELAYS:
        try:
            with connect(relay, open_timeout=10, close_timeout=5) as ws:
                ws.send(json.dumps(["REQ", "fcb", {"kinds": [30078], "authors": [keys.public_key().to_hex()],
                                                    "#d": [D_TAG], "limit": 1}]))
                deadline = time.time() + 10
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=10))
                    if msg[0] == "EVENT":
                        ev = msg[2]
                        if not best or ev["created_at"] > best["created_at"]:
                            best = ev
                    elif msg[0] == "EOSE":
                        break
        except Exception as exc:
            _log(f"WARN: {relay} : {exc}")
    if not best:
        return None
    clear = nostr_sdk.nip44_decrypt(keys.secret_key(), keys.public_key(), best["content"])
    return json.loads(clear)


def restore_from_relay(email: str, with_qdrant: bool = True) -> Dict[str, Any]:
    pointer = fetch_pointer(email)
    if not pointer:
        return {"status": "none"}
    counts = import_encrypted(email, pointer["cid"], pointer["key"], pointer.get("sha", ""), with_qdrant)
    return {"status": "restored", "from": pointer.get("at"), **counts}


def main() -> int:
    ap = argparse.ArgumentParser(description="Sauvegarde / restauration du cloud FaceCloud")
    ap.add_argument("action", choices=["snapshot", "export", "restore", "pin", "status", "zencard-export", "zencard-import",
                                           "zencard-links"])
    ap.add_argument("email")
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--from-relay", action="store_true")
    ap.add_argument("--bundle")
    ap.add_argument("--no-qdrant", action="store_true")
    ap.add_argument("--overwrite-settings", action="store_true")
    ap.add_argument("--gateway", metavar="URL", help="pin : rapatrier par la passerelle HTTP de l'ancienne station")
    ap.add_argument("--zone", metavar="ZONE", help="zencard-import : zone DNS de cette station (OVH_ZONE) pour réécrire les liens")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not (cloud_storage.GAME_NOSTR_PATH / args.email).is_dir():
        print(f"ERROR:ucloud_backup:{args.email}: MULTIPASS absent de cette station", file=sys.stderr)
        return 1
    if args.action == "snapshot":
        res = snapshot(args.email, force=args.force)
    elif args.action == "export":
        if not args.dir:
            ap.error("export : dossier de destination requis")
        res = export_bundle(args.email, Path(args.dir))
    elif args.action == "restore":
        if args.bundle:
            res = apply_bundle(args.email, Path(args.bundle), not args.no_qdrant, args.overwrite_settings)
        elif args.from_relay:
            res = restore_from_relay(args.email, not args.no_qdrant)
        else:
            ap.error("restore : --from-relay ou --bundle requis")
    elif args.action == "zencard-export":
        if not args.dir:
            ap.error("zencard-export : dossier de destination requis")
        res = {"files": export_zencard_content(args.email, Path(args.dir))}
    elif args.action == "zencard-import":
        if not args.dir:
            ap.error("zencard-import : archive zencard_content.tgz requise")
        res = import_zencard_content(args.email, Path(args.dir), args.zone or "")
    elif args.action == "zencard-links":
        if not args.dir:
            ap.error("zencard-links : zone DNS requise")
        res = rewrite_zencard_links(args.email, args.dir)
    elif args.action == "pin":
        res = pin_all(args.email, args.gateway)
    else:
        res = status(args.email)
    print(json.dumps(res))
    return 0 if not (isinstance(res, dict) and res.get("status") == "error") else 1


if __name__ == "__main__":
    sys.exit(main())
