"""
tests/test_contacts.py — Carnet FaceCloud : stockage chiffré, serveur CardDAV
(parcours d'un client type DAVx5), réconciliation des identités.

IPFS mocké (blobs en mémoire), chiffrement réel ; GAME_NOSTR_PATH redirigé
vers un répertoire temporaire — aucun compte réel n'est touché.
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

from services import cloud_storage as cs
from services import contacts_store as store
from services import identity_links

_TMP = Path(tempfile.mkdtemp(prefix="upassport-contacts-test-"))
cs.GAME_NOSTR_PATH = _TMP / "nostr"
cs.CACHE_DIR = _TMP / "cache"

D = "{DAV:}"
C = "{urn:ietf:params:xml:ns:carddav}"
VCARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:marie-1\r\nFN:Marie Dupont\r\nN:Dupont;Marie;;;\r\n"
         "EMAIL;TYPE=INTERNET:marie@example.org\r\nTEL;TYPE=CELL:+33600000000\r\n"
         "ADR;TYPE=HOME:;;1 rue des Lilas;Lyon;;69000;France\r\nX-CUSTOM-APP:conserve-moi\r\nEND:VCARD\r\n")


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


class FakeRelay:
    """Relais NOSTR en mémoire branché sur services.face_invites (aucun réseau)."""
    def __init__(self):
        self.events, self.up = [], True

    def query(self, filt, timeout=8.0, strict=False):
        if not self.up:
            if strict:
                from services import face_invites
                raise face_invites.RelayUnavailable("hors ligne")
            return []
        out = []
        for ev in self.events:
            if ev["kind"] not in filt.get("kinds", [ev["kind"]]):
                continue
            tags = [tuple(t[:2]) for t in ev["tags"]]
            if any(("t", v) not in tags for v in filt.get("#t", [])[:1]) or \
               any(("d", v) not in tags for v in filt.get("#d", [])[:1]) or \
               any(("i", v) not in tags for v in filt.get("#i", [])[:1]):
                continue
            out.append(ev)
        return out

    def publish(self, keys, spec):
        if not self.up:
            return []
        for d, t, content, expires in spec:
            self.events.append({"id": f"ev{len(self.events)}", "kind": 30078, "pubkey": str(keys), "created_at": int(time.time()),
                                "tags": [["d", d], ["t", t]], "content": content})
        return ["fake"]


def plug_relay(test):
    from services import face_invites as fi
    test.relay = FakeRelay()
    test._fi_real = (fi._query, fi._publish, fi._keys)
    fi._query, fi._publish, fi._keys = test.relay.query, test.relay.publish, lambda email: email
    test._pin_real = cs.ipfs_pin
    cs.ipfs_pin = lambda cid, timeout=300.0: True


def unplug_relay(test):
    from services import face_invites as fi
    fi._query, fi._publish, fi._keys = test._fi_real
    cs.ipfs_pin = test._pin_real


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIPFS()
        self._real = (cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis, cs.unpin_orphaned_cids)
        cs.ipfs_add_bytes, cs.ipfs_cat = self.fake.add_bytes, self.fake.cat
        cs._trigger_faceid_analysis = lambda *a, **k: None
        cs.unpin_orphaned_cids = lambda *a, **k: None
        self.email = f"c{time.time_ns()}@example.com"

    def tearDown(self):
        cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis, cs.unpin_orphaned_cids = self._real


# ═════════════════════════════════════════════════════════════════════════════
class TestStore(Base):
    def test_build_keeps_unknown_properties(self):
        fields = store.summarize(VCARD)
        fields["fn"] = "Marie Martin"
        out = store.build(fields, base_text=VCARD)
        self.assertIn("X-CUSTOM-APP:conserve-moi", out)
        self.assertIn("1 rue des Lilas", out)
        self.assertEqual(store.summarize(out)["fn"], "Marie Martin")
        self.assertEqual(store.summarize(out)["uid"], "marie-1")

    def test_put_get_etag_and_sync_journal(self):
        c1 = store.put(self.email, "a.vcf", VCARD)
        self.assertEqual(store.get_text(self.email, "a.vcf").strip()[:11], "BEGIN:VCARD")
        data = store.load(self.email)
        token = data["sync"]
        with self.assertRaises(store.PreconditionFailed):
            store.put(self.email, "a.vcf", VCARD, if_match='"autre"')
        store.put(self.email, "a.vcf", VCARD.replace("Marie Dupont", "Marie D."), if_match=c1["etag"])
        changed, deleted = store.changes_since(store.load(self.email), token)
        self.assertEqual((changed, deleted), (["a.vcf"], []))
        token = store.load(self.email)["sync"]
        store.delete(self.email, "a.vcf")
        changed, deleted = store.changes_since(store.load(self.email), token)
        self.assertEqual((changed, deleted), ([], ["a.vcf"]))

    def test_summary_is_encrypted_on_disk(self):
        store.put(self.email, "a.vcf", VCARD)
        raw = (cs.ucloud_dir(self.email) / "contacts.enc").read_bytes()
        self.assertNotIn(b"Marie", raw)

    def test_edit_through_mounted_disk_is_picked_up(self):
        store.put(self.email, "a.vcf", VCARD)
        cs.ingest_plaintext(self.email, "/Contacts/a.vcf",
                            VCARD.replace("Marie Dupont", "Marie Disque").encode(), content_type="text/vcard",
                            trigger_faceid=False)
        self.assertEqual(store.load(self.email)["cards"]["a.vcf"]["fn"], "Marie Disque")


# ═════════════════════════════════════════════════════════════════════════════
class TestCardDAV(Base):
    def setUp(self):
        super().setUp()
        from routers import carddav, contacts
        self._sched = contacts.schedule_face_refs
        contacts.schedule_face_refs = lambda *a, **k: None
        self._verify = cs.verify_basic_credentials
        cs.verify_basic_credentials = lambda u, p: self.email if (u, p) == (self.email, "secret") else None
        app = FastAPI()
        app.include_router(carddav.router)
        self.client = TestClient(app)
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"{self.email}:secret".encode()).decode()}

    def tearDown(self):
        from routers import contacts
        contacts.schedule_face_refs = self._sched
        cs.verify_basic_credentials = self._verify
        super().tearDown()

    def req(self, method, path, body="", **headers):
        h = dict(self.auth)
        h.update(headers)
        return self.client.request(method, path, content=body.encode() if isinstance(body, str) else body,
                                   headers=h)

    def test_auth_required_and_well_known(self):
        self.assertEqual(self.client.request("PROPFIND", "/carddav/").status_code, 401)
        r = self.client.get("/.well-known/carddav", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (301, "/carddav/"))

    def test_davx5_like_session(self):
        # Découverte : principal → home → carnet
        r = self.req("PROPFIND", "/carddav/", '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>', Depth="0")
        self.assertEqual(r.status_code, 207)
        self.assertEqual(ET.fromstring(r.content).find(f".//{D}current-user-principal/{D}href").text, "/carddav/principal/")
        r = self.req("PROPFIND", "/carddav/principal/", '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:prop><c:addressbook-home-set/></d:prop></d:propfind>')
        self.assertEqual(ET.fromstring(r.content).find(f".//{C}addressbook-home-set/{D}href").text, "/carddav/books/")
        r = self.req("PROPFIND", "/carddav/books/", "", Depth="1")
        hrefs = [e.text for e in ET.fromstring(r.content).iter(f"{D}href")]
        self.assertIn("/carddav/books/contacts/", hrefs)

        # Création, relecture
        r = self.req("PUT", "/carddav/books/contacts/x1.vcf", VCARD, **{"If-None-Match": "*", "Content-Type": "text/vcard"})
        self.assertEqual(r.status_code, 201)
        etag = r.headers["etag"]
        self.assertEqual(self.req("PUT", "/carddav/books/contacts/x1.vcf", VCARD, **{"If-None-Match": "*"}).status_code, 412)
        r = self.req("GET", "/carddav/books/contacts/x1.vcf")
        self.assertIn("Marie Dupont", r.text)
        self.assertEqual(r.headers["etag"], etag)

        # Même UID sous un autre nom → 409
        self.assertEqual(self.req("PUT", "/carddav/books/contacts/autre.vcf", VCARD).status_code, 409)

        # sync-collection initiale puis incrémentale
        body = '<d:sync-collection xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:sync-token/><d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>'
        r = self.req("REPORT", "/carddav/books/contacts/", body)
        root = ET.fromstring(r.content)
        token = root.find(f"{D}sync-token").text
        self.assertEqual(len(root.findall(f"{D}response")), 1)

        self.assertEqual(self.req("PUT", "/carddav/books/contacts/x1.vcf", VCARD.replace("Dupont", "Durand"),
                                  **{"If-Match": '"obsolete"'}).status_code, 412)
        self.assertEqual(self.req("PUT", "/carddav/books/contacts/x1.vcf", VCARD.replace("Marie Dupont", "Marie Durand"),
                                  **{"If-Match": etag}).status_code, 204)
        r = self.req("REPORT", "/carddav/books/contacts/", body.replace("<d:sync-token/>", f"<d:sync-token>{token}</d:sync-token>"))
        root = ET.fromstring(r.content)
        self.assertEqual([e.find(f"{D}href").text for e in root.findall(f"{D}response")], ["/carddav/books/contacts/x1.vcf"])
        token = root.find(f"{D}sync-token").text

        # multiget
        r = self.req("REPORT", "/carddav/books/contacts/",
                     '<c:addressbook-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:prop><d:getetag/><c:address-data/></d:prop>'
                     '<d:href>/carddav/books/contacts/x1.vcf</d:href><d:href>/carddav/books/contacts/absent.vcf</d:href></c:addressbook-multiget>')
        root = ET.fromstring(r.content)
        self.assertIn("Marie Durand", root.find(f".//{C}address-data").text)
        self.assertEqual(len(root.findall(f"{D}response")), 2)

        # Suppression → signalée en 404 à la synchro suivante
        self.assertEqual(self.req("DELETE", "/carddav/books/contacts/x1.vcf").status_code, 204)
        r = self.req("REPORT", "/carddav/books/contacts/", body.replace("<d:sync-token/>", f"<d:sync-token>{token}</d:sync-token>"))
        status = ET.fromstring(r.content).find(f"{D}response/{D}status").text
        self.assertIn("404", status)

        # Jeton inconnu → 403 valid-sync-token
        r = self.req("REPORT", "/carddav/books/contacts/", body.replace("<d:sync-token/>", "<d:sync-token>bidon</d:sync-token>"))
        self.assertEqual(r.status_code, 403)


# ═════════════════════════════════════════════════════════════════════════════
class TestBackup(Base):
    """Paquet de sauvegarde (UPassport/ucloud_backup.py) : export puis
    restauration en FUSION — rien de local n'est jamais écrasé."""

    def test_export_then_merge_restore(self):
        import ucloud_backup
        a, b = self.email, self.email.replace("@", "-b@")
        cs.ingest_plaintext(a, "/Photos/a.jpg", b"photo-A", content_type="image/jpeg")
        store.put(a, "m.vcf", VCARD)
        (cs.ucloud_dir(a) / "webdav_sources.json").write_text("[]")
        cs.ingest_plaintext(b, "/Photos/a.jpg", b"photo-B-locale", content_type="image/jpeg")
        out = _TMP / f"bundle-{time.time_ns()}"
        ucloud_backup.export_bundle(a, out)
        for f in out.rglob("*"):
            self.assertEqual(f.stat().st_mode & 0o777, 0o700 if f.is_dir() else 0o600)
        res = ucloud_backup.apply_bundle(b, out, with_qdrant=False)
        self.assertEqual(res["sources"], 1)
        self.assertEqual(cs.read_plaintext(b, "/Photos/a.jpg")[0], b"photo-B-locale", "fusion : le local gagne")
        self.assertIn("Marie Dupont", store.get_text(b, "m.vcf"), "la fiche de contact suit le cloud")
        self.assertEqual(store.load(b)["cards"]["m.vcf"]["fn"], "Marie Dupont")


