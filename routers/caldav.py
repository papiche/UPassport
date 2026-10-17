"""
routers/caldav.py — Serveur CalDAV (RFC 4791) des agendas du MULTIPASS.

Permet à un téléphone ou un logiciel de messagerie de SYNCHRONISER les agendas
chiffrés (services/calendar_store.py) : DAVx5 (Android), iOS / macOS (Calendrier),
Thunderbird. Mêmes identifiants que le disque WebDAV et le carnet CardDAV
(Basic `email:dav_token`, délivré par POST /api/cloud/enroll).

Arborescence exposée (indépendante de l'email — l'utilisateur est résolu depuis
l'authentification, jamais depuis l'URL) :

    /.well-known/caldav               → redirection 301 vers /caldav/
    /caldav/                          racine (current-user-principal)
    /caldav/principal/                principal (calendar-home-set)
    /caldav/calendars/                home des agendas
    /caldav/calendars/<agenda>/       un agenda (resourcetype calendar)
    /caldav/calendars/<agenda>/<n>.ics  une ressource (GET / PUT / DELETE)

Rapports : calendar-multiget, calendar-query (filtre de composant et de
période ; les récurrences sont toujours renvoyées, le client les développe),
sync-collection (RFC 6578). Écritures protégées par If-Match / If-None-Match
(412) et unicité de l'UID (409). Chaque écriture déclenche le pont NOSTR
(calendar_nostr_bridge.py, process séparé : le NSEC ne vit jamais ici) et une
lecture le déclenche aussi, au plus toutes les 2 min, pour ramener ce qui a été
créé depuis calendars.html. Exclu du limiteur de débit, comme /dav.
"""

from __future__ import annotations

import base64
import datetime as _dt
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from services import calendar_store, cloud_storage

logger = logging.getLogger(__name__)
router = APIRouter()

NS_D = "DAV:"
NS_C = "urn:ietf:params:xml:ns:caldav"
NS_CS = "http://calendarserver.org/ns/"
NS_IC = "http://apple.com/ns/ical/"
ET.register_namespace("d", NS_D)
ET.register_namespace("cal", NS_C)
ET.register_namespace("cs", NS_CS)
ET.register_namespace("ic", NS_IC)

ROOT = "/caldav/"
PRINCIPAL = "/caldav/principal/"
HOME = "/caldav/calendars/"
SYNC_PREFIX = "https://facecloud.uplanet/calsync/"
ALLOW = "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, REPORT"
METHODS = ["OPTIONS", "GET", "HEAD", "PUT", "DELETE", "PROPFIND", "PROPPATCH", "REPORT", "MKCALENDAR"]
BRIDGE_THROTTLE_S = 120
BRIDGE_SCRIPT = Path(__file__).resolve().parent.parent / "calendar_nostr_bridge.py"


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


