"""
services/contacts_store.py — Carnet d'adresses chiffré du MULTIPASS (vCard).

Une seule source de vérité pour TROIS usages :
  * l'onglet Contacts de FaceCloud (routers/contacts.py, JSON + NIP-98) ;
  * le serveur CardDAV (routers/carddav.py) synchronisé par téléphone et
    Thunderbird (DAVx5, iOS, Thunderbird) ;
  * l'import depuis un CardDAV externe ou un fichier .vcf.

Stockage — exactement le schéma prévu par l'esquisse de cloud_storage.py :
chaque fiche est une vCard rangée dans le cloud chiffré sous
`/Contacts/<nom>.vcf` (AES-256-GCM, une clé par fiche, IPFS ne voit jamais le
clair ; visible aussi sur le disque monté). `<nom>` est le nom de ressource
CardDAV : choisi par le client qui crée la fiche (DAVx5 met souvent un UUID),
distinct de la propriété UID de la vCard.

Résumé chiffré — `.ucloud/contacts.enc` (clé `.ucloud/contacts.key`, 0600) :
nom, emails, téléphones, petite photo, ETag, jeton de synchro de chaque
fiche, pour afficher la liste sans redéchiffrer N vCards depuis IPFS. Il
porte aussi le journal de synchro CardDAV (RFC 6578) : un compteur `sync`
incrémenté à chaque changement, le jeton de dernière modification de chaque
fiche, et les suppressions récentes (`deleted`). Une fiche modifiée par le
disque monté (hors de ce module) est détectée par son sha256 dans l'index et
réintégrée au prochain chargement.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
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

CONTACTS_DIR = "/Contacts"
MAX_VCARD_BYTES = 2 * 1024 * 1024      # photo comprise
AVATAR_PX = 96
DELETED_KEEP = 2000                     # suppressions mémorisées pour la synchro incrémentale
_NAME_RE = re.compile(r"^[A-Za-z0-9._@+=-]{1,120}\.vcf$")


class ContactError(ValueError):
    """Requête invalide (vCard illisible, nom de ressource refusé…)."""


# ═════════════════════════════════════════════════════════════════════════════
# Verrou + résumé chiffré
# ═════════════════════════════════════════════════════════════════════════════
def _udir(email: str):
    return cloud_storage.ensure_ucloud_dir(email)


@contextmanager
def _lock(email: str) -> Iterator[None]:
    """Verrou DÉDIÉ au carnet (pas index_lock : ingest_plaintext prend
    lui-même index_lock, un flock imbriqué sur le même fichier se bloquerait)."""
    fd = os.open(str(_udir(email) / ".contacts.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _key(email: str) -> str:
    path = _udir(email) / "contacts.key"
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
    return {"version": 1, "sync": 1, "cards": {}, "deleted": []}


def _read_summary(email: str) -> Dict[str, Any]:
    path = _udir(email) / "contacts.enc"
    if not path.exists():
        return _empty()
    try:
        clear = cloud_storage.uenc_codec.decrypt_aes256gcm(path.read_bytes(), _key(email))
        data = json.loads(clear.decode("utf-8"))
    except Exception as exc:
        logger.error("contacts: résumé illisible pour %s (%s) — reconstruit", email, exc)
        return _empty()
    data.setdefault("cards", {})
    data.setdefault("deleted", [])
    data.setdefault("sync", 1)
    return data


def _write_summary(email: str, data: Dict[str, Any]) -> None:
    payload, _iv = cloud_storage.uenc_codec.encrypt_aes256gcm(
        json.dumps(data, ensure_ascii=False).encode("utf-8"), _key(email))
    path = _udir(email) / "contacts.enc"
    tmp = path.with_name(".contacts.enc.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _path(name: str) -> str:
    return f"{CONTACTS_DIR}/{name}"


def check_name(name: str) -> str:
    """Nom de ressource accepté (pas de chemin, extension .vcf)."""
    if not _NAME_RE.match(name or ""):
        raise ContactError(f"nom de ressource refusé : {name!r}")
    return name


# ═════════════════════════════════════════════════════════════════════════════
# vCard ↔ champs
# ═════════════════════════════════════════════════════════════════════════════
def _vobject():
    import vobject  # noqa: WPS433 — import paresseux (UPassport démarre sans)
    return vobject


def parse(text: str):
    """vCard texte → objet vobject (lève ContactError)."""
    try:
        card = _vobject().readOne(text)
    except Exception as exc:
        raise ContactError(f"vCard illisible : {exc}") from exc
    if getattr(card, "name", "").upper() != "VCARD":
        raise ContactError("ce n'est pas une vCard")
    return card


def split_vcf(text: str) -> List[str]:
    """Un fichier .vcf peut contenir plusieurs fiches."""
    return [m.group(0) for m in re.finditer(r"BEGIN:VCARD.*?END:VCARD", text, re.S | re.I)]


def _values(card, prop: str) -> List[Dict[str, str]]:
    out = []
    for line in card.contents.get(prop, []):
        value = line.value
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        types = [t.lower() for t in line.params.get("TYPE", [])]
        kind = next((t for t in types if t not in ("internet", "pref", "voice")), "")
        if str(value).strip():
            out.append({"value": str(value).strip(), "type": kind})
    return out


def photo_bytes(card) -> Optional[bytes]:
    """Octets de la photo intégrée (PHOTO;ENCODING=b ou data: URI), sinon None."""
    for line in card.contents.get("photo", []):
        value = line.value
        if isinstance(value, bytes):
            return value
        if isinstance(value, str) and value.startswith("data:") and "," in value:
            try:
                return base64.b64decode(value.split(",", 1)[1])
            except Exception:
                return None
    return None


def _avatar(data: Optional[bytes]) -> str:
    if not data:
        return ""
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        img.thumbnail((AVATAR_PX, AVATAR_PX))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def summarize(text: str) -> Dict[str, Any]:
    """Champs affichés dans la liste et le panneau (sans la photo pleine)."""
    card = parse(text)
    fn = str(card.fn.value).strip() if hasattr(card, "fn") else ""
    given = family = ""
    if hasattr(card, "n"):
        n = card.n.value
        given, family = str(getattr(n, "given", "") or ""), str(getattr(n, "family", "") or "")
    if not fn:
        fn = (given + " " + family).strip()
    org = ""
    if hasattr(card, "org"):
        org = " ".join(str(v) for v in (card.org.value if isinstance(card.org.value, list) else [card.org.value]))
    nostr = ""
    for prop in ("x-nostr", "x-nostr-pubkey"):
        if prop in card.contents:
            nostr = str(card.contents[prop][0].value).strip()
    for impp in card.contents.get("impp", []):
        if str(impp.value).lower().startswith("nostr:"):
            nostr = nostr or str(impp.value)[6:]
    photo = photo_bytes(card)
    return {
        "uid": str(card.uid.value) if hasattr(card, "uid") else "",
        "fn": fn, "given": given, "family": family, "org": org.strip(),
        "emails": _values(card, "email"), "tels": _values(card, "tel"),
        "note": str(card.note.value) if hasattr(card, "note") else "",
        "bday": str(card.bday.value) if hasattr(card, "bday") else "",
        "nostr": nostr, "has_photo": bool(photo), "avatar": _avatar(photo),
    }


def build(fields: Dict[str, Any], base_text: Optional[str] = None,
          photo: Optional[bytes] = None, drop_photo: bool = False) -> str:
    """Écrit une vCard 3.0 depuis les champs de l'interface. Si `base_text`
    est fourni, les propriétés que l'interface ne connaît pas (adresse,
    anniversaire, groupes, champs propriétaires d'Apple/Google…) sont
    CONSERVÉES : on ne remplace que ce que l'interface édite."""
    vobject = _vobject()
    card = parse(base_text) if base_text else vobject.vCard()
    edited = ("fn", "n", "email", "tel", "org", "note", "x-nostr", "x-nostr-pubkey", "rev", "version")
    for prop in edited:
        card.contents.pop(prop, None)
    if drop_photo or photo is not None:
        card.contents.pop("photo", None)
    card.add("version").value = "3.0"
    fn = (fields.get("fn") or "").strip() or \
        f"{fields.get('given', '')} {fields.get('family', '')}".strip() or "Sans nom"
    card.add("fn").value = fn
    card.add("n").value = vobject.vcard.Name(family=(fields.get("family") or "").strip(),
                                             given=(fields.get("given") or "").strip())
    for item in fields.get("emails") or []:
        if (item.get("value") or "").strip():
            line = card.add("email")
            line.value = item["value"].strip()
            line.type_param = ["INTERNET"] + ([item["type"].upper()] if item.get("type") else [])
    for item in fields.get("tels") or []:
        if (item.get("value") or "").strip():
            line = card.add("tel")
            line.value = item["value"].strip()
            if item.get("type"):
                line.type_param = item["type"].upper()
    if (fields.get("org") or "").strip():
        card.add("org").value = [fields["org"].strip()]
    if (fields.get("note") or "").strip():
        card.add("note").value = fields["note"].strip()
    if (fields.get("nostr") or "").strip():
        card.add("x-nostr").value = fields["nostr"].strip()
    if not hasattr(card, "uid"):
        card.add("uid").value = fields.get("uid") or str(uuid.uuid4())
    if photo is not None:
        line = card.add("photo")
        line.value = photo
        line.encoding_param = "b"
        line.type_param = "JPEG"
    card.add("rev").value = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return card.serialize()


