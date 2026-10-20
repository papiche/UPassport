"""
services/gateway_fetch.py — Rapatrier des données IPFS par la PASSERELLE HTTP de l'ancienne station.

Quand un MULTIPASS change de **constellation** (autre UPLANETNAME = autre clé de swarm IPFS privé), l'IPFS de la nouvelle
station ne peut PAS joindre les blocs de l'ancienne : ni `ipfs pin add`, ni `ipfs get`, ni `ipfs cat` par l'API locale.
Reste la passerelle HTTP publique de l'ancienne station (« myIPFS », celle des liens des e-mails de désactivation).

On y demande le DAG complet au format **CAR** (`?format=car`, passerelle « de confiance minimale ») puis on l'importe avec
`ipfs dag import --pin-roots` : chaque bloc est vérifié contre son CID par IPFS lui-même (une passerelle menteuse ne peut
rien faire passer) et les CID sont PRÉSERVÉS — l'index ucloud (chemin ↔ CID) et son trousseau de clés restent valables tels
quels. Les petits blobs chiffrés déjà authentifiés par AES-GCM (archive de sauvegarde) se lisent en simple GET.

Garde-fous : passerelle http(s) sans identifiants, hôte public (boucle locale, réseaux privés et adresses réservées refusés
— ces appels peuvent être déclenchés depuis une API ; `GATEWAY_ALLOW_PRIVATE=1` pour une passerelle de LAN, côté opérateur),
aucune redirection suivie, taille bornée par CID, délai borné, flux écrit sur disque (jamais en mémoire).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

CID_RE = re.compile(r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|bafy[a-z2-7]{50,})$")
MAX_DAG_BYTES = 4 * 1024 ** 3       # par CID : un uDRIVE ou un fichier du cloud
MAX_BLOB_BYTES = 500 * 1024 ** 2    # archives chiffrées lues d'un bloc
TIMEOUT = 900.0                     # secondes, par requête
IPFS_ARGS: List[str] = []           # ["--offline"] dans les tests (dépôt isolé, pas de démon)


class GatewayError(Exception):
    """Échec explicite (passerelle refusée, DAG trop gros, bloc altéré…) — jamais un repli silencieux."""


def normalize_gateway(url: str, allow_private: Optional[bool] = None) -> str:
    """« https://ipfs.exemple.org/ipfs/Qm… » ou « https://ipfs.exemple.org/ » → « https://ipfs.exemple.org »."""
    allow_private = bool(os.environ.get("GATEWAY_ALLOW_PRIVATE")) if allow_private is None else allow_private
    u = urlsplit((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise GatewayError("passerelle invalide : une URL http(s) est attendue")
    if u.username or u.password:
        raise GatewayError("passerelle invalide : pas d'identifiants dans l'URL")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise GatewayError(f"passerelle introuvable ({u.hostname}) : {exc}")
    if not allow_private:
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved
                    or ip.is_unspecified):
                raise GatewayError(f"passerelle refusée : {u.hostname} résout vers une adresse non publique ({ip})")
    return f"{u.scheme}://{u.netloc}"


def split_gateway_url(text: str):
    """« https://hote/ipfs/<CID>[/…] » → (« https://hote », « <CID> ») ; un CID seul → (None, CID)."""
    t = (text or "").strip()
    if CID_RE.fullmatch(t):
        return None, t
    u = urlsplit(t)
    m = re.match(r"^/ipfs/([^/]+)", u.path or "")
    if u.scheme in ("http", "https") and u.netloc and m and CID_RE.fullmatch(m.group(1)):
        return f"{u.scheme}://{u.netloc}", m.group(1)
    raise GatewayError("ni un CID ni un lien de passerelle (…/ipfs/<CID>)")


def _stream_to(url: str, dest: Path, max_bytes: int, accept: str, timeout: float) -> int:
    got = 0
    try:
        with httpx.stream("GET", url, headers={"Accept": accept}, timeout=timeout, follow_redirects=False) as r:
            if r.status_code != 200:
                raise GatewayError(f"passerelle : HTTP {r.status_code} pour {url.split('?')[0]}")
            ctype = r.headers.get("content-type", "")
            if "ipld.car" in accept and "ipld.car" not in ctype:
                raise GatewayError(f"passerelle : format CAR non servi (content-type « {ctype[:60]} »)")
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes(65536):
                    got += len(chunk)
                    if got > max_bytes:
                        raise GatewayError(f"passerelle : plus de {max_bytes // (1024 ** 2)} Mo, abandon")
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        raise GatewayError(f"passerelle injoignable : {exc}")
    return got


def fetch_dag(cid: str, gateway: str, max_bytes: int = MAX_DAG_BYTES, timeout: float = TIMEOUT,
              allow_private: Optional[bool] = None) -> int:
    """Rapatrie le DAG de `cid` (CAR → `ipfs dag import --pin-roots`) ; le CID est préservé et épinglé en récursif.
    Retourne le nombre d'octets reçus. Lève GatewayError si quoi que ce soit ne va pas, y compris un bloc altéré."""
    if not CID_RE.fullmatch(cid or ""):
        raise GatewayError(f"CID invalide : {cid!r}")
    gw = normalize_gateway(gateway, allow_private)
    with tempfile.TemporaryDirectory(prefix="gwfetch_") as tmp:
        car = Path(tmp) / "dag.car"
        size = _stream_to(f"{gw}/ipfs/{cid}?format=car", car, max_bytes, "application/vnd.ipld.car", timeout)
        p = subprocess.run(["ipfs", *IPFS_ARGS, "dag", "import", "--pin-roots=true", str(car)],
                           capture_output=True, text=True, timeout=max(60, int(timeout)))
        if p.returncode != 0:
            raise GatewayError(f"import du CAR refusé (bloc altéré ?) : {(p.stderr or p.stdout).strip()[-200:]}")
    ok = subprocess.run(["ipfs", *IPFS_ARGS, "pin", "ls", "--type=recursive", cid], capture_output=True, timeout=60)
    if ok.returncode != 0:
        raise GatewayError(f"{cid[:16]}… importé mais non épinglé : la racine du CAR n'est pas celle demandée")
    return size


def fetch_bytes(cid: str, gateway: str, max_bytes: int = MAX_BLOB_BYTES, timeout: float = TIMEOUT,
                allow_private: Optional[bool] = None) -> bytes:
    """Lit un bloc de données (archive chiffrée AES-GCM : le contenu s'authentifie lui-même au déchiffrement)."""
    if not CID_RE.fullmatch(cid or ""):
        raise GatewayError(f"CID invalide : {cid!r}")
    gw = normalize_gateway(gateway, allow_private)
    with tempfile.TemporaryDirectory(prefix="gwfetch_") as tmp:
        dest = Path(tmp) / "blob"
        _stream_to(f"{gw}/ipfs/{cid}", dest, max_bytes, "*/*", timeout)
        return dest.read_bytes()