# ═════════════════════════════════════════════════════════════════════════════
# Authentification (Basic email:dav_token)
# ═════════════════════════════════════════════════════════════════════════════
def _auth(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
    except Exception:
        return None
    return cloud_storage.verify_basic_credentials(user, password)


def _unauthorized() -> Response:
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="FaceCloud"'})


# ═════════════════════════════════════════════════════════════════════════════
# XML
# ═════════════════════════════════════════════════════════════════════════════
def _xml(root: ET.Element, status: int = 207, headers: Optional[Dict[str, str]] = None) -> Response:
    body = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")
    h = {"DAV": "1, 3, calendar-access"}
    h.update(headers or {})
    return Response(content=body, status_code=status, media_type="application/xml; charset=utf-8", headers=h)


def _requested_props(body: bytes) -> Tuple[Optional[List[str]], bool]:
    """(propriétés demandées en notation {ns}tag, allprop). Corps vide =
    allprop (RFC 4918 §9.1)."""
    if not body or not body.strip():
        return None, True
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, True
    if root.find(q(NS_D, "allprop")) is not None:
        return None, True
    prop = root.find(q(NS_D, "prop"))
    if prop is None:
        return None, True
    return [child.tag for child in prop], False


def _response(parent: ET.Element, href: str, found: Dict[str, object], missing: List[str]) -> None:
    """Ajoute un <d:response> : `found` = {tag: valeur} où valeur est du texte,
    un Element, une liste d'Elements, ou None (propriété vide)."""
    resp = ET.SubElement(parent, q(NS_D, "response"))
    ET.SubElement(resp, q(NS_D, "href")).text = href
    if found:
        ps = ET.SubElement(resp, q(NS_D, "propstat"))
        prop = ET.SubElement(ps, q(NS_D, "prop"))
        for tag, value in found.items():
            el = ET.SubElement(prop, tag)
            if isinstance(value, ET.Element):
                el.append(value)
            elif isinstance(value, list):
                for v in value:
                    el.append(v)
            elif value is not None:
                el.text = str(value)
        ET.SubElement(ps, q(NS_D, "status")).text = "HTTP/1.1 200 OK"
    if missing:
        ps = ET.SubElement(resp, q(NS_D, "propstat"))
        prop = ET.SubElement(ps, q(NS_D, "prop"))
        for tag in missing:
            ET.SubElement(prop, tag)
        ET.SubElement(ps, q(NS_D, "status")).text = "HTTP/1.1 404 Not Found"


def _el(ns: str, tag: str, text: Optional[str] = None, children: Optional[List[ET.Element]] = None) -> ET.Element:
    el = ET.Element(q(ns, tag))
    if text is not None:
        el.text = text
    for c in children or []:
        el.append(c)
    return el


def _href(path: str) -> ET.Element:
    return _el(NS_D, "href", path)


def _collection_types(*extra: ET.Element) -> List[ET.Element]:
    return [_el(NS_D, "collection"), *extra]


def _privileges() -> List[ET.Element]:
    return [_el(NS_D, "privilege", children=[_el(NS_D, p)])
            for p in ("read", "write", "write-content", "bind", "unbind", "read-current-user-privilege-set")]


def _select(available: Dict[str, object], wanted: Optional[List[str]], allprop: bool) -> Tuple[Dict[str, object], List[str]]:
    if allprop or wanted is None:
        return available, []
    found = {t: available[t] for t in wanted if t in available}
    missing = [t for t in wanted if t not in available]
    return found, missing


# ═════════════════════════════════════════════════════════════════════════════
# Pont NOSTR (process séparé, jamais bloquant)
# ═════════════════════════════════════════════════════════════════════════════
def _spawn_bridge(email: str) -> None:
    subprocess.Popen([sys.executable, str(BRIDGE_SCRIPT), "sync", email], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _kick_bridge(email: str, force: bool = False) -> None:
    """Lance le pont (détaché). Sans `force`, au plus une fois par BRIDGE_THROTTLE_S."""
    if os.environ.get("CALENDAR_BRIDGE", "1") == "0":
        return
    try:
        marker = cloud_storage.ensure_ucloud_dir(email) / "calendar_bridge.kick"
        if not force and marker.exists() and time.time() - marker.stat().st_mtime < BRIDGE_THROTTLE_S:
            return
        marker.touch()
        _spawn_bridge(email)
    except Exception as exc:                    # le pont ne doit jamais casser la synchro du téléphone
        logger.warning("caldav: pont NOSTR non lancé (%s)", exc)


# ═════════════════════════════════════════════════════════════════════════════
# Propriétés par type de ressource
# ═════════════════════════════════════════════════════════════════════════════
def _common(email: str) -> Dict[str, object]:
    return {
        q(NS_D, "current-user-principal"): _href(PRINCIPAL),
        q(NS_D, "principal-URL"): _href(PRINCIPAL),
        q(NS_C, "calendar-home-set"): _href(HOME),
        q(NS_C, "calendar-user-address-set"): _href("mailto:" + email),
        q(NS_D, "owner"): _href(PRINCIPAL),
        q(NS_D, "current-user-privilege-set"): _privileges(),
    }


def _root_props(email: str) -> Dict[str, object]:
    props = _common(email)
    props[q(NS_D, "resourcetype")] = _collection_types()
    props[q(NS_D, "displayname")] = "FaceCloud"
    return props


def _principal_props(email: str) -> Dict[str, object]:
    props = _common(email)
    props[q(NS_D, "resourcetype")] = [_el(NS_D, "principal")]
    props[q(NS_D, "displayname")] = email
    return props


def _cal_href(cal: str) -> str:
    return HOME + quote(cal) + "/"


def _item_href(cal: str, name: str) -> str:
    return _cal_href(cal) + quote(name)


def _cal_props(email: str, data: dict, cal: str) -> Dict[str, object]:
    meta = data["cals"][cal]
    props = _common(email)
    props.update({
        q(NS_D, "resourcetype"): _collection_types(_el(NS_C, "calendar")),
        q(NS_D, "displayname"): meta.get("name") or cal,
        q(NS_C, "calendar-description"): "Agenda chiffré du MULTIPASS (FaceCloud)",
        q(NS_CS, "getctag"): calendar_store.ctag(data, cal),
        q(NS_D, "sync-token"): SYNC_PREFIX + str(data["sync"]),
        q(NS_C, "max-resource-size"): str(calendar_store.MAX_ICS_BYTES),
        q(NS_C, "supported-calendar-component-set"): [
            ET.Element(q(NS_C, "comp"), {"name": n}) for n in ("VEVENT", "VTODO")],
        q(NS_C, "supported-calendar-data"): [
            ET.Element(q(NS_C, "calendar-data"), {"content-type": "text/calendar", "version": "2.0"})],
        q(NS_D, "supported-report-set"): [
            _el(NS_D, "supported-report", children=[_el(NS_D, "report", children=[_el(ns, r)])])
            for ns, r in ((NS_C, "calendar-multiget"), (NS_C, "calendar-query"), (NS_D, "sync-collection"))
        ],
    })
    if meta.get("color"):
        props[q(NS_IC, "calendar-color")] = meta["color"]
    return props


def _item_props(email: str, item: dict, with_data: bool) -> Dict[str, object]:
    props = {
        q(NS_D, "resourcetype"): None,
        q(NS_D, "getetag"): item["etag"],
        q(NS_D, "getcontenttype"): "text/calendar; charset=utf-8; component=" + item.get("kind", "VEVENT"),
        q(NS_D, "displayname"): item.get("summary") or item["name"],
    }
    if with_data:
        try:
            props[q(NS_C, "calendar-data")] = calendar_store.get_text(email, item["cal"], item["name"])
        except KeyError:
            pass
    return props


def _parse_path(path: str) -> Tuple[Optional[str], Optional[str]]:
    """(agenda, nom) depuis un chemin/href ; nom None pour l'agenda lui-même."""
    path = unquote(re.sub(r"^https?://[^/]+", "", path or ""))
    if not path.startswith(HOME) or path == HOME:
        return None, None
    parts = path[len(HOME):].strip("/").split("/")
    if len(parts) == 1:
        return parts[0], None
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, None


# ═════════════════════════════════════════════════════════════════════════════
# Filtres calendar-query
# ═════════════════════════════════════════════════════════════════════════════
def _to_dt(value: str) -> Optional[_dt.datetime]:
    try:
        d = _dt.datetime.fromisoformat(value)
    except ValueError:
        try:
            d = _dt.datetime.combine(_dt.date.fromisoformat(value), _dt.time())
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _ical_utc(value: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        try:
            return _dt.datetime.strptime(value, "%Y%m%d").replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return None


def _matches(item: dict, root: ET.Element) -> bool:
    """Filtre minimal : type de composant et période. Un événement récurrent
    est toujours renvoyé (le client développe lui-même la récurrence)."""
    flt = root.find(q(NS_C, "filter"))
    if flt is None:
        return True
    comp = flt.find(q(NS_C, "comp-filter"))
    if comp is None:
        return True
    inner = comp.find(q(NS_C, "comp-filter"))
    if inner is not None and inner.get("name", "").upper() != item.get("kind", "VEVENT"):
        return False
    tr = (inner if inner is not None else comp).find(q(NS_C, "time-range"))
    if tr is None or item.get("rrule"):
        return True
    start = _to_dt(item.get("start", ""))
    if start is None:
        return True
    end = _to_dt(item.get("end", "")) or start
    t0, t1 = _ical_utc(tr.get("start", "")), _ical_utc(tr.get("end", ""))
    return (t1 is None or start < t1) and (t0 is None or end > t0 or start >= t0)


# ═════════════════════════════════════════════════════════════════════════════
# Routes
# ═════════════════════════════════════════════════════════════════════════════
@router.api_route("/.well-known/caldav", methods=METHODS, include_in_schema=False)
async def well_known(request: Request):
    return RedirectResponse(url=ROOT, status_code=301)


@router.api_route("/caldav", methods=METHODS, include_in_schema=False)
@router.api_route("/caldav/{path:path}", methods=METHODS, include_in_schema=False)
async def caldav(request: Request, path: str = ""):
    method = request.method.upper()
    if method == "OPTIONS":
        return Response(status_code=200, headers={"DAV": "1, 3, calendar-access", "Allow": ALLOW})
    email = _auth(request)
    if not email:
        return _unauthorized()

    full = "/caldav/" + path.lstrip("/")
    if not full.endswith("/") and not full.lower().endswith(".ics"):
        full += "/"
    body = await request.body()

    if method == "PROPFIND":
        return _propfind(email, full, body, request.headers.get("Depth", "0"))
    if method == "REPORT":
        cal, name = _parse_path(full)
        if not cal or name:
            return Response(status_code=403)
        return _report(email, cal, body)
    if method == "MKCALENDAR":
        return _precondition(NS_D, "forbidden", 403,
                             "les agendas sont créés dans UPlanet (calendars.html) ou par un événement NOSTR")
    if method == "PROPPATCH":
        return _proppatch(full, body)

    cal, name = _parse_path(full)
    if not cal or not name:
        return Response(status_code=405, headers={"Allow": "OPTIONS, PROPFIND, PROPPATCH, REPORT"})
    try:
        calendar_store.check_cal(cal)
        calendar_store.check_name(name)
    except calendar_store.CalendarError:
        return Response(status_code=403)

    if method in ("GET", "HEAD"):
        data = calendar_store.load(email)
        item = data["items"].get(calendar_store.item_key(cal, name))
        if not item:
            return Response(status_code=404)
        text = "" if method == "HEAD" else calendar_store.get_text(email, cal, name)
        return Response(content=text, media_type="text/calendar; charset=utf-8", headers={"ETag": item["etag"]})

    if method == "PUT":
        return _put(email, cal, name, body, request)

    if method == "DELETE":
        try:
            ok = calendar_store.delete(email, cal, name, if_match=request.headers.get("If-Match"))
        except calendar_store.PreconditionFailed:
            return Response(status_code=412)
        if ok:
            _kick_bridge(email, force=True)
        return Response(status_code=204 if ok else 404)

    return Response(status_code=405, headers={"Allow": ALLOW})


def _proppatch(full: str, body: bytes) -> Response:
    """Les propriétés d'agenda (nom, couleur, ordre) sont gérées côté UPlanet : refus propre
    plutôt qu'une erreur HTTP, pour que le client n'affiche pas d'échec de synchro."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return Response(status_code=400)
    ms = ET.Element(q(NS_D, "multistatus"))
    resp = ET.SubElement(ms, q(NS_D, "response"))
    ET.SubElement(resp, q(NS_D, "href")).text = full
    ps = ET.SubElement(resp, q(NS_D, "propstat"))
    prop = ET.SubElement(ps, q(NS_D, "prop"))
    for setter in list(root):
        p = setter.find(q(NS_D, "prop"))
        for child in (p if p is not None else []):
            ET.SubElement(prop, child.tag)
    ET.SubElement(ps, q(NS_D, "status")).text = "HTTP/1.1 403 Forbidden"
    return _xml(ms)


def _propfind(email: str, full: str, body: bytes, depth: str) -> Response:
    wanted, allprop = _requested_props(body)
    ms = ET.Element(q(NS_D, "multistatus"))
    data = calendar_store.load(email)
    if full in (ROOT, PRINCIPAL):
        props = _root_props(email) if full == ROOT else _principal_props(email)
        _response(ms, full, *_select(props, wanted, allprop))
    elif full == HOME:
        _response(ms, HOME, *_select(_root_props(email), wanted, allprop))
        if depth != "0":
            for cal in data["cals"]:
                _response(ms, _cal_href(cal), *_select(_cal_props(email, data, cal), wanted, allprop))
    else:
        cal, name = _parse_path(full)
        if not cal or cal not in data["cals"]:
            return Response(status_code=404)
        if not name:
            _response(ms, _cal_href(cal), *_select(_cal_props(email, data, cal), wanted, allprop))
            if depth != "0":
                _kick_bridge(email)
                want_data = bool(wanted) and q(NS_C, "calendar-data") in wanted
                for item in calendar_store.items_of(data, cal):
                    _response(ms, _item_href(cal, item["name"]),
                              *_select(_item_props(email, item, want_data), wanted, allprop))
        else:
            item = data["items"].get(calendar_store.item_key(cal, name))
            if not item:
                return Response(status_code=404)
            want_data = bool(wanted) and q(NS_C, "calendar-data") in wanted
            _response(ms, _item_href(cal, name), *_select(_item_props(email, item, want_data), wanted, allprop))
    return _xml(ms)


def _report(email: str, cal: str, body: bytes) -> Response:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return Response(status_code=400)
    data = calendar_store.load(email)
    if cal not in data["cals"]:
        return Response(status_code=404)
    prop_el = root.find(q(NS_D, "prop"))
    wanted = [c.tag for c in prop_el] if prop_el is not None else [q(NS_D, "getetag"), q(NS_C, "calendar-data")]
    want_data = q(NS_C, "calendar-data") in wanted
    ms = ET.Element(q(NS_D, "multistatus"))
    _kick_bridge(email)

    if root.tag == q(NS_C, "calendar-multiget"):
        for href_el in root.findall(q(NS_D, "href")):
            c2, name = _parse_path(href_el.text or "")
            item = data["items"].get(calendar_store.item_key(c2, name)) if c2 == cal and name else None
            if not item:
                resp = ET.SubElement(ms, q(NS_D, "response"))
                ET.SubElement(resp, q(NS_D, "href")).text = href_el.text
                ET.SubElement(resp, q(NS_D, "status")).text = "HTTP/1.1 404 Not Found"
                continue
            _response(ms, _item_href(cal, name), *_select(_item_props(email, item, want_data), wanted, False))
        return _xml(ms)

    if root.tag == q(NS_C, "calendar-query"):
        for item in calendar_store.items_of(data, cal):
            if _matches(item, root):
                _response(ms, _item_href(cal, item["name"]), *_select(_item_props(email, item, want_data), wanted, False))
        return _xml(ms)

    if root.tag == q(NS_D, "sync-collection"):
        token_el = root.find(q(NS_D, "sync-token"))
        token_text = (token_el.text or "").strip() if token_el is not None else ""
        token: Optional[int] = None
        if token_text:
            m = re.search(r"(\d+)$", token_text)
            if not m or not token_text.startswith(SYNC_PREFIX):
                return _sync_invalid()
            token = int(m.group(1))
        result = calendar_store.changes_since(data, cal, token)
        if result is None:
            return _sync_invalid()
        changed, deleted = result
        for name in changed:
            item = data["items"][calendar_store.item_key(cal, name)]
            _response(ms, _item_href(cal, name), *_select(_item_props(email, item, want_data), wanted, False))
        for name in deleted:
            resp = ET.SubElement(ms, q(NS_D, "response"))
            ET.SubElement(resp, q(NS_D, "href")).text = _item_href(cal, name)
            ET.SubElement(resp, q(NS_D, "status")).text = "HTTP/1.1 404 Not Found"
        ET.SubElement(ms, q(NS_D, "sync-token")).text = SYNC_PREFIX + str(data["sync"])
        return _xml(ms)

    return Response(status_code=403)


def _sync_invalid() -> Response:
    err = ET.Element(q(NS_D, "error"))
    ET.SubElement(err, q(NS_D, "valid-sync-token"))
    return _xml(err, status=403)


def _put(email: str, cal: str, name: str, body: bytes, request: Request) -> Response:
    try:
        text = body.decode("utf-8")
        info = calendar_store.summarize(text)
    except (UnicodeDecodeError, calendar_store.CalendarError) as exc:
        return _precondition(NS_C, "valid-calendar-data", 400, str(exc))
    if len(body) > calendar_store.MAX_ICS_BYTES:
        return _precondition(NS_C, "max-resource-size", 403, "ressource trop volumineuse")

    data = calendar_store.load(email)
    if cal not in data["cals"]:
        return Response(status_code=404)
    if info["uid"]:
        other = calendar_store.find_by_uid(data, cal, info["uid"])
        if other and other != name:
            err = ET.Element(q(NS_D, "error"))
            nuc = ET.SubElement(err, q(NS_C, "no-uid-conflict"))
            ET.SubElement(nuc, q(NS_D, "href")).text = _item_href(cal, other)
            return _xml(err, status=409)

    existed = calendar_store.item_key(cal, name) in data["items"]
    try:
        item = calendar_store.put(email, cal, name, text,
                                  if_match=request.headers.get("If-Match"),
                                  if_none_match=request.headers.get("If-None-Match", "").strip() == "*")
    except calendar_store.PreconditionFailed:
        return Response(status_code=412)
    except calendar_store.CalendarError as exc:
        return _precondition(NS_C, "valid-calendar-data", 400, str(exc))
    _kick_bridge(email, force=True)
    return Response(status_code=204 if existed else 201, headers={"ETag": item["etag"]})


def _precondition(ns: str, tag: str, status: int, message: str) -> Response:
    err = ET.Element(q(NS_D, "error"))
    ET.SubElement(err, q(ns, tag))
    ET.SubElement(err, q(NS_D, "responsedescription")).text = message
    return _xml(err, status=status)
