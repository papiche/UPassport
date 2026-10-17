"""
services/analysis_prefs.py — Préférences d'analyse du cloud personnel (`.ucloud/analysis_prefs.json`).

Le membre choisit CE QUE la station analyse de son cloud :
  faces     reconnaissance des visages (FaceCloud)         — défaut : oui
  describe  descriptions des photos par l'IA                — défaut : oui
  embed     recherche par le sens (vectorisation)           — défaut : oui
  exclude   dossiers que les analyses ignorent (préfixes de chemin DAV, ex. "/Photos/Perso")

Appliquées par : ucloud_db.describe_pending/describe_stale, ucloud_embed.pending (sélection),
ucloud_daily.plan (étapes), faceid_reprocess + cloud_storage._trigger_faceid_analysis (visages).
Un fichier exclu n'est ni supprimé ni caché : il reste dans le cloud, simplement jamais analysé.
Stdlib seule : importé aussi par satellite_face_matcher via ucloud_db.
"""
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

PREFS_FILE = "analysis_prefs.json"
DEFAULTS: Dict[str, Any] = {"faces": True, "describe": True, "embed": True, "exclude": []}
MAX_EXCLUDES = 50


def _norm(prefix: str) -> str:
    p = "/" + str(prefix or "").strip().replace("\\", "/").strip("/")
    return re.sub(r"/{2,}", "/", p)


def load(udir: Path) -> Dict[str, Any]:
    out = dict(DEFAULTS, exclude=[])
    try:
        d = json.loads((Path(udir) / PREFS_FILE).read_text(encoding="utf-8"))
        for k in ("faces", "describe", "embed"):
            if k in d:
                out[k] = bool(d[k])
        out["exclude"] = [p for p in dict.fromkeys(_norm(x) for x in d.get("exclude", [])) if p != "/"][:MAX_EXCLUDES]
    except Exception:
        pass
    return out


def save(udir: Path, prefs: Dict[str, Any]) -> Dict[str, Any]:
    cur = load(udir)
    for k in ("faces", "describe", "embed"):
        if k in prefs:
            cur[k] = bool(prefs[k])
    if "exclude" in prefs:
        if not isinstance(prefs["exclude"], list) or len(prefs["exclude"]) > MAX_EXCLUDES:
            raise ValueError("exclude : liste de 50 dossiers au plus")
        ex = []
        for x in prefs["exclude"]:
            p = _norm(x)
            if p == "/" or ".." in p.split("/") or len(p) > 300:
                raise ValueError(f"dossier invalide : {x!r}")
            ex.append(p)
        cur["exclude"] = list(dict.fromkeys(ex))
    p = Path(udir) / PREFS_FILE
    tmp = p.with_name(f".{PREFS_FILE}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return cur


def is_excluded(udir: Path, path: str, prefs: Dict[str, Any] = None) -> bool:
    prefs = prefs or load(udir)
    path = "/" + str(path).lstrip("/")
    return any(path == p or path.startswith(p + "/") for p in prefs["exclude"])


def sql_not_excluded(udir: Path, col: str = "path") -> Tuple[str, List[str]]:
    """Fragment « AND NOT (… ) » à ajouter à une requête sur `col` + ses paramètres ('' si rien n'est exclu)."""
    ex = load(udir)["exclude"]
    if not ex:
        return "", []
    clauses, params = [], []
    for p in ex:
        like = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
        clauses.append(f"({col} = ? OR {col} LIKE ? ESCAPE '\\')")
        params += [p, like]
    return " AND NOT (" + " OR ".join(clauses) + ")", params
