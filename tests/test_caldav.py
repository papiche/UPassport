"""
tests/test_caldav.py — Agenda chiffré : stockage, serveur CalDAV (parcours d'un
client type DAVx5) et pont NOSTR (push/pull, suppressions, conflits).

IPFS mocké (blobs en mémoire), chiffrement réel ; relais NOSTR en mémoire avec de
vraies clés et un vrai NIP-44 ; GAME_NOSTR_PATH redirigé vers un répertoire
temporaire — aucun compte réel n'est touché.
"""

import base64
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient

import calendar_nostr_bridge as bridge
from services import calendar_store as store
from services import cloud_storage as cs

_TMP = Path(tempfile.mkdtemp(prefix="upassport-caldav-test-"))
cs.GAME_NOSTR_PATH = _TMP / "nostr"
cs.CACHE_DIR = _TMP / "cache"

D = "{DAV:}"
C = "{urn:ietf:params:xml:ns:caldav}"


def ics(uid="rdv-1", summary="Dentiste", start="20261020T090000Z", end="20261020T100000Z", extra=""):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//FR\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:20261001T000000Z\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
            f"SUMMARY:{summary}\r\nDESCRIPTION:Cabinet\\, 2e étage\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n")


class FakeIPFS:
    def __init__(self):
        self.blobs = {}

    def add_bytes(self, payload, filename="blob.uenc", pin=True):
        import hashlib
        cid = "Qm" + hashlib.sha256(payload).hexdigest()[:44]
        self.blobs[cid] = payload
        return cid

    def cat(self, cid, max_bytes=None):
        return self.blobs[cid]


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIPFS()
        self._real = (cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis, cs.unpin_orphaned_cids)
        cs.ipfs_add_bytes, cs.ipfs_cat = self.fake.add_bytes, self.fake.cat
        cs._trigger_faceid_analysis = lambda *a, **k: None
        cs.unpin_orphaned_cids = lambda *a, **k: None
        self.email = f"k{time.time_ns()}@example.com"

    def tearDown(self):
        cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis, cs.unpin_orphaned_cids = self._real


# ═════════════════════════════════════════════════════════════════════════════
class TestStore(Base):
    def test_summarize(self):
        s = store.summarize(ics())
        self.assertEqual((s["uid"], s["summary"], s["kind"], s["all_day"]), ("rdv-1", "Dentiste", "VEVENT", False))
        self.assertEqual(s["description"], "Cabinet, 2e étage")
        day = store.summarize(ics(start="20261020", end="20261021").replace("DTSTART:", "DTSTART;VALUE=DATE:")
                              .replace("DTEND:", "DTEND;VALUE=DATE:"))
        self.assertTrue(day["all_day"])
        self.assertEqual(day["start"], "2026-10-20")

    def test_rejects_garbage(self):
        for bad in ("pas du calendrier", "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"):
            with self.assertRaises(store.CalendarError):
                store.summarize(bad)
        with self.assertRaises(store.CalendarError):
            store.check_name("../x.ics")
        with self.assertRaises(store.CalendarError):
            store.check_cal("a/b")

    def test_put_etag_ctag_and_journal(self):
        a = store.put(self.email, "personal", "a.ics", ics())
        self.assertTrue(a["cid"])
        d = store.load(self.email)
        c1, token = store.ctag(d, "personal"), d["sync"]
        with self.assertRaises(store.PreconditionFailed):
            store.put(self.email, "personal", "a.ics", ics(), if_match='"autre"')
        with self.assertRaises(store.PreconditionFailed):
            store.put(self.email, "personal", "a.ics", ics(), if_none_match=True)
        store.put(self.email, "personal", "a.ics", ics(summary="Dentiste 2"), if_match=a["etag"])
        d = store.load(self.email)
        self.assertNotEqual(store.ctag(d, "personal"), c1)
        self.assertEqual(store.changes_since(d, "personal", token), (["a.ics"], []))
        token = d["sync"]
        store.delete(self.email, "personal", "a.ics")
        self.assertEqual(store.changes_since(store.load(self.email), "personal", token), ([], ["a.ics"]))
        self.assertIsNone(store.changes_since(store.load(self.email), "personal", 10 ** 6))

    def test_calendars_are_independent(self):
        store.ensure_calendar(self.email, "famille", "Famille")
        store.put(self.email, "famille", "x.ics", ics("f-1"))
        d = store.load(self.email)
        ct = store.ctag(d, "personal")
        store.put(self.email, "famille", "y.ics", ics("f-2"))
        d = store.load(self.email)
        self.assertEqual(store.ctag(d, "personal"), ct)           # l'autre agenda n'a pas bougé
        self.assertEqual(len(store.items_of(d, "famille")), 2)
        self.assertEqual(d["cals"]["famille"]["name"], "Famille")

    def test_summary_encrypted_on_disk_and_plain_ics_encrypted_on_ipfs(self):
        store.put(self.email, "personal", "a.ics", ics(summary="SECRET-MEDICAL"))
        enc = (cs.ucloud_dir(self.email) / "calendar.enc").read_bytes()
        self.assertNotIn(b"SECRET-MEDICAL", enc)
        self.assertTrue(self.fake.blobs)
        for blob in self.fake.blobs.values():
            self.assertNotIn(b"SECRET-MEDICAL", blob)
        self.assertIn("SECRET-MEDICAL", store.get_text(self.email, "personal", "a.ics"))


