#!/usr/bin/env python3
"""
calendar_nostr_bridge.py — Pont entre l'agenda CalDAV chiffré (ucloud / IPFS) et NOSTR.

Process COURT et séparé d'UPassport : seul lui lit le NSEC du MULTIPASS (le
serveur web ne le charge jamais — cf. services/cloud_storage). Lancé :
  * détaché par routers/caldav.py après chaque écriture du téléphone (et, au plus
    toutes les 2 min, à chaque lecture) ;
  * chaque jour par RUNTIME/NOSTRCARD.refresh.sh (rattrapage).

Deux sens, une seule source de vérité par ressource :

  téléphone ──CalDAV──▶ /Calendar/<agenda>/<n>.ics (AES-256-GCM, IPFS) ──push──▶ kind 31922
  calendars.html / autre client ──kind 31922──▶ relais ──pull──▶ /Calendar/… ──CalDAV──▶ téléphone

Format NOSTR = celui de UPlanet/earth/calendars.html (NIP-52, kind 31924 pour les
agendas, 31922 pour les événements, `a` = 31924:<pub>:<agenda>). Tout ce qui part
du téléphone est chiffré NIP-44 pour soi-même (tag `private=nip44`, titre « 🔒 »),
contenu {title, description, ics, cid} : l'.ics complet (récurrences, alarmes,
participants) et le CID IPFS de sa copie chiffrée voyagent DANS le chiffré, rien
n'est lisible par le relais. Un événement public créé dans calendars.html reste
public quand on le modifie depuis le téléphone ; un NOUVEL événement du téléphone
n'est jamais publié en clair. Lit aussi les kind 31923 (NIP-52 à date Unix) d'autres clients.

Suppressions : kind 5 (NIP-09) avec tag `a` 31922:<pub>:<d>, dans les deux sens.
Conflit : la modification locale non encore poussée gagne (puis est poussée) ; sinon
l'événement le plus récent (created_at) l'emporte. Jamais d'écrasement silencieux.

Usage :
    calendar_nostr_bridge.py sync   <email>     pull puis push (défaut des déclencheurs)
    calendar_nostr_bridge.py pull   <email>
    calendar_nostr_bridge.py push   <email>
    calendar_nostr_bridge.py status <email>
"""

import sys as _sys
import os as _os

_venv_python = _os.path.expanduser("~/.astro/bin/python3")
if _os.path.exists(_venv_python) and _sys.executable != _venv_python:
    _os.execv(_venv_python, [_venv_python] + _sys.argv)

_UPASSPORT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _UPASSPORT_DIR not in _sys.path:
    _sys.path.insert(0, _UPASSPORT_DIR)
del _sys, _os

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services import calendar_store as store
from services import cloud_storage

RELAYS = ["ws://127.0.0.1:7777", "wss://relay.copylaradio.com"]
ICS_IN_EVENT_MAX = 30_000          # au-delà, l'événement n'embarque pas l'.ics (relais : ~64 Ko)
KIND_EVENT, KIND_CAL, KIND_TIME, KIND_DELETE = 31922, 31924, 31923, 5


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ═════════════════════════════════════════════════════════════════════════════
# Conversions (pures, testées)
# ═════════════════════════════════════════════════════════════════════════════
def tag(ev: Dict[str, Any], name: str) -> str:
    return next((t[1] for t in ev.get("tags", []) if len(t) > 1 and t[0] == name), "")


def iso_utc(value: str) -> str:
    """ISO 8601 (date, datetime naïf ou avec fuseau) → 'YYYY-MM-DDTHH:MM:SS.000Z' comme calendars.html."""
    if not value:
        return ""
    try:
        d = dt.datetime.fromisoformat(value)
    except ValueError:
        try:
            d = dt.datetime.combine(dt.date.fromisoformat(value), dt.time())
        except ValueError:
            return ""
    d = d.astimezone(dt.timezone.utc) if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n")


def _ics_stamp(value: str, all_day: bool = False) -> Optional[Tuple[str, str]]:
    """Tag NIP-52 (ISO, 'YYYY-MM-DD' ou secondes Unix) → (paramètre, valeur) iCalendar."""
    if not value:
        return None
    if value.isdigit():
        d = dt.datetime.fromtimestamp(int(value), dt.timezone.utc)
    else:
        iso = iso_utc(value)
        if not iso:
            return None
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if all_day or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return ";VALUE=DATE", d.strftime("%Y%m%d")
    return "", d.strftime("%Y%m%dT%H%M%SZ")