# ═════════════════════════════════════════════════════════════════════════════
class TestReceiving(Base):
    def test_blocked_sender_is_refused_on_every_channel(self):
        cs.set_sender_blocked(self.email, "ab" * 32, True)
        res = cs.ingest_received(self.email, b"photo", "image/jpeg", "x.jpg", "ab" * 32, "Spam")
        self.assertEqual(res["status"], "blocked")
        ok = cs.ingest_received(self.email, b"photo", "image/jpeg", "x.jpg", "cd" * 32, "Ami")
        self.assertEqual(ok["status"], "copied")
        cs.set_sender_blocked(self.email, "ab" * 32, False)
        self.assertEqual(cs.blocked_senders(self.email), [])


class TestStart(Base):
    """POST /api/facecloud/start avec une invitation : le MULTIPASS est créé
    pour l'email DE L'INVITATION (jamais un email saisi), les photos copiées
    avant la réponse, la clé renvoyée pour une connexion immédiate."""

    def setUp(self):
        super().setUp()
        plug_relay(self)

    def tearDown(self):
        unplug_relay(self)
        super().tearDown()

    def test_claim_creates_account_and_copies_photos(self):
        import json as _json
        import secrets as _secrets
        from fastapi.testclient import TestClient
        from core.config import settings
        from routers import facecloud, identity, mailjet

        owner, guest = self.email, self.email.replace("@", "-invite@")
        cs.ingest_plaintext(owner, "/Photos/groupe.jpg", b"photo-de-groupe", content_type="image/jpeg")
        token = _secrets.token_urlsafe(24)
        nostr = cs.GAME_NOSTR_PATH
        old_game = settings.GAME_PATH
        settings.GAME_PATH = nostr.parent
        try:
            (nostr / owner / ".face_invites").mkdir(parents=True, exist_ok=True)
            (nostr / owner / "HEX").write_text("ab" * 32)
            (nostr / owner / ".face_invites" / f"{token}.json").write_text(_json.dumps({
                "owner_email": owner, "collection": None, "to_email": guest,
                "photos": [{"point_id": "p1", "source_path": "/Photos/groupe.jpg"}],
                "created_at": int(time.time()), "expires_at": int(time.time()) + 3600}))

            async def fake_create(request, form):
                (nostr / form.email).mkdir(parents=True, exist_ok=True)
                (nostr / form.email / "HEX").write_text("cd" * 32)
                from fastapi.responses import JSONResponse
                return JSONResponse({"nsec": "nsec1test", "npub": "npub1test"})
            real = identity._scan_qr_impl
            identity._scan_qr_impl = fake_create
            app = FastAPI()
            app.include_router(facecloud.router)
            r = TestClient(app).post("/api/facecloud/start", data={"claim": token, "email": "pirate@evil.org"})
            identity._scan_qr_impl = real
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual((body["status"], body["email"], body["nsec"], body["photos"]), ("created", guest, "nsec1test", 1))
            self.assertFalse((nostr / "pirate@evil.org").exists(), "l'email saisi est ignoré quand il y a une invitation")
            self.assertFalse((nostr / owner / ".face_invites" / f"{token}.json").exists(), "invitation consommée")
        finally:
            settings.GAME_PATH = old_game


    def test_open_link_invite_uses_typed_email_and_is_single_use(self):
        import json as _json
        import secrets as _secrets
        from fastapi.testclient import TestClient
        from core.config import settings
        from routers import facecloud, identity

        owner, guest = self.email, self.email.replace("@", "-lien@")
        cs.ingest_plaintext(owner, "/Photos/fete.jpg", b"photo-de-fete", content_type="image/jpeg")
        token = _secrets.token_urlsafe(24)
        nostr = cs.GAME_NOSTR_PATH
        old_game = settings.GAME_PATH
        settings.GAME_PATH = nostr.parent
        try:
            (nostr / owner / ".face_invites").mkdir(parents=True, exist_ok=True)
            (nostr / owner / "HEX").write_text("ab" * 32)
            inv = nostr / owner / ".face_invites" / f"{token}.json"
            inv.write_text(_json.dumps({
                "owner_email": owner, "collection": None, "to_email": "", "open": True,
                "photos": [{"point_id": "p1", "source_path": "/Photos/fete.jpg"}],
                "created_at": int(time.time()), "expires_at": int(time.time()) + 3600}))

            async def fake_create(request, form):
                (nostr / form.email).mkdir(parents=True, exist_ok=True)
                (nostr / form.email / "HEX").write_text("cd" * 32)
                from fastapi.responses import JSONResponse
                return JSONResponse({"nsec": "nsec1test", "npub": "npub1test"})
            real = identity._scan_qr_impl
            identity._scan_qr_impl = fake_create
            app = FastAPI()
            app.include_router(facecloud.router)
            client = TestClient(app)
            info = client.get(f"/api/facecloud/invitation/{token}").json()
            self.assertTrue(info["open"])
            self.assertEqual(info["email"], "")
            r = client.post("/api/facecloud/start", data={"claim": token, "email": guest})
            identity._scan_qr_impl = real
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual((body["status"], body["email"], body["photos"]), ("created", guest, 1))
            self.assertFalse(inv.exists(), "jeton à usage unique : consommé")
        finally:
            settings.GAME_PATH = old_game

    def test_open_invite_is_not_claimable_without_its_token(self):
        import asyncio
        import json as _json
        import secrets as _secrets
        from core.config import settings
        from routers import mailjet

        owner, guest = self.email, self.email.replace("@", "-intrus@")
        token = _secrets.token_urlsafe(24)
        nostr = cs.GAME_NOSTR_PATH
        old_game = settings.GAME_PATH
        settings.GAME_PATH = nostr.parent
        try:
            (nostr / owner / ".face_invites").mkdir(parents=True, exist_ok=True)
            inv = nostr / owner / ".face_invites" / f"{token}.json"
            inv.write_text(_json.dumps({"owner_email": owner, "to_email": "", "open": True, "photos": [],
                                        "created_at": int(time.time()), "expires_at": int(time.time()) + 3600}))
            (nostr / guest).mkdir(parents=True, exist_ok=True)
            (nostr / guest / "HEX").write_text("ef" * 32)
            asyncio.run(mailjet.process_pending_face_claims(guest))            # sans jeton : rien
            self.assertTrue(inv.exists(), "une invitation par lien ne se réclame pas sans son jeton")
            asyncio.run(mailjet.process_pending_face_claims(guest, "autre-jeton"))
            self.assertTrue(inv.exists())
            asyncio.run(mailjet.process_pending_face_claims(guest, token))
            self.assertFalse(inv.exists())
        finally:
            settings.GAME_PATH = old_game