# ═════════════════════════════════════════════════════════════════════════════
class TestCalDAV(Base):
    def setUp(self):
        super().setUp()
        from routers import caldav
        self.caldav = caldav
        self.kicks = []
        self._kick = caldav._kick_bridge
        caldav._kick_bridge = lambda email, force=False: self.kicks.append(force)
        self._verify = cs.verify_basic_credentials
        cs.verify_basic_credentials = lambda u, p: self.email if (u, p) == (self.email, "secret") else None
        app = FastAPI()
        app.include_router(caldav.router)
        self.client = TestClient(app)
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"{self.email}:secret".encode()).decode()}

    def tearDown(self):
        self.caldav._kick_bridge = self._kick
        cs.verify_basic_credentials = self._verify
        super().tearDown()

    def req(self, method, path, body="", **headers):
        h = dict(self.auth)
        h.update(headers)
        return self.client.request(method, path, content=body.encode() if isinstance(body, str) else body, headers=h)

    def test_auth_required_and_well_known(self):
        self.assertEqual(self.client.request("PROPFIND", "/caldav/").status_code, 401)
        r = self.client.get("/.well-known/caldav", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (301, "/caldav/"))
        self.assertIn("calendar-access", self.client.request("OPTIONS", "/caldav/").headers["DAV"])

    def test_davx5_like_session(self):
        r = self.req("PROPFIND", "/caldav/", '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>', Depth="0")
        self.assertEqual(ET.fromstring(r.content).find(f".//{D}current-user-principal/{D}href").text, "/caldav/principal/")
        r = self.req("PROPFIND", "/caldav/principal/", '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>')
        self.assertEqual(ET.fromstring(r.content).find(f".//{C}calendar-home-set/{D}href").text, "/caldav/calendars/")
        r = self.req("PROPFIND", "/caldav/calendars/", "", Depth="1")
        self.assertIn("/caldav/calendars/personal/", [e.text for e in ET.fromstring(r.content).iter(f"{D}href")])
        r = self.req("PROPFIND", "/caldav/calendars/personal/",
                     '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/">'
                     '<d:prop><d:resourcetype/><d:displayname/><cs:getctag/><d:sync-token/>'
                     '<c:supported-calendar-component-set/></d:prop></d:propfind>', Depth="0")
        root = ET.fromstring(r.content)
        self.assertIsNotNone(root.find(f".//{D}resourcetype/{C}calendar"))
        self.assertEqual(root.find(f".//{D}displayname").text, "Mon agenda")
        self.assertEqual([c.get("name") for c in root.iter(f"{C}comp")], ["VEVENT", "VTODO"])

        # Création, relecture, modification protégée par ETag
        r = self.req("PUT", "/caldav/calendars/personal/rdv-1.ics", ics(), **{"If-None-Match": "*"})
        self.assertEqual(r.status_code, 201)
        etag = r.headers["ETag"]
        self.assertEqual(self.req("PUT", "/caldav/calendars/personal/rdv-1.ics", ics(), **{"If-None-Match": "*"}).status_code, 412)
        r = self.req("GET", "/caldav/calendars/personal/rdv-1.ics")
        self.assertIn("SUMMARY:Dentiste", r.text)
        self.assertEqual(self.req("PUT", "/caldav/calendars/personal/rdv-1.ics", ics(summary="X"), **{"If-Match": '"faux"'}).status_code, 412)
        r = self.req("PUT", "/caldav/calendars/personal/rdv-1.ics", ics(summary="Dentiste 2"), **{"If-Match": etag})
        self.assertEqual(r.status_code, 204)
        self.assertTrue(all(self.kicks) and len(self.kicks) == 2)         # chaque écriture réveille le pont

        # UID déjà pris sous un autre nom → 409
        r = self.req("PUT", "/caldav/calendars/personal/autre.ics", ics())
        self.assertEqual(r.status_code, 409)
        self.assertIsNotNone(ET.fromstring(r.content).find(f"{C}no-uid-conflict"))
        # Contenu invalide → 400
        self.assertEqual(self.req("PUT", "/caldav/calendars/personal/z.ics", "n'importe quoi").status_code, 400)

    def test_reports(self):
        for i, (start, rr) in enumerate((("20261020T090000Z", ""), ("20261120T090000Z", ""), ("20250101T090000Z", "RRULE:FREQ=YEARLY\r\n"))):
            self.req("PUT", f"/caldav/calendars/personal/e{i}.ics",
                     ics(f"u{i}", f"E{i}", start, start.replace("0900", "1000"), rr))
        base = "/caldav/calendars/personal/"
        multiget = ('<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/></d:prop>'
                    f'<d:href>{base}e0.ics</d:href><d:href>{base}absent.ics</d:href></c:calendar-multiget>')
        r = self.req("REPORT", base, multiget)
        root = ET.fromstring(r.content)
        self.assertEqual(len(root.findall(f"{D}response")), 2)
        self.assertIn("SUMMARY:E0", root.find(f".//{C}calendar-data").text)
        self.assertIn("404", ET.tostring(root).decode())

        query = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/></d:prop>'
                 '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                 '<c:time-range start="20261001T000000Z" end="20261031T000000Z"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
        hrefs = [e.text for e in ET.fromstring(self.req("REPORT", base, query).content).iter(f"{D}href")]
        self.assertIn(base + "e0.ics", hrefs)
        self.assertNotIn(base + "e1.ics", hrefs)             # novembre : hors période
        self.assertIn(base + "e2.ics", hrefs)                # récurrent : toujours renvoyé

    def test_sync_collection_incremental_and_invalid_token(self):
        base = "/caldav/calendars/personal/"
        self.req("PUT", base + "a.ics", ics("a", "A"))
        sync = lambda tok: self.req("REPORT", base, f'<d:sync-collection xmlns:d="DAV:"><d:sync-token>{tok}</d:sync-token>'
                                                    '<d:prop><d:getetag/></d:prop></d:sync-collection>')
        root = ET.fromstring(sync("").content)
        self.assertEqual([e.text for e in root.iter(f"{D}href")], [base + "a.ics"])
        token = root.find(f"{D}sync-token").text
        self.assertEqual([e.text for e in ET.fromstring(sync(token).content).iter(f"{D}href")], [])
        self.req("PUT", base + "b.ics", ics("b", "B"))
        self.req("DELETE", base + "a.ics")
        root = ET.fromstring(sync(token).content)
        statuses = {r.find(f"{D}href").text: (r.find(f"{D}status").text if r.find(f"{D}status") is not None else "200")
                    for r in root.findall(f"{D}response")}
        self.assertEqual(statuses[base + "a.ics"], "HTTP/1.1 404 Not Found")
        self.assertEqual(statuses[base + "b.ics"], "200")
        r = sync("https://autre/sync/3")
        self.assertEqual(r.status_code, 403)
        self.assertIsNotNone(ET.fromstring(r.content).find(f"{D}valid-sync-token"))

    def test_mkcalendar_refused_proppatch_polite(self):
        self.assertEqual(self.req("MKCALENDAR", "/caldav/calendars/nouveau/").status_code, 403)
        r = self.req("PROPPATCH", "/caldav/calendars/personal/",
                     '<d:propertyupdate xmlns:d="DAV:" xmlns:i="http://apple.com/ns/ical/"><d:set><d:prop><i:calendar-color>#f00</i:calendar-color></d:prop></d:set></d:propertyupdate>')
        self.assertEqual(r.status_code, 207)
        self.assertIn("403", r.text)
        self.assertEqual(self.req("GET", "/caldav/calendars/inconnu/x.ics").status_code, 404)

    def test_unknown_calendar_put_is_404(self):
        self.assertEqual(self.req("PUT", "/caldav/calendars/inconnu/x.ics", ics()).status_code, 404)


