"""
services/calendar_store.py — Agendas chiffrés du MULTIPASS (iCalendar).

Même schéma que services/contacts_store.py (carnet CardDAV), pour l'agenda :
  * le serveur CalDAV (routers/caldav.py) synchronisé par téléphone et
    Thunderbird (DAVx5, iOS/macOS Calendrier, Thunderbird) ;
  * le pont NOSTR (calendar_nostr_bridge.py) qui reflète chaque événement en
    kind 31922 (NIP-52, même format que UPlanet/earth/calendars.html) ;
  * « Mes calendriers » (calendars.html) qui lit ces mêmes événements.

Stockage : chaque ressource est un .ics rangé dans le cloud chiffré sous
`/Calendar/<agenda>/<nom>.ics` (AES-256-GCM, une clé par fichier, IPFS ne voit
jamais le clair ; visible aussi sur le disque monté). `<agenda>` est l'identifiant
(d-tag NIP-52 kind 31924) ; `personal` existe toujours.

Résumé chiffré — `.ucloud/calendar.enc` (clé `.ucloud/calendar.key`, 0600) :
titre, dates, ETag, CID IPFS et jeton de synchro de chaque ressource, plus le
journal RFC 6578 (compteur `sync`, suppressions récentes) et l'état du pont
NOSTR (`nostr` : d-tag, id et date du dernier événement, empreinte poussée).
Le pont n'est PAS dans UPassport : le NSEC n'est jamais chargé dans ce process
(cf. cloud_storage) — il vit dans un process court (calendar_nostr_bridge.py).
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from services import cloud_storage

logger = logging.getLogger(__name__)

CAL_DIR = "/Calendar"
DEFAULT_CAL = "personal"
DEFAULT_CAL_NAME = "Mon agenda"
MAX_ICS_BYTES = 1024 * 1024
DELETED_KEEP = 2000
_NAME_RE = re.compile(r"^[A-Za-z0-9._@+=-]{1,120}\.ics$")
_CAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_COMPONENTS = ("vevent", "vtodo", "vjournal")


class CalendarError(ValueError):
    """Requête invalide (iCalendar illisible, nom refusé…)."""


class PreconditionFailed(Exception):
    """If-Match / If-None-Match non satisfait (HTTP 412)."""


# ═════════════════════════════════════════════════════════════════════════════
# Verrou + résumé chiffré
# ═════════════════════════════════════════════════════════════════════════════
def _udir(email: str):
    return cloud_storage.ensure_ucloud_dir(email)


@contextmanager
def _lock(email: str) -> Iterator[None]:
    fd = os.open(str(_udir(email) / ".calendar.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _key(email: str) -> str:
    path = _udir(email) / "calendar.key"
    try:
        key = path.read_text().strip()
        if len(key) == 64:
            return key
    except OSError:
        pass
    key = os.urandom(32).hex()
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


def _empty() -> Dict[str, Any]:
    return {"version": 1, "sync": 1, "cals": {DEFAULT_CAL: {"name": DEFAULT_CAL_NAME, "token": 1}},
            "items": {}, "deleted": []}


def _read_summary(email: str) -> Dict[str, Any]:
    path = _udir(email) / "calendar.enc"
    if not path.exists():
        return _empty()
    try:
        clear = cloud_storage.uenc_codec.decrypt_aes256gcm(path.read_bytes(), _key(email))
        data = json.loads(clear.decode("utf-8"))
    except Exception as exc:
        logger.error("calendar: résumé illisible pour %s (%s) — reconstruit", email, exc)
        return _empty()
    data.setdefault("items", {})
    data.setdefault("deleted", [])
    data.setdefault("sync", 1)
    data.setdefault("cals", {})
    data["cals"].setdefault(DEFAULT_CAL, {"name": DEFAULT_CAL_NAME, "token": 1})
    return data


def _write_summary(email: str, data: Dict[str, Any]) -> None:
    payload, _iv = cloud_storage.uenc_codec.encrypt_aes256gcm(
        json.dumps(data, ensure_ascii=False).encode("utf-8"), _key(email))
    path = _udir(email) / "calendar.enc"
    tmp = path.with_name(".calendar.enc.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _path(cal: str, name: str) -> str:
    return f"{CAL_DIR}/{cal}/{name}"


def item_key(cal: str, name: str) -> str:
    return f"{cal}/{name}"


def check_cal(cal: str) -> str:
    if not _CAL_RE.match(cal or ""):
        raise CalendarError(f"identifiant d'agenda refusé : {cal!r}")
    return cal


def check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise CalendarError(f"nom de ressource refusé : {name!r}")
    return name


# ═════════════════════════════════════════════════════════════════════════════
# iCalendar ↔ champs
# ═════════════════════════════════════════════════════════════════════════════
def _vobject():
    import vobject  # noqa: WPS433 — import paresseux (UPassport démarre sans)
    return vobject


def parse(text: str):
    """iCalendar texte → objet vobject VCALENDAR (lève CalendarError)."""
    try:
        cal = _vobject().readOne(text)
    except Exception as exc:
        raise CalendarError(f"iCalendar illisible : {exc}") from exc
    if getattr(cal, "name", "").upper() != "VCALENDAR":
        raise CalendarError("ce n'est pas un VCALENDAR")
    if not any(cal.contents.get(c) for c in _COMPONENTS):
        raise CalendarError("aucun VEVENT / VTODO / VJOURNAL")
    return cal


def split_ics(text: str) -> List[str]:
    """Un fichier .ics peut contenir plusieurs calendriers (import)."""
    return [m.group(0) for m in re.finditer(r"BEGIN:VCALENDAR.*?END:VCALENDAR", text, re.S | re.I)]


def _iso(value: Any) -> Tuple[str, bool]:
    """(valeur ISO 8601, journée entière) d'un DTSTART/DTEND vobject."""
    if isinstance(value, _dt.datetime):
        return value.isoformat(), False
    if isinstance(value, _dt.date):
        return value.isoformat(), True
    return "", False