# ═════════════════════════════════════════════════════════════════════════════
class TestPortableInvite(Base):
    """Invitation portée par le relais commun : honorée par une AUTRE station que celle de l'expéditeur,
    et jamais de MULTIPASS en double si l'email existe déjà ailleurs dans la constellation."""

    def setUp(self):
        super().setUp()
        plug_relay(self)
        from core.config import settings
        from routers import facecloud, identity
        self.settings, self.old_game = settings, settings.GAME_PATH
        settings.GAME_PATH = cs.GAME_NOSTR_PATH.parent
        self.created = []

        async def fake_create(request, form):
            self.created.append(form.email)
            (cs.GAME_NOSTR_PATH / form.email).mkdir(parents=True, exist_ok=True)
            (cs.GAME_NOSTR_PATH / form.email / "HEX").write_text("cd" * 32)
            from fastapi.responses import JSONResponse
            return JSONResponse({"nsec": "nsec1test", "npub": "npub1test"})
        self._scan = identity._scan_qr_impl
        identity._scan_qr_impl = fake_create
        app = FastAPI()
        app.include_router(facecloud.router)
        self.client = TestClient(app)

    def tearDown(self):
        from routers import identity
        identity._scan_qr_impl = self._scan
        self.settings.GAME_PATH = self.old_game
        unplug_relay(self)
        super().tearDown()

    def _publish_invite(self, to_email=""):
        import secrets
        from services import face_invites
        owner = self.email
        cs.ingest_plaintext(owner, "/Photos/a.jpg", b"photo-a", content_type="image/jpeg")
        cs.ingest_plaintext(owner, "/Photos/b.jpg", b"photo-b", content_type="image/jpeg")
        token = secrets.token_urlsafe(24)
        data = {"owner_email": owner, "to_email": to_email, "open": not to_email, "expires_at": int(time.time()) + 3600,
                "photos": [{"point_id": "p1", "source_path": "/Photos/a.jpg"}, {"point_id": "p2", "source_path": "/Photos/b.jpg"}]}
        self.assertTrue(face_invites.publish(owner, token, data, "alice"))
        # AUCUN fichier local : la station qui honore l'invitation n'est pas celle de l'expéditeur
        return token

    def test_invite_content_is_unreadable_without_token(self):
        token = self._publish_invite()
        blob = "".join(ev["content"] for ev in self.relay.events)
        self.assertNotIn(token, blob)
        self.assertNotIn("photo-a", blob)
        from services import face_invites
        self.assertIsNone(face_invites.fetch("x" * 24))
        self.assertEqual(len(face_invites.fetch(token)["items"]), 2)

    def test_claim_on_another_station_creates_account_and_adopts_photos(self):
        token = self._publish_invite()
        guest = self.email.replace("@", "-ailleurs@")
        r = self.client.post("/api/facecloud/start", data={"claim": token, "email": guest})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["status"], body["email"], body["photos"]), ("created", guest, 2))
        self.assertEqual(self.created, [guest])
        # usage unique, même depuis une autre station
        self.assertEqual(self.client.get(f"/api/facecloud/invitation/{token}").status_code, 404)

    def test_existing_account_elsewhere_is_never_duplicated(self):
        from services import identity_links, node_share
        token = self._publish_invite()
        guest = self.email.replace("@", "-deja@")
        self.relay.events.append({"id": "k0", "kind": 0, "pubkey": "ee" * 32, "created_at": 1,
                                  "tags": [["i", f"email:{guest}", ""]], "content": "{}"})
        sent = []
        real = (identity_links.email_for_hex, node_share.node_hex_for, node_share.send_items)
        identity_links.email_for_hex = lambda h: {"hex": h, "where": "swarm", "node": "12D3KooWremote"}
        node_share.node_hex_for = lambda n: "ff" * 32
        node_share.send_items = lambda owner, fh, fn, acct, items, face_ref=None: sent.append((acct, items)) or {"status": "sent", "photos": len(items)}
        try:
            r = self.client.post("/api/facecloud/start", data={"claim": token, "email": guest})
        finally:
            identity_links.email_for_hex, node_share.node_hex_for, node_share.send_items = real
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["status"], r.json()["delivered"]), ("elsewhere", 2))
        self.assertEqual(self.created, [], "aucun MULTIPASS créé en double")
        self.assertEqual(sent[0][0]["hex"], "ee" * 32)
        self.assertEqual({i["name"] for i in sent[0][1]}, {"a.jpg", "b.jpg"})

    def test_claim_adds_to_existing_cloud_without_touching_own_photos(self):
        """Compte DÉJÀ présent ici, avec ses propres photos (dont l'une identique à une photo envoyée) :
        les nouvelles arrivent sous /Reçues/<expéditeur>/AAAA/MM/, le doublon est ignoré, l'existant intact."""
        from services import ucloud_db
        token = self._publish_invite()
        guest = self.email.replace("@", "-present@")
        (cs.GAME_NOSTR_PATH / guest).mkdir(parents=True, exist_ok=True)
        (cs.GAME_NOSTR_PATH / guest / "HEX").write_text("cd" * 32)
        cs.ingest_plaintext(guest, "/Photos/mienne.jpg", b"ma-photo", content_type="image/jpeg")
        cs.ingest_plaintext(guest, "/Photos/deja-la.jpg", b"photo-a", content_type="image/jpeg")   # = a.jpg d'Alice
        r = self.client.post("/api/facecloud/start", data={"claim": token, "email": guest})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "exists"))
        self.assertEqual(self.created, [])
        stats = ucloud_db.stats(cs.ucloud_dir(guest))
        self.assertEqual(stats["received"], 1, "b.jpg reçue ; a.jpg était déjà là (même contenu)")
        self.assertEqual(stats["files"], 3)
        self.assertEqual(cs.read_plaintext(guest, "/Photos/mienne.jpg")[0], b"ma-photo")
        rows, total = ucloud_db.page(cs.ucloud_dir(guest), "received")
        self.assertEqual(total, 1)
        self.assertRegex(rows[0][0], r"^/Reçues/.+/\d{4}/\d{2}/b\.jpg$")
        self.assertEqual(rows[0][1]["from_name"], "alice")
        # le même lien ne marche pas deux fois
        self.assertEqual(self.client.get(f"/api/facecloud/invitation/{token}").status_code, 404)

    def test_mixed_case_local_account_is_recognised_not_duplicated(self):
        token = self._publish_invite()
        guest = self.email.replace("@", "-casse@")
        mixed = guest.split("@")[0].upper() + "@Example.com"
        (cs.GAME_NOSTR_PATH / mixed).mkdir(parents=True, exist_ok=True)
        (cs.GAME_NOSTR_PATH / mixed / "HEX").write_text("cd" * 32)
        r = self.client.post("/api/facecloud/start", data={"claim": token, "email": mixed.lower()})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "exists"))
        self.assertEqual(self.created, [])
        self.assertEqual(cs.ucloud_db.stats(cs.ucloud_dir(mixed))["received"], 2, "photos rangées dans le vrai dossier")

    def test_account_elsewhere_with_unknown_station_keeps_invite_valid(self):
        token = self._publish_invite()
        guest = self.email.replace("@", "-inconnue@")
        self.relay.events.append({"id": "k0", "kind": 0, "pubkey": "ee" * 32, "created_at": 1,
                                  "tags": [["i", f"email:{guest}", ""]], "content": "{}"})
        r = self.client.post("/api/facecloud/start", data={"claim": token, "email": guest})
        self.assertEqual((r.json()["status"], r.json()["delivered"]), ("elsewhere", 0))
        self.assertEqual(self.created, [])
        self.assertEqual(self.client.get(f"/api/facecloud/invitation/{token}").status_code, 200)

    def test_relay_down_refuses_to_create(self):
        token = self._publish_invite()
        self.relay.up = False
        r = self.client.post("/api/facecloud/start", data={"claim": token, "email": "nouveau@example.org"})
        self.assertEqual(r.status_code, 404)           # invitation illisible sans relais
        r = self.client.post("/api/facecloud/start", data={"email": "nouveau@example.org"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.created, [])


class FakeQdrant:
    """Qdrant minimal en mémoire, branché à la place de `httpx` dans services.face_ref."""
    def __init__(self):
        self.cols = {}

    class _R:
        def __init__(self, code, data=None):
            self.status_code, self._d = code, data or {}

        def json(self):
            return self._d

    @staticmethod
    def _match(p, flt):
        pl = p["payload"]
        return all(pl.get(c["key"]) == c["match"]["value"] for c in (flt or {}).get("must", []))

    def _handle(self, method, url, json=None):
        col = url.split("/collections/")[1].split("/")[0].split("?")[0]
        if url.endswith(f"/collections/{col}"):
            if method == "GET":
                return self._R(200 if col in self.cols else 404)
            self.cols.setdefault(col, {})
            return self._R(200)
        pts = self.cols.get(col)
        if pts is None:
            return self._R(404)
        if "/points/scroll" in url:
            return self._R(200, {"result": {"points": [dict(id=k, **v) for k, v in pts.items()
                                                       if self._match(v, json.get("filter"))]}})
        if "/points/delete" in url:
            for k in [k for k, v in pts.items() if self._match(v, json.get("filter"))]:
                del pts[k]
            return self._R(200)
        if url.split("?")[0].endswith("/points") and method == "PUT":
            for p in json["points"]:
                pts[p["id"]] = {"vector": p["vector"], "payload": p["payload"]}
            return self._R(200)
        return self._R(400)

    def post(self, url, headers=None, json=None, timeout=None):
        return self._handle("POST", url, json)

    def Client(self, timeout=None):
        outer = self

        class C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, headers=None): return outer._handle("GET", url)
            def put(self, url, headers=None, json=None): return outer._handle("PUT", url, json)
            def post(self, url, headers=None, json=None): return outer._handle("POST", url, json)
        return C()


