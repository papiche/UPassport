"""
services/identity_links.py — Réconciliation des identités FaceCloud.

Une même personne peut exister sous TROIS formes :
  * une PERSONNE FaceCloud (visages rangés : name / pubkey / email / contact
    dans le payload Qdrant, cf. routers/mailjet.py) ;
  * une FICHE de contact (vCard : EMAIL, X-NOSTR, cf. contacts_store.py) ;
  * un ou plusieurs MULTIPASS (email → clé NOSTR HEX), sur cette station
    ou ailleurs dans la constellation.

Ce module est le SEUL endroit qui décide que deux de ces formes désignent la
même personne. Règles, de la plus sûre à la moins sûre :
  1. même clé NOSTR (HEX)                        → lien certain ;
  2. même email exact (insensible à la casse)    → lien certain ;
  3. même email CANONIQUE (`prenom+etiquette@d` ≡ `prenom@d`) → lien
     PROPOSÉ seulement : une personne peut avoir plusieurs MULTIPASS en
     sous-adresses « + », mais deux personnes différentes aussi (alias
     partagé d'une boîte collective) — l'utilisateur confirme.
La partie locale n'est normalisée QUE par le retrait du « +étiquette » et
la casse : les points (Gmail) ou d'autres alias dépendent du fournisseur et
ne sont jamais retirés.

Annuaire des MULTIPASS sans requête réseau : comptes de CETTE station
(~/.zen/game/nostr/<email>/HEX), son cache (~/.zen/tmp/<NODE>/TW/<email>/HEX)
et la constellation (~/.zen/tmp/swarm/<NODE>/TW/<email>/HEX). Mis en cache
5 minutes.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CACHE: Dict[str, object] = {"at": 0.0, "dir": None}
_TTL = 300.0


def canonical_email(email: str) -> str:
    """`Prenom.Nom+Photos@Exemple.org` → `prenom.nom@exemple.org`."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return ""
    local, _, domain = email.partition("@")
    return local.split("+", 1)[0] + "@" + domain


def _read_hex(path: Path) -> str:
    try:
        value = path.read_text().strip().lower()
    except OSError:
        return ""
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def multipass_directory() -> Dict[str, Dict[str, str]]:
    """{email: {hex, where: 'local'|'swarm', node}} — tous les MULTIPASS
    connus de cette station. Un compte local l'emporte sur une copie swarm."""
    now = time.time()
    if _CACHE["dir"] is not None and now - float(_CACHE["at"]) < _TTL:
        return _CACHE["dir"]  # type: ignore[return-value]
    zen = Path.home() / ".zen"
    out: Dict[str, Dict[str, str]] = {}
    for hex_file in (zen / "tmp" / "swarm").glob("*/TW/*@*/HEX"):
        h = _read_hex(hex_file)
        if h:
            out[hex_file.parent.name.lower()] = {"hex": h, "where": "swarm", "node": hex_file.parents[2].name}
    node = os.environ.get("IPFSNODEID", "")
    if node:
        for hex_file in (zen / "tmp" / node / "TW").glob("*@*/HEX"):
            h = _read_hex(hex_file)
            if h:
                out.setdefault(hex_file.parent.name.lower(), {"hex": h, "where": "swarm", "node": node})
    for hex_file in (zen / "game" / "nostr").glob("*@*/HEX"):
        h = _read_hex(hex_file)
        if h:
            out[hex_file.parent.name.lower()] = {"hex": h, "where": "local", "node": node}
    _CACHE.update(at=now, dir=out)
    return out


def accounts_for_emails(emails: List[str]) -> List[Dict[str, str]]:
    """MULTIPASS correspondant à une liste d'emails : exacts d'abord
    (`match: exact`), puis sous-adresses « + » de la même base (`alias`)."""
    directory = multipass_directory()
    wanted = {e.strip().lower() for e in emails if e and e.strip()}
    bases = {canonical_email(e) for e in wanted} - {""}
    found: Dict[str, Dict[str, str]] = {}
    for email, info in directory.items():
        if email in wanted:
            found[email] = dict(info, email=email, match="exact")
        elif canonical_email(email) in bases:
            found.setdefault(email, dict(info, email=email, match="alias"))
    return sorted(found.values(), key=lambda a: (a["match"] != "exact", a["where"] != "local", a["email"]))