def summarize(text: str) -> Dict[str, Any]:
    """Champs affichés et mis en miroir : ceux du composant principal (celui
    sans RECURRENCE-ID ; les exceptions d'une récurrence restent dans le .ics)."""
    cal = parse(text)
    for kind in _COMPONENTS:
        comps = cal.contents.get(kind, [])
        if comps:
            break
    main = next((c for c in comps if "recurrence-id" not in c.contents), comps[0])

    def val(prop: str) -> str:
        return str(main.contents[prop][0].value).strip() if prop in main.contents else ""

    start, all_day = ("", False)
    if "dtstart" in main.contents:
        start, all_day = _iso(main.dtstart.value)
    end = ""
    if "dtend" in main.contents:
        end = _iso(main.dtend.value)[0]
    elif "due" in main.contents:
        end = _iso(main.due.value)[0]
    return {
        "uid": val("uid"), "kind": kind.upper(), "summary": val("summary"), "description": val("description"),
        "location": val("location"), "start": start, "end": end, "all_day": all_day,
        "rrule": val("rrule"), "url": val("url"),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Lecture
# ═════════════════════════════════════════════════════════════════════════════
def _etag(sha: str) -> str:
    return '"' + sha[:32] + '"'


def _refresh_from_index(email: str, data: Dict[str, Any]) -> bool:
    """Réconcilie le résumé avec l'index du cloud : ressources ajoutées,
    modifiées ou supprimées PAR LE DISQUE MONTÉ (hors de ce module)."""
    rows = cloud_storage.db(email).execute(
        "SELECT path,data FROM entries WHERE parent=? OR parent LIKE ?", (CAL_DIR, CAL_DIR + "/%"))
    present: Dict[str, Tuple[str, str, str, str]] = {}
    for path, raw in rows:
        e = json.loads(raw)
        parts = path[len(CAL_DIR) + 1:].split("/") if path.startswith(CAL_DIR + "/") else []
        if len(parts) == 2 and isinstance(e, dict) and e.get("type") == "file" \
                and _CAL_RE.match(parts[0]) and _NAME_RE.match(parts[1]):
            present[item_key(parts[0], parts[1])] = (parts[0], parts[1], e.get("sha256_plain") or "", e.get("cid") or "")
    changed = False
    for key, (cal, name, sha, cid) in present.items():
        item = data["items"].get(key)
        if cal not in data["cals"]:
            data["sync"] += 1
            data["cals"][cal] = {"name": cal, "token": data["sync"]}
            changed = True
        if item and item.get("sha") == sha:
            continue
        try:
            text = cloud_storage.read_plaintext(email, _path(cal, name))[0].decode("utf-8", "replace")
            info = summarize(text)
        except Exception as exc:
            logger.warning("calendar: %s illisible (%s) — ignorée", key, exc)
            continue
        data["sync"] += 1
        info.update(cal=cal, name=name, sha=sha, cid=cid, etag=_etag(sha), token=data["sync"],
                    mtime=int(time.time()), nostr=(item or {}).get("nostr"))
        data["items"][key] = info
        changed = True
    for key in [k for k in data["items"] if k not in present]:
        data["sync"] += 1
        old = data["items"].pop(key)
        data["deleted"].append({"key": key, "cal": old["cal"], "token": data["sync"], "nostr": old.get("nostr")})
        changed = True
    data["deleted"] = data["deleted"][-DELETED_KEEP:]
    return changed


def load(email: str) -> Dict[str, Any]:
    """Résumé à jour (réconcilié avec le disque monté)."""
    with _lock(email):
        data = _read_summary(email)
        if _refresh_from_index(email, data):
            _write_summary(email, data)
        return data


def get_text(email: str, cal: str, name: str) -> str:
    check_cal(cal)
    check_name(name)
    try:
        return cloud_storage.read_plaintext(email, _path(cal, name))[0].decode("utf-8", "replace")
    except FileNotFoundError as exc:
        raise KeyError(item_key(cal, name)) from exc


def items_of(data: Dict[str, Any], cal: str) -> List[Dict[str, Any]]:
    return [i for i in data["items"].values() if i["cal"] == cal]


def find_by_uid(data: Dict[str, Any], cal: str, uid: str) -> Optional[str]:
    for i in items_of(data, cal):
        if i.get("uid") == uid:
            return i["name"]
    return None


def ctag(data: Dict[str, Any], cal: str) -> str:
    """Change à chaque modification de l'agenda `cal` (et seulement de lui)."""
    tokens = [i["token"] for i in items_of(data, cal)] + [d["token"] for d in data["deleted"] if d["cal"] == cal]
    tokens.append(data["cals"].get(cal, {}).get("token", 1))
    return str(max(tokens))


def changes_since(data: Dict[str, Any], cal: str, token: Optional[int]) -> Optional[Tuple[List[str], List[str]]]:
    """(noms modifiés, noms supprimés) de `cal` depuis `token`, ou None si le
    jeton est trop ancien ou inconnu (le client resynchronise tout)."""
    if token is None:
        return [i["name"] for i in items_of(data, cal)], []
    oldest = data["deleted"][0]["token"] if len(data["deleted"]) >= DELETED_KEEP else 0
    if token < oldest or token > data["sync"]:
        return None
    changed = [i["name"] for i in items_of(data, cal) if i.get("token", 0) > token]
    alive = {i["name"] for i in items_of(data, cal)}
    deleted = sorted({d["key"].split("/", 1)[1] for d in data["deleted"]
                      if d["cal"] == cal and d["token"] > token and d["key"].split("/", 1)[1] not in alive})
    return changed, deleted


# ═════════════════════════════════════════════════════════════════════════════
# Écriture
# ═════════════════════════════════════════════════════════════════════════════
def _ensure_dir(email: str, cal: str) -> None:
    with cloud_storage.index_lock(email):
        with cloud_storage.ucloud_db.transaction(cloud_storage.ucloud_dir(email)) as conn:
            cloud_storage.ucloud_db.ensure_parents(conn, _path(cal, "x.ics"))


def ensure_calendar(email: str, cal: str, name: Optional[str] = None) -> Dict[str, Any]:
    """Déclare un agenda (ex. découvert côté NOSTR, kind 31924). Idempotent."""
    check_cal(cal)
    with _lock(email):
        data = _read_summary(email)
        _refresh_from_index(email, data)
        entry = data["cals"].get(cal)
        if entry is None or (name and entry.get("name") != name):
            data["sync"] += 1
            data["cals"][cal] = {"name": name or (entry or {}).get("name") or cal, "token": data["sync"]}
            _write_summary(email, data)
        return data["cals"][cal]


def put(email: str, cal: str, name: str, text: str, *, if_match: Optional[str] = None,
        if_none_match: bool = False, nostr: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Crée ou remplace une ressource. `if_match` / `if_none_match` appliquent
    la sémantique HTTP de CalDAV (PreconditionFailed → 412). `nostr` : état du
    pont à enregistrer (sinon l'état précédent est conservé)."""
    check_cal(cal)
    check_name(name)
    raw = text.encode("utf-8")
    if len(raw) > MAX_ICS_BYTES:
        raise CalendarError(f"ressource trop volumineuse (> {MAX_ICS_BYTES // 1024} Ko)")
    info = summarize(text)
    _ensure_dir(email, cal)
    key = item_key(cal, name)
    with _lock(email):
        data = _read_summary(email)
        _refresh_from_index(email, data)
        current = data["items"].get(key)
        if if_none_match and current:
            raise PreconditionFailed(current["etag"])
        if if_match and (not current or current["etag"] != if_match):
            raise PreconditionFailed(current["etag"] if current else "")
        entry = cloud_storage.ingest_plaintext(email, _path(cal, name), raw,
                                               content_type="text/calendar", trigger_faceid=False)
        data["sync"] += 1
        if cal not in data["cals"]:
            data["cals"][cal] = {"name": cal, "token": data["sync"]}
        sha = entry.get("sha256_plain") or hashlib.sha256(raw).hexdigest()
        info.update(cal=cal, name=name, sha=sha, cid=entry.get("cid") or "", etag=_etag(sha),
                    token=data["sync"], mtime=int(time.time()),
                    nostr=nostr if nostr is not None else (current or {}).get("nostr"))
        data["items"][key] = info
        _write_summary(email, data)
        return info


def delete(email: str, cal: str, name: str, if_match: Optional[str] = None) -> bool:
    check_cal(cal)
    check_name(name)
    key = item_key(cal, name)
    with _lock(email):
        data = _read_summary(email)
        _refresh_from_index(email, data)
        current = data["items"].get(key)
        if not current:
            return False
        if if_match and current["etag"] != if_match:
            raise PreconditionFailed(current["etag"])
        cloud_storage.delete_paths(email, [_path(cal, name)])
        data["sync"] += 1
        data["deleted"].append({"key": key, "cal": cal, "token": data["sync"], "nostr": current.get("nostr")})
        data["deleted"] = data["deleted"][-DELETED_KEEP:]
        del data["items"][key]
        _write_summary(email, data)
        return True


def set_nostr(email: str, key: str, state: Optional[Dict[str, Any]]) -> None:
    """État du pont NOSTR d'une ressource, SANS toucher au .ics ni au jeton de
    synchro (ce n'est pas un changement d'événement)."""
    with _lock(email):
        data = _read_summary(email)
        if key in data["items"]:
            data["items"][key]["nostr"] = state
            _write_summary(email, data)


def ack_deleted(email: str, keys: List[str]) -> None:
    """Marque des suppressions comme déjà relayées sur NOSTR (kind 5 publié)."""
    wanted = set(keys)
    with _lock(email):
        data = _read_summary(email)
        for d in data["deleted"]:
            if d["key"] in wanted and d.get("nostr"):
                d["nostr"] = dict(d["nostr"], deleted=True)
        _write_summary(email, data)


def new_name() -> str:
    return f"{uuid.uuid4()}.ics"
