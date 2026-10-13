"""
services/ucloud_db.py — Stockage SQLite (WAL) de l'index et du keyring d'un
cloud `.ucloud` (un MULTIPASS = un fichier `~/.zen/game/nostr/<EMAIL>/.ucloud/ucloud.db`).

POURQUOI : `index.json` + `keyring.json` étaient relus ET réécrits EN ENTIER à
chaque PUT (≈ 24 Mo + 6 Mo à 50 000 photos : ~0,8 s d'E/S, 5 à 10× sur un RPi),
donc un import de masse était quadratique et chaque requête DAV reparsait tout.
Ici une écriture touche UNE ligne, un PROPFIND lit UN dossier (index sur
`parent`), la recherche par hash / CID passe par un index, et SQLite apporte
des transactions (plus d'index tronqué ni de keyring désynchronisé).

MODULE VOLONTAIREMENT AUTONOME (stdlib seule, aucun import de `core.config`) :
`Astroport.ONE/IA/bro/satellite_face_matcher.py` l'importe par chemin pour
patcher une entrée ou lire une clé sans dépendre de FastAPI.

SCHÉMA (version 1)
    entries(path PK, parent, type, cid, mime, size, mtime, taken_at,
            sha_orig, sha_plain, faceid_status, origin, data)
        `data` = l'entrée COMPLÈTE en JSON (même contenu que dans l'ancien
        index.json : c'est la source de vérité) ; les autres colonnes en sont
        dérivées pour les requêtes (statistiques, doublons, dossiers).
    keyring(cid PK, data)      `data` = {"key_hex": "<64 hex>"} (idem keyring.json)
    meta(key PK, value)        owner_hex, rev, updated_at, migrated

INVARIANT : tout ancêtre d'une entrée est une entrée `dir` EXPLICITE (la
migration depuis index.json ajoute les dossiers implicites manquants).

CONCURRENCE : les écritures applicatives gardent le flock `.lock`
(`cloud_storage.index_lock`, que le matcher partage) EN PLUS de la transaction
SQLite — un appelant historique qui ferait `load_index` → modification →
`save_index` ne peut donc pas écraser une ligne insérée entre-temps.

MIGRATION : au premier accès, si `ucloud.db` n'existe pas, `index.json` et
`keyring.json` sont importés (une transaction, sous flock `.migrate.lock`) puis
renommés en `*.migrated-<date>` ; `export_json()` régénère ces fichiers pour les
sauvegardes et les scripts bash (nostr_DESTROY_TW.sh).
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

DB_NAME = "ucloud.db"
SCHEMA_VERSION = 4
FILE_MODE = 0o600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
    path          TEXT PRIMARY KEY,
    parent        TEXT NOT NULL,
    type          TEXT NOT NULL,
    cid           TEXT,
    mime          TEXT,
    size          INTEGER,
    mtime         INTEGER,
    taken_at      INTEGER,
    sha_orig      TEXT,
    sha_plain     TEXT,
    faceid_status TEXT,
    origin        TEXT,
    data          TEXT NOT NULL,
    phash         INTEGER,
    lat           REAL,
    lon           REAL,
    desc_text     TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS entries_parent    ON entries(parent);
CREATE INDEX IF NOT EXISTS entries_cid       ON entries(cid)       WHERE cid IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_sha_orig  ON entries(sha_orig)  WHERE sha_orig IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_sha_plain ON entries(sha_plain) WHERE sha_plain IS NOT NULL;
CREATE TABLE IF NOT EXISTS keyring(
    cid  TEXT PRIMARY KEY,
    data TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;
"""

_LOCAL = threading.local()


# ═════════════════════════════════════════════════════════════════════════════
# Chemins
# ═════════════════════════════════════════════════════════════════════════════
def parent_of(path: str) -> str:
    """'/a/b.txt' → '/a' ; '/a' → '/'."""
    head = path.rsplit("/", 1)[0]
    return head or "/"


def _range(path: str) -> Tuple[str, str]:
    """Bornes [lo, hi[ des clés strictement SOUS `path` (« / » = 0x2F, « 0 » = 0x30)."""
    p = path.rstrip("/")
    return p + "/", p + "0"


# ═════════════════════════════════════════════════════════════════════════════
# Connexion (une par thread et par fichier) + migration
# ═════════════════════════════════════════════════════════════════════════════
def db_path(udir: Path) -> Path:
    return Path(udir) / DB_NAME