# ═════════════════════════════════════════════════════════════════════════════
class FakeRelay:
    """Relais en mémoire : même contrat que bridge._query / bridge._publish."""
    def __init__(self):
        self.events = []
        self.up = True
        self.n = 0

    def query(self, filters):
        out = []
        for ev in self.events:
            for f in filters:
                if ev["kind"] in f.get("kinds", []) and ev["pubkey"] in f.get("authors", [ev["pubkey"]]):
                    out.append(ev)
                    break
        return out

    def publish(self, keys, kind, content, tags, created_at):
        if not self.up:
            return None, []
        import nostr_sdk
        self.n += 1
        b = nostr_sdk.EventBuilder(nostr_sdk.Kind(kind), content).tags([nostr_sdk.Tag.parse(t) for t in tags]) \
            .custom_created_at(nostr_sdk.Timestamp.from_secs(created_at))
        import json
        ev = json.loads(b.finalize(keys).as_json())
        self.events.append(ev)
        return ev["id"], ["fake"]


class TestBridge(Base):
    def setUp(self):
        super().setUp()
        import nostr_sdk
        self.sdk = nostr_sdk
        self.keys = nostr_sdk.Keys.generate()
        self.pub = self.keys.public_key().to_hex()
        self.relay = FakeRelay()

    def push(self):
        return bridge.push(self.email, self.keys, self.relay.publish)

    def pull(self):
        return bridge.pull(self.email, self.keys, self.relay.query)

    def decrypt(self, content):
        return self.sdk.nip44_decrypt(self.keys.secret_key(), self.keys.public_key(), content)

    def test_pure_conversions(self):
        self.assertEqual(bridge.iso_utc("2026-10-20T09:00:00+02:00"), "2026-10-20T07:00:00.000Z")
        self.assertEqual(bridge.iso_utc("2026-10-20"), "2026-10-20T00:00:00.000Z")
        text = bridge.event_to_ics("pevt-1", "Marché, bio", "ligne1\nligne2", "2026-10-20T09:00:00.000Z", "2026-10-20T10:00:00.000Z")
        s = store.summarize(text)
        self.assertEqual((s["uid"], s["summary"], s["description"]), ("pevt-1", "Marché, bio", "ligne1\nligne2"))
        unix = bridge.event_to_ics("t-1", "NIP-52 unix", "", "1792000000", "")
        self.assertIn("DTSTART:", unix)
        with self.assertRaises(store.CalendarError):
            bridge.event_to_ics("x", "sans date", "", "", "")

    def test_push_is_encrypted_and_idempotent(self):
        store.put(self.email, "personal", "rdv.ics", ics(summary="Oncologue"))
        self.assertEqual(self.push()["pushed"], 1)
        ev = self.relay.events[0]
        self.assertEqual(ev["kind"], 31922)
        self.assertNotIn("Oncologue", str(ev))                              # rien en clair, ni tag ni contenu
        self.assertIn(["private", "nip44"], ev["tags"])
        self.assertIn(["a", f"31924:{self.pub}:personal"], ev["tags"])
        self.assertEqual(bridge.tag(ev, "name"), "🔒")
        payload = __import__("json").loads(self.decrypt(ev["content"]))
        self.assertEqual(payload["title"], "Oncologue")                    # lisible par calendars.html (title/description)
        self.assertIn("BEGIN:VEVENT", payload["ics"])
        self.assertTrue(payload["cid"])
        self.assertEqual(self.push()["pushed"], 0)                          # rien de neuf → rien republié
        store.put(self.email, "personal", "rdv.ics", ics(summary="Oncologue 2"))
        self.assertEqual(self.push()["pushed"], 1)
        self.assertEqual(bridge.tag(self.relay.events[1], "d"), bridge.tag(self.relay.events[0], "d"))  # même d : remplacé
        self.assertGreater(self.relay.events[1]["created_at"], self.relay.events[0]["created_at"])

    def test_push_failure_retries(self):
        store.put(self.email, "personal", "rdv.ics", ics())
        self.relay.up = False
        self.assertEqual(self.push()["errors"], 1)
        self.relay.up = True
        self.assertEqual(self.push()["pushed"], 1)

    def test_roundtrip_pull_creates_item_without_echo(self):
        store.put(self.email, "personal", "rdv.ics", ics(summary="Aller-retour"))
        self.push()
        # Autre station : même clé, agenda vide, mêmes événements sur le relais
        self.email = f"autre{time.time_ns()}@example.com"
        stats = self.pull()
        self.assertEqual(stats["created"], 1)
        items = store.items_of(store.load(self.email), "personal")
        self.assertEqual(items[0]["summary"], "Aller-retour")
        self.assertEqual(self.push()["pushed"], 0)                          # l'événement tiré n'est pas renvoyé
        self.assertEqual(self.pull()["created"], 0)

    def _calendars_html_event(self, d, title, start, private=True, created_at=None, cal="personal"):
        """Événement tel que l'écrit calendars.html (kind 31922)."""
        content = self.sdk.nip44_encrypt(self.keys.secret_key(), self.keys.public_key(),
                                         __import__("json").dumps({"title": title, "description": "notes"}),
                                         self.sdk.Nip44Version.V2) if private else "notes"
        tags = [["d", d], ["name", "🔒" if private else title], ["start", start],
                ["a", f"31924:{self.pub}:{cal}"]] + ([["private", "nip44"]] if private else [])
        self.relay.publish(self.keys, 31922, content, tags, created_at or int(time.time()))

    def test_pull_calendars_html_events_private_and_public(self):
        self._calendars_html_event("pevt-1", "🌱 Semis", "2026-10-20T08:00:00.000Z")
        self.relay.publish(self.keys, 31924, "", [["d", "marche"], ["title", "Marché public"]], int(time.time()))
        self._calendars_html_event("event-9", "Marché bio", "2026-10-25T07:00:00.000Z", private=False, cal="marche")
        stats = self.pull()
        self.assertEqual((stats["created"], stats["calendars"]), (2, 1))
        d = store.load(self.email)
        self.assertEqual(d["cals"]["marche"]["name"], "Marché public")
        self.assertEqual(store.items_of(d, "personal")[0]["summary"], "🌱 Semis")
        self.assertEqual(store.items_of(d, "marche")[0]["summary"], "Marché bio")
        # Modifié depuis le téléphone : l'événement public reste public, mais un NOUVEL événement part chiffré
        name = store.items_of(d, "marche")[0]["name"]
        store.put(self.email, "marche", name, store.get_text(self.email, "marche", name).replace("Marché bio", "Marché bio 2"))
        store.put(self.email, "marche", "nouveau.ics", ics("nv", "Rendez-vous privé"))
        self.assertEqual(self.push()["pushed"], 2)
        pushed = self.relay.events[-2:]
        public = next(e for e in pushed if bridge.tag(e, "d") == "event-9")
        self.assertEqual(bridge.tag(public, "name"), "Marché bio 2")
        private = next(e for e in pushed if bridge.tag(e, "d") != "event-9")
        self.assertEqual(bridge.tag(private, "name"), "🔒")
        self.assertNotIn("Rendez-vous privé", str(private))

    def test_remote_update_and_local_edit_wins(self):
        self._calendars_html_event("pevt-1", "V1", "2026-10-20T08:00:00.000Z", created_at=1000)
        self.pull()
        name = store.items_of(store.load(self.email), "personal")[0]["name"]
        self._calendars_html_event("pevt-1", "V2 distante", "2026-10-20T08:00:00.000Z", created_at=2000)
        self.assertEqual(self.pull()["updated"], 1)
        self.assertEqual(store.items_of(store.load(self.email), "personal")[0]["summary"], "V2 distante")
        # Modification locale non poussée + nouvelle version distante → le local gagne, puis est poussé
        store.put(self.email, "personal", name, ics("pevt-1", "Locale"))
        self._calendars_html_event("pevt-1", "V3 distante", "2026-10-20T08:00:00.000Z", created_at=3000)
        self.assertEqual(self.pull()["skipped"], 1)
        self.assertEqual(store.items_of(store.load(self.email), "personal")[0]["summary"], "Locale")
        self.assertEqual(self.push()["pushed"], 1)

    def test_deletions_both_ways(self):
        store.put(self.email, "personal", "rdv.ics", ics(summary="À supprimer"))
        self.push()
        d_tag = bridge.tag(self.relay.events[0], "d")
        store.delete(self.email, "personal", "rdv.ics")
        self.assertEqual(self.push()["deleted"], 1)
        kill = self.relay.events[-1]
        self.assertEqual(kill["kind"], 5)
        self.assertIn(["a", f"31922:{self.pub}:{d_tag}"], kill["tags"])
        self.assertEqual(self.push()["deleted"], 0)                         # relayée une seule fois
        self.assertEqual(self.pull()["created"], 0)                         # l'événement supprimé ne ressuscite pas

        # Suppression faite dans calendars.html (kind 5) → disparaît du téléphone, sans écho
        self._calendars_html_event("pevt-2", "Distant", "2026-10-21T08:00:00.000Z", created_at=int(time.time()) - 100)
        self.pull()
        self.assertEqual(len(store.items_of(store.load(self.email), "personal")), 1)
        self.relay.publish(self.keys, 5, "", [["a", f"31922:{self.pub}:pevt-2"]], int(time.time()))
        self.assertEqual(self.pull()["deleted"], 1)
        self.assertEqual(store.items_of(store.load(self.email), "personal"), [])
        before = len(self.relay.events)
        self.assertEqual(self.push()["deleted"], 0)
        self.assertEqual(len(self.relay.events), before)

    def test_restored_account_readopts_existing_event_instead_of_duplicating(self):
        self._calendars_html_event("pevt-1", "Semis", "2026-10-20T08:00:00.000Z")
        self.pull()
        # état du pont perdu (le résumé n'est pas dans la sauvegarde) : l'événement distant revient
        key = store.item_key("personal", store.items_of(store.load(self.email), "personal")[0]["name"])
        store.set_nostr(self.email, key, None)
        self.pull()
        self.assertEqual(len(store.items_of(store.load(self.email), "personal")), 1)

    def test_vtodo_not_mirrored_and_big_ics_not_embedded(self):
        todo = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:t1\r\nSUMMARY:Acheter du pain\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
        store.put(self.email, "personal", "t.ics", todo)
        self.assertEqual(self.push()["skipped"], 1)
        self.assertEqual(self.relay.events, [])
        store.put(self.email, "personal", "big.ics", ics("big", "Gros", extra="DESCRIPTION:" + "x" * 40000 + "\r\n"))
        self.push()
        payload = __import__("json").loads(self.decrypt(self.relay.events[0]["content"]))
        self.assertNotIn("ics", payload)                                    # trop gros pour un relais : seul le CID voyage
        self.assertTrue(payload["cid"])


if __name__ == "__main__":
    unittest.main()