class TestFaceRef(Base):
    """Empreinte de visage confiée avec les photos : reçue, jamais en double, révocable, bloquable."""

    def setUp(self):
        super().setUp()
        from services import face_ref
        self.fr = face_ref
        self.q = FakeQdrant()
        self._httpx = face_ref.httpx
        face_ref.httpx = self.q
        self.owner = self.email
        self.dest = self.email.replace("@", "-ami@")
        for e, h in ((self.owner, "ab" * 32), (self.dest, "cd" * 32)):
            (cs.GAME_NOSTR_PATH / e).mkdir(parents=True, exist_ok=True)
            (cs.GAME_NOSTR_PATH / e / ".secret.nostr").write_text(f"HEX={h};")
        self.vec = [0.01] * 512
        self.q.cols[f"faces_{'ab' * 32}"] = {"p1": {"vector": self.vec, "payload": {
            "name": "Moi", "pubkey": "ab" * 32, "timestamp": "2026-10-14T10:00:00"}},
            "p2": {"vector": [0.02] * 512, "payload": {"name": "Inconnu_x", "pubkey": None}}}
        cs.ingest_plaintext(self.owner, "/Photos/x.jpg", b"photo-x", content_type="image/jpeg")

    def tearDown(self):
        self.fr.httpx = self._httpx
        super().tearDown()

    def _dest_points(self):
        return self.q.cols.get(f"faces_{'cd' * 32}", {})

    def test_local_share_gives_reference_once(self):
        cs.share_copy(self.owner, "/Photos/x.jpg", self.dest, "ab" * 32, "alice")
        pts = self._dest_points()
        self.assertEqual(len(pts), 1, "seul MON visage (pubkey = ma clé) part, pas l'inconnu")
        pl = next(iter(pts.values()))["payload"]
        self.assertEqual((pl["ref"], pl["pubkey"], pl["name"]), ("shared", "ab" * 32, "alice"))
        self.assertIn("cd" * 32, self.fr.given(self.owner))
        pts.clear()
        cs.ingest_plaintext(self.owner, "/Photos/y.jpg", b"photo-y", content_type="image/jpeg")
        cs.share_copy(self.owner, "/Photos/y.jpg", self.dest, "ab" * 32, "alice")
        self.assertEqual(len(self._dest_points()), 0, "déjà confiée à l'identique : pas renvoyée")

    def test_opt_out_and_blocked_sender(self):
        self.fr.set_sharing(self.owner, False)
        cs.share_copy(self.owner, "/Photos/x.jpg", self.dest, "ab" * 32, "alice")
        self.assertEqual(len(self._dest_points()), 0)
        self.fr.set_sharing(self.owner, True)
        cs.set_sender_blocked(self.dest, "ab" * 32, True)
        self.assertEqual(self.fr.store(self.dest, "ab" * 32, "alice", [self.vec]), 0)

    def test_invalid_vectors_refused_and_replaced_not_cumulated(self):
        self.assertEqual(self.fr.store(self.dest, "ab" * 32, "a", [[0.1] * 10, ["x"] * 512, [float("nan")] * 512]), 0)
        self.assertEqual(self.fr.store(self.dest, "ab" * 32, "a", [self.vec]), 1)
        self.assertEqual(self.fr.store(self.dest, "ab" * 32, "a", [[0.5] * 512]), 1)
        self.assertEqual(len(self._dest_points()), 1)

    def test_receive_from_other_station_and_revoke(self):
        from services import node_share
        msg = {"to_email": self.dest, "to_hex": "cd" * 32, "from_hex": "ee" * 32, "from_name": "bob",
               "items": [], "face_ref": {"vectors": [self.vec]}}
        self.assertEqual(node_share.receive(msg)["status"], "ok")
        self.assertEqual(len(self._dest_points()), 1)
        node_share.receive({"to_email": self.dest, "to_hex": "cd" * 32, "from_hex": "ee" * 32, "face_ref_revoke": True})
        self.assertEqual(len(self._dest_points()), 0)

    def test_portable_invite_carries_reference_to_new_account(self):
        from services import face_invites
        plug_relay(self)
        try:
            import secrets
            data = {"owner_email": self.owner, "to_email": "", "open": True, "expires_at": int(time.time()) + 3600,
                    "photos": [{"point_id": "p1", "source_path": "/Photos/x.jpg"}]}
            token = secrets.token_urlsafe(24)
            self.assertTrue(face_invites.publish(self.owner, token, data, "alice"))
            blob = "".join(ev["content"] for ev in self.relay.events)
            self.assertNotIn("0.01", blob, "le vecteur n'est pas lisible sur le relais")
            record = face_invites.fetch(token)
            self.assertEqual(len(record["face_ref"]["vectors"]), 1)
            counts = face_invites.adopt_items(self.dest, record)
            self.assertEqual(counts["copied"], 1)
            self.assertEqual(len(self._dest_points()), 1)
        finally:
            unplug_relay(self)


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self._dir = identity_links.multipass_directory
        identity_links.multipass_directory = lambda: {
            "fred@x.org": {"hex": "a" * 64, "where": "local", "node": "N1"},
            "fred+labo@x.org": {"hex": "b" * 64, "where": "swarm", "node": "N2"},
            "marie@y.org": {"hex": "c" * 64, "where": "swarm", "node": "N2"},
        }

    def tearDown(self):
        identity_links.multipass_directory = self._dir

    def test_canonical(self):
        self.assertEqual(identity_links.canonical_email(" Fred+Labo@X.org "), "fred@x.org")
        self.assertEqual(identity_links.canonical_email("f.red@x.org"), "f.red@x.org")
        self.assertEqual(identity_links.canonical_email("pas-un-email"), "")

    def test_accounts_exact_then_alias(self):
        accounts = identity_links.accounts_for_emails(["fred@x.org"])
        self.assertEqual([(a["email"], a["match"]) for a in accounts],
                         [("fred@x.org", "exact"), ("fred+labo@x.org", "alias")])

    def test_reconcile_person_contact(self):
        contacts = {"m.vcf": {"emails": [{"value": "marie@y.org"}], "nostr": ""},
                    "f.vcf": {"emails": [{"value": "fred+photos@x.org"}], "nostr": ""}}
        persons = [{"key": "pk:" + "c" * 64, "pubkey": "c" * 64, "email": "", "contact": ""},
                   {"key": "nm:fred", "pubkey": "", "email": "fred@x.org", "contact": ""}]
        out = identity_links.reconcile(contacts, persons)
        self.assertEqual((out["persons"]["pk:" + "c" * 64]["contact"], out["persons"]["pk:" + "c" * 64]["contact_match"]),
                         ("m.vcf", "exact"))
        self.assertEqual((out["persons"]["nm:fred"]["contact"], out["persons"]["nm:fred"]["contact_match"]),
                         ("f.vcf", "alias"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