# ═════════════════════════════════════════════════════════════════════════════
# Lecture
# ═════════════════════════════════════════════════════════════════════════════
def _etag(sha: str) -> str:
    return '"' + sha[:32] + '"'


def _refresh_from_index(email: str, data: Dict[str, Any]) -> bool:
    """Réconcilie le résumé avec l'index du cloud : fiches ajoutées, modifiées
    ou supprimées PAR LE DISQUE MONTÉ (hors de ce module). Retourne True si
    le résumé a changé."""
    # Seulement le dossier des contacts (SQLite, index sur `parent`), pas tout l'index.
    entries = {p: json.loads(d) for p, d in cloud_storage.db(email).execute(
        "SELECT path,data FROM entries WHERE parent=?", (CONTACTS_DIR,))}
    prefix = CONTACTS_DIR + "/"
    present = {}
    for path, e in entries.items():
        if path.startswith(prefix) and isinstance(e, dict) and e.get("type") == "file" \
                and path.lower().endswith(".vcf") and "/" not in path[len(prefix):]:
            present[path[len(prefix):]] = e.get("sha256_plain") or ""
    changed = False
    for name, sha in present.items():
        card = data["cards"].get(name)
        if card and card.get("sha") == sha:
            continue
        try:
            text = cloud_storage.read_plaintext(email, _path(name))[0].decode("utf-8", "replace")
            info = summarize(text)
        except Exception as exc:
            logger.warning("contacts: %s illisible (%s) — ignorée", name, exc)
            continue
        data["sync"] += 1
        info.update(name=name, sha=sha, etag=_etag(sha), token=data["sync"], mtime=int(time.time()),
                    source=(card or {}).get("source"), remote=(card or {}).get("remote"),
                    face=(card or {}).get("face"))
        data["cards"][name] = info
        changed = True
    for name in [n for n in data["cards"] if n not in present]:
        data["sync"] += 1
        data["deleted"].append({"name": name, "token": data["sync"]})
        del data["cards"][name]
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


