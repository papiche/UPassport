"""
services/webdav_client.py — Client WebDAV SORTANT minimal (PROPFIND + GET).

Pour importer des photos depuis un serveur WebDAV TIERS (un autre NextCloud,
ownCloud, ou une autre station Astroport) vers le cloud chiffré `.ucloud`
d'un MULTIPASS — cf. `webdav_import.py` (import quotidien) et
`routers/cloud.py` (parcours de dossiers pour le sélecteur de la page
ucloud.html). Aucune bibliothèque WebDAV dédiée dans ce repo : `httpx`
suffit, PROPFIND n'est qu'une méthode HTTP personnalisée (même approche que
`Astroport.ONE/IA/bro/nextcloud_bro_sync.sh::_sync_webdav()`, en Python et
avec un vrai parseur XML plutôt qu'une regex sur les `<D:href>`).

Toutes les fonctions sont asynchrones (réutilisées telles quelles par les
routes FastAPI et par `webdav_import.py` via `asyncio.run()`).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:resourcetype/>
    <D:getcontentlength/>
    <D:getlastmodified/>
    <D:getetag/>
  </D:prop>
</D:propfind>"""


def _localname(tag: str) -> str:
    """'{DAV:}propname' → 'propname' — insensible au préfixe de namespace
    (NextCloud/ownCloud/wsgidav n'utilisent pas tous le même préfixe)."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


class WebdavError(RuntimeError):
    """Erreur de connexion/auth/protocole — message déjà prêt pour l'utilisateur."""


def _root_base(root_url: str) -> str:
    return root_url if root_url.endswith("/") else root_url + "/"


def _child_url(root_url: str, rel_path: str, folder: bool = False) -> str:
    """URL de `rel_path` sous `root_url`, SEGMENTS pourcent-encodés.

    `urljoin(base, "Photo: x.jpg")` prenait « Photo: » pour un schéma et rendait
    `Photo: x.jpg` ; un `#` ou un `?` dans un nom tronquait l'URL — ces fichiers
    échouaient chaque jour pour toujours. On concatène donc nous-mêmes."""
    base = _root_base(root_url)
    rel = rel_path.strip("/")
    if not rel:
        return base
    url = base + quote(rel, safe="/")
    return url + "/" if folder else url


# Le client WebDAV sortant ne doit pas servir de proxy vers l'intérieur de la
# station (IPFS 5001, API station 12345, métadonnées cloud 169.254.169.254…).
# Les adresses PRIVÉES (LAN) restent permises : une autre station Astroport
# chez soi est un usage légitime. Bloqués : boucle locale, link-local, adresses
# non routables, multicast. (Reste ouvert : le DNS rebinding, qui demanderait de
# figer l'IP résolue dans le transport.)
def _forbidden_ip(ip: "ipaddress._BaseAddress") -> bool:
    return (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
            or ip.is_reserved)


_HOST_OK: Dict[str, float] = {}


