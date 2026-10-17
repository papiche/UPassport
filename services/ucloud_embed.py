"""
services/ucloud_embed.py — Recherche PAR LE SENS dans les photos d'un cloud FaceCloud.

Chaque description de photo (IA ou saisie) est transformée en vecteur par un modèle d'embedding d'Ollama ; une
requête (« vacances à la mer », « fête d'anniversaire ») devient un vecteur et les photos les plus proches sortent,
même sans aucun mot en commun. Ni les descriptions ni les vecteurs ne quittent la station : tout reste dans
`.ucloud/ucloud.db` (table `embeddings`, clé = CID : une photo déplacée garde son vecteur).

Modèle : `paraphrase-multilingual` (multilingue, 768 dimensions). Comparé le 2026-10-16 à `nomic-embed-text` sur 12
requêtes en français : 11/12 bonnes photos en tête contre 8/12, et un écart net entre les bonnes réponses (cosinus
≈ 0,68) et les autres (≈ 0,20), ce qui permet un seuil de coupure fiable.

Classement HYBRIDE : cosinus sémantique + bonus quand TOUS les mots de la requête sont dans la description ou le nom
du fichier (un nom propre, un mot rare). Les photos sans vecteur (pas encore indexées) restent trouvables par mots.

Bibliothèque standard + numpy + ucloud_db : appelé aussi par `ucloud_embed.py` (script).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from services import trace, ucloud_db

MODEL = os.environ.get("FACECLOUD_EMBED_MODEL", "paraphrase-multilingual:latest")
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL
BATCH = 32
# Cosinus minimal pour qu'une photo soit « proche » : dépend du modèle (échelle des cosinus). Mesuré le 2026-10-16 sur
# 39 requêtes françaises / 30 descriptions : paraphrase-multilingual bons ≈ 0,60 autres ≈ 0,21 ; bge-m3 bons ≈ 0,58 autres
# ≈ 0,40 ; qwen3-embedding:4b bons ≈ 0,55 autres ≈ 0,29. Valeurs prudentes (on préfère un résultat de trop à un oubli).
MIN_COSINE_BY_MODEL = {"paraphrase-multilingual": 0.30, "bge-m3": 0.45, "qwen3-embedding": 0.36, "nomic-embed-text": 0.50}


def min_cosine(model: str = "") -> float:
    base = (model or MODEL).split(":")[0]
    return MIN_COSINE_BY_MODEL.get(base, 0.40)

KEEP_BELOW_TOP = 0.28        # on garde ce qui reste dans cet écart du meilleur résultat
KEYWORD_BONUS = 0.5          # tous les mots de la requête présents : passe devant les simples ressemblances
MAX_RESULTS = 3000

_cache_lock = threading.Lock()
_matrix_cache: Dict[str, Tuple[Any, List[str], np.ndarray]] = {}


class EmbedError(RuntimeError):
    pass


def embed_texts(texts: List[str], model: str = MODEL, timeout: float = 120.0) -> np.ndarray:
    """Vecteurs normalisés (n × d), float32. Lève EmbedError si Ollama ne répond pas ou n'a pas le modèle."""
    body = json.dumps({"model": model, "input": texts, "keep_alive": "10m"}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", body, {"Content-Type": "application/json"})
    trace.log("embed", f"POST {OLLAMA_URL}/api/embed modèle={model} {len(texts)} texte(s), {sum(map(len, texts))} caractères", 2)
    try:
        with trace.Timer() as t_http, urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        trace.log("embed", f"réponse en {t_http.s:.2f}s : {len(data.get('embeddings') or [])} vecteur(s), chargement modèle "
                           f"{(data.get('load_duration') or 0) / 1e9:.2f}s, {data.get('prompt_eval_count', '?')} jetons", 2)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise EmbedError(f"Ollama HTTP {exc.code} : {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EmbedError(f"Ollama injoignable : {exc}") from exc
    vecs = np.array(data.get("embeddings") or [], dtype=np.float32)
    if vecs.ndim != 2 or len(vecs) != len(texts):
        raise EmbedError("réponse d'embedding invalide")
    vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
    return vecs


_KW = re.compile(r"mots?[- ]cl[ée]s?\s*:\s*(.+)$", re.I | re.S)


def text_for(desc: str, path: str = "") -> str:
    """Texte embarqué : les mots-clés D'ABORD (le modèle ne lit que ~128 mots : la fin serait coupée), puis la phrase."""
    desc = (desc or "").strip()
    m = _KW.search(desc)
    if m:
        body = desc[:m.start()].strip()
        return f"{m.group(1).strip()}. {body}"
    return desc


_qcache: Dict[str, np.ndarray] = {}


def query_vec(q: str, model: str = MODEL) -> np.ndarray:
    """Vecteur d'une requête (mémoïsé : paginer ou filtrer ne rappelle pas Ollama)."""
    key = f"{model}|{q.strip().lower()}"
    v = _qcache.get(key)
    if v is None:
        v = embed_texts([q.strip()], model)[0]
        if len(_qcache) > 256:
            _qcache.clear()
        _qcache[key] = v
    return v


def ensure_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS embeddings(cid TEXT PRIMARY KEY, model TEXT NOT NULL, dsrc TEXT, vec BLOB NOT NULL) WITHOUT ROWID")


def _blob(v: np.ndarray) -> bytes:
    return v.astype(np.float16).tobytes()


def counts(udir: Path, model: str = MODEL) -> Dict[str, int]:
    """{described, indexed, pending} : photos décrites / déjà vectorisées avec CE modèle et CETTE description / restantes."""
    conn = ucloud_db.connect(udir)
    ensure_table(conn)
    described = conn.execute(f"SELECT COUNT(*) FROM entries WHERE {ucloud_db._IMG} AND desc_text IS NOT NULL").fetchone()[0]
    indexed = conn.execute(
        f"SELECT COUNT(*) FROM entries e JOIN embeddings m ON m.cid=e.cid WHERE {ucloud_db._IMG.replace('type=', 'e.type=').replace('cid IS', 'e.cid IS').replace('mime', 'e.mime')} "
        "AND e.desc_text IS NOT NULL AND m.model=? AND m.dsrc=e.desc_text", (model,)).fetchone()[0]
    return {"described": described, "indexed": indexed, "pending": max(0, described - indexed)}


def pending(udir: Path, model: str = MODEL, limit: int = 100000) -> List[Tuple[str, str, str]]:
    """[(cid, desc_text normalisé, texte lisible de la description)] à vectoriser (plus récentes d'abord)."""
    conn = ucloud_db.connect(udir)
    ensure_table(conn)
    excl, xp = ucloud_db._prefs().sql_not_excluded(udir, "e.path")     # dossiers exclus par le membre
    rows = conn.execute(
        "SELECT e.cid, e.desc_text, json_extract(e.data,'$.desc.text') FROM entries e LEFT JOIN embeddings m ON m.cid=e.cid "
        "WHERE e.type='file' AND e.cid IS NOT NULL AND e.mime LIKE 'image/%' AND e.desc_text IS NOT NULL "
        f"AND (m.cid IS NULL OR m.model<>? OR m.dsrc IS NOT e.desc_text) {excl} "
        f"ORDER BY {ucloud_db._TAKEN.replace('taken_at', 'e.taken_at').replace('mtime', 'e.mtime')} DESC LIMIT ?", (model, *xp, limit)).fetchall()
    seen, out = set(), []
    for cid, norm, text in rows:           # une photo en plusieurs exemplaires (même CID) : un seul calcul
        if cid not in seen:
            seen.add(cid)
            out.append((cid, norm, text))
    return out


def store(udir: Path, items: List[Tuple[str, str]], vecs: np.ndarray, model: str = MODEL) -> None:
    """items = [(cid, desc_text normalisé)] ; vecs alignés."""
    conn = ucloud_db.connect(udir)
    ensure_table(conn)
    conn.execute("BEGIN")
    try:
        conn.executemany("INSERT OR REPLACE INTO embeddings(cid,model,dsrc,vec) VALUES(?,?,?,?)",
                         [(cid, model, dsrc, _blob(v)) for (cid, dsrc), v in zip(items, vecs)])
        # Génération des vecteurs : le job tourne dans un AUTRE processus que le serveur, qui doit recharger sa matrice.
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('emb_gen',?)", (str(time.time_ns()),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _matrix(udir: Path, model: str) -> Tuple[List[str], np.ndarray]:
    """(CID, matrice n × d float32), mémoïsée par révision de l'index (rechargée si l'index change)."""
    key = f"{udir}|{model}"
    conn = ucloud_db.connect(udir)
    ensure_table(conn)
    gen = conn.execute("SELECT value FROM meta WHERE key='emb_gen'").fetchone()
    rev = (ucloud_db.rev(udir), gen[0] if gen else "")
    with _cache_lock:
        hit = _matrix_cache.get(key)
        if hit and hit[0] == rev:
            return hit[1], hit[2]
    rows = conn.execute("SELECT cid, vec FROM embeddings WHERE model=?", (model,)).fetchall()
    cids = [r[0] for r in rows]
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1).astype(np.float32) if rows else np.zeros((0, 1), np.float32)
    with _cache_lock:
        _matrix_cache[key] = (rev, cids, mat)
    return cids, mat


def _where(flt: str, since: Optional[int], until: Optional[int], lat: Optional[float], lon: Optional[float]) -> Tuple[str, list]:
    where, args = ucloud_db._FILTERS.get(flt, "1"), []
    if since is not None:
        where += f" AND {ucloud_db._TAKEN} >= ?"; args.append(int(since))
    if until is not None:
        where += f" AND {ucloud_db._TAKEN} < ?"; args.append(int(until))
    if lat is not None and lon is not None:
        where += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        args += [lat - 0.0051, lat + 0.0051, lon - 0.0051, lon + 0.0051]
    return where, args


def search(udir: Path, q: str, qvec: Optional[np.ndarray], flt: str = "all", since: Optional[int] = None,
           until: Optional[int] = None, lat: Optional[float] = None, lon: Optional[float] = None,
           model: str = MODEL) -> List[Tuple[str, float]]:
    """[(chemin, score)] du meilleur au moins bon. `qvec` None : mots seulement (Ollama indisponible)."""
    where, args = _where(flt, since, until, lat, lon)
    conn = ucloud_db.connect(udir)
    terms = ucloud_db.search_terms(q)
    scores: Dict[str, float] = {}

    # 1. Par le sens : cosinus entre la requête et chaque photo vectorisée.
    if qvec is not None:
        cids, mat = _matrix(udir, model)
        if len(cids) and mat.shape[1] == len(qvec):
            cos = mat @ qvec
            cand = np.where(cos >= min_cosine(model))[0]
            if len(cand):
                top = float(cos[cand].max())
                cand = cand[cos[cand] >= top - KEEP_BELOW_TOP]
                best = {cids[i]: float(cos[i]) for i in cand}
                for path, cid in conn.execute(f"SELECT path, cid FROM entries WHERE {ucloud_db._IMG} AND {where} AND cid IN "
                                              f"({','.join('?' * min(len(best), 900))})", args + list(best)[:900]) if best else []:
                    scores[path] = max(scores.get(path, 0.0), best[cid])
                if len(best) > 900:                              # gros résultat : par paquets (limite de variables SQLite)
                    keys = list(best)
                    for i in range(900, len(keys), 900):
                        chunk = keys[i:i + 900]
                        for path, cid in conn.execute(f"SELECT path, cid FROM entries WHERE {ucloud_db._IMG} AND {where} AND cid IN "
                                                      f"({','.join('?' * len(chunk))})", args + chunk):
                            scores[path] = max(scores.get(path, 0.0), best[cid])

    # 2. Par les mots : tous les mots dans le chemin OU la description (accents, casse, pluriel ignorés).
    if terms:
        kw_where, kw_args = "", []
        for t in terms:
            like = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            kw_where += " AND (path LIKE ? ESCAPE '\\' OR desc_text LIKE ? ESCAPE '\\')"
            kw_args += [like, like]
        for (path,) in conn.execute(f"SELECT path FROM entries WHERE {ucloud_db._IMG} AND {where}{kw_where} LIMIT {MAX_RESULTS}", args + kw_args):
            scores[path] = scores.get(path, 0.0) + KEYWORD_BONUS

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:MAX_RESULTS]


def entries_for(udir: Path, paths: List[str]) -> List[Tuple[str, Dict[str, Any]]]:
    conn = ucloud_db.connect(udir)
    out = []
    for p in paths:
        e = ucloud_db.get_entry(conn, p)
        if e:
            out.append((p, e))
    return out