def get_text(email: str, name: str) -> str:
    check_name(name)
    try:
        return cloud_storage.read_plaintext(email, _path(name))[0].decode("utf-8", "replace")
    except FileNotFoundError as exc:
        raise KeyError(name) from exc


def find_by_uid(data: Dict[str, Any], uid: str) -> Optional[str]:
    for name, card in data["cards"].items():
        if card.get("uid") == uid:
            return name
    return None


def ctag(data: Dict[str, Any]) -> str:
    return str(data["sync"])


def changes_since(data: Dict[str, Any], token: Optional[int]) -> Optional[Tuple[List[str], List[str]]]:
    """(noms modifiés, noms supprimés) depuis `token`, ou None si le jeton est
    trop ancien ou inconnu (le client doit alors tout resynchroniser)."""
    if token is None:
        return list(data["cards"]), []
    oldest = data["deleted"][0]["token"] if len(data["deleted"]) >= DELETED_KEEP else 0
    if token < oldest or token > data["sync"]:
        return None
    changed = [n for n, c in data["cards"].items() if c.get("token", 0) > token]
    deleted = sorted({d["name"] for d in data["deleted"] if d["token"] > token and d["name"] not in data["cards"]})
    return changed, deleted


# ═════════════════════════════════════════════════════════════════════════════
# Écriture
# ═════════════════════════════════════════════════════════════════════════════
def _ensure_dir(email: str) -> None:
    with cloud_storage.index_lock(email):
        with cloud_storage.ucloud_db.transaction(cloud_storage.ucloud_dir(email)) as conn:
            cloud_storage.ucloud_db.ensure_parents(conn, _path("x.vcf"))