def event_to_ics(d_tag: str, title: str, description: str, start: str, end: str) -> str:
    """Construit un VEVENT depuis les champs d'un événement NIP-52 sans .ics embarqué."""
    s = _ics_stamp(start)
    if not s:
        raise store.CalendarError("événement sans date de début exploitable")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//UPlanet//FaceCloud//FR", "BEGIN:VEVENT",
             f"UID:{d_tag}", "DTSTAMP:" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
             f"DTSTART{s[0]}:{s[1]}"]
    e = _ics_stamp(end, all_day=s[0] != "")
    if e:
        lines.append(f"DTEND{e[0]}:{e[1]}")
    lines.append("SUMMARY:" + _ics_escape(title or "Événement"))
    if description:
        lines.append("DESCRIPTION:" + _ics_escape(description))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def resource_name(d_tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@+=-]", "_", d_tag)[:110] + ".ics"


def d_for_uid(uid: str) -> str:
    return "ics-" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:24]


def calendar_of(ev: Dict[str, Any], pub: str) -> str:
    prefix = f"{KIND_CAL}:{pub}:"
    for t in ev.get("tags", []):
        if len(t) > 1 and t[0] == "a" and t[1].startswith(prefix):
            return t[1][len(prefix):] or store.DEFAULT_CAL
    return store.DEFAULT_CAL


def build_push_spec(item: Dict[str, Any], ics_text: str, pub: str, prev: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Tags + contenu clair d'un kind 31922 pour une ressource locale (chiffrement fait par l'appelant)."""
    nostr = prev or {}
    d_tag = nostr.get("d") or d_for_uid(item.get("uid") or item["name"])
    tags = [["d", d_tag], ["name", item.get("summary") or "Événement"],
            ["start", iso_utc(item.get("start", "")) or iso_utc(dt.date.today().isoformat())],
            ["a", f"{KIND_CAL}:{pub}:{item['cal']}"], ["t", "food-production"]]
    if item.get("end"):
        tags.append(["end", iso_utc(item["end"])])
    payload = {"v": 1, "title": item.get("summary") or "Événement", "description": item.get("description", ""),
               "cid": item.get("cid", "")}
    if len(ics_text.encode("utf-8")) <= ICS_IN_EVENT_MAX:
        payload["ics"] = ics_text
    return {"d": d_tag, "tags": tags, "payload": payload, "clear": bool(nostr.get("clear"))}


def ics_from_event(ev: Dict[str, Any], decrypt) -> Tuple[str, bool]:
    """(.ics, clair ?) d'un événement distant. `decrypt(content)` déchiffre le NIP-44 personnel."""
    d_tag = tag(ev, "d")
    private = tag(ev, "private") == "nip44"
    title, description, ics = tag(ev, "name"), ev.get("content", ""), ""
    if private:
        data = json.loads(decrypt(ev["content"]))
        title, description, ics = data.get("title") or "Événement", data.get("description", ""), data.get("ics", "")
    if ics:
        store.parse(ics)                       # refuse un .ics embarqué invalide
        return ics, not private
    return event_to_ics(d_tag, title, description, tag(ev, "start"), tag(ev, "end")), not private


# ═════════════════════════════════════════════════════════════════════════════
# Relais
# ═════════════════════════════════════════════════════════════════════════════
def _keys(email: str):
    import nostr_sdk
    raw = (cloud_storage.GAME_NOSTR_PATH / email / ".secret.nostr").read_text()
    m = re.search(r"NSEC=([^;\s]+)", raw)
    if not m:
        raise RuntimeError("NSEC introuvable")
    return nostr_sdk.Keys.parse(m.group(1))


def _query(filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from websockets.sync.client import connect
    found: Dict[str, Dict[str, Any]] = {}
    for relay in RELAYS:
        try:
            with connect(relay, open_timeout=10, close_timeout=5) as ws:
                ws.send(json.dumps(["REQ", "calbr", *filters]))
                deadline = time.time() + 20
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=15))
                    if msg[0] == "EVENT":
                        found[msg[2]["id"]] = msg[2]
                    elif msg[0] == "EOSE":
                        break
        except Exception as exc:
            _log(f"WARN: {relay} : {exc}")
    return list(found.values())


