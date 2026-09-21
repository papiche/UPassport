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
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# Configuration
# ═════════════════════════════════════════════════════════════════════════════
GAME_NOSTR_PATH: Path = settings.GAME_PATH / "nostr"
UCLOUD_DIRNAME = ".ucloud"
DAV_TOKEN_FILENAME = "dav_token"
CACHE_DIR: Path = settings.ZEN_PATH / "tmp" / "ucloud_cache"
CACHE_TTL = 60  # secondes — filet de sécurité si le `finally` a échoué

# API du daemon IPFS local (Kubo). Volontairement 127.0.0.1 : on ne pousse
# jamais un blob via une gateway distante.
IPFS_API = os.environ.get("UCLOUD_IPFS_API", "http://127.0.0.1:5001")
IPFS_TIMEOUT = 60

# Même borne que routers/media_upload.py::_UENC_MAX_FILE_SIZE — AESGCM est
# one-shot, il n'existe pas de cadre de chunking dans le format UENC v1.
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

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


def token_path(email: str) -> Path:
    return ucloud_dir(email) / DAV_TOKEN_FILENAME


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
    """
    ensure_ucloud_dir(email)
    lp = lock_path(email)
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
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
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
    """Charge index.json (retourne un index vide si absent)."""
    data = _read_json(index_path(email), empty_index())
    data.setdefault("version", INDEX_VERSION)
    data.setdefault("entries", {})
    data.setdefault("owner_hex", "")
    data.setdefault("updated_at", 0)
    if not isinstance(data["entries"], dict):
        raise RuntimeError(f"ERROR:ucloud:index.entries malformé pour {email}")
    return data


def save_index(email: str, index: Dict[str, Any]) -> None:
    """Écrit index.json atomiquement (0600) en rafraîchissant `updated_at`."""
    index["version"] = INDEX_VERSION
    index["updated_at"] = int(time.time())
    ensure_ucloud_dir(email)
    _write_json_atomic(index_path(email), index)


def load_keyring(email: str) -> Dict[str, Any]:
    """Charge keyring.json : {"<cid>": {"key_hex": "<64 hex>"}, ...}."""
    return _read_json(keyring_path(email), {})


def save_keyring(email: str, keyring: Dict[str, Any]) -> None:
    ensure_ucloud_dir(email)
    _write_json_atomic(keyring_path(email), keyring)


def get_key_hex(email: str, cid: str) -> Optional[str]:
    """Clé AES-256 (hex) associée à un CID, ou None si absente du keyring."""
    entry = load_keyring(email).get(cid)
    if isinstance(entry, dict):
        key = entry.get("key_hex")
        return key if isinstance(key, str) and key else None
    return None


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
    """Récupère le blob chiffré `cid`, borné à `max_bytes` (défaut MAX_FILE_SIZE).

    La borne est appliquée EN COURS DE STREAMING : un CID pointant sur un objet
    énorme ne peut pas faire exploser la RAM du process UPassport.
    """
    limit = MAX_FILE_SIZE if max_bytes is None else max_bytes
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


def plaintext_stream(data: bytes, suffix: str = ".bin") -> _SelfClosingReader:
    """Matérialise `data` (clair) en 0600 et rend un flux auto-purgeant."""
    return _SelfClosingReader(EphemeralPlaintext(data, suffix=suffix))


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
        self._stop = threading.Event()

    def run(self) -> None:  # pragma: no cover - boucle infinie
        ensure_cache_dir()
        while not self._stop.wait(self.interval):
            try:
                purge_stale_cache()
            except Exception as exc:
                logger.error("ucloud: purge TTL en échec: %s", exc)

    def stop(self) -> None:  # pragma: no cover
        self._stop.set()


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
            out[k.strip().upper()] = v.strip()
    return out


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
    for secret in GAME_NOSTR_PATH.glob("*/.secret.nostr"):
        email = secret.parent.name
        if "@" not in email:
            continue
        if hex_for_email(email) == target:
            return email
    return None


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
    path = token_path(email)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
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
    logger.info("ucloud: token DAV créé pour %s (hex %s…)", email, hex_pubkey[:16])
    return token


def revoke_dav_token(email: str) -> bool:
    try:
        token_path(email).unlink()
        logger.info("ucloud: token DAV révoqué pour %s", email)
        return True
    except FileNotFoundError:
        return False


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
    if hmac.compare_digest(str(desc["token"]), password):
        return user_name
    return None


def _extract_gps_umap(plaintext: bytes) -> Optional[Dict[str, Any]]:
    """EXIF GPS → {"lat", "lon", "umap_key"} ou None. Best effort : appelé au
    moment du PUT, jamais via le Brain (aucune coordonnée ne doit transiter
    par un job d'analyse — c'est une donnée locale, extraite localement).
    """
    try:
        from PIL import Image, ExifTags
        from PIL.ExifTags import GPSTAGS
    except ImportError:
        return None

    def _to_degrees(value) -> float:
        d, m, s = (float(x) for x in value)
        return d + m / 60.0 + s / 3600.0

    try:
        with Image.open(io.BytesIO(plaintext)) as img:
            exif = img.getexif()
            if not exif:
                return None
            gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo) if hasattr(ExifTags, "IFD") else None
            if not gps_ifd:
                return None
            gps = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
            lat_dms, lat_ref = gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")
            lon_dms, lon_ref = gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")
            if not (lat_dms and lon_dms and lat_ref and lon_ref):
                return None
            lat = _to_degrees(lat_dms)
            if str(lat_ref).upper().startswith("S"):
                lat = -lat
            lon = _to_degrees(lon_dms)
            if str(lon_ref).upper().startswith("W"):
                lon = -lon
    except Exception as exc:
        logger.debug("ucloud: extraction EXIF GPS échouée : %s", exc)
        return None

    # Grille UMAP (0.01°) — même arrondi que geo.py::get_my_gps_coordinates,
    # pour que ces photos se retrouvent dans la même cellule que le reste.
    return {"lat": round(lat, 2), "lon": round(lon, 2),
            "umap_key": f"{lat:.2f},{lon:.2f}"}


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

    `target_pubkey`/`target_name` (enrôlement supervisé, cf. begin_write) :
    transmis tels quels au script, qui les inclut dans le job si présents —
    satellite_face_matcher.py cataloguera alors directement sous cette
    identité au lieu de lancer la recherche par similarité / Inconnu_xxx.
    """
    hex_pubkey = hex_for_email(email)
    if not hex_pubkey:
        logger.warning("ucloud: FaceID non déclenché pour %s — HEX introuvable", email)
        return
    trigger_sh = settings.TOOLS_PATH / "trigger_bro_vision_analysis.sh"
    if not trigger_sh.exists():
        logger.warning("ucloud: FaceID non déclenché — %s introuvable", trigger_sh)
        return
    try:
        cmd = ["bash", str(trigger_sh), email, hex_pubkey, path, cid, key_hex]
        if target_pubkey:
            cmd += [target_pubkey, target_name or ""]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Confirme le LANCEMENT (pas le succès de l'envoi DM lui-même — ça,
        # seul ~/.zen/tmp/bro_vision_trigger.log le sait, cf. le script) :
        # sans cette ligne, un déclenchement réussi et un déclenchement
        # silencieusement absent sont indiscernables dans le journal UPassport.
        logger.info("ucloud: FaceID déclenché pour %s %s (cid=%s…, pid=%s)",
                     email, path, cid[:16], proc.pid)
    except Exception as exc:
        logger.warning("ucloud: déclenchement FaceID impossible pour %s: %s", email, exc)


# ═════════════════════════════════════════════════════════════════════════════
# Couche DAV (wsgidav)
# ═════════════════════════════════════════════════════════════════════════════
from wsgidav import util  # noqa: E402
from wsgidav.dav_error import (  # noqa: E402
    HTTP_FORBIDDEN,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
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
INDEX_ENVIRON_KEY = "ucloud.index"


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

                event = _decode_and_verify_nip98_event(header)
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
            payload = ipfs_cat(cid)
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
        self._pending_content_type = content_type
        return _EncryptingWriteBuffer(self)

    def _commit_plaintext(self, plaintext: bytes) -> None:
        """Chiffre, pousse sur IPFS, met à jour index + keyring atomiquement."""
        # Clé AES-256 ALÉATOIRE PAR FICHIER. Jamais de clé unique par
        # utilisateur : la compromission d'un fichier partagé ne doit pas donner
        # accès à tout le cloud.
        key_hex = os.urandom(32).hex()
        payload, iv_hex = uenc_codec.encrypt_aes256gcm(plaintext, key_hex)

        # Upload du blob DÉJÀ CHIFFRÉ. IPFS ne voit jamais le clair.
        cid = ipfs_add_bytes(payload, filename=f"{self.name}.uenc")

        content_type = getattr(self, "_pending_content_type", None)
        if not content_type:
            content_type = mimetypes.guess_type(self.name)[0] or "application/octet-stream"

        # GPS EXIF — extrait ICI (clair déjà en main, avant chiffrement),
        # jamais via le Brain : aucune coordonnée ne doit transiter par un job
        # d'analyse déportable. Best effort, silencieux si absent/illisible.
        geo = _extract_gps_umap(plaintext) if content_type.startswith("image/") else None

        now = int(time.time())
        with index_lock(self.email):
            idx = load_index(self.email)
            keyring = load_keyring(self.email)
            previous = idx["entries"].get(self.path, {})

            ensure_parent_dirs(idx, self.path)
            entry = {
                "type": "file",
                "cid": cid,
                "enc": ENC_LABEL,
                "iv_hex": iv_hex,
                "mime": content_type,
                "size_plain": len(plaintext),
                "size_cid": len(payload),
                "sha256_plain": hashlib.sha256(plaintext).hexdigest(),
                "mtime": now,
                "created_at": previous.get("created_at", now),
            }
            if geo:
                entry["geo"] = geo
                logger.info("ucloud: GPS EXIF %s → %s (umap=%s)",
                             self.path, (geo["lat"], geo["lon"]), geo["umap_key"])
            idx["entries"][self.path] = entry
            keyring[cid] = {"key_hex": key_hex}
            # Une clé devenue orpheline (ancien CID remplacé) est retirée.
            keyring = prune_keyring(idx, keyring)
            keyring[cid] = {"key_hex": key_hex}

            save_keyring(self.email, keyring)
            save_index(self.email, idx)

        self.entry = entry
        self._was_new = False
        self.provider._invalidate(self.environ)
        logger.info(
            "ucloud: PUT %s → cid=%s (%d octets clairs / %d chiffrés)",
            self.path,
            cid,
            len(plaintext),
            len(payload),
        )

        if content_type.startswith("image/"):
            # Enrôlement supervisé (FaceCloud "Mon visage" / "Photos d'un
            # ami") : le client peut cibler explicitement une identité via ces
            # deux en-têtes, plutôt que de laisser l'auto-détection créer un
            # Inconnu_xxx. Absents = comportement automatique inchangé.
            target_pubkey = self.environ.get("HTTP_X_FACEID_TARGET_PUBKEY", "").strip().lower()
            target_name = self.environ.get("HTTP_X_FACEID_TARGET_NAME", "").strip()
            if len(target_pubkey) != 64 or not all(c in "0123456789abcdef" for c in target_pubkey):
                target_pubkey = ""
            _trigger_faceid_analysis(self.email, self.path, cid, key_hex,
                                      target_pubkey=target_pubkey or None,
                                      target_name=target_name or None)

    def end_write(self, *, with_errors):
        """Notification post-PUT. En cas d'erreur, on annule l'entrée créée."""
        if with_errors and self._was_new:
            logger.warning("ucloud: PUT en erreur sur %s — rollback", self.path)
            try:
                with index_lock(self.email):
                    idx = load_index(self.email)
                    if idx["entries"].get(self.path, {}).get("cid") is None:
                        idx["entries"].pop(self.path, None)
                        save_index(self.email, idx)
            except Exception as exc:  # pragma: no cover
                logger.error("ucloud: rollback impossible pour %s: %s", self.path, exc)

    # ── Suppression / déplacement ────────────────────────────────────────────
    def delete(self):
        if self._readonly():
            raise DAVError(HTTP_FORBIDDEN, "Ressource partagée : lecture seule")
        with index_lock(self.email):
            idx = load_index(self.email)
            keyring = load_keyring(self.email)
            remove_subtree(idx, self.path)
            # Le blob reste sur IPFS (il peut être épinglé ailleurs, ou partagé) ;
            # c'est la CLÉ qu'on détruit — sans elle le blob est du bruit.
            keyring = prune_keyring(idx, keyring)
            save_keyring(self.email, keyring)
            save_index(self.email, idx)
        # Le cache d'index par requête doit être invalidé : wsgidav revérifie
        # `provider.exists()` juste après le delete.
        self.provider._invalidate(self.environ)
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)
        logger.info("ucloud: DELETE %s", self.path)

    def copy_move_single(self, dest_path, *, is_move):
        if is_move and self._readonly():
            raise DAVError(HTTP_FORBIDDEN, "Ressource partagée : lecture seule")
        dest = normalize_path(dest_path)
        now = int(time.time())
        with index_lock(self.email):
            idx = load_index(self.email)
            src = idx["entries"].get(self.path)
            if src is None:
                raise DAVError(HTTP_NOT_FOUND, self.path)
            ensure_parent_dirs(idx, dest)
            # COPY partage le même CID et donc la même clé : pas de
            # re-chiffrement, pas de nouvel upload. La copie devient modifiable.
            clone = dict(src)
            clone["mtime"] = now
            clone.pop("readonly", None)
            idx["entries"][dest] = clone
            if is_move:
                idx["entries"].pop(self.path, None)
            save_index(self.email, idx)
        self.provider._invalidate(self.environ)
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
                idx = load_index(self.email)
                entry = idx["entries"].get(self.path)
                if entry is not None:
                    entry["mtime"] = int(secs)
                    save_index(self.email, idx)
            self.provider._invalidate(self.environ)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Ressource collection
# ─────────────────────────────────────────────────────────────────────────────
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
        idx = self.provider._index_for(self.environ)
        return list_children(idx, self.path)

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
        with index_lock(self.email):
            idx = load_index(self.email)
            ensure_parent_dirs(idx, path)
            idx["entries"][path] = entry
            save_index(self.email, idx)
        self.provider._invalidate(self.environ)
        res = UCloudFileResource(path, self.environ, self.email, entry)
        res._was_new = True
        return res

    def create_collection(self, name: str):
        assert "/" not in name
        path = normalize_path(util.join_uri(self.path, name))
        with index_lock(self.email):
            idx = load_index(self.email)
            if path in idx["entries"]:
                raise DAVError(HTTP_FORBIDDEN, f"{path} existe déjà")
            ensure_parent_dirs(idx, path)
            idx["entries"][path] = {"type": "dir", "mtime": int(time.time())}
            save_index(self.email, idx)
        self.provider._invalidate(self.environ)
        logger.info("ucloud: MKCOL %s", path)

    def delete(self):
        if self.path == "/":
            raise DAVError(HTTP_FORBIDDEN, "La racine ne peut pas être supprimée")
        with index_lock(self.email):
            idx = load_index(self.email)
            keyring = load_keyring(self.email)
            remove_subtree(idx, self.path)
            keyring = prune_keyring(idx, keyring)
            save_keyring(self.email, keyring)
            save_index(self.email, idx)
        self.provider._invalidate(self.environ)
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)
        logger.info("ucloud: DELETE (collection) %s", self.path)

    def copy_move_single(self, dest_path, *, is_move):
        dest = normalize_path(dest_path)
        with index_lock(self.email):
            idx = load_index(self.email)
            ensure_parent_dirs(idx, dest)
            if dest not in idx["entries"]:
                idx["entries"][dest] = {"type": "dir", "mtime": int(time.time())}
            save_index(self.email, idx)
        self.provider._invalidate(self.environ)

    def support_recursive_move(self, dest_path):
        return True

    def move_recursive(self, dest_path):
        """Déplace la collection ET tout son sous-arbre en une transaction."""
        dest = normalize_path(dest_path)
        prefix = self.path + "/" if self.path != "/" else "/"
        with index_lock(self.email):
            idx = load_index(self.email)
            ensure_parent_dirs(idx, dest)
            moved = {}
            for key in [
                k for k in idx["entries"] if k == self.path or k.startswith(prefix)
            ]:
                suffix = key[len(self.path):]
                moved[normalize_path(dest + suffix)] = idx["entries"].pop(key)
            idx["entries"].update(moved)
            save_index(self.email, idx)
        self.provider._invalidate(self.environ)
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

    # ── Cache d'index par requête ────────────────────────────────────────────
    def _index_for(self, environ: dict) -> Dict[str, Any]:
        """Index de l'utilisateur courant, mémoïsé pour la durée de la requête.

        Un PROPFIND Depth:1 instancie N ressources ; sans ce cache, l'index
        serait relu N fois depuis le disque.
        """
        cached = environ.get(INDEX_ENVIRON_KEY)
        if cached is not None:
            return cached
        email = current_email(environ)
        if not email:
            raise DAVError(HTTP_FORBIDDEN, "Requête non authentifiée")
        idx = load_index(email)
        environ[INDEX_ENVIRON_KEY] = idx
        return idx

    def _invalidate(self, environ: dict) -> None:
        environ.pop(INDEX_ENVIRON_KEY, None)

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

        idx = self._index_for(environ)
        entry = get_entry(idx, norm)

        if entry is None:
            # Dossier implicite : /a/b n'a pas d'entrée mais /a/b/c.txt existe.
            if list_children(idx, norm):
                entry = {"type": "dir", "mtime": idx.get("updated_at", 0)}
            else:
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
    chaque réponse, une violation RFC 7230 §3.2.2 (header singleton dupliqué)
    que le client `neon` de davfs2 tolère mal — symptôme constaté : montage
    OK mais tout accès au point de montage échoue en `Invalid argument`
    (confirmé via http.client brut : httplib voit bien DEUX en-têtes `date`
    distincts sur une même réponse PROPFIND)."""
    def app(environ, start_response):
        def filtered_start_response(status, headers, exc_info=None):
            headers = [(k, v) for k, v in headers if k.lower() != "date"]
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

    ensure_cache_dir()
    purge_stale_cache(ttl=0)  # nettoyage d'un éventuel crash précédent
    start_purge_thread()
    _dav_app = _strip_duplicate_date_header(WsgiDAVApp(build_wsgi_dav_config()))
    logger.info("ucloud: WebDAV chiffré monté sous %s/ (cache %s)", DAV_MOUNT_PREFIX, CACHE_DIR)
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