def put(email: str, name: str, text: str, *, if_match: Optional[str] = None,
        if_none_match: bool = False, source: Optional[str] = None,
        remote: Optional[Dict[str, Any]] = None, face: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Crée ou remplace une fiche. `if_match` (ETag attendu) / `if_none_match`
    (création seulement) appliquent la sémantique HTTP de CardDAV : lève
    PreconditionFailed si la fiche a changé entre-temps (jamais d'écrasement
    silencieux d'une modification faite sur un autre appareil)."""
    check_name(name)
    raw = text.encode("utf-8")
    if len(raw) > MAX_VCARD_BYTES:
        raise ContactError(f"fiche trop volumineuse (> {MAX_VCARD_BYTES // 1024} Ko)")
    info = summarize(text)
    _ensure_dir(email)
    with _lock(email):
        data = _read_summary(email)
        _refresh_from_index(email, data)
        current = data["cards"].get(name)
        if if_none_match and current:
            raise PreconditionFailed(current["etag"])
        if if_match and (not current or current["etag"] != if_match):
            raise PreconditionFailed(current["etag"] if current else "")
        entry = cloud_storage.ingest_plaintext(email, _path(name), raw,
                                               content_type="text/vcard", trigger_faceid=False)
        data["sync"] += 1
        sha = entry.get("sha256_plain") or hashlib.sha256(raw).hexdigest()
        info.update(name=name, sha=sha, etag=_etag(sha), token=data["sync"], mtime=int(time.time()),
                    source=source if source is not None else (current or {}).get("source"),
                    remote=remote if remote is not None else (current or {}).get("remote"),
                    face=face if face is not None else (current or {}).get("face"))
        if current and current.get("avatar") != info["avatar"] and face is None:
            info["face"] = None              # photo changée : référence FaceID à refaire
        data["cards"][name] = info
        _write_summary(email, data)
        return info


class PreconditionFailed(Exception):
    """If-Match / If-None-Match non satisfait (HTTP 412)."""


def delete(email: str, name: str, if_match: Optional[str] = None) -> bool:
    check_name(name)
    with _lock(email):
        data = _read_summary(email)
        _refresh_from_index(email, data)
        current = data["cards"].get(name)
        if not current:
            return False
        if if_match and current["etag"] != if_match:
            raise PreconditionFailed(current["etag"])
        cloud_storage.delete_paths(email, [_path(name)])
        data["sync"] += 1
        data["deleted"].append({"name": name, "token": data["sync"]})
        data["deleted"] = data["deleted"][-DELETED_KEEP:]
        del data["cards"][name]
        _write_summary(email, data)
        return True


def set_meta(email: str, name: str, **meta: Any) -> None:
    """Métadonnées FaceCloud d'une fiche (référence FaceID…), SANS toucher à
    la vCard ni au jeton de synchro (ce n'est pas un changement de contact)."""
    with _lock(email):
        data = _read_summary(email)
        if name in data["cards"]:
            data["cards"][name].update(meta)
            _write_summary(email, data)


def new_name() -> str:
    return f"{uuid.uuid4()}.vcf"