def _publish(keys, kind: int, content: str, tags: List[List[str]], created_at: int) -> Tuple[Optional[str], List[str]]:
    """(id de l'événement, relais l'ayant accepté)."""
    import nostr_sdk
    from websockets.sync.client import connect
    builder = nostr_sdk.EventBuilder(nostr_sdk.Kind(kind), content).tags([nostr_sdk.Tag.parse(t) for t in tags]) \
        .custom_created_at(nostr_sdk.Timestamp.from_secs(created_at))
    ev = json.loads(builder.finalize(keys).as_json())
    ok: List[str] = []
    for relay in RELAYS:
        try:
            with connect(relay, open_timeout=10, close_timeout=5) as ws:
                ws.send(json.dumps(["EVENT", ev]))
                deadline = time.time() + 10
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=10))
                    if msg[0] == "OK":
                        if msg[2]:
                            ok.append(relay)
                        else:
                            _log(f"WARN: {relay} a refusé l'événement : {msg[3] if len(msg) > 3 else ''}")
                        break
        except Exception as exc:
            _log(f"WARN: {relay} injoignable ({exc})")
    return (ev["id"] if ok else None), ok


# ═════════════════════════════════════════════════════════════════════════════
# Pull : NOSTR → agenda
# ═════════════════════════════════════════════════════════════════════════════
def _unique_name(data: Dict[str, Any], cal: str, d_tag: str) -> str:
    name = resource_name(d_tag)
    while store.item_key(cal, name) in data["items"]:
        name = f"{uuid.uuid4().hex[:8]}-{resource_name(d_tag)}"[:120]
    return name


def pull(email: str, keys=None, query=None) -> Dict[str, int]:
    keys = keys or _keys(email)
    query = query or _query
    import nostr_sdk
    pub = keys.public_key().to_hex()

    def decrypt(content: str) -> str:
        return nostr_sdk.nip44_decrypt(keys.secret_key(), keys.public_key(), content)

    events = query([{"kinds": [KIND_CAL, KIND_EVENT, KIND_TIME], "authors": [pub], "limit": 5000},
                    {"kinds": [KIND_DELETE], "authors": [pub], "limit": 2000}])
    stats = {"calendars": 0, "created": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}

    for ev in sorted((e for e in events if e["kind"] == KIND_CAL), key=lambda e: e["created_at"]):
        d_tag = tag(ev, "d")
        if d_tag and store._CAL_RE.match(d_tag):
            before = store.load(email)["cals"].get(d_tag)
            store.ensure_calendar(email, d_tag, tag(ev, "title") or None)
            stats["calendars"] += before is None

    deleted_at: Dict[str, int] = {}
    for ev in events:
        if ev["kind"] == KIND_DELETE:
            for t in ev["tags"]:
                if len(t) > 1 and t[0] == "a":
                    deleted_at[t[1]] = max(deleted_at.get(t[1], 0), ev["created_at"])

    latest: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        if ev["kind"] in (KIND_EVENT, KIND_TIME) and tag(ev, "d"):
            cur = latest.get(tag(ev, "d"))
            if not cur or ev["created_at"] > cur["created_at"]:
                latest[tag(ev, "d")] = ev

    data = store.load(email)
    by_d = {i["nostr"]["d"]: i for i in data["items"].values() if i.get("nostr") and i["nostr"].get("d")}

    for d_tag, ev in latest.items():
        local = by_d.get(d_tag)
        gone = max(deleted_at.get(f"{KIND_EVENT}:{pub}:{d_tag}", 0), deleted_at.get(f"{KIND_TIME}:{pub}:{d_tag}", 0))
        if gone >= ev["created_at"]:
            if local and gone > (local["nostr"].get("at") or 0) and local["sha"] == local["nostr"].get("sha"):
                store.delete(email, local["cal"], local["name"])
                store.ack_deleted(email, [store.item_key(local["cal"], local["name"])])
                stats["deleted"] += 1
            continue
        if local:
            st = local["nostr"]
            if st.get("id") == ev["id"] or ev["created_at"] <= (st.get("at") or 0) or local["sha"] != st.get("sha"):
                stats["skipped"] += 1
                continue
        try:
            ics, clear = ics_from_event(ev, decrypt)
            cal = calendar_of(ev, pub)
            if cal not in data["cals"]:
                store.ensure_calendar(email, cal)
                data = store.load(email)
            if not local:
                # Compte restauré (l'état du pont n'est pas dans la sauvegarde) : l'événement existe déjà
                # localement sous son UID — on le ré-adopte au lieu de créer un doublon sur le téléphone.
                twin = store.find_by_uid(data, cal, store.summarize(ics)["uid"])
                local = data["items"].get(store.item_key(cal, twin)) if twin else None
            name = local["name"] if local else _unique_name(data, cal, d_tag)
            if local and local["cal"] != cal:
                cal, name = local["cal"], local["name"]       # un événement ne change pas d'agenda côté téléphone
            info = store.put(email, cal, name, ics, nostr={"d": d_tag, "id": ev["id"], "at": ev["created_at"],
                                                             "sha": None, "clear": clear})
            store.set_nostr(email, store.item_key(cal, name),
                            {"d": d_tag, "id": ev["id"], "at": ev["created_at"], "sha": info["sha"], "clear": clear})
            stats["updated" if local else "created"] += 1
        except Exception as exc:
            _log(f"WARN: événement {d_tag} ignoré ({exc})")
            stats["errors"] += 1
        data = store.load(email)
        by_d = {i["nostr"]["d"]: i for i in data["items"].values() if i.get("nostr") and i["nostr"].get("d")}
    return stats