def _open(udir: Path) -> sqlite3.Connection:
    udir = Path(udir)
    udir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(udir, 0o700)
    except OSError:
        pass
    path = db_path(udir)
    if not path.exists():
        # Créé en 0600 AVANT sqlite : -wal et -shm héritent du mode du fichier principal.
        os.close(os.open(str(path), os.O_CREAT | os.O_RDWR, FILE_MODE))
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    _migrate_from_json(udir, conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Schéma v2 : `phash` (empreinte perceptuelle dHash 64 bits, doublons) et les
    index qui en dépendent ; ajoutés à chaud aux bases créées en v1."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    if "phash" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN phash INTEGER")
    # Tri de la galerie (date de prise de vue, sinon d'envoi) : sans cet index,
    # chaque page triait toute la table ; l'expression DOIT être celle de `page()`.
    conn.execute("CREATE INDEX IF NOT EXISTS entries_taken ON entries(COALESCE(taken_at, mtime) DESC) "
                 "WHERE type='file' AND cid IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS entries_phash ON entries(phash) WHERE phash IS NOT NULL")
    # Schéma v3 : coordonnées en colonnes (vue carte) — rattrapées depuis le JSON des lignes existantes.
    if "lat" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN lat REAL")
        conn.execute("ALTER TABLE entries ADD COLUMN lon REAL")
        conn.execute("UPDATE entries SET lat=json_extract(data,'$.geo.lat'), lon=json_extract(data,'$.geo.lon') "
                     "WHERE json_extract(data,'$.geo.lat') IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS entries_geo ON entries(lat, lon) WHERE lat IS NOT NULL")
    # Schéma v4 : description de la photo (IA ou saisie à la main) — cherchable.
    if "desc_text" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN desc_text TEXT")
        conn.execute("UPDATE entries SET desc_text=json_extract(data,'$.desc.text') WHERE json_extract(data,'$.desc.text') IS NOT NULL")


def connect(udir: Path) -> sqlite3.Connection:
    """Connexion du thread courant (recréée si le fichier a été remplacé/supprimé,
    par exemple par nostr_RESTORE_TW.sh)."""
    udir = Path(udir)
    key = str(db_path(udir))
    conns: Dict[str, Tuple[sqlite3.Connection, int]] = getattr(_LOCAL, "conns", None) or {}
    _LOCAL.conns = conns
    cached = conns.get(key)
    try:
        ino = os.stat(key).st_ino
    except FileNotFoundError:
        ino = -1
    if cached and cached[1] == ino:
        return cached[0]
    if cached:
        try:
            cached[0].close()
        except sqlite3.Error:
            pass
    conn = _open(udir)
    conns[key] = (conn, os.stat(key).st_ino)
    return conn


def exists(udir: Path) -> bool:
    return db_path(udir).exists()


