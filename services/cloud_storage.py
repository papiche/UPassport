"""
services/cloud_storage.py — Cloud personnel chiffré (WebDAV) du MULTIPASS.

Monté DANS le process UPassport (port 54321) sous `/dav/` via a2wsgi
(cf. 54321.py). Remplace l'ancien démon autonome `ucloudd` (port 8342) : même
logique de stockage et de chiffrement, mais plus AUCUNE duplication de
l'authentification NOSTR — NIP-98 est vérifiée par `services.nostr`, le module
de référence de UPassport.

    PUT  : flux DAV → buffer borné (20 MB) → clé AES-256 ALÉATOIRE PAR FICHIER
           → uenc_codec.encrypt_aes256gcm() → ipfs add → index + keyring
    GET  : index → CID → ipfs cat → uenc_codec.decrypt_aes256gcm()
           → fichier éphémère 0600 → flux HTTP → purge immédiate

⚠️  SYSTÈME PARALLÈLE AU uDRIVE PUBLIC. `Astroport.ONE/tools/
    generate_ipfs_structure.sh` + `manifest.json` produisent un uDRIVE EN CLAIR
    publié sur IPNS sous `APP/uDRIVE/` — ce mécanisme reste INCHANGÉ. Ici rien
    n'est publié sur IPNS, rien n'est écrit sous `APP/uDRIVE/`, et tout ce qui
    part vers IPFS est déjà chiffré.

Stockage par utilisateur :

    ~/.zen/game/nostr/{EMAIL}/.ucloud/index.json    0600  chemin DAV ↔ CID ↔ méta
    ~/.zen/game/nostr/{EMAIL}/.ucloud/keyring.json  0600  clé AES-256 par CID
    ~/.zen/game/nostr/{EMAIL}/.ucloud/dav_token     0600  token opaque Basic Auth
    ~/.zen/game/nostr/{EMAIL}/.ucloud/.lock         0600  verrou fcntl.flock
    ~/.zen/tmp/ucloud_cache/                        0700  clair éphémère (TTL 60 s)

POURQUOI INDEX ET KEYRING SÉPARÉS : une fuite du seul `index.json` (copie de
debug, backup rsync, ticket support…) n'expose AUCUNE clé de déchiffrement.
`iv_hex` n'est pas secret isolément et reste dans l'index ; `key_hex` n'existe
QUE dans le keyring.

─────────────────────────────────────────────────────────────────────────────
EXTENSION CardDAV / CalDAV (esquisse, NON implémentée)
─────────────────────────────────────────────────────────────────────────────
Le même index accueillerait contacts et événements sans refonte :
  * nouvelles valeurs de `type` : "contact" et "event" à côté de "file"/"dir" ;
  * le corps vCard (.vcf) ou iCalendar (.ics) est chiffré UENC et poussé sur
    IPFS exactement comme un fichier — mêmes champs cid / enc / iv_hex / key_hex ;
  * chemins conventionnels : /Contacts/{uid}.vcf et /Calendars/{cal}/{uid}.ics,
    les collections portant en plus les propriétés mortes DAV:resourcetype
    addressbook / calendar (REPORT addressbook-query / calendar-query restant à
    implémenter par-dessus) ;
  * un champ `etag` par entrée suffit à la synchro incrémentale des clients
    (Thunderbird, DAVx5), l'index étant déjà la source de vérité des mtime.
Aucune modification du schéma d'index n'est requise : seul `type` s'enrichit.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional
from urllib.parse import unquote

import httpx

from core.config import settings
from services import storage_quota, ucloud_db

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# Configuration
# ═════════════════════════════════════════════════════════════════════════════
GAME_NOSTR_PATH: Path = settings.GAME_PATH / "nostr"
UCLOUD_DIRNAME = ".ucloud"
DAV_TOKEN_FILENAME = "dav_token"
WEBDAV_SOURCES_FILENAME = "webdav_sources.json"
CACHE_DIR: Path = settings.ZEN_PATH / "tmp" / "ucloud_cache"
CACHE_TTL = 60  # secondes — filet de sécurité si le `finally` a échoué

# API du daemon IPFS local (Kubo). Volontairement 127.0.0.1 : on ne pousse
# jamais un blob via une gateway distante.
IPFS_API = os.environ.get("UCLOUD_IPFS_API", "http://127.0.0.1:5001")
IPFS_TIMEOUT = 60

# Même borne que routers/media_upload.py::_UENC_MAX_FILE_SIZE — AESGCM est
# one-shot, il n'existe pas de cadre de chunking dans le format UENC v1.
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

# Surcoût du format UENC (magic+version+type+IV = 18 octets, tag GCM = 16) : un
# clair de MAX_FILE_SIZE octets donne un blob un peu plus gros. La borne de
# LECTURE doit le tolérer, sinon un fichier de exactement 20 Mo écrit par un PUT
# serait illisible.
UENC_OVERHEAD = 64

# Préfixe de montage dans l'app FastAPI (cf. 54321.py). Sert uniquement à
# construire l'URL annoncée aux clients par /api/cloud/enroll.
DAV_MOUNT_PREFIX = "/dav"

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_CACHE_PREFIX = "ucloud-"

INDEX_VERSION = 1
_INDEX_NAME = "index.json"
_KEYRING_NAME = "keyring.json"
_LOCK_NAME = ".lock"

ENC_LABEL = "uenc-aes256gcm"


# ─────────────────────────────────────────────────────────────────────────────
# Codec UENC partagé — Astroport.ONE/tools/uenc_codec.py
# ─────────────────────────────────────────────────────────────────────────────
# MÊME PATTERN que routers/media_upload.py (sys.path.insert + import) : le
# format binaire UENC ("UENC" + VERSION + ENC_TYPE + IV(12) + CT+TAG) est un
# contrat inter-projets (media_upload, bro_dm_daemon.sh, pipeline NIP-17/FaceID,
# ce module). Le vendoriser introduirait un risque de divergence silencieuse.
sys.path.insert(0, str(settings.TOOLS_PATH))
import uenc_codec  # noqa: E402

# HEIC/HEIF (iPhone) : sans ce plugin facultatif, Pillow ne lit pas ces photos (pas de
# miniature, pas d'EXIF). `pip install pillow-heif` l'active, sinon repli silencieux.
try:  # pragma: no cover - dépend de l'environnement
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass


# ═════════════════════════════════════════════════════════════════════════════
# Chemins par utilisateur
# ═════════════════════════════════════════════════════════════════════════════
def nostr_dir(email: str) -> Path:
    """Répertoire MULTIPASS (contient .secret.nostr)."""
    return GAME_NOSTR_PATH / email


def ucloud_dir(email: str) -> Path:
    return GAME_NOSTR_PATH / email / UCLOUD_DIRNAME


def index_path(email: str) -> Path:
    return ucloud_dir(email) / _INDEX_NAME


def keyring_path(email: str) -> Path:
    return ucloud_dir(email) / _KEYRING_NAME


def lock_path(email: str) -> Path:
    return ucloud_dir(email) / _LOCK_NAME


def sources_lock_path(email: str) -> Path:
    return ucloud_dir(email) / ".sources.lock"


def token_path(email: str) -> Path:
    return ucloud_dir(email) / DAV_TOKEN_FILENAME


def webdav_sources_path(email: str) -> Path:
    return ucloud_dir(email) / WEBDAV_SOURCES_FILENAME


def ensure_ucloud_dir(email: str) -> Path:
    d = ucloud_dir(email)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, _DIR_MODE)
    except OSError as exc:  # pragma: no cover - dépend du FS
        logger.warning("ucloud: chmod 0700 impossible sur %s: %s", d, exc)
    return d


# ═════════════════════════════════════════════════════════════════════════════
# Verrou inter-process
# ═════════════════════════════════════════════════════════════════════════════
@contextmanager
def index_lock(email: str, timeout: float = 10.0) -> Iterator[None]:
    """Verrou exclusif (fcntl.flock) autour des sections critiques index/keyring.

    flock est par descripteur : on en ouvre un dédié à chaque acquisition, donc
    c'est bien un verrou par section critique et non par process. Il protège
    aussi bien les threads du pool a2wsgi que d'éventuels scripts bash.

    ⚠️ BLOQUANT (poll `time.sleep`) : depuis une route `async def`, l'appeler
    dans `asyncio.to_thread`, jamais directement dans la boucle d'événements.
    """
    with _flock(lock_path(email), email, timeout):
        yield


@contextmanager
def sources_lock(email: str, timeout: float = 10.0) -> Iterator[None]:
    """Verrou des fichiers de petite config (sources WebDAV, expéditeurs
    bloqués) : distinct de `index_lock` pour ne pas être retenu pendant un
    gros import, mais lui aussi inter-process (script d'import ↔ serveur)."""
    with _flock(sources_lock_path(email), email, timeout):
        yield


def try_lock_nowait(path: Path):
    """Prend un flock exclusif NON bloquant sur `path` (créé si besoin) et
    rend le descripteur ouvert, ou None si quelqu'un le détient déjà.
    L'appelant doit fermer le descripteur (`os.close`) pour libérer. Sert à
    interdire deux imports simultanés d'une même source."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, _FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


@contextmanager
def _flock(lp: Path, email: str, timeout: float) -> Iterator[None]:
    ensure_ucloud_dir(email)
    fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, _FILE_MODE)
    try:
        os.fchmod(fd, _FILE_MODE)
    except OSError:
        pass
    deadline = time.time() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"ERROR:ucloud:index_lock timeout ({timeout}s) sur {lp}"
                    )
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:  # pragma: no cover
                logger.warning("ucloud: déverrouillage échoué sur %s: %s", lp, exc)
        os.close(fd)


# ═════════════════════════════════════════════════════════════════════════════
# Lecture / écriture atomique (0600)
# ═════════════════════════════════════════════════════════════════════════════
def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))  # copie profonde du défaut
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        # Pas de 2>/dev/null silencieux : on remonte une erreur explicite.
        raise RuntimeError(f"ERROR:ucloud:index illisible {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"ERROR:ucloud:index malformé (pas un objet) {path}")
    return data


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Écrit `data` en JSON de façon atomique, en mode 0600.

    tmp (même répertoire → même FS) → fsync → os.replace, atomique POSIX :
    aucun lecteur ne peut observer un index tronqué.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, _DIR_MODE)
    except OSError:
        pass
    # pid + uuid : deux threads du même process ne partagent jamais un tmp.
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # Compact : l'index est réécrit en entier à chaque PUT, l'indentation
            # et le tri des clés coûtaient ~2× en CPU et en octets sur 50 000
            # entrées, sans aucun lecteur humain.
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
        os.chmod(path, _FILE_MODE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ═════════════════════════════════════════════════════════════════════════════
# API index / keyring
# ═════════════════════════════════════════════════════════════════════════════
def empty_index(owner_hex: str = "") -> Dict[str, Any]:
    return {
        "version": INDEX_VERSION,
        "owner_hex": owner_hex,
        "updated_at": int(time.time()),
        "entries": {},
    }


def load_index(email: str) -> Dict[str, Any]:
    """TOUT l'index (O(n)) sous la forme historique {version, owner_hex,
    updated_at, entries}, lu depuis SQLite. Réservé aux appelants qui parcourent
    tout (statistiques, sauvegarde, scripts) ; les chemins chauds utilisent
    `db_entry`, `db_children`, `ingest_many`… (une ligne, pas tout l'index)."""
    ensure_ucloud_dir(email)
    data = ucloud_db.load_all(ucloud_dir(email))
    data["version"] = INDEX_VERSION
    return data


def save_index(email: str, index: Dict[str, Any]) -> None:
    """Enregistre un index complet : diff des lignes changées/supprimées (O(n)).
    Chemin HISTORIQUE (`load_index` → modifier → `save_index`, sous `index_lock`) ;
    les chemins chauds écrivent une ligne via `ucloud_db.transaction`."""
    ensure_ucloud_dir(email)
    ucloud_db.save_all(ucloud_dir(email), index)


def load_keyring(email: str) -> Dict[str, Any]:
    """{"<cid>": {"key_hex": "<64 hex>"}, ...} depuis SQLite (O(n))."""
    ensure_ucloud_dir(email)
    return ucloud_db.load_keyring_all(ucloud_dir(email))


def save_keyring(email: str, keyring: Dict[str, Any]) -> None:
    ensure_ucloud_dir(email)
    ucloud_db.save_keyring_all(ucloud_dir(email), keyring)


def db(email: str):
    """Connexion SQLite du thread courant (lecture) pour `email`."""
    ensure_ucloud_dir(email)
    return ucloud_db.connect(ucloud_dir(email))


def db_entry(email: str, path: str) -> Optional[Dict[str, Any]]:
    """UNE entrée (ou None) — la racine « / » est un dossier implicite."""
    p = normalize_path(path)
    if p == "/":
        return {"type": "dir", "mtime": ucloud_db.updated_at(ucloud_dir(email))}
    return ucloud_db.get_entry(db(email), p)


def db_children(email: str, path: str) -> List[str]:
    return ucloud_db.children(db(email), normalize_path(path))


def db_present_hashes(email: str, hashes: List[str]) -> List[str]:
    """Empreintes SHA-256 déjà présentes dans le cloud (octets reçus OU image stockée)."""
    clean = [h.lower() for h in hashes if re.fullmatch(r"[0-9a-fA-F]{64}", h or "")]
    return ucloud_db.present_hashes(ensure_ucloud_dir(email), clean)


# ─────────────────────────────────────────────────────────────────────────────
# Cache DISQUE des miniatures de photos — chiffré, comme celui des visages
# ─────────────────────────────────────────────────────────────────────────────
# Les miniatures de la galerie n'avaient qu'un cache MÉMOIRE (perdu à chaque
# redémarrage, 3000 entrées) : chaque visite redécodait des centaines de photos de
# plusieurs Mo. Ici : JPEG chiffré AES-256-GCM (même codec UENC, clé propre au compte,
# 0600 à côté du trousseau) — jamais de clair sur disque, instantané ensuite.
def _tcache_key(email: str) -> str:
    path = ucloud_dir(email) / "thumbcache.key"
    try:
        key = path.read_text().strip()
        if len(key) == 64:
            return key
    except OSError:
        pass
    ensure_ucloud_dir(email)
    key = os.urandom(32).hex()
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


def _tcache_path(email: str, cid: str, size: int) -> Path:
    name = hashlib.sha256(f"photo|{cid}|{size}".encode()).hexdigest()[:40]
    return ucloud_dir(email) / "thumbcache" / f"{name}.uenc"


def thumb_cache_get(email: str, cid: str, size: int) -> Optional[bytes]:
    path = _tcache_path(email, cid, size)
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    try:
        return uenc_codec.decrypt_aes256gcm(blob, _tcache_key(email))
    except Exception:
        path.unlink(missing_ok=True)              # altéré / clé changée : on recalculera
        return None


def thumb_cache_put(email: str, cid: str, size: int, jpeg: bytes) -> None:
    path = _tcache_path(email, cid, size)
    payload, _iv = uenc_codec.encrypt_aes256gcm(jpeg, _tcache_key(email))
    path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, path)