# ═════════════════════════════════════════════════════════════════════════════
# Push : agenda → NOSTR
# ═════════════════════════════════════════════════════════════════════════════
def push(email: str, keys=None, publish=None) -> Dict[str, int]:
    keys = keys or _keys(email)
    publish = publish or _publish
    import nostr_sdk
    pub = keys.public_key().to_hex()
    stats = {"pushed": 0, "deleted": 0, "skipped": 0, "errors": 0}
    data = store.load(email)

    for key, item in list(data["items"].items()):
        prev = item.get("nostr") or {}
        if prev.get("sha") == item["sha"]:
            continue
        if item.get("kind") != "VEVENT":
            stats["skipped"] += 1
            continue
        try:
            ics = store.get_text(email, item["cal"], item["name"])
            spec = build_push_spec(item, ics, pub, prev)
            if spec["clear"]:
                spec["tags"][1] = ["name", spec["payload"]["title"]]
                content = spec["payload"]["description"]
            else:
                spec["tags"][1] = ["name", "🔒"]
                spec["tags"].append(["private", "nip44"])
                content = nostr_sdk.nip44_encrypt(keys.secret_key(), keys.public_key(),
                                                  json.dumps(spec["payload"], ensure_ascii=False),
                                                  nostr_sdk.Nip44Version.V2)
            at = max(int(time.time()), (prev.get("at") or 0) + 1)
            ev_id, relays = publish(keys, KIND_EVENT, content, spec["tags"], at)
            if not ev_id:
                raise RuntimeError("aucun relais n'a accepté l'événement")
            store.set_nostr(email, key, {"d": spec["d"], "id": ev_id, "at": at, "sha": item["sha"],
                                         "clear": spec["clear"]})
            stats["pushed"] += 1
        except Exception as exc:
            _log(f"WARN: {key} non publié ({exc})")
            stats["errors"] += 1

    done: List[str] = []
    for d in data["deleted"]:
        st = d.get("nostr")
        if not st or not st.get("d") or st.get("deleted"):
            continue
        try:
            ref = f"{KIND_EVENT}:{pub}:{st['d']}"
            ev_id, _ = publish(keys, KIND_DELETE, "Supprimé depuis l'agenda CalDAV",
                               [["a", ref], ["e", st.get("id") or ""]] if st.get("id") else [["a", ref]],
                               int(time.time()))
            if not ev_id:
                raise RuntimeError("aucun relais n'a accepté la suppression")
            done.append(d["key"])
            stats["deleted"] += 1
        except Exception as exc:
            _log(f"WARN: suppression de {d['key']} non publiée ({exc})")
            stats["errors"] += 1
    if done:
        store.ack_deleted(email, done)
    return stats


# ═════════════════════════════════════════════════════════════════════════════
def sync(email: str) -> Dict[str, Any]:
    """Pull puis push, un seul passage à la fois par MULTIPASS (verrou non bloquant)."""
    lock_path = store._udir(email) / ".calendar_bridge.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"status": "busy"}
        keys = _keys(email)
        res = {"status": "ok", "pull": pull(email, keys), "push": push(email, keys)}
        res["at"] = int(time.time())
        path = store._udir(email) / "calendar_bridge.json"
        fd2 = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd2, "w") as fh:
            json.dump(res, fh)
        return res
    finally:
        os.close(fd)


def status(email: str) -> Dict[str, Any]:
    try:
        last = json.loads((store._udir(email) / "calendar_bridge.json").read_text())
    except (OSError, ValueError):
        last = {}
    data = store.load(email)
    items = list(data["items"].values())
    return {"last": last, "calendars": len(data["cals"]), "items": len(items),
            "pending_push": sum(1 for i in items if i.get("kind") == "VEVENT" and (i.get("nostr") or {}).get("sha") != i["sha"])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("cmd", choices=["sync", "pull", "push", "status"])
    ap.add_argument("email")
    args = ap.parse_args()
    try:
        if args.cmd == "sync":
            out = sync(args.email)
        elif args.cmd == "pull":
            out = pull(args.email)
        elif args.cmd == "push":
            out = push(args.email)
        else:
            out = status(args.email)
    except Exception as exc:
        print(f"ERROR:calendar_bridge:{exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