def _meta_get(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def _migrate_from_json(udir: Path, conn: sqlite3.Connection) -> None:
    if _meta_get(conn, "migrated"):
        return
    lock_fd = os.open(str(udir / ".migrate.lock"), os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if _meta_get(conn, "migrated"):      # fait par un autre process pendant l'attente
            return
        idx_file, key_file = udir / "index.json", udir / "keyring.json"
        index: Dict[str, Any] = {}
        keyring: Dict[str, Any] = {}
        try:
            if idx_file.is_file():
                index = json.loads(idx_file.read_text(encoding="utf-8"))
                if not isinstance(index, dict):
                    raise RuntimeError(f"ERROR:ucloud:index malformé (pas un objet) {idx_file}")
            if key_file.is_file():
                keyring = json.loads(key_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Jamais de base vide à la place d'un index illisible : on s'arrête, bruyamment.
            raise RuntimeError(f"ERROR:ucloud:index illisible {idx_file}: {exc}") from exc
        conn.execute("BEGIN IMMEDIATE")
        try:
            if index:
                _replace_all(conn, index.get("entries") or {})
                _meta_set(conn, "owner_hex", str(index.get("owner_hex") or ""))
            if isinstance(keyring, dict):
                for cid, k in keyring.items():
                    conn.execute("INSERT OR REPLACE INTO keyring(cid,data) VALUES(?,?)",
                                 (cid, json.dumps(k, separators=(",", ":"))))
            _meta_set(conn, "migrated", str(int(time.time())))
            _meta_set(conn, "schema", str(SCHEMA_VERSION))
            _bump(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for f in (idx_file, key_file):
            if f.is_file():
                os.replace(f, f.with_name(f"{f.name}.migrated-{stamp}"))
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


# ═════════════════════════════════════════════════════════════════════════════
# Transactions
# ═════════════════════════════════════════════════════════════════════════════
@contextmanager
def transaction(udir: Path) -> Iterator[sqlite3.Connection]:
    """Transaction d'ÉCRITURE (BEGIN IMMEDIATE) ; incrémente `rev` au commit."""
    conn = connect(udir)
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        _bump(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _bump(conn: sqlite3.Connection) -> None:
    rev = int(_meta_get(conn, "rev", "0") or 0) + 1
    _meta_set(conn, "rev", str(rev))
    _meta_set(conn, "updated_at", str(int(time.time())))


def rev(udir: Path) -> int:
    """Révision courante : change à chaque écriture (clé des caches de lecture)."""
    return int(_meta_get(connect(udir), "rev", "0") or 0)


def updated_at(udir: Path) -> int:
    return int(_meta_get(connect(udir), "updated_at", "0") or 0)


def owner_hex(udir: Path) -> str:
    return _meta_get(connect(udir), "owner_hex", "") or ""


def set_owner_hex(conn: sqlite3.Connection, value: str) -> None:
    _meta_set(conn, "owner_hex", value or "")


# ═════════════════════════════════════════════════════════════════════════════
# Entrées
# ═════════════════════════════════════════════════════════════════════════════
def _phash_int(value: Any) -> Optional[int]:
    """'f0e1…' (16 hex) → entier signé 64 bits (SQLite n'a pas d'entier non signé)."""
    try:
        v = int(str(value), 16)
    except (TypeError, ValueError):
        return None
    return v - (1 << 64) if v >= (1 << 63) else v


def _geo(e: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    g = e.get("geo")
    try:
        if isinstance(g, dict) and g.get("lat") is not None and g.get("lon") is not None:
            return float(g["lat"]), float(g["lon"])
    except (TypeError, ValueError):
        pass
    return None, None


def _desc_text(e: Dict[str, Any]) -> Optional[str]:
    d = e.get("desc")
    return d.get("text") if isinstance(d, dict) and d.get("text") else None


def _cols(path: str, e: Dict[str, Any]) -> Tuple[Any, ...]:
    fid = e.get("faceid")
    lat, lon = _geo(e)
    return (
        path, parent_of(path), e.get("type") or "file", e.get("cid") or None, e.get("mime"),
        e.get("size_plain") if isinstance(e.get("size_plain"), int) else None,
        e.get("mtime") if isinstance(e.get("mtime"), int) else None,
        e.get("taken_at") if isinstance(e.get("taken_at"), int) else None,
        e.get("sha256_orig") or None, e.get("sha256_plain") or None,
        fid.get("status") if isinstance(fid, dict) else None,
        e.get("origin"),
        json.dumps(e, ensure_ascii=False, separators=(",", ":")),
        _phash_int(e.get("phash")),
        lat, lon, _desc_text(e),
    )


_UPSERT = ("INSERT OR REPLACE INTO entries(path,parent,type,cid,mime,size,mtime,taken_at,"
           "sha_orig,sha_plain,faceid_status,origin,data,phash,lat,lon,desc_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def put_entry(conn: sqlite3.Connection, path: str, entry: Dict[str, Any]) -> None:
    conn.execute(_UPSERT, _cols(path, entry))


def get_entry(conn: sqlite3.Connection, path: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT data FROM entries WHERE path=?", (path,)).fetchone()
    return json.loads(row[0]) if row else None


def children(conn: sqlite3.Connection, dirpath: str) -> List[str]:
    """Noms des membres DIRECTS de `dirpath`, triés."""
    prefix = "/" if dirpath == "/" else dirpath + "/"
    return sorted(r[0][len(prefix):] for r in conn.execute("SELECT path FROM entries WHERE parent=?", (dirpath,)))


def subtree(conn: sqlite3.Connection, path: str) -> List[Tuple[str, Dict[str, Any]]]:
    """`path` lui-même et tout ce qui est dessous : [(chemin, entrée)]."""
    lo, hi = _range(path)
    rows = conn.execute("SELECT path,data FROM entries WHERE path=? OR (path>=? AND path<?)",
                        (path, lo, hi)).fetchall()
    return [(p, json.loads(d)) for p, d in rows]


def delete_subtree(conn: sqlite3.Connection, path: str) -> List[Tuple[str, Dict[str, Any]]]:
    removed = subtree(conn, path)
    if removed:
        lo, hi = _range(path)
        conn.execute("DELETE FROM entries WHERE path=? OR (path>=? AND path<?)", (path, lo, hi))
    return removed


def ensure_parents(conn: sqlite3.Connection, path: str, now: Optional[int] = None) -> None:
    """Crée les dossiers manquants au-dessus de `path` ; ValueError si un ancêtre est un fichier."""
    segs = path.strip("/").split("/")[:-1]
    now = int(time.time()) if now is None else now
    cur = ""
    for seg in segs:
        cur = f"{cur}/{seg}"
        row = conn.execute("SELECT type FROM entries WHERE path=?", (cur,)).fetchone()
        if row is None:
            put_entry(conn, cur, {"type": "dir", "mtime": now})
        elif row[0] != "dir":
            raise ValueError(f"ERROR:ucloud:{cur} existe déjà et n'est pas un dossier")


def find_by_hashes(conn: sqlite3.Connection, hashes: List[str]) -> Optional[str]:
    """Chemin d'une entrée dont sha_orig OU sha_plain figure dans `hashes`."""
    for h in hashes:
        row = conn.execute("SELECT path FROM entries WHERE sha_orig=? OR sha_plain=? LIMIT 1", (h, h)).fetchone()
        if row:
            return row[0]
    return None


def cid_referenced(conn: sqlite3.Connection, cid: str) -> bool:
    return conn.execute("SELECT 1 FROM entries WHERE cid=? LIMIT 1", (cid,)).fetchone() is not None


def patch_entry(udir: Path, path: str, fn: Callable[[Dict[str, Any]], bool]) -> bool:
    """Lit l'entrée, applique `fn(entry)` (qui la modifie en place et rend True si
    elle a changé) et l'enregistre — UNE ligne, sans réécrire l'index. Rend False
    si l'entrée n'existe pas ou n'a pas changé."""
    with transaction(udir) as conn:
        entry = get_entry(conn, path)
        if entry is None:
            return False
        if not fn(entry):
            return False
        put_entry(conn, path, entry)
        return True


# ═════════════════════════════════════════════════════════════════════════════
# Keyring
# ═════════════════════════════════════════════════════════════════════════════
def key_get(conn: sqlite3.Connection, cid: str) -> Optional[str]:
    row = conn.execute("SELECT data FROM keyring WHERE cid=?", (cid,)).fetchone()
    if not row:
        return None
    try:
        key = json.loads(row[0]).get("key_hex")
    except (ValueError, AttributeError):
        return None
    return key if isinstance(key, str) and key else None


def key_set(conn: sqlite3.Connection, cid: str, data: Dict[str, Any]) -> None:
    conn.execute("INSERT OR REPLACE INTO keyring(cid,data) VALUES(?,?)",
                 (cid, json.dumps(data, separators=(",", ":"))))


def prune_keys(conn: sqlite3.Connection, candidates: Optional[List[str]] = None) -> List[str]:
    """Supprime les clés dont plus aucune entrée ne référence le CID ; rend les CID
    retirés (à dépingler). `candidates` borne la vérification (O(retirés))."""
    if candidates is None:
        rows = conn.execute("SELECT cid FROM keyring WHERE cid NOT IN "
                            "(SELECT cid FROM entries WHERE cid IS NOT NULL)").fetchall()
        orphans = [r[0] for r in rows]
    else:
        orphans = [c for c in dict.fromkeys(candidates)
                   if c and not cid_referenced(conn, c)]
        orphans = [c for c in orphans if conn.execute("SELECT 1 FROM keyring WHERE cid=?", (c,)).fetchone()]
    for c in orphans:
        conn.execute("DELETE FROM keyring WHERE cid=?", (c,))
    return orphans


# ═════════════════════════════════════════════════════════════════════════════
# Vues « tout l'index » (compatibilité, lecture/écriture O(n))
# ═════════════════════════════════════════════════════════════════════════════
def load_all(udir: Path) -> Dict[str, Any]:
    conn = connect(udir)
    entries = {p: json.loads(d) for p, d in conn.execute("SELECT path,data FROM entries")}
    return {"version": 1, "owner_hex": _meta_get(conn, "owner_hex", "") or "",
            "updated_at": int(_meta_get(conn, "updated_at", "0") or 0), "entries": entries}


def _replace_all(conn: sqlite3.Connection, entries: Dict[str, Any]) -> None:
    """Fait correspondre la table à `entries` (diff : upsert des lignes changées,
    suppression des absentes, dossiers parents ajoutés si manquants)."""
    now = int(time.time())
    current = dict(conn.execute("SELECT path,data FROM entries").fetchall())
    wanted: Dict[str, Dict[str, Any]] = {p: e for p, e in entries.items() if isinstance(e, dict)}
    for path in list(wanted):                       # ancêtres implicites → dossiers explicites
        cur = ""
        for seg in path.strip("/").split("/")[:-1]:
            cur = f"{cur}/{seg}"
            if cur not in wanted:
                wanted[cur] = {"type": "dir", "mtime": now}
    for path, e in wanted.items():
        cols = _cols(path, e)
        if current.get(path) != cols[12]:          # cols[12] = `data` (JSON complet)
            conn.execute(_UPSERT, cols)
    gone = [p for p in current if p not in wanted]
    for i in range(0, len(gone), 500):
        chunk = gone[i:i + 500]
        conn.execute(f"DELETE FROM entries WHERE path IN ({','.join('?' * len(chunk))})", chunk)


def save_all(udir: Path, index: Dict[str, Any]) -> None:
    """Enregistre un index complet (chemin HISTORIQUE `load_index` → modifier →
    `save_index`). O(n) : les chemins chauds utilisent `transaction` + `put_entry`."""
    with transaction(udir) as conn:
        _replace_all(conn, index.get("entries") or {})
        if index.get("owner_hex"):
            _meta_set(conn, "owner_hex", str(index["owner_hex"]))


def load_keyring_all(udir: Path) -> Dict[str, Any]:
    conn = connect(udir)
    return {cid: json.loads(d) for cid, d in conn.execute("SELECT cid,data FROM keyring")}


def save_keyring_all(udir: Path, keyring: Dict[str, Any]) -> None:
    with transaction(udir) as conn:
        current = {cid: d for cid, d in conn.execute("SELECT cid,data FROM keyring")}
        for cid, k in keyring.items():
            d = json.dumps(k, separators=(",", ":"))
            if current.get(cid) != d:
                conn.execute("INSERT OR REPLACE INTO keyring(cid,data) VALUES(?,?)", (cid, d))
        for cid in current:
            if cid not in keyring:
                conn.execute("DELETE FROM keyring WHERE cid=?", (cid,))


# ═════════════════════════════════════════════════════════════════════════════
# Statistiques (GET /api/cloud/status) et export JSON
# ═════════════════════════════════════════════════════════════════════════════
def stats(udir: Path) -> Dict[str, Any]:
    """Fichiers (avec contenu), octets, statuts FaceID des images PROPRES et copies
    reçues — en SQL, sans charger les entrées."""
    conn = connect(udir)
    files, size = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM entries WHERE type='file' AND cid IS NOT NULL").fetchone()
    faceid: Dict[str, int] = {}
    received = 0
    for status, origin, n in conn.execute(
            "SELECT faceid_status, origin, COUNT(*) FROM entries "
            "WHERE type='file' AND cid IS NOT NULL AND mime LIKE 'image/%' GROUP BY faceid_status, origin"):
        if origin == "facecloud-share":
            received += n
        else:
            faceid[status or "none"] = faceid.get(status or "none", 0) + n
    return {"files": files, "bytes": size, "faceid": faceid, "received": received}


def count_under(udir: Path, prefix: str) -> int:
    lo, hi = _range(prefix)
    return connect(udir).execute(
        "SELECT COUNT(*) FROM entries WHERE type='file' AND path>=? AND path<?", (lo, hi)).fetchone()[0]


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, FILE_MODE)
    os.replace(tmp, path)


def export_json(udir: Path, dest: Path, index_name: str = "index.json",
                keyring_name: str = "keyring.json") -> Dict[str, int]:
    """Écrit l'index et le keyring au format JSON HISTORIQUE (sauvegardes, scripts
    bash : `nostr_DESTROY_TW.sh`, `jq`). Rend {"entries", "keys"}."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    index, keyring = load_all(udir), load_keyring_all(udir)
    _write_private(dest / index_name, json.dumps(index, ensure_ascii=False, separators=(",", ":")))
    _write_private(dest / keyring_name, json.dumps(keyring, ensure_ascii=False, separators=(",", ":")))
    return {"entries": len(index["entries"]), "keys": len(keyring)}


# ═════════════════════════════════════════════════════════════════════════════
# Galerie paginée, doublons, présence par empreinte
# ═════════════════════════════════════════════════════════════════════════════
_TAKEN = "COALESCE(taken_at, mtime)"        # identique à l'index `entries_taken`
_IMG = "type='file' AND cid IS NOT NULL AND mime LIKE 'image/%'"
_FILTERS = {
    "all": "1",
    "faces": "faceid_status='faces' AND COALESCE(origin,'')<>'facecloud-share'",
    "no_face": "faceid_status='no_face'",
    "waiting": "COALESCE(origin,'')<>'facecloud-share' AND (faceid_status IS NULL OR faceid_status IN ('pending','timeout'))",
    "received": "origin='facecloud-share'",
}


def gallery_counts(udir: Path) -> Dict[str, int]:
    """Nombre d'images par filtre de galerie, en UNE requête."""
    conn = connect(udir)
    row = conn.execute(
        f"SELECT COUNT(*),"
        f" SUM(CASE WHEN {_FILTERS['faces']} THEN 1 ELSE 0 END),"
        f" SUM(CASE WHEN {_FILTERS['no_face']} THEN 1 ELSE 0 END),"
        f" SUM(CASE WHEN {_FILTERS['waiting']} THEN 1 ELSE 0 END),"
        f" SUM(CASE WHEN {_FILTERS['received']} THEN 1 ELSE 0 END)"
        f" FROM entries WHERE {_IMG}").fetchone()
    return dict(zip(("all", "faces", "no_face", "waiting", "received"), (int(x or 0) for x in row)))


def page(udir: Path, flt: str = "all", q: str = "", offset: int = 0, limit: int = 120,
         want_total: bool = True, since: Optional[int] = None, until: Optional[int] = None,
         lat: Optional[float] = None, lon: Optional[float] = None) -> Tuple[List[Tuple[str, Dict[str, Any]]], int]:
    """Une page d'images, la plus récente d'abord (date de prise de vue, sinon d'envoi).
    Rend ([(chemin, entrée)], total pour ce filtre ou -1 si `want_total` est faux : le compte
    parcourt toutes les lignes, on ne le refait pas à chaque page). `q` : sous-chaîne du chemin."""
    where = _FILTERS.get(flt, "1")
    args: List[Any] = []
    if q:
        # Le chemin OU la description (IA ou saisie) contient la recherche.
        where += " AND (path LIKE ? ESCAPE '\\' OR desc_text LIKE ? ESCAPE '\\')"
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [like, like]
    if since is not None:
        where += f" AND {_TAKEN} >= ?"
        args.append(int(since))
    if until is not None:
        where += f" AND {_TAKEN} < ?"
        args.append(int(until))
    if lat is not None and lon is not None:       # un lieu = une case de 0,01° (valeurs stockées arrondies)
        where += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        args += [lat - 0.0051, lat + 0.0051, lon - 0.0051, lon + 0.0051]
    conn = connect(udir)
    total = conn.execute(f"SELECT COUNT(*) FROM entries WHERE {_IMG} AND {where}", args).fetchone()[0] if want_total else -1
    rows = conn.execute(
        f"SELECT path,data FROM entries WHERE {_IMG} AND {where} "
        f"ORDER BY {_TAKEN} DESC, path LIMIT ? OFFSET ?", args + [int(limit), int(offset)]).fetchall()
    return [(p, json.loads(d)) for p, d in rows], total


def present_hashes(udir: Path, hashes: List[str]) -> List[str]:
    """Parmi `hashes` (sha-256 hex), ceux déjà dans le cloud (octets reçus OU image stockée)."""
    conn = connect(udir)
    found: List[str] = []
    for h in hashes:
        if conn.execute("SELECT 1 FROM entries WHERE type='file' AND cid IS NOT NULL AND (sha_orig=? OR sha_plain=?) LIMIT 1",
                        (h, h)).fetchone():
            found.append(h)
    return found


def exact_duplicates(udir: Path, limit: int = 300) -> List[List[Tuple[str, Dict[str, Any]]]]:
    """Groupes de fichiers au contenu IDENTIQUE (même empreinte), les plus gros gains d'abord."""
    conn = connect(udir)
    key = "COALESCE(sha_orig, sha_plain)"
    hashes = [r[0] for r in conn.execute(
        f"SELECT {key} FROM entries WHERE type='file' AND cid IS NOT NULL AND {key} IS NOT NULL "
        f"GROUP BY {key} HAVING COUNT(*)>1 ORDER BY SUM(size) DESC LIMIT ?", (limit,))]
    # Lignes des groupes en quelques requêtes INDEXÉES (sha_orig / sha_plain) — une requête par
    # groupe sur `COALESCE(...)` parcourait toute la table à chaque fois (33 s pour 300 groupes).
    by_key: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {h: [] for h in hashes}
    for i in range(0, len(hashes), 300):
        chunk = hashes[i:i + 300]
        q = ",".join("?" * len(chunk))
        for p, d in conn.execute(
                f"SELECT path,data FROM entries WHERE type='file' AND cid IS NOT NULL AND "
                f"(sha_orig IN ({q}) OR (sha_orig IS NULL AND sha_plain IN ({q})))", chunk + chunk):
            e = json.loads(d)
            k = e.get("sha256_orig") or e.get("sha256_plain")
            if k in by_key:
                by_key[k].append((p, e))
    groups = [by_key[h] for h in hashes if len(by_key[h]) > 1]
    return groups


def phash_pending(udir: Path, limit: int = 100000) -> List[str]:
    """Images sans empreinte perceptuelle (anciennes, importées avant le schéma v2), récentes d'abord."""
    return [r[0] for r in connect(udir).execute(
        f"SELECT path FROM entries WHERE {_IMG} AND phash IS NULL ORDER BY {_TAKEN} DESC LIMIT ?", (limit,))]


def phash_count(udir: Path) -> Tuple[int, int]:
    """(images avec empreinte, images au total)."""
    row = connect(udir).execute(f"SELECT SUM(CASE WHEN phash IS NOT NULL THEN 1 ELSE 0 END), COUNT(*) FROM entries WHERE {_IMG}").fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def _popcount(x: int) -> int:
    return bin(x).count("1")


def near_duplicates(udir: Path, exclude: set, max_dist: int = 6, limit: int = 300,
                    max_bucket: int = 300) -> List[List[Tuple[str, Dict[str, Any]]]]:
    """Groupes d'images QUASI identiques (redimensionnées, recompressées, recadrées
    légèrement) : distance de Hamming ≤ `max_dist` entre empreintes dHash 64 bits.
    Recherche par bandes (4 × 16 bits : deux empreintes à ≤ 3 bits d'écart partagent au
    moins une bande entière, et on vérifie ensuite la distance exacte) — O(n) au lieu de
    O(n²) sur des dizaines de milliers de photos. `exclude` : chemins déjà dans un groupe
    d'exacts. Les empreintes dégénérées (image unie : 0 ou 2⁶⁴−1) sont ignorées."""
    conn = connect(udir)
    rows = [(p, ph & ((1 << 64) - 1)) for p, ph in conn.execute(
        f"SELECT path,phash FROM entries WHERE {_IMG} AND phash IS NOT NULL")
        if p not in exclude]
    rows = [(p, h) for p, h in rows if h not in (0, (1 << 64) - 1)]
    parent = list(range(len(rows)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for band in range(4):
        buckets: Dict[int, List[int]] = {}
        for i, (_p, h) in enumerate(rows):
            buckets.setdefault((h >> (16 * band)) & 0xFFFF, []).append(i)
        for members in buckets.values():
            if len(members) < 2 or len(members) > max_bucket:
                continue
            for a in range(len(members)):
                for b in range(a + 1, len(members)):
                    i, j = members[a], members[b]
                    if find(i) != find(j) and _popcount(rows[i][1] ^ rows[j][1]) <= max_dist:
                        parent[find(i)] = find(j)
    groups: Dict[int, List[str]] = {}
    for i, (p, _h) in enumerate(rows):
        groups.setdefault(find(i), []).append(p)
    out = []
    for paths in sorted((g for g in groups.values() if len(g) > 1), key=len, reverse=True)[:limit]:
        out.append([(p, get_entry(conn, p) or {}) for p in paths])
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Carte : photos géolocalisées entre deux dates
# ═════════════════════════════════════════════════════════════════════════════
def geo_months(udir: Path) -> List[Tuple[str, int]]:
    """[(AAAA-MM, nombre de photos géolocalisées)] — l'histogramme du sélecteur de dates."""
    return [(m, n) for m, n in connect(udir).execute(
        f"SELECT strftime('%Y-%m', {_TAKEN}, 'unixepoch') AS m, COUNT(*) FROM entries "
        f"WHERE {_IMG} AND lat IS NOT NULL GROUP BY m ORDER BY m")]


def geo_points(udir: Path, since: Optional[int] = None, until: Optional[int] = None,
               limit: int = 4000) -> List[Dict[str, Any]]:
    """Lieux (cases de 0,01° ≈ 1 km, déjà l'arrondi stocké) avec le nombre de photos prises entre
    `since` et `until`, la plus récente comme couverture. Regroupé EN SQL : la carte ne reçoit
    jamais une ligne par photo."""
    where, args = _IMG + " AND lat IS NOT NULL", []
    if since is not None:
        where += f" AND {_TAKEN} >= ?"
        args.append(int(since))
    if until is not None:
        where += f" AND {_TAKEN} < ?"
        args.append(int(until))
    rows = connect(udir).execute(
        f"SELECT lat, lon, COUNT(*), MIN({_TAKEN}), MAX({_TAKEN}), path FROM entries WHERE {where} "
        f"GROUP BY lat, lon ORDER BY COUNT(*) DESC LIMIT ?", args + [int(limit)]).fetchall()
    return [{"lat": a, "lon": b, "n": n, "first": lo, "last": hi, "cover": p} for a, b, n, lo, hi, p in rows]


# ═════════════════════════════════════════════════════════════════════════════
# Souvenirs du jour et événements (regroupement par date de prise de vue)
# ═════════════════════════════════════════════════════════════════════════════
def memories(udir: Path, month: int, day: int, this_year: int, window: int = 1) -> List[Dict[str, Any]]:
    """« Il y a N ans » : photos prises le même jour (± `window` jours) lors des années PASSÉES.
    Une ligne par année : nombre de photos, couverture (la plus ancienne du jour), intervalle
    [since, until) en secondes pour ouvrir la galerie dessus. Calculé en SQL sur l'index `entries_taken`."""
    from datetime import datetime, timedelta, timezone
    out = []
    years = [y for (y,) in connect(udir).execute(
        f"SELECT DISTINCT CAST(strftime('%Y', {_TAKEN}, 'unixepoch') AS INTEGER) FROM entries WHERE {_IMG}") if y and y < this_year]
    for y in sorted(years, reverse=True):
        try:
            centre = datetime(y, month, day, tzinfo=timezone.utc)
        except ValueError:                       # 29 février d'une année non bissextile
            continue
        since = int((centre - timedelta(days=window)).timestamp())
        until = int((centre + timedelta(days=window + 1)).timestamp())
        row = connect(udir).execute(
            f"SELECT COUNT(*), path FROM entries WHERE {_IMG} AND {_TAKEN} >= ? AND {_TAKEN} < ? "
            f"ORDER BY {_TAKEN} ASC LIMIT 1", (since, until)).fetchone()
        if row and row[0]:
            out.append({"year": y, "ago": this_year - y, "n": int(row[0]), "cover": row[1], "since": since, "until": until})
    return out


def events(udir: Path, gap: int = 6 * 3600, min_photos: int = 6, limit: int = 60,
           offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """Événements : suites de photos dont deux prises consécutives sont distantes de moins de `gap`
    secondes (6 h par défaut : une soirée, une journée de balade, un week-end continu). Au moins
    `min_photos` photos. Rend (événements récents d'abord, total). Seules les dates sont lues (pas
    de JSON), le regroupement est linéaire. `cover` = photo du milieu, `lat/lon` = lieu le plus fréquent."""
    rows = connect(udir).execute(
        f"SELECT {_TAKEN}, path, lat, lon FROM entries WHERE {_IMG} AND {_TAKEN} > 0 ORDER BY {_TAKEN}").fetchall()
    groups: List[List[Tuple[int, str, Any, Any]]] = []
    for r in rows:
        if groups and r[0] - groups[-1][-1][0] <= gap:
            groups[-1].append(r)
        else:
            groups.append([r])
    evs = []
    for g in groups:
        if len(g) < min_photos:
            continue
        places: Dict[Tuple[float, float], int] = {}
        for _t, _p, la, lo in g:
            if la is not None:
                places[(la, lo)] = places.get((la, lo), 0) + 1
        best = max(places, key=places.get) if places else None
        evs.append({"since": g[0][0], "until": g[-1][0] + 1, "n": len(g), "cover": g[len(g) // 2][1],
                    "lat": best[0] if best else None, "lon": best[1] if best else None})
    evs.sort(key=lambda e: e["since"], reverse=True)
    return evs[offset:offset + limit], len(evs)


# ═════════════════════════════════════════════════════════════════════════════
# Analyses : où en est chaque tâche ? (visages, dates/lieux, descriptions)
# ═════════════════════════════════════════════════════════════════════════════
def analysis_counts(udir: Path) -> Dict[str, Any]:
    """Compteurs par tâche sur les images PROPRES (hors copies reçues, jamais analysées) — en UNE requête.
    faces : done = faces+no_face ; waiting = jamais analysée ; pending = envoyée, résultat attendu ;
    timeout = résultat jamais revenu. enrich : empreinte (phash). describe : description présente."""
    row = connect(udir).execute(
        "SELECT COUNT(*),"
        " SUM(CASE WHEN faceid_status IN ('faces','no_face') THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN faceid_status IS NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN faceid_status='pending' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN faceid_status='timeout' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN faceid_status='unreadable' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN phash IS NOT NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN desc_text IS NOT NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN lat IS NOT NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN taken_at IS NOT NULL THEN 1 ELSE 0 END) "
        f"FROM entries WHERE {_IMG} AND COALESCE(origin,'')<>'facecloud-share'").fetchone()
    keys = ("images", "faces_done", "faces_waiting", "faces_pending", "faces_timeout", "unreadable",
            "hashed", "described", "located", "dated")
    return dict(zip(keys, (int(x or 0) for x in row)))


def describe_pending(udir: Path, limit: int = 100000) -> List[str]:
    """Images sans description, récentes d'abord (les copies reçues sont décrites aussi : on les voit)."""
    return [r[0] for r in connect(udir).execute(
        f"SELECT path FROM entries WHERE {_IMG} AND desc_text IS NULL AND COALESCE(faceid_status,'')<>'unreadable' "
        f"ORDER BY {_TAKEN} DESC LIMIT ?", (limit,))]
