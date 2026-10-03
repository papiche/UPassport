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

import xml.etree.ElementTree as ET
from typing import Any, Dict, List
from urllib.parse import unquote, urljoin

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
    base = _root_base(root_url)
    rel = rel_path.strip("/")
    target_url = urljoin(base, rel + "/") if rel else base

    try:
        async with httpx.AsyncClient(timeout=timeout, auth=(username, password)) as client:
            resp = await client.request(
                "PROPFIND", target_url,
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


async def walk_files(root_url: str, username: str, password: str,
                      rel_path: str = "", max_files: int = 2000) -> List[Dict[str, Any]]:
    """Tous les FICHIERS sous `rel_path`, parcouru récursivement niveau par
    niveau (pas de Depth: infinity, cf. `list_folder`). Plafonné à
    `max_files` par prudence (un dossier mal choisi — toute la photothèque —
    ne doit pas bloquer indéfiniment l'import quotidien). Un sous-dossier qui
    échoue (404 sur une entrée fantôme du serveur distant, permission refusée
    sur UN dossier précis, etc.) est ignoré plutôt que de faire échouer tout
    le reste de l'arborescence."""
    out: List[Dict[str, Any]] = []
    entries = await list_folder(root_url, username, password, rel_path)
    for entry in entries:
        if len(out) >= max_files:
            break
        if entry["is_dir"]:
            try:
                out.extend(await walk_files(root_url, username, password, entry["path"],
                                             max_files=max_files - len(out)))
            except WebdavError:
                continue
        else:
            out.append(entry)
    return out[:max_files]


async def download_file(root_url: str, username: str, password: str, rel_path: str,
                         timeout: float = 120.0) -> bytes:
    """Contenu brut d'UN fichier distant (`rel_path`, relatif à `root_url`)."""
    base = _root_base(root_url)
    target_url = urljoin(base, rel_path.strip("/"))
    try:
        async with httpx.AsyncClient(timeout=timeout, auth=(username, password)) as client:
            resp = await client.get(target_url)
    except httpx.HTTPError as exc:
        raise WebdavError(f"téléchargement impossible : {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise WebdavError(f"téléchargement refusé (HTTP {resp.status_code})")
    return resp.content


async def check_connection(root_url: str, username: str, password: str) -> None:
    """Lève WebdavError si (url, identifiants) ne permettent pas un PROPFIND
    minimal sur la racine — utilisé à l'ajout d'une source pour un retour
    d'erreur immédiat plutôt qu'un échec silencieux le lendemain en cron."""
    await list_folder(root_url, username, password, "")
