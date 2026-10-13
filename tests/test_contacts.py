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