def cids_referenced_elsewhere(email: str, cids: List[str]) -> set:
    """Parmi `cids`, ceux qu'un AUTRE compte de CETTE station référence encore.

    Depuis que le partage garde le MÊME bloc chiffré (même CID, même clé) dans le cloud
    du destinataire, plusieurs comptes d'une station peuvent référencer un CID : le démon
    IPFS n'a qu'UN épinglage par CID, donc le dépingler parce que l'un des comptes supprime
    sa copie priverait les autres de leur photo. Lecture seule des bases voisines."""
    import sqlite3
    wanted = [c for c in dict.fromkeys(cids) if c]
    found: set = set()
    if not wanted or not GAME_NOSTR_PATH.is_dir():
        return found
    for dbfile in GAME_NOSTR_PATH.glob(f"*/{UCLOUD_DIRNAME}/{ucloud_db.DB_NAME}"):
        if dbfile.parent.parent.name == email:
            continue
        try:
            conn = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            continue
        try:
            for i in range(0, len(wanted), 500):
                chunk = [c for c in wanted[i:i + 500] if c not in found]
                if not chunk:
                    continue
                q = ",".join("?" * len(chunk))
                found.update(r[0] for r in conn.execute(f"SELECT cid FROM entries WHERE cid IN ({q})", chunk))
        except sqlite3.Error as exc:
            logger.warning("ucloud: base voisine illisible %s : %s", dbfile, exc)
        finally:
            conn.close()
    return found


def ipfs_pin(cid: str, timeout: float = 300.0) -> bool:
    """Épingle `cid` sur CE démon (récupère le bloc auprès du swarm s'il manque). Best-effort."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(_ipfs_api("pin/add"), params={"arg": cid, "progress": "false"})
        if resp.status_code == 200:
            return True
        logger.warning("ucloud: ipfs pin add %s → HTTP %s: %s", cid, resp.status_code, resp.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("ucloud: ipfs pin add %s injoignable: %s", cid, exc)
    return False


def unpin_cids(cids: List[str], email: Optional[str] = None) -> List[str]:
    """Dépingle ces CID (devenus orphelins : clé détruite). Best-effort. Avec `email`, un CID
    encore référencé par un autre compte de la station reste épinglé (cf. ci-dessus)."""
    if email:
        keep = cids_referenced_elsewhere(email, cids)
        if keep:
            logger.info("ucloud: %d CID gardé(s) épinglé(s) : partagés avec un autre compte", len(keep))
        cids = [c for c in cids if c not in keep]
    done = [c for c in cids if ipfs_unpin(c)]
    if done:
        logger.info("ucloud: %d CID dépinglé(s) (clé détruite, plus référencé)", len(done))
    return done


# ─────────────────────────────────────────────────────────────────────────────
# Lecture « tout l'index » mémoïsée
# ─────────────────────────────────────────────────────────────────────────────
# Pour les rares lecteurs qui parcourent TOUT (liste de la galerie, doublons à
# l'import) : l'objet est PARTAGÉ tant que la révision SQLite (`meta.rev`, qui
# change à chaque écriture) est la même. ⚠️ Lecture seule : ne JAMAIS modifier.
_RO_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_RO_CACHE_MAX = 6          # ≈ 3 comptes actifs (index + keyring)
_RO_LOCK = threading.Lock()


def _ro(kind: str, email: str, loader: Callable[[str], Dict[str, Any]]) -> Dict[str, Any]:
    udir = ucloud_dir(email)
    ensure_ucloud_dir(email)
    revision = ucloud_db.rev(udir)        # relevée AVANT la lecture : au pire un rechargement de trop
    key = (kind, email)
    with _RO_LOCK:
        hit = _RO_CACHE.get(key)
        if hit and hit[0] == revision:
            _RO_CACHE.move_to_end(key)
            return hit[1]
    obj = loader(email)
    with _RO_LOCK:
        _RO_CACHE[key] = (revision, obj)
        _RO_CACHE.move_to_end(key)
        while len(_RO_CACHE) > _RO_CACHE_MAX:
            _RO_CACHE.popitem(last=False)
    return obj


def index_ro(email: str) -> Dict[str, Any]:
    """Index complet en lecture seule, mémoïsé (cf. ci-dessus)."""
    return _ro("index", email, load_index)


def keyring_ro(email: str) -> Dict[str, Any]:
    return _ro("keyring", email, load_keyring)


def get_key_hex(email: str, cid: str) -> Optional[str]:
    """Clé AES-256 (hex) associée à un CID, ou None si absente du keyring
    (une requête indexée, pas tout le keyring)."""
    return ucloud_db.key_get(db(email), cid)


def cipher_limit(entry: Dict[str, Any]) -> int:
    """Borne de lecture IPFS pour UNE entrée : la taille chiffrée CONNUE de
    l'index (un import peut stocker plus de 20 Mo, que le PUT refuserait mais
    qu'il faut pouvoir relire — GET /dav, miniatures, partage)."""
    return max(MAX_FILE_SIZE + UENC_OVERHEAD, int(entry.get("size_cid") or 0) + 4096)


