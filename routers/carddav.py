"""
routers/carddav.py — Serveur CardDAV (RFC 6352) du carnet FaceCloud.

Permet à un téléphone ou un logiciel de messagerie de SYNCHRONISER le carnet
chiffré du MULTIPASS (services/contacts_store.py) : DAVx5 (Android), iOS /
macOS (Contacts), Thunderbird. Même identifiants que l'accès disque WebDAV
(Basic `email:dav_token`, délivré par POST /api/cloud/enroll).

Arborescence exposée (indépendante de l'email — l'utilisateur est résolu
depuis l'authentification, jamais depuis l'URL) :

    /.well-known/carddav           → redirection 301 vers /carddav/
    /carddav/                      racine (current-user-principal)
    /carddav/principal/            principal (addressbook-home-set)
    /carddav/books/                home des carnets
    /carddav/books/contacts/       LE carnet (resourcetype addressbook)
    /carddav/books/contacts/<n>.vcf  une fiche (GET / PUT / DELETE)

Rapports pris en charge sur le carnet : addressbook-multiget,
addressbook-query (toutes les fiches — les clients filtrent eux-mêmes),
sync-collection (RFC 6578, synchro incrémentale ; jeton trop ancien → 403
valid-sync-token, le client repart de zéro). Écritures protégées par
If-Match / If-None-Match (412 si la fiche a changé ailleurs) et unicité
de l'UID (409). Exclu du limiteur de débit, comme /dav (core/middleware.py) :
une synchro émet des rafales de requêtes.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from services import cloud_storage, contacts_store

logger = logging.getLogger(__name__)
router = APIRouter()

NS_D = "DAV:"
NS_C = "urn:ietf:params:xml:ns:carddav"
NS_CS = "http://calendarserver.org/ns/"
ET.register_namespace("d", NS_D)
ET.register_namespace("card", NS_C)
ET.register_namespace("cs", NS_CS)

ROOT = "/carddav/"
PRINCIPAL = "/carddav/principal/"
HOME = "/carddav/books/"
BOOK = "/carddav/books/contacts/"
SYNC_PREFIX = "https://facecloud.uplanet/sync/"
ALLOW = "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, REPORT"
METHODS = ["OPTIONS", "GET", "HEAD", "PUT", "DELETE", "PROPFIND", "REPORT"]


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
    h = {"DAV": "1, 3, addressbook"}
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
# Propriétés par type de ressource
# ═════════════════════════════════════════════════════════════════════════════
def _common(email: str) -> Dict[str, object]:
    return {
        q(NS_D, "current-user-principal"): _href(PRINCIPAL),
        q(NS_D, "principal-URL"): _href(PRINCIPAL),
        q(NS_C, "addressbook-home-set"): _href(HOME),
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
    props[q(NS_C, "principal-address")] = None
    return props


def _book_props(email: str, data: dict) -> Dict[str, object]:
    props = _common(email)
    props.update({
        q(NS_D, "resourcetype"): _collection_types(_el(NS_C, "addressbook")),
        q(NS_D, "displayname"): "FaceCloud",
        q(NS_C, "addressbook-description"): "Carnet chiffré du MULTIPASS (FaceCloud)",
        q(NS_CS, "getctag"): contacts_store.ctag(data),
        q(NS_D, "sync-token"): SYNC_PREFIX + str(data["sync"]),
        q(NS_C, "max-resource-size"): str(contacts_store.MAX_VCARD_BYTES),
        q(NS_C, "supported-address-data"): [
            ET.Element(q(NS_C, "address-data-type"), {"content-type": "text/vcard", "version": "3.0"}),
            ET.Element(q(NS_C, "address-data-type"), {"content-type": "text/vcard", "version": "4.0"}),
        ],
        q(NS_D, "supported-report-set"): [
            _el(NS_D, "supported-report", children=[_el(NS_D, "report", children=[_el(ns, r)])])
            for ns, r in ((NS_C, "addressbook-multiget"), (NS_C, "addressbook-query"), (NS_D, "sync-collection"))
        ],
    })
    return props


def _card_props(email: str, card: dict, with_data: bool) -> Dict[str, object]:
    props = {
        q(NS_D, "resourcetype"): None,
        q(NS_D, "getetag"): card["etag"],
        q(NS_D, "getcontenttype"): "text/vcard; charset=utf-8",
        q(NS_D, "displayname"): card.get("fn") or card["name"],
    }
    if with_data:
        try:
            props[q(NS_C, "address-data")] = contacts_store.get_text(email, card["name"])
        except KeyError:
            pass
    return props


def _card_href(name: str) -> str:
    return BOOK + quote(name)


def _name_from(path: str) -> Optional[str]:
    """Nom de fiche depuis un chemin/href ("…/contacts/x.vcf" → "x.vcf")."""
    path = unquote(re.sub(r"^https?://[^/]+", "", path or ""))
    if not path.startswith(BOOK) or path == BOOK:
        return None
    name = path[len(BOOK):].strip("/")
    return name if "/" not in name else None


# ═════════════════════════════════════════════════════════════════════════════
# Routes
# ═════════════════════════════════════════════════════════════════════════════
@router.api_route("/.well-known/carddav", methods=METHODS, include_in_schema=False)
async def well_known(request: Request):
    return RedirectResponse(url=ROOT, status_code=301)


@router.api_route("/carddav", methods=METHODS, include_in_schema=False)
@router.api_route("/carddav/{path:path}", methods=METHODS, include_in_schema=False)
async def carddav(request: Request, path: str = ""):
    method = request.method.upper()
    if method == "OPTIONS":
        return Response(status_code=200, headers={"DAV": "1, 3, addressbook", "Allow": ALLOW})
    email = _auth(request)
    if not email:
        return _unauthorized()

    full = "/carddav/" + path.lstrip("/")
    if not full.endswith("/") and not full.lower().endswith(".vcf"):
        full += "/"
    body = await request.body()

    if method == "PROPFIND":
        return _propfind(email, full, body, request.headers.get("Depth", "0"))
    if method == "REPORT":
        if full != BOOK:
            return Response(status_code=403)
        return _report(email, body)

    name = _name_from(full)
    if not name:
        return Response(status_code=405, headers={"Allow": "OPTIONS, PROPFIND, REPORT"})
    try:
        contacts_store.check_name(name)
    except contacts_store.ContactError:
        return Response(status_code=403)

    if method in ("GET", "HEAD"):
        data = contacts_store.load(email)
        card = data["cards"].get(name)
        if not card:
            return Response(status_code=404)
        text = "" if method == "HEAD" else contacts_store.get_text(email, name)
        return Response(content=text, media_type="text/vcard; charset=utf-8", headers={"ETag": card["etag"]})

    if method == "PUT":
        return _put(email, name, body, request)

    if method == "DELETE":
        try:
            ok = contacts_store.delete(email, name, if_match=request.headers.get("If-Match"))
        except contacts_store.PreconditionFailed:
            return Response(status_code=412)
        return Response(status_code=204 if ok else 404)

    return Response(status_code=405, headers={"Allow": ALLOW})


def _propfind(email: str, full: str, body: bytes, depth: str) -> Response:
    wanted, allprop = _requested_props(body)
    ms = ET.Element(q(NS_D, "multistatus"))
    data = contacts_store.load(email)
    if full in (ROOT, PRINCIPAL):
        props = _root_props(email) if full == ROOT else _principal_props(email)
        _response(ms, full, *_select(props, wanted, allprop))
    elif full == HOME:
        _response(ms, HOME, *_select(_root_props(email), wanted, allprop))
        if depth != "0":
            _response(ms, BOOK, *_select(_book_props(email, data), wanted, allprop))
    elif full == BOOK:
        _response(ms, BOOK, *_select(_book_props(email, data), wanted, allprop))
        if depth != "0":
            want_data = bool(wanted) and q(NS_C, "address-data") in wanted
            for card in data["cards"].values():
                _response(ms, _card_href(card["name"]),
                          *_select(_card_props(email, card, want_data), wanted, allprop))
    else:
        name = _name_from(full)
        card = data["cards"].get(name) if name else None
        if not card:
            return Response(status_code=404)
        want_data = bool(wanted) and q(NS_C, "address-data") in wanted
        _response(ms, _card_href(name), *_select(_card_props(email, card, want_data), wanted, allprop))
    return _xml(ms)


def _report(email: str, body: bytes) -> Response:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return Response(status_code=400)
    prop_el = root.find(q(NS_D, "prop"))
    wanted = [c.tag for c in prop_el] if prop_el is not None else [q(NS_D, "getetag"), q(NS_C, "address-data")]
    want_data = q(NS_C, "address-data") in wanted
    data = contacts_store.load(email)
    ms = ET.Element(q(NS_D, "multistatus"))

    if root.tag == q(NS_C, "addressbook-multiget"):
        for href_el in root.findall(q(NS_D, "href")):
            name = _name_from(href_el.text or "")
            card = data["cards"].get(name) if name else None
            if not card:
                resp = ET.SubElement(ms, q(NS_D, "response"))
                ET.SubElement(resp, q(NS_D, "href")).text = href_el.text
                ET.SubElement(resp, q(NS_D, "status")).text = "HTTP/1.1 404 Not Found"
                continue
            _response(ms, _card_href(name), *_select(_card_props(email, card, want_data), wanted, False))
        return _xml(ms)

    if root.tag == q(NS_C, "addressbook-query"):
        for card in data["cards"].values():
            _response(ms, _card_href(card["name"]), *_select(_card_props(email, card, want_data), wanted, False))
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
        result = contacts_store.changes_since(data, token)
        if result is None:
            return _sync_invalid()
        changed, deleted = result
        for name in changed:
            _response(ms, _card_href(name), *_select(_card_props(email, data["cards"][name], want_data), wanted, False))
        for name in deleted:
            resp = ET.SubElement(ms, q(NS_D, "response"))
            ET.SubElement(resp, q(NS_D, "href")).text = _card_href(name)
            ET.SubElement(resp, q(NS_D, "status")).text = "HTTP/1.1 404 Not Found"
        ET.SubElement(ms, q(NS_D, "sync-token")).text = SYNC_PREFIX + str(data["sync"])
        return _xml(ms)

    return Response(status_code=403)


def _sync_invalid() -> Response:
    err = ET.Element(q(NS_D, "error"))
    ET.SubElement(err, q(NS_D, "valid-sync-token"))
    return _xml(err, status=403)


def _put(email: str, name: str, body: bytes, request: Request) -> Response:
    try:
        text = body.decode("utf-8")
        cards = contacts_store.split_vcf(text)
        if len(cards) != 1:
            raise contacts_store.ContactError("une ressource = une seule vCard")
        info = contacts_store.summarize(cards[0])
    except (UnicodeDecodeError, contacts_store.ContactError) as exc:
        return _precondition(NS_C, "valid-address-data", 400, str(exc))
    if len(body) > contacts_store.MAX_VCARD_BYTES:
        return _precondition(NS_C, "max-resource-size", 403, "fiche trop volumineuse")

    data = contacts_store.load(email)
    if info["uid"]:
        other = contacts_store.find_by_uid(data, info["uid"])
        if other and other != name:
            err = ET.Element(q(NS_D, "error"))
            nuc = ET.SubElement(err, q(NS_C, "no-uid-conflict"))
            ET.SubElement(nuc, q(NS_D, "href")).text = _card_href(other)
            return _xml(err, status=409)

    existed = name in data["cards"]
    try:
        card = contacts_store.put(email, name, cards[0],
                                  if_match=request.headers.get("If-Match"),
                                  if_none_match=request.headers.get("If-None-Match", "").strip() == "*")
    except contacts_store.PreconditionFailed:
        return Response(status_code=412)
    except contacts_store.ContactError as exc:
        return _precondition(NS_C, "valid-address-data", 400, str(exc))
    from routers import contacts as contacts_router      # référence FaceID de la photo
    contacts_router.schedule_face_refs(email)
    return Response(status_code=204 if existed else 201, headers={"ETag": card["etag"]})


def _precondition(ns: str, tag: str, status: int, message: str) -> Response:
    err = ET.Element(q(NS_D, "error"))
    ET.SubElement(err, q(ns, tag))
    ET.SubElement(err, q(NS_D, "responsedescription")).text = message
    return _xml(err, status=status)
