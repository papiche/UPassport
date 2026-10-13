"""
services/carddav_client.py — Client CardDAV minimal pour IMPORTER un carnet
externe (Nextcloud, ownCloud, iCloud, Radicale, Baïkal…) dans le carnet
chiffré FaceCloud (services/contacts_store.py, routers/contacts.py).

Découverte (RFC 6764 / 6352) à partir de ce que l'utilisateur connaît : une
simple adresse de serveur suffit — /.well-known/carddav, puis
current-user-principal, puis addressbook-home-set, puis les carnets
(resourcetype addressbook). Une URL qui désigne déjà un carnet est acceptée
telle quelle. Les fiches sont lues par PROPFIND Depth:1 (href + ETag) puis
REPORT addressbook-multiget par lots — seul mode accepté par tous (iCloud
refuse les GET fiche par fiche en masse). Google n'accepte que OAuth : non
pris en charge ici (exporter un .vcf depuis Google Contacts à la place).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx

NS_D = "DAV:"
NS_C = "urn:ietf:params:xml:ns:carddav"
TIMEOUT = 30.0
MULTIGET_BATCH = 50


class CardDAVError(Exception):
    pass


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _normalize(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise CardDAVError("adresse vide")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


async def _dav(client: httpx.AsyncClient, method: str, url: str, body: str, depth: str = "0") -> ET.Element:
    try:
        resp = await client.request(method, url, content=body.encode("utf-8"),
                                    headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
    except httpx.HTTPError as exc:
        raise CardDAVError(f"serveur injoignable ({type(exc).__name__})") from exc
    if resp.status_code == 401:
        raise CardDAVError("identifiant ou mot de passe refusé")
    if resp.status_code not in (200, 207):
        raise CardDAVError(f"{method} refusé (HTTP {resp.status_code})")
    try:
        return ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise CardDAVError("réponse illisible (ce n'est pas un serveur CardDAV ?)") from exc


def _responses(root: ET.Element):
    for resp in root.findall(_q(NS_D, "response")):
        href = (resp.findtext(_q(NS_D, "href")) or "").strip()
        props = {}
        for ps in resp.findall(_q(NS_D, "propstat")):
            if "200" not in (ps.findtext(_q(NS_D, "status")) or ""):
                continue
            prop = ps.find(_q(NS_D, "prop"))
            if prop is not None:
                for child in prop:
                    props[child.tag] = child
        yield href, props


def _href_in(el: Optional[ET.Element]) -> Optional[str]:
    if el is None:
        return None
    href = el.find(_q(NS_D, "href"))
    return (href.text or "").strip() if href is not None else None


PROPFIND_DISCOVERY = (
    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><d:current-user-principal/><d:resourcetype/><d:displayname/>"
    "<card:addressbook-home-set/></d:prop></d:propfind>"
)


async def discover(url: str, username: str, password: str) -> List[Dict[str, str]]:
    """Carnets accessibles : [{url, name}]. Lève CardDAVError avec un message
    lisible (identifiants refusés, aucun carnet trouvé…)."""
    base = _normalize(url)
    origin = "{0.scheme}://{0.netloc}".format(urlparse(base))
    async with httpx.AsyncClient(timeout=TIMEOUT, auth=(username, password), follow_redirects=True) as client:
        candidates = [base]
        if urlparse(base).path in ("", "/"):
            candidates = [origin + "/.well-known/carddav", base]
        root = None
        start = None
        last_error: Optional[Exception] = None
        for cand in candidates:
            try:
                root = await _dav(client, "PROPFIND", cand, PROPFIND_DISCOVERY)
                start = cand
                break
            except CardDAVError as exc:
                last_error = exc
                if "refusé (HTTP 401" in str(exc) or "identifiant" in str(exc):
                    raise
        if root is None:
            raise last_error or CardDAVError("aucun serveur CardDAV à cette adresse")

        # L'URL donnée est-elle déjà un carnet ?
        for href, props in _responses(root):
            rt = props.get(_q(NS_D, "resourcetype"))
            if rt is not None and rt.find(_q(NS_C, "addressbook")) is not None:
                name = (props.get(_q(NS_D, "displayname")).text if props.get(_q(NS_D, "displayname")) is not None else "") or "Contacts"
                return [{"url": urljoin(start, href or start), "name": name}]

        principal = None
        home = None
        for _href, props in _responses(root):
            home = home or _href_in(props.get(_q(NS_C, "addressbook-home-set")))
            principal = principal or _href_in(props.get(_q(NS_D, "current-user-principal")))
        if not home and principal:
            root = await _dav(client, "PROPFIND", urljoin(start, principal), PROPFIND_DISCOVERY)
            for _href, props in _responses(root):
                home = home or _href_in(props.get(_q(NS_C, "addressbook-home-set")))
        if not home:
            raise CardDAVError("connexion réussie, mais aucun carnet d'adresses trouvé")

        home_url = urljoin(start, home)
        root = await _dav(client, "PROPFIND", home_url, PROPFIND_DISCOVERY, depth="1")
        books = []
        for href, props in _responses(root):
            rt = props.get(_q(NS_D, "resourcetype"))
            if rt is None or rt.find(_q(NS_C, "addressbook")) is None:
                continue
            dn = props.get(_q(NS_D, "displayname"))
            books.append({"url": urljoin(home_url, href),
                          "name": ((dn.text if dn is not None else "") or href.rstrip("/").rsplit("/", 1)[-1]).strip()})
        if not books:
            raise CardDAVError("connexion réussie, mais aucun carnet d'adresses trouvé")
        return books


async def fetch_all(book_url: str, username: str, password: str) -> List[Tuple[str, str, str]]:
    """Toutes les fiches d'un carnet : [(href, etag, vcard)]."""
    async with httpx.AsyncClient(timeout=TIMEOUT, auth=(username, password), follow_redirects=True) as client:
        root = await _dav(client, "PROPFIND", book_url,
                          '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getetag/>'
                          '<d:resourcetype/></d:prop></d:propfind>', depth="1")
        hrefs = []
        for href, props in _responses(root):
            rt = props.get(_q(NS_D, "resourcetype"))
            if rt is not None and rt.find(_q(NS_D, "collection")) is not None:
                continue
            if href:
                hrefs.append(href)
        out: List[Tuple[str, str, str]] = []
        for i in range(0, len(hrefs), MULTIGET_BATCH):
            chunk = hrefs[i:i + MULTIGET_BATCH]
            body = ('<?xml version="1.0"?><card:addressbook-multiget xmlns:d="DAV:" '
                    'xmlns:card="urn:ietf:params:xml:ns:carddav"><d:prop><d:getetag/><card:address-data/>'
                    '</d:prop>' + "".join(f"<d:href>{_esc(h)}</d:href>" for h in chunk) +
                    "</card:addressbook-multiget>")
            root = await _dav(client, "REPORT", book_url, body, depth="1")
            for href, props in _responses(root):
                data = props.get(_q(NS_C, "address-data"))
                etag = props.get(_q(NS_D, "getetag"))
                if data is not None and (data.text or "").strip():
                    out.append((href, (etag.text or "").strip() if etag is not None else "", data.text))
        return out


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