async def validate_remote_url(url: str) -> None:
    """Lève WebdavError si `url` n'est pas un http(s) vers une adresse permise."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebdavError("adresse invalide (http:// ou https:// attendu)")
    host = parts.hostname
    import time as _t
    if _t.time() - _HOST_OK.get(host, 0.0) < 300:
        return                      # déjà validé : un parcours fait des milliers d'appels
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or 443,
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise WebdavError("nom de serveur introuvable")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if _forbidden_ip(ip):
            raise WebdavError("adresse interdite (boucle locale / réseau réservé)")
    _HOST_OK[host] = _t.time()


_MAX_REDIRECTS = 3


async def _request(client: "httpx.AsyncClient", method: str, url: str, **kw) -> "httpx.Response":
    """Requête avec redirections suivies À LA MAIN (≤ 3) : http→https ou ajout du
    `/` final d'un Nextcloud étaient rejetés (« HTTP 301 »), et une redirection
    automatique aurait pu mener vers une adresse interne non revalidée."""
    for _ in range(_MAX_REDIRECTS + 1):
        await validate_remote_url(url)
        resp = await client.request(method, url, **kw)
        if resp.status_code in (301, 302, 307, 308) and resp.headers.get("location"):
            nxt = httpx.URL(url).join(resp.headers["location"])
            if nxt.host != httpx.URL(url).host:
                raise WebdavError("redirection vers un autre serveur refusée")
            url = str(nxt)
            continue
        return resp
    raise WebdavError("trop de redirections")


async def list_folder(root_url: str, username: str, password: str,
                       rel_path: str = "", timeout: float = 20.0) -> List[Dict[str, Any]]:
    """Enfants DIRECTS (Depth: 1) de `rel_path` (relatif à `root_url`, la
    racine WebDAV choisie par l'utilisateur — ex. son dossier personnel sur
    un NextCloud). Retourne [{name, path, is_dir, size, mtime}], `path` déjà
    relatif à `root_url` (à repasser tel quel pour descendre d'un niveau).

    Depth: 1 plutôt que Depth: infinity (parcours récursif manuel, cf.
    `walk_files`) : de nombreux serveurs (NextCloud par défaut) refusent ou
    limitent Depth: infinity pour raisons de charge — Depth: 1 est le seul
    appel universellement supporté."""
    rel = rel_path.strip("/")
    target_url = _child_url(root_url, rel, folder=True)

    try:
        async with httpx.AsyncClient(timeout=timeout, auth=(username, password)) as client:
            resp = await _request(
                client, "PROPFIND", target_url,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=_PROPFIND_BODY,
            )
    except httpx.HTTPError as exc:
        raise WebdavError(f"connexion impossible : {type(exc).__name__}") from exc

    if resp.status_code == 401:
        raise WebdavError("identifiants refusés (401)")
    if resp.status_code not in (207, 200):
        raise WebdavError(f"réponse inattendue du serveur WebDAV (HTTP {resp.status_code})")

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise WebdavError(f"réponse PROPFIND illisible : {exc}") from exc

    # Le préfixe à retrancher de chaque href est celui du DOSSIER INTERROGÉ
    # (`target_url`), PAS celui de la racine `root_url` — sans quoi un enfant
    # d'un sous-dossier (ex. Photos/WdTest/top.jpg quand on interroge
    # Photos/WdTest/) contient encore un '/' après retranchement du préfixe
    # racine et se fait rejeter par le garde-fou Depth:1 ci-dessous (bug
    # corrigé le 2026-10-03 — toute PROPFIND sur un sous-dossier renvoyait
    # silencieusement une liste vide).
    target_path = httpx.URL(target_url).path
    items: List[Dict[str, Any]] = []
    for response_el in root:
        if _localname(response_el.tag) != "response":
            continue
        href = None
        is_dir = False
        size = 0
        mtime = None
        for child in response_el:
            ctag = _localname(child.tag)
            if ctag == "href":
                href = unquote(child.text or "")
            elif ctag == "propstat":
                for prop in child.iter():
                    ptag = _localname(prop.tag)
                    if ptag == "resourcetype":
                        is_dir = any(_localname(c.tag) == "collection" for c in prop)
                    elif ptag == "getcontentlength" and prop.text:
                        try:
                            size = int(prop.text)
                        except ValueError:
                            pass
                    elif ptag == "getlastmodified" and prop.text:
                        mtime = prop.text
        if not href or not href.startswith(target_path):
            continue   # hors du dossier interrogé — ignoré par prudence
        child_rel = href[len(target_path):].strip("/")
        if not child_rel or "/" in child_rel:
            continue   # le dossier courant lui-même, ou un petit-enfant (garde-fou Depth:1)
        items.append({
            "name": child_rel,
            "path": f"{rel}/{child_rel}".strip("/") if rel else child_rel,
            "is_dir": is_dir,
            "size": size,
            "mtime": mtime,
        })
    items.sort(key=lambda it: (not it["is_dir"], it["name"].lower()))
    return items


# Plafond du parcours récursif. Il compte TOUS les fichiers vus, déjà
# importés compris : à 2000 (valeur d'origine), une archive plus grosse
# restait figée pour toujours une fois ses 2000 premiers fichiers importés
# (constaté le 2026-10-12). Le parcours ne coûte qu'un PROPFIND par dossier ;
# les fichiers déjà vus sont écartés par le cache de webdav_import.py sans
# être téléchargés.
WALK_MAX_FILES = int(os.environ.get("WEBDAV_WALK_MAX", "50000"))


WALK_CONCURRENCY = int(os.environ.get("WEBDAV_WALK_CONCURRENCY", "8"))
# Garde-fous d'un arbre pathologique (boucle de liens, dossiers fantômes) : sans
# ces bornes, un arbre de dossiers SANS fichier bouclait indéfiniment.
WALK_MAX_DIRS = int(os.environ.get("WEBDAV_WALK_MAX_DIRS", "40000"))
WALK_MAX_DEPTH = int(os.environ.get("WEBDAV_WALK_MAX_DEPTH", "40"))


async def walk_files(root_url: str, username: str, password: str,
                      rel_path: str = "", max_files: int = WALK_MAX_FILES,
                      progress: Optional[Callable[[int, int], None]] = None) -> List[Dict[str, Any]]:
    """Tous les FICHIERS sous `rel_path`, parcouru niveau par niveau (pas de
    Depth: infinity, cf. `list_folder`), jusqu'à WALK_CONCURRENCY dossiers
    interrogés EN PARALLÈLE. Une photothèque rangée par jour compte des
    milliers de dossiers à ~0,7 s le PROPFIND : en série, le seul parcours
    dépassait 30 min (constaté le 2026-10-12, archive 2007→2026).

    Plafonné à `max_files` par prudence. Quand le plafond coupe, on garde les
    fichiers dont le CHEMIN est le plus GRAND : dans une archive rangée par
    année/mois/jour ce sont les plus RÉCENTS — l'ancien tri croissant écartait
    précisément les nouvelles photos, qui n'étaient alors jamais importées.
    Ordre de sortie : plus récent d'abord (stable d'un passage à l'autre).
    Un sous-dossier qui échoue est ignoré ; seul l'échec de la RACINE est remonté.

    `progress(dossiers_lus, fichiers_vus)` est appelé au plus toutes les 10 s : sans lui, un parcours de
    plusieurs milliers de dossiers ressemble à un programme figé."""
    sem = asyncio.Semaphore(WALK_CONCURRENCY)
    out: List[Dict[str, Any]] = []
    stats = {"dirs": 0, "files": 0, "t": time.monotonic()}

    def tick(force: bool = False) -> None:
        if progress and (force or time.monotonic() - stats["t"] >= 10):
            stats["t"] = time.monotonic()
            progress(stats["dirs"], stats["files"])

    async def listing(path: str, root: bool) -> List[Dict[str, Any]]:
        async with sem:
            try:
                entries = await list_folder(root_url, username, password, path)
            except WebdavError:
                if root:
                    raise
                return []
        stats["dirs"] += 1
        stats["files"] += sum(1 for e in entries if not e["is_dir"])
        tick()
        return entries

    level = [rel_path]
    seen = {rel_path.strip("/")}
    first = True
    depth = 0
    while level and len(out) < max_files and depth <= WALK_MAX_DEPTH:
        results = await asyncio.gather(*(listing(p, first) for p in level))
        first = False
        depth += 1
        level = []
        for entries in results:
            for entry in entries:
                if entry["is_dir"]:
                    key = entry["path"].strip("/")
                    if key in seen or len(seen) >= WALK_MAX_DIRS:
                        continue          # cycle ou arbre démesuré
                    seen.add(key)
                    level.append(entry["path"])
                else:
                    out.append(entry)
    tick(force=True)
    out.sort(key=lambda e: e["path"], reverse=True)
    return out[:max_files]


MAX_DOWNLOAD_BYTES = int(os.environ.get("WEBDAV_MAX_DOWNLOAD", str(200 * 1024 * 1024)))


async def download_file(root_url: str, username: str, password: str, rel_path: str,
                         timeout: float = 120.0, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """Contenu brut d'UN fichier distant (`rel_path`, relatif à `root_url`),
    lu EN FLUX et refusé au-delà de `max_bytes` (jamais un fichier de plusieurs
    Go entier en RAM, sur un RPi, avant même d'être chiffré)."""
    target_url = _child_url(root_url, rel_path)
    try:
        async with httpx.AsyncClient(timeout=timeout, auth=(username, password)) as client:
            await validate_remote_url(target_url)
            async with client.stream("GET", target_url) as resp:
                if resp.status_code != 200:
                    raise WebdavError(f"téléchargement refusé (HTTP {resp.status_code})")
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise WebdavError(f"fichier trop volumineux ({declared} octets)")
                chunks: List[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise WebdavError(f"fichier trop volumineux (> {max_bytes} octets)")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise WebdavError(f"téléchargement impossible : {type(exc).__name__}") from exc
    return b"".join(chunks)


async def check_connection(root_url: str, username: str, password: str) -> None:
    """Lève WebdavError si (url, identifiants) ne permettent pas un PROPFIND
    minimal sur la racine — utilisé à l'ajout d'une source pour un retour
    d'erreur immédiat plutôt qu'un échec silencieux le lendemain en cron."""
    await list_folder(root_url, username, password, "")