# ═════════════════════════════════════════════════════════════════════════════
# Manipulation des chemins DAV
# ═════════════════════════════════════════════════════════════════════════════
def normalize_path(path: str) -> str:
    """Normalise un chemin DAV en forme canonique : '/a/b', racine = '/'.

    Rejette toute tentative de traversée ('..'), même si wsgidav normalise déjà
    en amont : l'index est la frontière de confiance de ce module.
    """
    if not path:
        return "/"
    p = "/" + path.strip("/")
    if p == "/":
        return "/"
    parts: List[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError(f"ERROR:ucloud:chemin invalide (traversée): {path!r}")
        parts.append(seg)
    return "/" + "/".join(parts)


def parent_path(path: str) -> str:
    p = normalize_path(path)
    if p == "/":
        return "/"
    head = p.rsplit("/", 1)[0]
    return head if head else "/"


def base_name(path: str) -> str:
    p = normalize_path(path)
    return "" if p == "/" else p.rsplit("/", 1)[1]


def get_entry(index: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    """Entrée d'index pour un chemin. La racine '/' est un dossier implicite."""
    p = normalize_path(path)
    if p == "/":
        return {"type": "dir", "mtime": index.get("updated_at", 0)}
    entry = index["entries"].get(p)
    return entry if isinstance(entry, dict) else None


def list_children(index: Dict[str, Any], path: str) -> List[str]:
    """Noms des membres DIRECTS de la collection `path` (sans doublon).

    Les dossiers intermédiaires implicites (une entrée `/a/b/c.txt` sans entrée
    `/a/b`) sont matérialisés ici : un PROPFIND reste cohérent même si le
    dossier n'a jamais été créé explicitement par MKCOL.
    """
    p = normalize_path(path)
    prefix = "/" if p == "/" else p + "/"
    names: List[str] = []
    seen = set()
    for key in index["entries"]:
        if not key.startswith(prefix) or key == p:
            continue
        rest = key[len(prefix):]
        if not rest:
            continue
        name = rest.split("/", 1)[0]
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return sorted(names)


def build_children_map(index: Dict[str, Any]) -> Dict[str, List[str]]:
    """{dossier: [noms des membres directs]} pour TOUT l'index, en UN parcours.

    `list_children` est O(n) par dossier : un PROPFIND `Depth: infinity` (ou
    une résolution de dossier implicite) devenait O(n × dossiers). Ici les
    ancêtres implicites sont matérialisés comme dans `list_children`."""
    children: Dict[str, set] = {}
    for key in index["entries"]:
        if not key.startswith("/") or key == "/":
            continue
        parts = key.strip("/").split("/")
        cur = ""
        for seg in parts:
            parent = cur or "/"
            children.setdefault(parent, set()).add(seg)
            cur = f"{cur}/{seg}"
    return {d: sorted(names) for d, names in children.items()}


def ensure_parent_dirs(index: Dict[str, Any], path: str) -> None:
    """Crée les entrées `dir` manquantes pour tous les ancêtres de `path`."""
    p = normalize_path(path)
    if p == "/":
        return
    segs = p.strip("/").split("/")[:-1]
    now = int(time.time())
    cur = ""
    for seg in segs:
        cur = f"{cur}/{seg}"
        existing = index["entries"].get(cur)
        if existing is None:
            index["entries"][cur] = {"type": "dir", "mtime": now}
        elif existing.get("type") != "dir":
            raise ValueError(f"ERROR:ucloud:{cur} existe déjà et n'est pas un dossier")


def remove_subtree(index: Dict[str, Any], path: str) -> List[Dict[str, Any]]:
    """Supprime `path` et tous ses descendants. Retourne les entrées ôtées."""
    p = normalize_path(path)
    prefix = p + "/" if p != "/" else "/"
    removed: List[Dict[str, Any]] = []
    for key in [k for k in index["entries"] if k == p or k.startswith(prefix)]:
        removed.append(index["entries"].pop(key))
    return removed


def subtree_paths(index: Dict[str, Any], path: str) -> List[str]:
    """Chemins des FICHIERS de `path` et de tout son sous-arbre (à relever AVANT
    `remove_subtree`/déplacement, qui ne rendent pas les clés)."""
    p = normalize_path(path)
    prefix = p + "/" if p != "/" else "/"
    return [k for k, e in index["entries"].items()
            if (k == p or k.startswith(prefix)) and isinstance(e, dict) and e.get("type") == "file"]


def prune_keyring(index: Dict[str, Any], keyring: Dict[str, Any]) -> Dict[str, Any]:
    """Retire du keyring les clés dont plus aucune entrée ne référence le CID.

    Appelé après DELETE/MOVE : une clé orpheline est un secret conservé sans
    raison. On ne supprime que si AUCUNE entrée ne pointe encore vers ce CID
    (une COPY partage le même CID et donc la même clé).
    """
    live = {
        e.get("cid")
        for e in index["entries"].values()
        if isinstance(e, dict) and e.get("cid")
    }
    return {cid: v for cid, v in keyring.items() if cid in live}


# ═════════════════════════════════════════════════════════════════════════════
# Catalogue de visages Qdrant (`faces_<hex>`) — suivre les chemins du cloud
# ═════════════════════════════════════════════════════════════════════════════
# Les points de visages référencent la photo par `source_path`. Sans ces helpers
# un MOVE/renommage DAV cassait toutes les vignettes de la table de tri (404) et
# une suppression laissait des visages fantômes (rangés, comptés, proposés à
# l'envoi) dont la photo n'existe plus. Best-effort : JAMAIS d'exception — un
# Qdrant absent ne doit pas faire échouer une opération sur les fichiers.
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
_QDRANT_BATCH = 200


def _qdrant_headers() -> Dict[str, str]:
    try:
        for line in (Path.home() / ".zen" / "ai-company" / ".env").read_text().splitlines():
            if line.startswith("QDRANT_API_KEY="):
                return {"api-key": line.split("=", 1)[1].strip()}
    except OSError:
        pass
    return {}


def _faces_collection_for(email: str) -> Optional[str]:
    hex_pk = hex_for_email(email)
    return f"faces_{hex_pk}" if hex_pk else None


def _qdrant_post(collection: str, route: str, body: Dict[str, Any]) -> bool:
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(f"{QDRANT_URL}/collections/{collection}/{route}",
                               params={"wait": "true"}, headers=_qdrant_headers(), json=body)
        if resp.status_code == 404:      # pas de catalogue de visages : rien à faire
            return True
        if resp.status_code != 200:
            logger.warning("ucloud: Qdrant %s/%s → HTTP %s: %s", collection, route,
                           resp.status_code, resp.text[:200])
            return False
        return True
    except httpx.HTTPError as exc:
        logger.warning("ucloud: Qdrant injoignable (%s) : %s", collection, exc)
        return False


def faces_forget_paths(email: str, paths: List[str]) -> None:
    """Oublie les visages catalogués depuis ces photos (supprimées/remplacées)."""
    paths = [p for p in dict.fromkeys(paths) if p]
    collection = _faces_collection_for(email) if paths else None
    if not collection:
        return
    for i in range(0, len(paths), _QDRANT_BATCH):
        chunk = paths[i:i + _QDRANT_BATCH]
        _qdrant_post(collection, "points/delete",
                     {"filter": {"must": [{"key": "source_path", "match": {"any": chunk}}]}})


def faces_follow_moves(email: str, moves: Dict[str, str]) -> None:
    """Met à jour `source_path` des visages quand leurs photos changent de chemin."""
    collection = _faces_collection_for(email) if moves else None
    if not collection:
        return
    for old, new in moves.items():
        if old != new:
            _qdrant_post(collection, "points/payload",
                         {"payload": {"source_path": new},
                          "filter": {"must": [{"key": "source_path", "match": {"value": old}}]}})


# ═════════════════════════════════════════════════════════════════════════════
# Client IPFS minimal (blobs DÉJÀ chiffrés uniquement)
# ═════════════════════════════════════════════════════════════════════════════
class IPFSError(RuntimeError):
    """Erreur de communication avec le daemon IPFS local."""


def _ipfs_api(path: str) -> str:
    return f"{IPFS_API.rstrip('/')}/api/v0/{path.lstrip('/')}"


def ipfs_add_bytes(payload: bytes, filename: str = "blob.uenc", pin: bool = True) -> str:
    """Ajoute `payload` (chiffré) à IPFS et retourne son CID.

    `cid-version=0` pour rester cohérent avec le reste de l'écosystème Astroport
    (CIDv0 `Qm…`, ceux que manipulent les scripts bash existants).
    """
    params = {
        "pin": "true" if pin else "false",
        "cid-version": "0",
        "wrap-with-directory": "false",
        "quieter": "true",
    }
    files = {"file": (filename, payload, "application/octet-stream")}
    try:
        with httpx.Client(timeout=IPFS_TIMEOUT) as client:
            resp = client.post(_ipfs_api("add"), params=params, files=files)
    except httpx.HTTPError as exc:
        raise IPFSError(f"ERROR:ucloud:ipfs add injoignable ({IPFS_API}): {exc}") from exc

    if resp.status_code != 200:
        raise IPFSError(f"ERROR:ucloud:ipfs add HTTP {resp.status_code}: {resp.text[:300]}")

    # `add` peut retourner plusieurs lignes JSON (NDJSON) ; le dernier objet
    # porteur d'un Hash est celui du fichier ajouté.
    cid: Optional[str] = None
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("Hash"):
            cid = obj["Hash"]
    if not cid:
        raise IPFSError(f"ERROR:ucloud:ipfs add sans Hash exploitable: {resp.text[:300]}")
    logger.debug("ucloud: ipfs add OK cid=%s (%d octets chiffrés)", cid, len(payload))
    return cid


def ipfs_cat(cid: str, max_bytes: Optional[int] = None) -> bytes:
    """Récupère le blob chiffré `cid`, borné à `max_bytes` (défaut MAX_FILE_SIZE
    + UENC_OVERHEAD ; pour un fichier importé plus gros, passer `cipher_limit(entry)`).

    La borne est appliquée EN COURS DE STREAMING : un CID pointant sur un objet
    énorme ne peut pas faire exploser la RAM du process UPassport.
    """
    limit = MAX_FILE_SIZE + UENC_OVERHEAD if max_bytes is None else max_bytes
    chunks: List[bytes] = []
    total = 0
    try:
        with httpx.Client(timeout=IPFS_TIMEOUT) as client:
            with client.stream("POST", _ipfs_api("cat"), params={"arg": cid}) as resp:
                if resp.status_code != 200:
                    resp.read()
                    raise IPFSError(
                        f"ERROR:ucloud:ipfs cat HTTP {resp.status_code} cid={cid}: "
                        f"{resp.text[:300]}"
                    )
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise IPFSError(
                            f"ERROR:ucloud:ipfs cat cid={cid} dépasse la limite "
                            f"({limit} octets)"
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise IPFSError(f"ERROR:ucloud:ipfs cat injoignable cid={cid}: {exc}") from exc
    return b"".join(chunks)


def ipfs_unpin(cid: str) -> bool:
    """Retire l'épinglage d'un CID (best-effort, ne lève JAMAIS) : un échec
    d'unpin ne doit jamais faire échouer une suppression utilisateur — ce qui
    compte réellement pour la confidentialité, c'est la clé AES détruite
    (cf. `unpin_orphaned_cids`), pas la libération d'espace disque IPFS.
    Ne force pas de garbage collection (`ipfs repo gc`) : c'est une opération
    globale et coûteuse, à laisser au rythme de maintenance de la station,
    pas à chaque suppression."""
    try:
        with httpx.Client(timeout=IPFS_TIMEOUT) as client:
            resp = client.post(_ipfs_api("pin/rm"), params={"arg": cid})
        if resp.status_code == 200:
            return True
        logger.warning("ucloud: ipfs pin rm %s → HTTP %s: %s", cid, resp.status_code, resp.text[:200])
        return False
    except httpx.HTTPError as exc:
        logger.warning("ucloud: ipfs pin rm %s injoignable: %s", cid, exc)
        return False


def unpin_orphaned_cids(old_keyring: Dict[str, Any], new_keyring: Dict[str, Any]) -> List[str]:
    """CIDs présents dans `old_keyring` mais plus dans `new_keyring` (après
    `prune_keyring`, donc plus référencés par aucune entrée de l'index) :
    leur clé de déchiffrement est détruite, leur bloc IPFS ne sert plus à
    rien dans ce cloud — on en retire l'épinglage pour que `ipfs repo gc`
    puisse un jour récupérer l'espace. Retourne les CIDs réellement
    dépinglés (un échec individuel n'empêche pas les autres)."""
    orphaned = set(old_keyring) - set(new_keyring)
    unpinned = [cid for cid in orphaned if ipfs_unpin(cid)]
    if unpinned:
        logger.info("ucloud: %d CID dépinglé(s) (clé détruite, plus référencé)", len(unpinned))
    return unpinned


# ═════════════════════════════════════════════════════════════════════════════
# Cache éphémère du CLAIR (0600) + purge TTL
# ═════════════════════════════════════════════════════════════════════════════
def ensure_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CACHE_DIR, _DIR_MODE)
    except OSError as exc:  # pragma: no cover
        logger.warning("ucloud: chmod 0700 impossible sur %s: %s", CACHE_DIR, exc)
    return CACHE_DIR


class EphemeralPlaintext:
    """Fichier temporaire 0600 contenant du CLAIR, auto-détruit à la sortie."""

    def __init__(self, data: bytes, suffix: str = ".bin") -> None:
        ensure_cache_dir()
        # mkstemp crée déjà en 0600 ; on force explicitement pour ne dépendre
        # d'aucun umask.
        fd, name = tempfile.mkstemp(
            prefix=_CACHE_PREFIX, suffix=suffix, dir=str(CACHE_DIR)
        )
        self.path = Path(name)
        try:
            os.fchmod(fd, _FILE_MODE)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except BaseException:
            self.purge()
            raise
        self._stream: Optional[BinaryIO] = None

    def open(self) -> BinaryIO:
        self._stream = open(self.path, "rb")
        return self._stream

    def purge(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover
            logger.error("ucloud: purge du cache impossible %s: %s", self.path, exc)

    def __enter__(self) -> "EphemeralPlaintext":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.purge()


class _SelfClosingReader:
    """Flux qui purge le fichier éphémère à `close()`.

    wsgidav lit le flux rendu par `get_content()` puis appelle `close()` : c'est
    le seul point où l'on sait que la réponse a été servie. On y accroche donc
    la destruction du clair.
    """

    def __init__(self, ephemeral: EphemeralPlaintext) -> None:
        self._eph = ephemeral
        self._fh = ephemeral.open()

    def read(self, size: int = -1) -> bytes:
        return self._fh.read(size)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._fh.seek(offset, whence)

    def tell(self) -> int:
        return self._fh.tell()

    def __iter__(self):
        return iter(self._fh)

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            self._eph.purge()


def plaintext_stream(data: bytes, suffix: str = ".bin") -> io.BytesIO:
    """Flux lisible du clair, EN MÉMOIRE uniquement.

    Le clair est déjà entier en RAM (AES-GCM one-shot) : l'écrire en plus dans
    ~/.zen/tmp/ucloud_cache n'apportait rien qu'un clair sur disque (non chiffré,
    survivant à un crash jusqu'au TTL) et un thread de purge. `EphemeralPlaintext`
    et `purge_stale_cache` restent pour d'éventuels appelants externes."""
    return io.BytesIO(data)


def purge_stale_cache(ttl: Optional[int] = None) -> int:
    """Supprime les fichiers de cache plus vieux que `ttl` secondes."""
    ttl = CACHE_TTL if ttl is None else ttl
    if not CACHE_DIR.exists():
        return 0
    now = time.time()
    removed = 0
    for entry in CACHE_DIR.iterdir():
        if not entry.name.startswith(_CACHE_PREFIX) or not entry.is_file():
            continue
        try:
            if now - entry.stat().st_mtime < ttl:
                continue
            entry.unlink()
            removed += 1
            logger.warning(
                "ucloud: cache clair résiduel purgé par TTL: %s "
                "(un bloc finally n'a pas fait son travail)",
                entry.name,
            )
        except FileNotFoundError:
            continue
        except OSError as exc:  # pragma: no cover
            logger.error("ucloud: purge TTL impossible %s: %s", entry, exc)
    return removed


class _PurgeThread(threading.Thread):
    """Thread daemon qui appelle `purge_stale_cache()` toutes les TTL/2 s."""

    def __init__(self, interval: Optional[float] = None) -> None:
        super().__init__(name="ucloud-cache-purge", daemon=True)
        self.interval = interval or max(5.0, CACHE_TTL / 2)
        self._stop_event = threading.Event()

    def run(self) -> None:  # pragma: no cover - boucle infinie
        ensure_cache_dir()
        while not self._stop_event.wait(self.interval):
            try:
                purge_stale_cache()
            except Exception as exc:
                logger.error("ucloud: purge TTL en échec: %s", exc)

    def stop(self) -> None:  # pragma: no cover
        self._stop_event.set()


_purge_thread: Optional[_PurgeThread] = None


def start_purge_thread() -> Optional[_PurgeThread]:
    """Démarre (une seule fois) le thread de purge du cache éphémère."""
    global _purge_thread
    if _purge_thread is not None and _purge_thread.is_alive():
        return _purge_thread
    _purge_thread = _PurgeThread()
    _purge_thread.start()
    return _purge_thread


# ═════════════════════════════════════════════════════════════════════════════
# Résolution EMAIL ↔ HEX (.secret.nostr) et token DAV
# ═════════════════════════════════════════════════════════════════════════════
# Format écrit par Astroport.ONE/tools/make_NOSTRCARD.sh (chmod 600) :
#     NSEC=nsec1…; NPUB=npub1…; HEX=<64 hex>;
# On ne lit QUE les champs publics (HEX/NPUB) : ce module n'a aucun besoin de
# signer quoi que ce soit, le NSEC n'est jamais chargé en mémoire.
def _parse_secret_nostr(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.error("ucloud: lecture impossible de %s: %s", path, exc)
        return out
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            k = k.strip().upper()
            if k in ("HEX", "NPUB"):      # le NSEC n'est jamais conservé en mémoire
                out[k] = v.strip()
    raw = ""                              # et la chaîne brute est lâchée aussitôt
    return out


_HEX_MAP: Dict[str, str] = {}
_HEX_MAP_AT = 0.0
_HEX_MAP_TTL = 300.0
_HEX_MAP_LOCK = threading.Lock()


def hex_for_email(email: str) -> Optional[str]:
    """HEX pubkey MULTIPASS de `email`, lu depuis .secret.nostr."""
    path = nostr_dir(email) / ".secret.nostr"
    if not path.exists():
        return None
    fields = _parse_secret_nostr(path)
    hex_pub = (fields.get("HEX") or "").lower()
    if len(hex_pub) == 64 and all(c in "0123456789abcdef" for c in hex_pub):
        return hex_pub
    npub = fields.get("NPUB")
    if not npub:
        return None
    from utils.crypto import npub_to_hex

    return npub_to_hex(npub)


def email_for_hex(hex_pubkey: str) -> Optional[str]:
    """Recherche inverse HEX → EMAIL (scan ~/.zen/game/nostr/*/.secret.nostr).

    Même pattern de scan que routers/identity.py::_find_email_by_npub.
    """
    target = (hex_pubkey or "").lower()
    if len(target) != 64:
        return None
    if not GAME_NOSTR_PATH.is_dir():
        return None
    # Carte hex → email mise en cache : chaque requête NIP-98 relisait TOUS les
    # `.secret.nostr` de la station (O(comptes) lectures disque). Un hex inconnu
    # force une reconstruction (au plus 1 par 5 s) pour voir un compte créé
    # depuis ; une entrée en cache est revalidée (le compte a pu disparaître).
    now = time.time()
    with _HEX_MAP_LOCK:
        email = _HEX_MAP.get(target)
        fresh = now - _HEX_MAP_AT < _HEX_MAP_TTL
    if email and fresh and (nostr_dir(email) / ".secret.nostr").exists():
        return email
    with _HEX_MAP_LOCK:
        if email is None and now - _HEX_MAP_AT < 5.0:
            return None
    mapping: Dict[str, str] = {}
    for secret in GAME_NOSTR_PATH.glob("*/.secret.nostr"):
        name = secret.parent.name
        if "@" not in name:
            continue
        h = hex_for_email(name)
        if h:
            mapping[h] = name
    with _HEX_MAP_LOCK:
        _HEX_MAP.clear()
        _HEX_MAP.update(mapping)
        globals()["_HEX_MAP_AT"] = time.time()
    return mapping.get(target)


def load_dav_token(email: str) -> Optional[Dict[str, Any]]:
    """Descripteur du token : {"token", "hex", "npub", "created_at"} ou None."""
    path = token_path(email)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("ucloud: dav_token illisible pour %s: %s", email, exc)
        return None
    if not isinstance(data, dict) or not data.get("token"):
        logger.error("ucloud: dav_token malformé pour %s", email)
        return None
    return data


def create_dav_token(email: str, hex_pubkey: str) -> str:
    """Génère et persiste un nouveau token DAV (0600). Révoque le précédent.

    Le token est un secret opaque de 256 bits : les clients DAV standards
    (Nautilus, Finder, davfs2, Explorateur Windows) ne savent pas signer un
    event NOSTR par requête, ils ont besoin d'un mot de passe Basic Auth. Sa
    délivrance, elle, exige une preuve de possession du nsec MULTIPASS
    (NIP-98/NIP-42, cf. routers/cloud.py).
    """
    from utils.crypto import hex_to_npub

    ensure_ucloud_dir(email)
    token = os.urandom(32).hex()
    payload = {
        "token": token,
        "hex": hex_pubkey,
        "npub": hex_to_npub(hex_pubkey) or "",
        "created_at": int(time.time()),
    }
    _write_json_atomic(token_path(email), payload)
    logger.info("ucloud: token DAV créé pour %s (hex %s…)", email, hex_pubkey[:16])
    return token


def revoke_dav_token(email: str) -> bool:
    try:
        token_path(email).unlink()
        logger.info("ucloud: token DAV révoqué pour %s", email)
        return True
    except FileNotFoundError:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# Sources WebDAV externes — import quotidien depuis un cloud tiers
# ═════════════════════════════════════════════════════════════════════════════
# Permet d'alimenter régulièrement son propre FaceCloud depuis un dossier d'un
# AUTRE serveur WebDAV (NextCloud, ownCloud, une autre station Astroport…) —
# cf. webdav_import.py (script batch, invoqué par
# RUNTIME/NOSTRCARD.refresh.sh) et routers/cloud.py (CRUD + parcours de
# dossiers pour le sélecteur de ucloud.html). Même discipline que dav_token :
# JSON en clair, 0600, écriture atomique — la frontière de confiance est le
# filesystem local de la station (ce ne sont QUE des identifiants vers un
# serveur tiers, jamais la clé MULTIPASS elle-même).

def load_webdav_sources(email: str) -> List[Dict[str, Any]]:
    """[{id, label, url, username, password, remote_path, dest_prefix,
    created_at, last_sync}] — `last_sync` est None tant qu'aucun import n'a
    encore tourné, sinon {"at", "imported", "skipped", "errors"}."""
    path = webdav_sources_path(email)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("ucloud: webdav_sources illisible pour %s: %s", email, exc)
        return []
    sources = data.get("sources") if isinstance(data, dict) else None
    return sources if isinstance(sources, list) else []


def save_webdav_sources(email: str, sources: List[Dict[str, Any]]) -> None:
    """Écriture atomique. Les appelants qui MODIFIENT la liste doivent tenir
    `sources_lock` autour du couple lecture/écriture (cf. ci-dessous)."""
    _write_json_atomic(webdav_sources_path(email), {"sources": sources})  # type: ignore[arg-type]


def add_webdav_source(email: str, label: str, url: str, username: str, password: str,
                       remote_path: str, dest_prefix: str) -> Dict[str, Any]:
    source_id = os.urandom(8).hex()
    with sources_lock(email):
        sources = load_webdav_sources(email)
        # Le dossier de destination doit être SOUS /Photos/ (jamais /Contacts,
        # /Reçues…) et propre à CETTE source : deux sources de même libellé
        # fusionnaient leurs compteurs et leurs noms de fichiers.
        dest_prefix = sanitize_dest_prefix(dest_prefix, source_id,
                                           [s.get("dest_prefix") for s in sources])
        entry = {
            "id": source_id,
            "label": label,
            "url": url,
            "username": username,
            "password": password,
            "remote_path": remote_path,
            "dest_prefix": dest_prefix,
            "created_at": int(time.time()),
            "last_sync": None,
        }
        sources.append(entry)
        save_webdav_sources(email, sources)
    logger.info("ucloud: source WebDAV '%s' ajoutée pour %s", label, email)
    return entry


def sanitize_dest_prefix(requested: str, source_id: str, taken: List[Optional[str]]) -> str:
    """Dossier de destination d'une source : normalisé, sous /Photos/, unique."""
    try:
        p = normalize_path(requested or "")
    except ValueError:
        p = "/"
    if p == "/Photos" or not p.startswith("/Photos/"):
        p = f"/Photos/Import_{source_id[:6]}"
    used = {t.rstrip("/") for t in taken if t}
    if p in used:
        p = f"{p}_{source_id[:6]}"
    return p


_EDITABLE_SOURCE_FIELDS = ("label", "url", "username", "password", "remote_path")


def get_webdav_source(email: str, source_id: str) -> Optional[Dict[str, Any]]:
    return next((s for s in load_webdav_sources(email) if s.get("id") == source_id), None)


def update_webdav_source(email: str, source_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Modifie une source (libellé, adresse, identifiant, mot de passe, dossier
    distant) sous `sources_lock`. Une valeur None/absente n'est pas modifiée (un
    mot de passe vide = on garde l'actuel). `id`, `dest_prefix` et `created_at`
    ne changent jamais : les photos déjà importées restent rattachées à leur
    dossier. Rend la source mise à jour, ou None si elle n'existe pas."""
    with sources_lock(email):
        sources = load_webdav_sources(email)
        for src in sources:
            if src.get("id") != source_id:
                continue
            moved = False
            for key in _EDITABLE_SOURCE_FIELDS:
                value = changes.get(key)
                if value is None or (key == "password" and value == ""):
                    continue
                if key in ("url", "remote_path", "username") and src.get(key) != value:
                    moved = True
                src[key] = value
            if moved:
                src["last_sync"] = None          # autre serveur/dossier : l'ancien bilan ne vaut plus
            save_webdav_sources(email, sources)
            logger.info("ucloud: source WebDAV %s modifiée pour %s", source_id, email)
            return src
    return None


def remove_webdav_source(email: str, source_id: str) -> bool:
    with sources_lock(email):
        sources = load_webdav_sources(email)
        kept = [s for s in sources if s.get("id") != source_id]
        if len(kept) == len(sources):
            return False
        save_webdav_sources(email, kept)
    logger.info("ucloud: source WebDAV %s supprimée pour %s", source_id, email)
    return True


def record_webdav_sync(email: str, source_id: str, summary: Dict[str, Any]) -> None:
    """Écrit le résumé du dernier import (`imported`/`skipped`/`errors`/`at`)
    — appelé par webdav_import.py en fin de passage, lu par l'UI pour
    afficher l'état de chaque source sans rejouer l'import. Sous
    `sources_lock` : sans lui, un ajout de source depuis l'UI pendant l'import
    quotidien était écrasé par la liste périmée lue au début."""
    with sources_lock(email):
        sources = load_webdav_sources(email)
        touched = False
        for s in sources:
            if s.get("id") == source_id:
                s["last_sync"] = summary
                touched = True
        if touched:
            save_webdav_sources(email, sources)


def webdav_import_lock_path(email: str, source_id: str) -> Path:
    return ucloud_dir(email) / f".webdav_import_{source_id}.lock"


def verify_basic_credentials(user_name: str, password: str) -> Optional[str]:
    """Valide un couple Basic Auth `email:dav_token`. Retourne l'email ou None.

    Comparaison à temps constant (hmac.compare_digest) : pas d'oracle de timing
    permettant de deviner le token octet par octet.
    """
    if not user_name or not password:
        return None
    # Un nom d'utilisateur contenant '/' ou '..' ne doit jamais devenir un chemin.
    if "/" in user_name or ".." in user_name:
        logger.warning("ucloud: nom d'utilisateur rejeté (chemin): %r", user_name)
        return None
    desc = load_dav_token(user_name)
    if not desc:
        return None
    # En bytes : `compare_digest(str, str)` lève TypeError sur un caractère non
    # ASCII (→ 500 au lieu de 401) et le mot de passe vient du client.
    if hmac.compare_digest(str(desc["token"]).encode("utf-8"), password.encode("utf-8")):
        return user_name
    return None


_GENERIC_MIMES = {"", "application/octet-stream", "binary/octet-stream",
                  "application/x-unknown", "application/unknown"}
_JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".localized", "ehthumbs.db"}
_MAX_DECODE_PIXELS = 100_000_000      # garde anti « bombe de décompression »


def is_junk_name(name: str) -> bool:
    """Fichiers parasites que Finder / l'Explorateur posent sur un disque monté
    (`._IMG.jpg` AppleDouble, `.DS_Store`, `Thumbs.db`…). Stockés tels quels
    (le client s'attend à ce que le PUT réussisse) mais JAMAIS traités comme
    des photos : un `._IMG.jpg` déclenchait FaceID et restait « en attente »."""
    n = (name or "").lower()
    return n in _JUNK_NAMES or n.startswith("._")


def _sniff_mime(data: bytes) -> Optional[str]:
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"GIF8":
        return "image/gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heim", b"heis"):
        return "image/heic"
    return None


def resolve_content_type(path: str, declared: Optional[str], data: bytes = b"") -> str:
    """Mime d'un fichier. Le type DÉCLARÉ par le client est ignoré s'il est
    générique (rclone, davfs2, gvfs envoient `application/octet-stream` : la
    photo n'était alors ni orientée, ni géolocalisée, ni analysée, ni listée
    dans la galerie) : on devine par l'extension, puis par les octets."""
    mime = (declared or "").split(";")[0].strip().lower()
    if mime in _GENERIC_MIMES:
        mime = (mimetypes.guess_type(path)[0] or "").lower()
        if mime == "" or mime in _GENERIC_MIMES:
            ext = os.path.splitext(path)[1].lower()
            mime = {".heic": "image/heic", ".heif": "image/heif"}.get(ext, "")
        if not mime and data:
            mime = _sniff_mime(data) or ""
    return mime or "application/octet-stream"


def _exif_degrees(value) -> float:
    d, m, s = (float(x) for x in value)
    return d + m / 60.0 + s / 3600.0


def compute_phash(plaintext: bytes) -> Optional[str]:
    """Empreinte perceptuelle dHash 64 bits (16 hex) de l'image, REDRESSÉE selon son EXIF :
    la même photo, redimensionnée ou recompressée, a une empreinte à quelques bits près
    (distance de Hamming) — contrairement au SHA-256, qui change au moindre octet.
    Décodage JPEG réduit (`draft`) : ~quelques ms même sur du 12 Mpx. None si illisible."""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(plaintext)) as img:
            if img.width * img.height > _MAX_DECODE_PIXELS:
                return None
            img.draft("L", (160, 160))
            img = ImageOps.exif_transpose(img)
            small = img.convert("L").resize((9, 8), Image.LANCZOS)
        px = list(small.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
        return f"{bits:016x}"
    except Exception as exc:
        logger.debug("ucloud: phash impossible : %s", exc)
        return None


def _inspect_image(plaintext: bytes) -> Dict[str, Any]:
    """Lit UNE fois l'image ORIGINALE (avant toute normalisation, qui ré-encode
    et perd l'EXIF) : {"ok", "orientation", "geo", "taken_at"}. Best effort."""
    out: Dict[str, Any] = {"ok": False, "orientation": 1, "geo": None, "taken_at": None}
    try:
        from PIL import Image, ExifTags
        from PIL.ExifTags import GPSTAGS
    except ImportError:
        out["ok"] = True              # pas de Pillow : on ne bloque rien
        return out
    try:
        with Image.open(io.BytesIO(plaintext)) as img:
            if img.width * img.height > _MAX_DECODE_PIXELS:
                return out
            out["ok"] = True
            exif = img.getexif()
            if not exif:
                return out
            out["orientation"] = exif.get(0x0112, 1) or 1
            ifd = exif.get_ifd(ExifTags.IFD.Exif) if hasattr(ExifTags, "IFD") else {}
            raw = ifd.get(0x9003) or ifd.get(0x9004) or exif.get(0x0132)
            if isinstance(raw, str) and len(raw) >= 19:
                try:
                    out["taken_at"] = int(time.mktime(time.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")))
                except (ValueError, OverflowError):
                    pass
            gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo) if hasattr(ExifTags, "IFD") else None
            if gps_ifd:
                gps = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
                lat_dms, lat_ref = gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")
                lon_dms, lon_ref = gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")
                if lat_dms and lon_dms and lat_ref and lon_ref:
                    lat, lon = _exif_degrees(lat_dms), _exif_degrees(lon_dms)
                    if str(lat_ref).upper().startswith("S"):
                        lat = -lat
                    if str(lon_ref).upper().startswith("W"):
                        lon = -lon
                    # NaN (dénominateur nul) / hors bornes : JSON invalide ou absurde.
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                        # Grille UMAP (0.01°) — même arrondi que geo.py::get_my_gps_coordinates
                        out["geo"] = {"lat": round(lat, 2), "lon": round(lon, 2),
                                      "umap_key": f"{lat:.2f},{lon:.2f}"}
    except Exception as exc:
        logger.debug("ucloud: inspection image échouée : %s", exc)
        if not out["ok"]:
            return out
    return out


def _extract_gps_umap(plaintext: bytes) -> Optional[Dict[str, Any]]:
    """EXIF GPS → {"lat", "lon", "umap_key"} ou None (compat). Appelé au PUT,
    jamais via le Brain : aucune coordonnée ne transite par un job d'analyse."""
    return _inspect_image(plaintext)["geo"]


def _normalize_image_orientation(plaintext: bytes, content_type: str) -> bytes:
    """Ré-encode `plaintext` à l'endroit si un tag EXIF Orientation ≠ 1 est
    présent — sinon retourne `plaintext` inchangé (pas de recompression).
    Appelé ICI, avant chiffrement, pour que l'image STOCKÉE soit canoniquement
    à l'endroit (ComfyUI normalise l'orientation avant détection : sans cela le
    bbox ne correspondait pas au pixel stocké, cf. incident du 2026-10-03).

    Le ré-encodage CONSERVE l'EXIF (sans l'Orientation) et le profil ICC : il
    les perdait tous deux (GPS, date de prise de vue, couleurs P3 des iPhone).
    Best effort : toute image sans rotation ou illisible ressort inchangée."""
    if not content_type.startswith("image/") or content_type == "image/svg+xml":
        return plaintext
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(plaintext)) as img:
            if img.width * img.height > _MAX_DECODE_PIXELS:
                return plaintext
            orientation = img.getexif().get(0x0112, 1)
            if orientation in (0, 1):
                return plaintext
            fmt = img.format or "JPEG"
            if fmt not in ("JPEG", "PNG", "WEBP"):
                return plaintext              # HEIF… : on ne ré-encode pas un format que l'on ne maîtrise pas
            icc = img.info.get("icc_profile")
            transposed = ImageOps.exif_transpose(img)
            if transposed is None:
                return plaintext
            exif = transposed.getexif()
            exif.pop(0x0112, None)        # désormais « à l'endroit »
            if fmt == "JPEG" and transposed.mode not in ("RGB", "L"):
                transposed = transposed.convert("RGB")
            kwargs: Dict[str, Any] = {"exif": exif.tobytes()}
            if icc:
                kwargs["icc_profile"] = icc
            if fmt == "JPEG":
                kwargs["quality"] = 95
            buf = io.BytesIO()
            try:
                transposed.save(buf, format=fmt, **kwargs)
            except Exception:             # format sans EXIF/ICC : repli sans
                buf = io.BytesIO()
                transposed.save(buf, format=fmt, **({"quality": 95} if fmt == "JPEG" else {}))
            return buf.getvalue()
    except Exception as exc:
        logger.debug("ucloud: normalisation orientation EXIF échouée : %s", exc)
        return plaintext


def _free_path_db(conn, path: str, hashes: set) -> str:
    """Version SQLite de `_free_path` : le nom libre est choisi dans la MÊME
    transaction que l'écriture (pas de course entre deux envois simultanés)."""
    def free(p: str) -> bool:
        e = ucloud_db.get_entry(conn, p)
        if e is None:
            return True
        return e.get("type") == "file" and bool(hashes & {e.get("sha256_plain"), e.get("sha256_orig")})
    if free(path):
        return path
    stem, dot, ext = path.rpartition(".")
    if not dot or "/" in ext:
        stem, ext = path, ""
    else:
        ext = "." + ext
    n = 2
    while not free(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


def _free_path(idx: Dict[str, Any], path: str, hashes: set) -> str:
    """`path` s'il est libre (ou déjà occupé par ce MÊME contenu), sinon
    `<nom>-2.<ext>`, `-3`… Appelé SOUS index_lock : le choix du nom et
    l'écriture sont atomiques (deux envois simultanés d'IMG_0001.jpg
    s'écrasaient l'un l'autre, CID et clé du premier détruits)."""
    def free(p: str) -> bool:
        e = idx["entries"].get(p)
        if e is None:
            return True
        return isinstance(e, dict) and e.get("type") == "file" and bool(
            hashes & {e.get("sha256_plain"), e.get("sha256_orig")})
    if free(path):
        return path
    stem, dot, ext = path.rpartition(".")
    if not dot or "/" in ext:
        stem, ext = path, ""
    else:
        ext = "." + ext
    n = 2
    while not free(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


class _Prepared:
    """Un fichier prêt à être commité : chiffré, blob déjà sur IPFS, index PAS
    encore touché (cf. `ingest_many`)."""
    __slots__ = ("path", "cid", "key_hex", "iv_hex", "payload_len", "plaintext_len", "content_type",
                 "sha_orig", "sha_plain", "info", "decodable", "is_image", "junk", "extra",
                 "unique", "trigger", "target_pubkey", "target_name", "entry", "replaced_path")


def _prepare_ingest(path: str, plaintext: bytes, content_type: Optional[str],
                    target_pubkey: Optional[str], target_name: Optional[str],
                    trigger_faceid: bool, extra: Optional[Dict[str, Any]], unique: bool) -> _Prepared:
    """Partie coûteuse et SANS verrou : inspection EXIF, orientation, AES-GCM,
    `ipfs add`. Ne touche ni l'index ni le keyring."""
    pr = _Prepared()
    pr.path = normalize_path(path)
    pr.content_type = resolve_content_type(pr.path, content_type, plaintext)
    pr.junk = is_junk_name(base_name(pr.path))
    pr.is_image = pr.content_type.startswith("image/") and not pr.junk

    # Hash des octets REÇUS : sert à reconnaître un contenu déjà importé même
    # si l'image stockée a été ré-encodée (rotation EXIF) et donc change de hash.
    pr.sha_orig = hashlib.sha256(plaintext).hexdigest()
    pr.info = {"ok": True, "geo": None, "taken_at": None, "phash": None}
    if pr.is_image:
        # EXIF lu AVANT normalisation (qui ré-encode), GPS jamais envoyé au Brain.
        pr.info = _inspect_image(plaintext)
        pr.info["phash"] = compute_phash(plaintext) if pr.info["ok"] else None
        plaintext = _normalize_image_orientation(plaintext, pr.content_type)
    pr.decodable = pr.info["ok"] or pr.content_type in ("image/heic", "image/heif")

    # Clé AES-256 ALÉATOIRE PAR FICHIER (la compromission d'un fichier partagé
    # ne doit pas donner accès à tout le cloud).
    pr.key_hex = os.urandom(32).hex()
    payload, pr.iv_hex = uenc_codec.encrypt_aes256gcm(plaintext, pr.key_hex)
    # Upload du blob DÉJÀ CHIFFRÉ. IPFS ne voit jamais le clair.
    pr.cid = ipfs_add_bytes(payload, filename=f"{base_name(pr.path)}.uenc")
    pr.payload_len, pr.plaintext_len = len(payload), len(plaintext)
    pr.sha_plain = hashlib.sha256(plaintext).hexdigest()
    pr.extra, pr.unique, pr.trigger = extra, unique, trigger_faceid
    pr.target_pubkey, pr.target_name = target_pubkey, target_name
    pr.entry, pr.replaced_path = None, None
    return pr


def ingest_many(email: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ingère plusieurs fichiers avec UNE SEULE lecture + écriture de l'index et du
    keyring (et un seul verrou).

    Chaque écriture réécrit l'index en entier (≈ 24 Mo à 50 000 photos : ~0,8 s
    de lecture+écriture+fsync, 5 à 10× plus sur un RPi) : un import de masse
    fichier par fichier était quadratique. Groupés par lots, le coût d'index est
    divisé par la taille du lot. `items` = dicts de paramètres de
    `ingest_plaintext` (`path`, `plaintext`, + options). Tout-ou-rien côté index :
    si l'écriture échoue, TOUS les blobs du lot sont dépinglés."""
    # Espace du MULTIPASS (10 Go, 128 Go avec une ZenCard) : refusé AVANT de chiffrer et d'épingler quoi que ce soit.
    incoming = sum(len(it["plaintext"]) for it in items)
    storage_quota.check(nostr_dir(email), incoming)
    prepared: List[_Prepared] = []
    try:
        for it in items:
            prepared.append(_prepare_ingest(
                it["path"], it["plaintext"], it.get("content_type"), it.get("target_pubkey"),
                it.get("target_name"), it.get("trigger_faceid", True), it.get("extra"),
                bool(it.get("unique", False))))
    except BaseException:
        for pr in prepared:               # blobs déjà ajoutés, jamais référencés
            ipfs_unpin(pr.cid)
        raise
    if not prepared:
        return []

    now = int(time.time())
    udir = ucloud_dir(email)
    ensure_ucloud_dir(email)
    candidates: List[str] = []            # CID remplacés / écrasés, à dépingler s'ils sont orphelins
    try:
        with index_lock(email):
            with ucloud_db.transaction(udir) as conn:
                for pr in prepared:
                    path = pr.path
                    if pr.unique:
                        path = _free_path_db(conn, path, {pr.sha_orig, pr.sha_plain})
                    previous = ucloud_db.get_entry(conn, path) or {}
                    if previous.get("type") == "file" and previous.get("cid"):
                        candidates.append(previous["cid"])
                        if previous.get("sha256_orig", previous.get("sha256_plain")) != pr.sha_orig:
                            pr.replaced_path = path
                    ucloud_db.ensure_parents(conn, path, now)
                    entry = {
                        "type": "file",
                        "cid": pr.cid,
                        "enc": ENC_LABEL,
                        "iv_hex": pr.iv_hex,
                        "mime": pr.content_type,
                        "size_plain": pr.plaintext_len,
                        "size_cid": pr.payload_len,
                        "sha256_plain": pr.sha_plain,
                        "sha256_orig": pr.sha_orig,
                        "mtime": now,
                        "created_at": previous.get("created_at", now),
                    }
                    if pr.info["taken_at"]:
                        entry["taken_at"] = pr.info["taken_at"]
                    if pr.info.get("phash"):
                        entry["phash"] = pr.info["phash"]        # détection des quasi-doublons
                    if pr.is_image and not pr.decodable:
                        # Pas une image lisible (ou bombe de décompression) : jamais
                        # envoyée à FaceID, et pas comptée « en attente » à vie.
                        entry["faceid"] = {"status": "unreadable", "checked_at": now}
                    if pr.extra:
                        entry.update(pr.extra)
                    if pr.info["geo"]:
                        entry["geo"] = pr.info["geo"]
                        logger.info("ucloud: GPS EXIF extrait pour %s (umap=%s)", base_name(path),
                                    pr.info["geo"]["umap_key"])
                    # Clé AVANT l'entrée : l'index ne référence jamais une clé absente.
                    ucloud_db.key_set(conn, pr.cid, {"key_hex": pr.key_hex})
                    ucloud_db.put_entry(conn, path, entry)
                    pr.path, pr.entry = path, entry
                # Clés devenues orphelines (ancien CID remplacé, ou CID du lot écrasé
                # par un autre fichier du lot) : retirées, puis dépinglées plus bas.
                orphans = ucloud_db.prune_keys(conn, candidates + [pr.cid for pr in prepared])
    except BaseException:
        # Les blobs sont épinglés mais aucune entrée ni clé ne les référence :
        # sans ce dépin ils resteraient sur IPFS, orphelins, pour toujours.
        for pr in prepared:
            ipfs_unpin(pr.cid)
        raise
    # Hors du verrou (I/O réseau IPFS).
    storage_quota.bump(nostr_dir(email), incoming)
    unpin_cids(orphans, email)
    faces_forget_paths(email, [pr.replaced_path for pr in prepared if pr.replaced_path])

    results: List[Dict[str, Any]] = []
    for pr in prepared:
        logger.info("ucloud: ingest %s → cid=%s (%d octets clairs / %d chiffrés) [%s]",
                    pr.path, pr.cid, pr.plaintext_len, pr.payload_len, email)
        if pr.trigger and pr.is_image and pr.decodable:
            _trigger_faceid_analysis(email, pr.path, pr.cid, pr.key_hex,
                                     target_pubkey=pr.target_pubkey, target_name=pr.target_name)
        res = dict(pr.entry)
        res["path"] = pr.path
        results.append(res)
    return results


def ingest_plaintext(email: str, path: str, plaintext: bytes,
                      content_type: Optional[str] = None,
                      target_pubkey: Optional[str] = None,
                      target_name: Optional[str] = None,
                      trigger_faceid: bool = True,
                      extra: Optional[Dict[str, Any]] = None,
                      unique: bool = False) -> Dict[str, Any]:
    """Chiffre `plaintext`, le pousse sur IPFS, met à jour index+keyring de
    `email`, et déclenche FaceID si c'est une image — le coeur de
    `UCloudFileResource._commit_plaintext()`, extrait pour être appelable hors
    du contexte DAV/wsgidav (import en masse, cf. `cloud_import.py`). Retourne
    l'entrée d'index créée/mise à jour, plus la clé `path` (chemin FINAL).
    Pour plusieurs fichiers d'un coup, `ingest_many` (un seul aller-retour d'index).

    `path` est normalisé ici. `unique=True` : si le chemin est pris par un AUTRE
    contenu, un suffixe `-2`… est choisi SOUS le verrou (import, partage) au lieu
    d'écraser ; un PUT DAV garde `unique=False` (écraser est son sens).

    `trigger_faceid=False` + `extra` : copie REÇUE d'un autre compte (cf.
    `share_copy()`) — champs d'origine ajoutés à l'entrée, et surtout PAS
    d'analyse FaceID (ping-pong infini entre amis réciproques)."""
    return ingest_many(email, [{
        "path": path, "plaintext": plaintext, "content_type": content_type,
        "target_pubkey": target_pubkey, "target_name": target_name,
        "trigger_faceid": trigger_faceid, "extra": extra, "unique": unique,
    }])[0]


MAX_DESC = 1200


def set_photo_description(email: str, path: str, text: str, src: str = "ai", model: str = "") -> str:
    """Enregistre la description d'une photo (`entry.desc = {text, src: ai|user, at, model?}`), cherchable.
    Rend « set » | « cleared » (texte vide : description retirée) | « kept_user » (une description saisie à la
    main n'est JAMAIS remplacée par l'IA) | « missing » (photo introuvable). Le texte de l'utilisateur prime."""
    text = " ".join((text or "").split())[:MAX_DESC]
    result = {"r": "missing"}

    def patch(e: Dict[str, Any]) -> bool:
        cur = e.get("desc") if isinstance(e.get("desc"), dict) else {}
        e.pop("desc_job", None)
        if cur.get("src") == "user" and src != "user":
            result["r"] = "kept_user"
            return True
        if not text:
            e.pop("desc", None)
            result["r"] = "cleared"
        else:
            e["desc"] = {"text": text, "src": src, "at": int(time.time())}
            if model:
                e["desc"]["model"] = model
            result["r"] = "set"
        return True
    with index_lock(email):
        ucloud_db.patch_entry(ucloud_dir(email), normalize_path(path), patch)
    return result["r"]


def ensure_folder(email: str, path: str) -> str:
    """Crée le dossier `path` (et ses parents) s'il n'existe pas — idempotent, sans erreur s'il est déjà là (MKCOL, lui, répond 405).
    Rend le chemin normalisé. Un fichier de ce nom → ValueError."""
    p = normalize_path(path)
    ensure_ucloud_dir(email)
    with index_lock(email):
        with ucloud_db.transaction(ucloud_dir(email)) as conn:
            cur = ucloud_db.get_entry(conn, p)
            if cur is not None and cur.get("type") != "dir":
                raise ValueError(f"ERROR:ucloud:{p} existe déjà et n'est pas un dossier")
            if cur is None:
                ucloud_db.ensure_parents(conn, p)
                ucloud_db.put_entry(conn, p, {"type": "dir", "mtime": int(time.time())})
    return p


def read_plaintext(email: str, path: str) -> tuple:
    """(clair, mime) d'un fichier du cloud de `email` — déchiffré EN MÉMOIRE,
    jamais écrit sur disque. Lève FileNotFoundError / RuntimeError."""
    entry = db_entry(email, path)
    if not entry or entry.get("type") != "file" or not entry.get("cid"):
        raise FileNotFoundError(f"ERROR:ucloud:{path} introuvable pour {email}")
    key_hex = get_key_hex(email, entry["cid"])
    if not key_hex:
        raise RuntimeError(f"ERROR:ucloud:clé absente du keyring pour {path}")
    # Borne = taille chiffrée CONNUE de l'index (pas MAX_FILE_SIZE) : un import
    # WebDAV peut légitimement stocker une photo > 20 Mo (panorama, RAW
    # converti), que le PUT /dav/ aurait refusée mais qu'il faut pouvoir relire.
    plaintext = uenc_codec.decrypt_aes256gcm(ipfs_cat(entry["cid"], max_bytes=cipher_limit(entry)), key_hex)
    return plaintext, (entry.get("mime") or "application/octet-stream")


def _safe_segment(name: str, fallback: str = "Ami") -> str:
    """Nom lisible utilisable comme segment de chemin DAV (pas de / ni ..)."""
    cleaned = "".join(c for c in (name or "") if c not in '/\\:*?"<>|').strip(" .")
    return cleaned[:60] or fallback


_SHARED_FIELDS = ("type", "cid", "enc", "iv_hex", "mime", "size_plain", "size_cid", "sha256_plain", "sha256_orig", "phash", "taken_at")


def adopt_blob(recipient_email: str, source: Dict[str, Any], key_hex: str, name: str,
               from_hex: str, from_name: str = "", verify: bool = False) -> Dict[str, Any]:
    """Range dans le cloud de `recipient_email` une photo reçue SANS la rechiffrer : MÊME bloc
    chiffré (même CID), MÊME clé — `source` = l'entrée d'index de l'expéditeur, `key_hex` sa clé.

    Pourquoi : rechiffrer avec une clé neuve par destinataire créait autant de CID que de
    copies pour une même photo (autant de blocs à stocker, à épingler, à transférer, et aucune
    redondance : chaque station n'avait que SA copie). Avec un secret unifié, tous les comptes
    qui reçoivent la photo référencent le même CID : un seul bloc sur IPFS, épinglé par chaque
    station qui le détient — vraie répartition des copies.

    Compromis assumé : la clé d'UNE photo est connue de l'expéditeur et de ses destinataires
    (qui ont de toute façon le clair), jamais celle des autres fichiers ; et deux comptes qui
    partagent une photo deviennent corrélables (même CID). Le CID n'est dépinglé que lorsque
    PLUS aucun compte ne le référence (`cids_referenced_elsewhere`).

    `verify=True` (reçu d'une AUTRE station, donc non fiable) : le bloc est récupéré, déchiffré
    avec la clé annoncée et son empreinte contrôlée avant d'être rangé ; les métadonnées sont
    alors RECALCULÉES, jamais prises sur parole. Épingle le bloc sur ce démon.
    Retourne {"status": "copied"|"duplicate"|"blocked", "path": ...}."""
    if from_hex and from_hex.lower() in blocked_senders(recipient_email):
        return {"status": "blocked", "path": ""}
    cid = str(source.get("cid") or "")
    if not cid or not re.fullmatch(r"[0-9a-f]{64}", key_hex or ""):
        raise ValueError("ERROR:ucloud:partage sans CID ou sans clé")
    meta = {k: source[k] for k in _SHARED_FIELDS if source.get(k) is not None}
    if verify:
        blob = ipfs_cat(cid, max_bytes=max(MAX_FILE_SIZE * 3, int(source.get("size_cid") or 0) + 4096))
        plaintext = uenc_codec.decrypt_aes256gcm(blob, key_hex)       # lève si la clé ne correspond pas
        sha = hashlib.sha256(plaintext).hexdigest()
        claimed = source.get("sha256_plain") or source.get("sha")
        if claimed and claimed != sha:
            raise ValueError("ERROR:ucloud:empreinte différente de celle annoncée")
        meta.update({"type": "file", "cid": cid, "enc": ENC_LABEL, "iv_hex": blob[6:18].hex(),
                     "size_plain": len(plaintext), "size_cid": len(blob), "sha256_plain": sha,
                     "mime": resolve_content_type(name, source.get("mime"), plaintext)})
        meta.setdefault("sha256_orig", sha)
    hashes = [h for h in (meta.get("sha256_plain"), meta.get("sha256_orig")) if h]
    ensure_ucloud_dir(recipient_email)
    dup = ucloud_db.find_by_hashes(db(recipient_email), hashes) if hashes else None
    if dup:
        return {"status": "duplicate", "path": dup}
    # Espace du destinataire plein : la photo reste chez l'expéditeur (jamais perdue, jamais forcée).
    try:
        storage_quota.check(nostr_dir(recipient_email), int(meta.get("size") or meta.get("size_plain") or 0))
    except storage_quota.QuotaExceeded:
        return {"status": "quota", "path": ""}
    # Reçu d'une autre station : le bloc n'est pas chez nous, il faut l'épingler (sinon il
    # disparaît avec l'expéditeur). Même station : l'expéditeur l'a déjà épinglé sur ce démon.
    if verify and not ipfs_pin(cid):
        raise IPFSError(f"ERROR:ucloud:impossible d'épingler {cid}")

    now = int(time.time())
    safe_name = _safe_segment(name, "photo.jpg")
    sender = _safe_segment(from_name, from_hex[:8] or 'Ami')
    # Une vidéo reçue (vidéo FUN d'un ami) va dans /VideoFUN/Reçues/<expéditeur>/ : elle apparaît dans l'onglet Vidéos du destinataire.
    base = f"/VideoFUN/Reçues/{sender}" if str(meta.get("mime") or "").startswith("video/") else f"/Reçues/{sender}/{time.strftime('%Y/%m')}"
    with index_lock(recipient_email):
        with ucloud_db.transaction(ucloud_dir(recipient_email)) as conn:
            path = _free_path_db(conn, normalize_path(f"{base}/{safe_name}"), set(hashes))
            ucloud_db.ensure_parents(conn, path, now)
            entry = dict(meta, mtime=now, created_at=now, origin="facecloud-share",
                         from_hex=from_hex, from_name=from_name, received_at=now)
            ucloud_db.key_set(conn, cid, {"key_hex": key_hex})
            ucloud_db.put_entry(conn, path, entry)
    storage_quota.bump(nostr_dir(recipient_email), int(meta.get("size") or meta.get("size_plain") or 0))
    # Jamais d'analyse FaceID sur une copie reçue (ping-pong entre amis réciproques).
    return {"status": "copied", "path": path}


def share_copy(owner_email: str, source_path: str, recipient_email: str,
               from_hex: str, from_name: str = "") -> Dict[str, Any]:
    """Partage une photo du cloud de `owner_email` avec `recipient_email` (même station) :
    le destinataire référence le MÊME bloc chiffré, avec la même clé (`adopt_blob`) — aucun
    rechiffrement, aucun nouvel envoi sur IPFS —, rangé sous
    `/Reçues/<expéditeur>/YYYY/MM/<nom>`.

    C'est le canal UNIQUE de partage local FaceCloud — reconnaissance automatique
    (satellite_face_matcher.py via ucloud_share.py), envoi explicite (« Envoyer ses
    photos », routers/mailjet.py) et récupération après invitation.

    Idempotent par contenu : si le destinataire a déjà ce clair (même sha256, n'importe où
    dans son cloud), rien n'est recopié.
    Retourne {"status": "copied"|"duplicate"|"blocked", "path": ...}."""
    path = normalize_path(source_path)
    entry = db_entry(owner_email, path)
    if not entry or entry.get("type") != "file" or not entry.get("cid"):
        raise FileNotFoundError(f"ERROR:ucloud:{path} introuvable pour {owner_email}")
    key_hex = get_key_hex(owner_email, entry["cid"])
    if not key_hex:
        raise RuntimeError(f"ERROR:ucloud:clé absente du keyring pour {path}")
    res = adopt_blob(recipient_email, entry, key_hex, base_name(path), from_hex, from_name)
    if res["status"] == "copied":
        logger.info("ucloud: partage %s:%s → %s:%s (même CID %s…)", owner_email, source_path,
                    recipient_email, res["path"], entry["cid"][:12])
    if res["status"] in ("copied", "duplicate"):
        # Qui reçoit mes photos reçoit aussi mon empreinte de visage (une fois, cf. services/face_ref.py) :
        # sa station me reconnaîtra seule dans les siennes. Best-effort, jamais bloquant.
        try:
            from services import face_ref
            face_ref.give_local(owner_email, recipient_email, from_name)
        except Exception as exc:
            logger.warning("ucloud: empreinte non transmise (%s)", exc)
    return res


def blocked_senders(email: str) -> List[str]:
    """Clés (hex) dont `email` refuse les photos (« Ne plus recevoir de
    photos de X », FaceCloud). Appliqué dans ingest_received, donc à TOUS les
    canaux : copie locale, autre station, invitation."""
    try:
        data = json.loads((ucloud_dir(email) / "blocked_senders.json").read_text())
        return [str(h).lower() for h in data if isinstance(h, str)]
    except (OSError, ValueError):
        return []


def set_sender_blocked(email: str, hex_pubkey: str, blocked: bool) -> List[str]:
    hex_pubkey = (hex_pubkey or "").lower()
    with sources_lock(email):
        current = [h for h in blocked_senders(email) if h != hex_pubkey]
        if blocked and re.fullmatch(r"[0-9a-f]{64}", hex_pubkey):
            current.append(hex_pubkey)
        _write_json_atomic(ucloud_dir(email) / "blocked_senders.json", current)  # type: ignore[arg-type]
    return current


def ingest_received(recipient_email: str, plaintext: bytes, mime: str, name: str,
                    from_hex: str, from_name: str = "") -> Dict[str, Any]:
    """Range une photo REÇUE dans le cloud de `recipient_email` :
    `/Reçues/<expéditeur>/YYYY/MM/<nom>`, rechiffrée (clé neuve), SANS analyse
    FaceID (une copie reçue analysée renverrait la photo à l'expéditeur —
    ping-pong), idempotente par contenu. Commun à la copie entre comptes
    d'une même station (share_copy) et à la réception depuis une AUTRE
    station par le canal inter-NODE (services/node_share.py)."""
    if from_hex and from_hex.lower() in blocked_senders(recipient_email):
        return {"status": "blocked", "path": ""}
    plain_hash = hashlib.sha256(plaintext).hexdigest()
    ensure_ucloud_dir(recipient_email)
    dup = ucloud_db.find_by_hashes(db(recipient_email), [plain_hash])
    if dup:
        return {"status": "duplicate", "path": dup}

    stamp = time.strftime("%Y/%m")
    base = f"/Reçues/{_safe_segment(from_name, from_hex[:8] or 'Ami')}/{stamp}"
    name = _safe_segment(name, "photo.jpg")
    # `unique=True` : le suffixe -2… est choisi SOUS le verrou de l'index.
    try:
        res = ingest_plaintext(recipient_email, f"{base}/{name}", plaintext, content_type=mime,
                               trigger_faceid=False, unique=True,
                               extra={"origin": "facecloud-share", "from_hex": from_hex,
                                      "from_name": from_name, "received_at": int(time.time())})
    except storage_quota.QuotaExceeded:
        return {"status": "quota", "path": ""}
    return {"status": "copied", "path": res["path"]}


# Un import de 1000 photos lançait 1000 `bash` simultanés (un par image) : on
# borne à quelques lanceurs ; chaque processus est attendu (plus de zombies) et
# sa sortie d'erreur journalisée (elle partait dans /dev/null).
_FACEID_POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("UCLOUD_FACEID_TRIGGERS", "4")),
                                  thread_name_prefix="ucloud-faceid")
_FACEID_TRIGGER_TIMEOUT = 120


def _run_faceid_trigger(cmd: List[str], env: Dict[str, str], email: str, path: str) -> None:
    try:
        proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=_FACEID_TRIGGER_TIMEOUT, start_new_session=True)
        if proc.returncode != 0:
            logger.warning("ucloud: FaceID %s %s : trigger exit %s: %s", email, path,
                           proc.returncode, (proc.stderr or b"").decode("utf-8", "replace")[-300:])
    except subprocess.TimeoutExpired:
        logger.warning("ucloud: FaceID %s %s : trigger > %ss", email, path, _FACEID_TRIGGER_TIMEOUT)
    except Exception as exc:
        logger.warning("ucloud: déclenchement FaceID impossible pour %s: %s", email, exc)


def _trigger_faceid_analysis(email: str, path: str, cid: str, key_hex: str,
                              target_pubkey: Optional[str] = None,
                              target_name: Optional[str] = None) -> None:
    """Déclenche l'analyse FaceID sur une image tout juste PUT — en arrière-plan,
    best effort (ne doit jamais ralentir ni faire échouer la réponse DAV).

    `cid` référence le blob UENC CHIFFRÉ (jamais un lien en clair) : la clé
    voyage avec le job dans le DM NIP-44 déjà chiffré entre le Satellite et le
    Brain GPU — le Brain déchiffre en mémoire pour l'analyse, ne persiste
    jamais le clair, et ne renvoie que les embeddings. Voir
    tools/trigger_bro_vision_analysis.sh pour le détail du contrat.

    La clé AES passe par la VARIABLE D'ENVIRONNEMENT `UCLOUD_DECRYPTION_KEY`
    (lisible de son seul propriétaire dans /proc/<pid>/environ), JAMAIS en argv
    où tout processus local la voyait dans `ps`.

    `target_pubkey`/`target_name` (enrôlement supervisé) : transmis tels quels ;
    satellite_face_matcher.py cataloguera alors directement sous cette identité.
    """
    hex_pubkey = hex_for_email(email)
    if not hex_pubkey:
        logger.warning("ucloud: FaceID non déclenché pour %s — HEX introuvable", email)
        return
    try:   # choix du membre : visages désactivés ou dossier exclu → aucune analyse (le fichier reste simplement stocké)
        from services import analysis_prefs
        _p = analysis_prefs.load(ucloud_dir(email))
        if not _p["faces"] or analysis_prefs.is_excluded(ucloud_dir(email), path, _p):
            logger.info("ucloud: FaceID non déclenché pour %s — désactivé ou dossier exclu par le membre", path)
            return
    except Exception as exc:
        logger.warning("ucloud: préférences d'analyse illisibles pour %s (%s) — analyse par défaut", email, exc)
    trigger_sh = settings.TOOLS_PATH / "trigger_bro_vision_analysis.sh"
    if not trigger_sh.exists():
        logger.warning("ucloud: FaceID non déclenché — %s introuvable", trigger_sh)
        return
    # 5e argument vide : le script retombe sur UCLOUD_DECRYPTION_KEY.
    cmd = ["bash", str(trigger_sh), email, hex_pubkey, path, cid, ""]
    if target_pubkey:
        cmd += [target_pubkey, target_name or ""]
    env = dict(os.environ, UCLOUD_DECRYPTION_KEY=key_hex)
    try:
        _FACEID_POOL.submit(_run_faceid_trigger, cmd, env, email, path)
    except RuntimeError as exc:           # pool arrêté (fermeture du process)
        logger.warning("ucloud: déclenchement FaceID impossible pour %s: %s", email, exc)
        return
    logger.info("ucloud: FaceID en file pour %s %s (cid=%s…)", email, path, cid[:16])


# ═════════════════════════════════════════════════════════════════════════════
# Opérations d'arbre sur SQLite (suppression, déplacement) — partagées par DAV et l'API
# ═════════════════════════════════════════════════════════════════════════════
def delete_paths(email: str, paths: List[str], skip_readonly: bool = False):
    """Supprime chaque chemin ET son sous-arbre, détruit les clés devenues
    orphelines puis dépingle leurs CID et oublie les visages de ces photos.
    Rend (supprimés, ignorés_lecture_seule). Une transaction, O(supprimés)."""
    deleted: List[str] = []
    skipped: List[str] = []
    files: List[str] = []
    cids: List[str] = []
    ensure_ucloud_dir(email)
    with index_lock(email):
        with ucloud_db.transaction(ucloud_dir(email)) as conn:
            for p in paths:
                entry = ucloud_db.get_entry(conn, p)
                if entry is None:
                    continue
                if skip_readonly and entry.get("readonly"):
                    skipped.append(p)
                    continue
                for rp, re_ in ucloud_db.delete_subtree(conn, p):
                    if re_.get("type") == "file":
                        files.append(rp)
                    if re_.get("cid"):
                        cids.append(re_["cid"])
                deleted.append(p)
            # La clé est détruite (sans elle le blob IPFS n'est que du bruit chiffré)
            # ET le CID devenu orphelin est dépinglé, hors du verrou.
            orphans = ucloud_db.prune_keys(conn, cids)
    unpin_cids(orphans, email)
    faces_forget_paths(email, files)
    return deleted, skipped


def move_subtree(email: str, src: str, dest: str) -> Dict[str, str]:
    """Déplace `src` et tout son sous-arbre vers `dest` (une transaction).
    Rend {ancien chemin: nouveau chemin} des FICHIERS (pour les visages Qdrant)."""
    renames: Dict[str, str] = {}
    ensure_ucloud_dir(email)
    with index_lock(email):
        with ucloud_db.transaction(ucloud_dir(email)) as conn:
            rows = ucloud_db.subtree(conn, src)
            if not rows:
                raise DAVError(HTTP_NOT_FOUND, src)
            ucloud_db.ensure_parents(conn, dest)
            ucloud_db.delete_subtree(conn, src)
            for path, entry in rows:
                new = normalize_path(dest + path[len(src):])
                ucloud_db.put_entry(conn, new, entry)
                if entry.get("type") == "file":
                    renames[path] = new
    return renames


# ═════════════════════════════════════════════════════════════════════════════
# Couche DAV (wsgidav)
# ═════════════════════════════════════════════════════════════════════════════
from wsgidav import util  # noqa: E402
from wsgidav.dav_error import (  # noqa: E402
    HTTP_FORBIDDEN,
    HTTP_INSUFFICIENT_STORAGE,
    HTTP_INTERNAL_ERROR,
    HTTP_METHOD_NOT_ALLOWED,
    HTTP_NOT_FOUND,
    HTTP_PRECONDITION_FAILED,
    DAVError,
)
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider  # noqa: E402
from wsgidav.dc.base_dc import BaseDomainController  # noqa: E402
from wsgidav.mw.base_mw import BaseMiddleware  # noqa: E402

# Clé environ posée par le middleware NIP-98 APRÈS vérification Schnorr. Elle ne
# commence PAS par "HTTP_" : WSGI ne mappe les en-têtes client que vers des clés
# HTTP_*, donc aucun client ne peut la forger. Le middleware la purge malgré
# tout à l'entrée (ceinture + bretelles).
TRUSTED_USER_ENVIRON_KEY = "ucloud.auth.verified_user"
EMAIL_ENVIRON_KEY = "ucloud.email"


def current_email(environ) -> Optional[str]:
    """Email MULTIPASS authentifié pour la requête WSGI courante."""
    return environ.get(EMAIL_ENVIRON_KEY) or environ.get("wsgidav.auth.user_name") or None


class UCloudDomainController(BaseDomainController):
    """Contrôleur de domaine : Basic Auth `email:dav_token`.

    Le digest n'est PAS supporté : il exigerait de dériver un HA1 depuis le
    token, donc de le manipuler sous une autre forme. UPassport étant servi
    derrière TLS (nginx-proxy-manager), Basic est le bon choix.
    """

    def __init__(self, wsgidav_app=None, config=None):
        super().__init__(wsgidav_app, config)

    def __str__(self):
        return "cloud_storage.UCloudDomainController"

    def get_domain_realm(self, path_info, environ):
        return "UPlanet Cloud"

    def require_authentication(self, realm, environ):
        return True

    def supports_http_digest_auth(self):
        return False

    def basic_auth_user(self, realm, user_name, password, environ):
        email = verify_basic_credentials(user_name, password)
        if not email:
            logger.warning("ucloud: Basic Auth refusée pour %r", user_name)
            return False
        if environ is not None:
            environ["wsgidav.auth.user_name"] = email
            environ[EMAIL_ENVIRON_KEY] = email
        return True


class Nip98AuthMiddleware(BaseMiddleware):
    """Accepte `Authorization: Nostr <base64>` et résout le MULTIPASS associé.

    LA VÉRIFICATION EST DÉLÉGUÉE À `services.nostr._decode_and_verify_nip98_event`
    — le module de référence de UPassport (id NIP-01 + Schnorr BIP-340 réelle via
    utils.crypto.verify_nostr_event + fraîcheur). Aucune duplication de crypto
    ici : c'est précisément ce que l'intégration dans UPassport supprime.

    En cas de succès, pose `environ[TRUSTED_USER_ENVIRON_KEY] = email` et laisse
    HTTPAuthenticator court-circuiter Basic. En cas d'échec, ne pose rien : la
    requête retombe naturellement sur Basic Auth (401 + WWW-Authenticate).
    """

    def __init__(self, wsgidav_app, next_app, config):
        super().__init__(wsgidav_app, next_app, config)

    def __call__(self, environ, start_response):
        environ.pop(TRUSTED_USER_ENVIRON_KEY, None)  # anti-forge
        header = environ.get("HTTP_AUTHORIZATION", "")
        if header.startswith("Nostr "):
            try:
                from services.nostr import _decode_and_verify_nip98_event

                from services.nostr import check_nip98_binding

                event = _decode_and_verify_nip98_event(header)
                # SCRIPT_NAME (montage) + PATH_INFO = chemin vu par le client.
                req_path = (environ.get("SCRIPT_NAME", "") or "") + (environ.get("PATH_INFO", "") or "")
                check_nip98_binding(event, environ.get("REQUEST_METHOD", "GET"), req_path)
                hex_pubkey = str(event["pubkey"]).lower()
                email = email_for_hex(hex_pubkey)
                if email:
                    environ[TRUSTED_USER_ENVIRON_KEY] = email
                    environ[EMAIL_ENVIRON_KEY] = email
                    logger.info("ucloud: NIP-98 OK pour %s (%s…)", email, hex_pubkey[:16])
                else:
                    logger.warning(
                        "ucloud: NIP-98 valide mais pubkey %s… inconnue de cette station",
                        hex_pubkey[:16],
                    )
            except Exception as exc:
                logger.warning("ucloud: NIP-98 refusée: %s", exc)
        return self.next_app(environ, start_response)


# ─────────────────────────────────────────────────────────────────────────────
# Buffer d'écriture chiffrant
# ─────────────────────────────────────────────────────────────────────────────
class _EncryptingWriteBuffer:
    """Accumule le corps d'un PUT, puis chiffre et pousse sur IPFS à `close()`.

    POURQUOI UN BUFFER ET PAS UN STREAM CHIFFRANT : `AESGCM.encrypt()` est
    one-shot (cf. Astroport.ONE/tools/uenc_codec.py). Le format UENC v1 décrit
    un unique bloc GCM avec un seul tag d'authentification ; il n'y a pas de
    cadre de chunking défini. Un chiffrement incrémental impliquerait d'inventer
    un format multi-blocs — une divergence du contrat inter-projets UENC. D'où
    la limite MAX_FILE_SIZE (identique à media_upload::_UENC_MAX_FILE_SIZE).
    """

    def __init__(self, resource: "UCloudFileResource") -> None:
        self._res = resource
        self._chunks: List[bytes] = []
        self._size = 0
        self._closed = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("ERROR:ucloud:écriture sur un buffer fermé")
        self._size += len(data)
        if self._size > MAX_FILE_SIZE:
            self._chunks.clear()
            raise DAVError(
                HTTP_FORBIDDEN,
                f"Fichier trop volumineux (> {MAX_FILE_SIZE} octets). "
                f"Le cloud chiffré ne gère pas encore le chunking.",
            )
        self._chunks.append(data)
        return len(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        plaintext = b"".join(self._chunks)
        self._chunks.clear()
        self._res._commit_plaintext(plaintext)

    # writelines() n'est volontairement PAS exposé : on force le passage par
    # write() pour contrôler la borne de taille à chaque chunk.


# ─────────────────────────────────────────────────────────────────────────────
# Ressource fichier
# ─────────────────────────────────────────────────────────────────────────────
class UCloudFileResource(DAVNonCollection):
    """Fichier virtuel : métadonnées dans l'index, contenu chiffré sur IPFS."""

    def __init__(self, path: str, environ: dict, email: str, entry: Dict[str, Any]):
        super().__init__(path, environ)
        self.email = email
        self.entry = entry
        self.name = base_name(path)
        self._was_new = False  # positionné par create_empty_resource()

    # ── Propriétés live ──────────────────────────────────────────────────────
    def get_content_length(self) -> Optional[int]:
        size = self.entry.get("size_plain")
        if isinstance(size, int):
            return size
        # Un partage NIP-17 peut arriver sans taille connue (size_plain: null).
        # DAV tolère l'absence de getcontentlength ; on ne ment pas sur la taille.
        return None

    def get_content_type(self) -> Optional[str]:
        mime = self.entry.get("mime")
        if mime:
            return mime
        guessed, _ = mimetypes.guess_type(self.name)
        return guessed or "application/octet-stream"

    def get_creation_date(self) -> Optional[float]:
        return self.entry.get("created_at") or self.entry.get("received_at")

    def get_display_name(self) -> str:
        return self.name

    def get_last_modified(self):
        return self.entry.get("mtime") or self.entry.get("received_at") or 0

    def get_etag(self) -> Optional[str]:
        cid = self.entry.get("cid")
        mtime = self.entry.get("mtime") or self.entry.get("received_at") or 0
        # Le CID est un hash de contenu : ETag fort naturel. On y joint le mtime
        # pour distinguer deux entrées partageant un CID (COPY).
        return f"{cid}-{mtime}" if cid else None

    def support_etag(self) -> bool:
        return True

    def support_ranges(self) -> bool:
        # Un Range impliquerait de déchiffrer tout le blob pour n'en servir
        # qu'une tranche : possible, mais non annoncé pour l'instant.
        return False

    def is_link(self) -> bool:
        return False

    def _readonly(self) -> bool:
        return bool(self.entry.get("readonly"))

    # ── Lecture (GET) ────────────────────────────────────────────────────────
    def get_content(self):
        """Télécharge le blob chiffré, le déchiffre EN UNE FOIS, sert le clair.

        PAS DE STREAMING CRYPTO CHUNKÉ — c'est un choix de sécurité :
        `AESGCM.decrypt()` NE REND LE CLAIR QU'APRÈS avoir validé le tag
        d'authentification GCM sur la totalité du message. Un déchiffrement
        chunk-par-chunk rendrait au client des octets encore NON AUTHENTIFIÉS :
        si le blob avait été substitué sur IPFS, le client aurait déjà consommé
        du contenu forgé. D'où le blob entier en mémoire, borné par MAX_FILE_SIZE.
        """
        cid = self.entry.get("cid")
        if not cid:
            # Ressource créée par create_empty_resource() et jamais écrite.
            return plaintext_stream(b"", suffix=".empty")

        key_hex = get_key_hex(self.email, cid)
        if not key_hex:
            logger.error(
                "ucloud: aucune clé dans le keyring pour cid=%s (%s) — "
                "index et keyring désynchronisés",
                cid,
                self.path,
            )
            raise DAVError(
                HTTP_INTERNAL_ERROR,
                "Clé de déchiffrement absente du keyring pour cette ressource",
            )

        try:
            payload = ipfs_cat(cid, max_bytes=cipher_limit(self.entry))
        except IPFSError as exc:
            logger.error("ucloud: récupération IPFS échouée %s: %s", cid, exc)
            raise DAVError(HTTP_INTERNAL_ERROR, f"IPFS indisponible: {exc}") from exc

        try:
            plaintext = uenc_codec.decrypt_aes256gcm(payload, key_hex)
        except Exception as exc:
            # InvalidTag (clé fausse / blob altéré) ou ValueError (magic, version).
            # AUCUN octet de clair n'est renvoyé : AESGCM.decrypt() lève AVANT de
            # rendre quoi que ce soit. On répond 500, jamais un clair partiel.
            logger.error(
                "ucloud: déchiffrement refusé pour %s (cid=%s): %s: %s",
                self.path,
                cid,
                type(exc).__name__,
                exc,
            )
            raise DAVError(
                HTTP_INTERNAL_ERROR,
                f"Déchiffrement impossible ({type(exc).__name__}) — "
                f"clé invalide ou blob altéré",
            ) from exc

        suffix = os.path.splitext(self.name)[1] or ".bin"
        return plaintext_stream(plaintext, suffix=suffix)

    # ── Écriture (PUT) ───────────────────────────────────────────────────────
    def begin_write(self, *, content_type=None):
        if self._readonly():
            raise DAVError(
                HTTP_FORBIDDEN, "Ressource reçue par partage NIP-17 : lecture seule"
            )
        # `If-None-Match: *` : « crée, mais n'écrase pas » — l'interface s'en sert
        # pour ne pas remplacer une photo de même nom (IMG_0001.jpg d'un autre
        # appareil) ; réponse 412, le client renomme.
        if (self.environ.get("HTTP_IF_NONE_MATCH", "").strip() == "*"
                and self.entry.get("cid")):
            raise DAVError(HTTP_PRECONDITION_FAILED, "Un fichier de ce nom existe déjà")
        self._pending_content_type = content_type
        declared = self.environ.get("CONTENT_LENGTH", "")
        if declared.isdigit() and int(declared) > MAX_FILE_SIZE:
            # Refus AVANT de lire le corps (sinon on bufferise 20 Mo pour rien).
            raise DAVError(HTTP_FORBIDDEN, f"Fichier trop volumineux (> {MAX_FILE_SIZE} octets).")
        return _EncryptingWriteBuffer(self)

    def _commit_plaintext(self, plaintext: bytes) -> None:
        """Chiffre, pousse sur IPFS, met à jour index + keyring atomiquement.

        Coeur de la logique délégué à `ingest_plaintext()` (module-level) —
        réutilisé tel quel par `cloud_import.py` pour l'import en masse
        (NextCloud, archives) hors du contexte DAV/wsgidav."""
        content_type = getattr(self, "_pending_content_type", None)

        # Enrôlement supervisé (FaceCloud "Mon visage" / "Photos d'un
        # ami") : le client peut cibler explicitement une identité via ces
        # deux en-têtes, plutôt que de laisser l'auto-détection créer un
        # Inconnu_xxx. Absents = comportement automatique inchangé.
        target_pubkey = self.environ.get("HTTP_X_FACEID_TARGET_PUBKEY", "").strip().lower()
        # Nom percent-encodé par le client (un header HTTP ne porte que du Latin-1 :
        # « 李 » faisait échouer tout l'envoi) ; un nom brut reste accepté.
        target_name = unquote(self.environ.get("HTTP_X_FACEID_TARGET_NAME", "")).strip()
        if len(target_pubkey) != 64 or not all(c in "0123456789abcdef" for c in target_pubkey):
            target_pubkey = ""

        try:
            entry = ingest_plaintext(
                self.email, self.path, plaintext,
                content_type=content_type,
                target_pubkey=target_pubkey or None,
                target_name=target_name or None,
            )
        except storage_quota.QuotaExceeded as exc:
            raise DAVError(HTTP_INSUFFICIENT_STORAGE,
                           f"Espace épuisé ({storage_quota.human(exc.used)} utilisés, forfait {storage_quota.human(exc.quota)}) : "
                           "libérez de la place ou devenez parrain de l'infrastructure ẐEN.")

        self.entry = entry
        self._was_new = False
        logger.info(
            "ucloud: PUT %s → cid=%s (%d octets clairs / %d chiffrés)",
            self.path, entry["cid"], entry["size_plain"], entry["size_cid"],
        )

    def end_write(self, *, with_errors):
        """Notification post-PUT. En cas d'erreur, on annule l'entrée créée."""
        if with_errors and self._was_new:
            logger.warning("ucloud: PUT en erreur sur %s — rollback", self.path)
            try:
                with index_lock(self.email):
                    with ucloud_db.transaction(ucloud_dir(self.email)) as conn:
                        cur = ucloud_db.get_entry(conn, self.path)
                        if cur is not None and cur.get("cid") is None:
                            ucloud_db.delete_subtree(conn, self.path)
            except Exception as exc:  # pragma: no cover
                logger.error("ucloud: rollback impossible pour %s: %s", self.path, exc)

    # ── Suppression / déplacement ────────────────────────────────────────────
    def delete(self):
        if self._readonly():
            raise DAVError(HTTP_FORBIDDEN, "Ressource partagée : lecture seule")
        delete_paths(self.email, [self.path])
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)
        logger.info("ucloud: DELETE %s", self.path)

    def copy_move_single(self, dest_path, *, is_move):
        if is_move and self._readonly():
            raise DAVError(HTTP_FORBIDDEN, "Ressource partagée : lecture seule")
        dest = normalize_path(dest_path)
        now = int(time.time())
        ensure_ucloud_dir(self.email)
        with index_lock(self.email):
            with ucloud_db.transaction(ucloud_dir(self.email)) as conn:
                src = ucloud_db.get_entry(conn, self.path)
                if src is None:
                    raise DAVError(HTTP_NOT_FOUND, self.path)
                ucloud_db.ensure_parents(conn, dest, now)
                # COPY partage le même CID et donc la même clé : pas de
                # re-chiffrement, pas de nouvel upload. La copie devient modifiable.
                clone = dict(src)
                clone["mtime"] = now
                clone.pop("readonly", None)
                ucloud_db.put_entry(conn, dest, clone)
                if is_move:
                    ucloud_db.delete_subtree(conn, self.path)
        if is_move:
            faces_follow_moves(self.email, {self.path: dest})
        logger.info("ucloud: %s %s → %s", "MOVE" if is_move else "COPY", self.path, dest)

    def support_recursive_move(self, dest_path):
        return True

    def move_recursive(self, dest_path):
        self.copy_move_single(dest_path, is_move=True)

    def set_last_modified(self, dest_path, time_stamp, *, dry_run):
        secs = (
            time_stamp
            if isinstance(time_stamp, int)
            else util.parse_time_string(time_stamp)
        )
        if not dry_run:
            with index_lock(self.email):
                ucloud_db.patch_entry(ucloud_dir(self.email), self.path,
                                      lambda e: e.update(mtime=int(secs)) or True)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Ressource collection
# ─────────────────────────────────────────────────────────────────────────────
_GHOST_TTL = 3600  # secondes avant qu'un placeholder sans contenu soit purgé


class UCloudCollection(DAVCollection):
    """Dossier virtuel : dérivé des préfixes de chemins présents dans l'index."""

    def __init__(self, path: str, environ: dict, email: str, entry: Dict[str, Any]):
        super().__init__(path, environ)
        self.email = email
        self.entry = entry
        self.name = base_name(path) or "/"

    def get_display_name(self) -> str:
        return self.name

    def get_creation_date(self) -> Optional[float]:
        return self.entry.get("created_at") or self.entry.get("mtime")

    def get_last_modified(self):
        return self.entry.get("mtime") or 0

    def get_directory_info(self):
        return None

    def get_etag(self) -> Optional[str]:
        return None

    def support_etag(self) -> bool:
        return False

    def is_link(self) -> bool:
        return False

    def get_member_names(self) -> List[str]:
        return db_children(self.email, self.path)

    def get_member(self, name: str):
        return self.provider.get_resource_inst(util.join_uri(self.path, name), self.environ)

    # ── Écriture ─────────────────────────────────────────────────────────────
    def create_empty_resource(self, name: str):
        """Crée un placeholder (cid=None) ; le contenu arrive via begin_write().

        Appelé par wsgidav pour PUT sur un chemin inexistant, et pour LOCK sur
        une URL non mappée.
        """
        assert "/" not in name
        path = normalize_path(util.join_uri(self.path, name))
        now = int(time.time())
        entry = {
            "type": "file",
            "cid": None,
            "enc": ENC_LABEL,
            "iv_hex": None,
            "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size_plain": 0,
            "size_cid": 0,
            "sha256_plain": None,
            "mtime": now,
            "created_at": now,
        }
        ensure_ucloud_dir(self.email)
        with index_lock(self.email):
            with ucloud_db.transaction(ucloud_dir(self.email)) as conn:
                # Placeholders abandonnés (client coupé avant la fin du PUT, LOCK
                # sans écriture) : fichiers de 0 octet fantômes dans la galerie.
                conn.execute("DELETE FROM entries WHERE type='file' AND cid IS NULL AND mtime<?",
                             (now - _GHOST_TTL,))
                ucloud_db.ensure_parents(conn, path, now)
                ucloud_db.put_entry(conn, path, entry)
        res = UCloudFileResource(path, self.environ, self.email, entry)
        res._was_new = True
        return res

    def create_collection(self, name: str):
        assert "/" not in name
        path = normalize_path(util.join_uri(self.path, name))
        ensure_ucloud_dir(self.email)
        with index_lock(self.email):
            with ucloud_db.transaction(ucloud_dir(self.email)) as conn:
                if ucloud_db.get_entry(conn, path) is not None:
                    # RFC 4918 §9.3.1 : 405 si la ressource existe (les clients s'en
                    # servent pour distinguer « déjà là » d'une vraie erreur).
                    raise DAVError(HTTP_METHOD_NOT_ALLOWED, f"{path} existe déjà")
                ucloud_db.ensure_parents(conn, path)
                ucloud_db.put_entry(conn, path, {"type": "dir", "mtime": int(time.time())})
        logger.info("ucloud: MKCOL %s", path)

    def delete(self):
        if self.path == "/":
            raise DAVError(HTTP_FORBIDDEN, "La racine ne peut pas être supprimée")
        delete_paths(self.email, [self.path])
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)
        logger.info("ucloud: DELETE (collection) %s", self.path)

    def copy_move_single(self, dest_path, *, is_move):
        dest = normalize_path(dest_path)
        ensure_ucloud_dir(self.email)
        with index_lock(self.email):
            with ucloud_db.transaction(ucloud_dir(self.email)) as conn:
                ucloud_db.ensure_parents(conn, dest)
                if ucloud_db.get_entry(conn, dest) is None:
                    ucloud_db.put_entry(conn, dest, {"type": "dir", "mtime": int(time.time())})

    def support_recursive_move(self, dest_path):
        return True

    def move_recursive(self, dest_path):
        """Déplace la collection ET tout son sous-arbre en une transaction."""
        dest = normalize_path(dest_path)
        renames = move_subtree(self.email, self.path, dest)
        faces_follow_moves(self.email, renames)
        logger.info("ucloud: MOVE (collection) %s → %s", self.path, dest)


# ─────────────────────────────────────────────────────────────────────────────
# Provider
# ─────────────────────────────────────────────────────────────────────────────
class UCloudDAVProvider(DAVProvider):
    """Fournisseur DAV multi-utilisateur adossé aux index `.ucloud` chiffrés.

    On n'utilise PAS `FilesystemProvider` : il n'existe aucun répertoire réel
    derrière les chemins DAV — chaque ressource est une vue sur une entrée
    d'index dont le contenu vit chiffré sur IPFS.

    Chaque requête authentifiée est rattachée à UN MULTIPASS (email) ; la racine
    DAV « / » est la racine du cloud de cet utilisateur. Deux utilisateurs
    servis par le même process ne voient jamais l'arborescence de l'autre : la
    résolution part de `current_email(environ)`, JAMAIS du chemin.
    """

    def __repr__(self) -> str:
        return "UCloudDAVProvider"

    def is_readonly(self) -> bool:
        return False

    # ── Résolution ───────────────────────────────────────────────────────────
    def get_resource_inst(self, path: str, environ: dict):
        self._count_get_resource_inst += 1
        email = current_email(environ)
        if not email:
            raise DAVError(HTTP_FORBIDDEN, "Requête non authentifiée")

        try:
            norm = normalize_path(path)
        except ValueError as exc:
            logger.warning("ucloud: chemin rejeté %r: %s", path, exc)
            raise DAVError(HTTP_FORBIDDEN, "Chemin invalide") from exc

        # UNE requête SQLite indexée (clé primaire) : plus de chargement de tout
        # l'index par requête. Les dossiers sont toujours des entrées explicites
        # (invariant de ucloud_db : la migration matérialise les implicites).
        entry = db_entry(email, norm)
        if entry is None:
            return None
        if entry.get("type") == "dir":
            return UCloudCollection(norm, environ, email, entry)
        return UCloudFileResource(norm, environ, email, entry)

    def exists(self, path: str, environ: dict) -> bool:
        return self.get_resource_inst(path, environ) is not None


# ═════════════════════════════════════════════════════════════════════════════
# Construction de l'application WSGI
# ═════════════════════════════════════════════════════════════════════════════
def build_wsgi_dav_config() -> Dict[str, Any]:
    from wsgidav.error_printer import ErrorPrinter
    from wsgidav.http_authenticator import HTTPAuthenticator
    from wsgidav.request_resolver import RequestResolver

    return {
        "provider_mapping": {"/": UCloudDAVProvider()},
        # INDISPENSABLE sous `app.mount("/dav", …)` : wsgidav ne dérive PAS les
        # href de SCRIPT_NAME. Sans `mount_path`, un PROPFIND renverrait des
        # href `/Documents/f.txt` au lieu de `/dav/Documents/f.txt` — le client
        # DAV suivrait ces liens hors du montage (404), et un COPY/MOVE dont le
        # Destination porte le préfixe serait rejeté en 409.
        "mount_path": DAV_MOUNT_PREFIX,
        "verbose": 1,
        # `enable: False` : wsgidav ne reconfigure PAS le logging global du
        # process UPassport (core/logging.py en est le seul propriétaire).
        "logging": {"enable": False, "enable_loggers": []},
        # Nip98AuthMiddleware DOIT précéder HTTPAuthenticator : c'est lui qui
        # pose la clé de confiance consommée par `trusted_auth_header`.
        "middleware_stack": [
            ErrorPrinter,
            Nip98AuthMiddleware,
            HTTPAuthenticator,
            RequestResolver,  # doit rester le dernier
        ],
        "http_authenticator": {
            "domain_controller": UCloudDomainController,
            "accept_basic": True,
            # Digest exigerait de dériver un HA1 depuis le token : refusé.
            "accept_digest": False,
            "default_to_digest": False,
            "trusted_auth_header": TRUSTED_USER_ENVIRON_KEY,
        },
        # Le navigateur de répertoires rendrait le contenu déchiffré dans une
        # page HTML au premier GET d'un dossier : inutile pour un point de
        # montage, et une surface d'attaque en plus.
        "dir_browser": {"enable": False},
        "lock_storage": True,
        "property_manager": True,
        "suppress_version_info": True,
        # CORS est déjà géré par le CORSMiddleware de FastAPI (54321.py) :
        # ne pas le dupliquer dans la pile wsgidav.
    }


_dav_app: Optional[Callable] = None


def _strip_duplicate_date_header(wsgi_app: Callable) -> Callable:
    """Retire l'en-tête `Date` posé par wsgidav — uvicorn (via a2wsgi) en
    ajoute systématiquement le sien à la réponse ASGI finale, SANS dédupliquer
    si l'app WSGI en a déjà posé un. Résultat : deux en-têtes `Date` sur
    chaque réponse, une violation RFC 7230 §3.2.2 que le client `neon` de
    davfs2 tolère mal (montage OK mais accès en `Invalid argument`).

    Ajoute aussi, sur les GET, `nosniff` + `Content-Disposition: attachment` +
    un CSP `sandbox` : le mime d'un fichier vient du client (ou d'un expéditeur
    via /Reçues/) et ce contenu est servi sur l'origine de l'API — un `text/html`
    ne doit jamais s'y exécuter."""
    def app(environ, start_response):
        is_get = environ.get("REQUEST_METHOD") == "GET"

        def filtered_start_response(status, headers, exc_info=None):
            headers = [(k, v) for k, v in headers if k.lower() != "date"]
            headers.append(("X-Content-Type-Options", "nosniff"))
            if is_get and status.startswith("200"):
                names = {k.lower() for k, _ in headers}
                if "content-disposition" not in names:
                    headers.append(("Content-Disposition", "attachment"))
                headers.append(("Content-Security-Policy", "sandbox; default-src 'none'"))
            return start_response(status, headers, exc_info)
        return wsgi_app(environ, filtered_start_response)
    return app


def build_wsgi_dav_app() -> Callable:
    """Construit (et mémoïse) l'application WSGI WebDAV à monter sous /dav.

    Appelée depuis 54321.py :
        app.mount("/dav", WSGIMiddleware(build_wsgi_dav_app()))

    a2wsgi.WSGIMiddleware exécute l'app WSGI dans un pool de threads : les I/O
    bloquantes (ipfs add/cat) et le CPU crypto (AES-GCM) ne figent pas la boucle
    d'événements de uvicorn. C'est ce qui rend l'intégration in-process possible
    sans démon séparé.

    NB: `starlette.middleware.wsgi.WSGIMiddleware` est DÉPRÉCIÉE (starlette
    0.52+) et pointe elle-même vers a2wsgi — ne pas l'utiliser.
    """
    global _dav_app
    if _dav_app is not None:
        return _dav_app
    from wsgidav.wsgidav_app import WsgiDAVApp

    # Nettoyage des clairs laissés par d'anciennes versions (qui les écrivaient
    # sur disque) ; plus aucun thread de purge : le clair reste en mémoire.
    purge_stale_cache(ttl=0)
    _dav_app = _strip_duplicate_date_header(WsgiDAVApp(build_wsgi_dav_config()))
    logger.info("ucloud: WebDAV chiffré monté sous %s/", DAV_MOUNT_PREFIX)
    return _dav_app


# ═════════════════════════════════════════════════════════════════════════════
# URL publique du point de montage DAV
# ═════════════════════════════════════════════════════════════════════════════
async def dav_public_url() -> str:
    """URL publique du cloud DAV de CETTE station, terminée par '/'.

    Le domaine n'est JAMAIS codé en dur : il vient de `uSPOT`, la variable que
    `Astroport.ONE/tools/my.sh` exporte et que `_12345.sh` publie déjà dans
    12345.json. On la lit avec le même helper que les autres routers
    (`utils.helpers.get_env_from_mysh`), avec `settings.uSPOT` en repli.
    """
    base = ""
    try:
        from utils.helpers import get_env_from_mysh

        base = (await get_env_from_mysh("uSPOT", "")) or ""
    except Exception as exc:
        logger.warning("ucloud: lecture uSPOT via my.sh impossible: %s", exc)
    if not base:
        base = str(settings.uSPOT or "http://127.0.0.1:54321")
    return f"{base.rstrip('/')}{DAV_MOUNT_PREFIX}/"