def email_for_hex(hex_pubkey: str) -> Optional[Dict[str, str]]:
    """Inverse : le MULTIPASS (email, station) d'une clé, s'il est connu."""
    hex_pubkey = (hex_pubkey or "").lower()
    for email, info in multipass_directory().items():
        if info["hex"] == hex_pubkey:
            return dict(info, email=email)
    return None


def reconcile(contacts: Dict[str, dict], persons: List[dict]) -> Dict[str, object]:
    """Rapprochements pour l'interface.

    contacts : {nom_ressource: résumé contacts_store (emails, nostr…)}
    persons  : [{key, pubkey, email, contact}] — personnes FaceCloud
    Retourne :
      contacts[nom] = {multipass: [comptes…], person: clé ou None, person_match}
      persons[clé]  = {multipass: [comptes…], contact: nom ou None, contact_match}
    `*_match` vaut 'linked' (lien enregistré), 'exact' (clé ou email
    identiques — certain) ou 'alias' (sous-adresse « + » — à confirmer)."""
    out_c: Dict[str, dict] = {}
    by_hex: Dict[str, str] = {}
    by_email: Dict[str, str] = {}
    by_base: Dict[str, str] = {}
    for name, c in contacts.items():
        emails = [e["value"] for e in c.get("emails") or []]
        accounts = accounts_for_emails(emails)
        nostr = (c.get("nostr") or "").lower()
        if re.fullmatch(r"[0-9a-f]{64}", nostr) and not any(a["hex"] == nostr for a in accounts):
            known = email_for_hex(nostr)
            accounts.insert(0, dict(known, match="exact") if known else
                            {"hex": nostr, "email": "", "where": "nostr", "node": "", "match": "exact"})
        out_c[name] = {"multipass": accounts, "person": None, "person_match": None}
        for a in accounts:
            if a["match"] == "exact":
                by_hex.setdefault(a["hex"], name)
        for e in emails:
            by_email.setdefault(e.strip().lower(), name)
            base = canonical_email(e)
            if base:
                by_base.setdefault(base, name)

    out_p: Dict[str, dict] = {}
    for p in persons:
        key = p["key"]
        accounts: List[Dict[str, str]] = []
        if p.get("pubkey"):
            known = email_for_hex(p["pubkey"])
            accounts.append(dict(known, match="exact") if known else
                            {"hex": p["pubkey"], "email": "", "where": "nostr", "node": "", "match": "exact"})
        if p.get("email"):
            for a in accounts_for_emails([p["email"]]):
                if not any(x["hex"] == a["hex"] for x in accounts):
                    accounts.append(a)
        contact, match = None, None
        if p.get("contact") and p["contact"] in contacts:
            contact, match = p["contact"], "linked"
        elif p.get("pubkey") and p["pubkey"].lower() in by_hex:
            contact, match = by_hex[p["pubkey"].lower()], "exact"
        elif p.get("email") and p["email"].strip().lower() in by_email:
            contact, match = by_email[p["email"].strip().lower()], "exact"
        else:
            for a in accounts:
                if a.get("email") and a["email"] in by_email:
                    contact, match = by_email[a["email"]], "exact"
                    break
            if not contact and p.get("email") and canonical_email(p["email"]) in by_base:
                contact, match = by_base[canonical_email(p["email"])], "alias"
        out_p[key] = {"multipass": accounts, "contact": contact, "contact_match": match}
        if contact and out_c[contact]["person"] is None:
            out_c[contact].update(person=key, person_match=match)
    return {"contacts": out_c, "persons": out_p}
