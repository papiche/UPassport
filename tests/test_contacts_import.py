"""Import de contacts : suivi d'avancement (fichier .vcf et carnet CardDAV) — magasin chiffré simulé."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from routers import contacts as ct  # noqa: E402
from services.nostr import require_nostr_auth  # noqa: E402

EMAIL = "import-test@example.invalid"


def vcf(n):
    return "".join(f"BEGIN:VCARD\nVERSION:3.0\nUID:uid-{i}\nFN:Contact {i}\nEND:VCARD\n" for i in range(n))


class FakeStore:
    """Remplace contacts_store : aucune écriture, aucun IPFS."""
    class ContactError(ValueError):
        pass

    def __init__(self):
        self.puts = 0
        self.slow = 0.0

    def split_vcf(self, text):
        return [c for c in text.split("END:VCARD") if "BEGIN:VCARD" in c]

    def load(self, email):
        return {"cards": {}}

    def summarize(self, text):
        uid = text.split("UID:")[1].split("\n")[0] if "UID:" in text else ""
        if "ILLISIBLE" in text:
            raise self.ContactError("fiche illisible")
        return {"uid": uid}

    def find_by_uid(self, data, uid):
        return None

    def new_name(self):
        return f"n{self.puts}.vcf"

    def put(self, email, name, text, source=None, **kw):
        time.sleep(self.slow)
        self.puts += 1
        return {"sha": "x"}


class ImportTests(unittest.TestCase):
    def setUp(self):
        ct._IMPORT_JOBS.clear()
        self.store = FakeStore()
        self._p = [
            mock.patch.object(ct, "contacts_store", self.store),
            mock.patch.object(ct, "_email_for_authenticated_npub", lambda npub: EMAIL),
            mock.patch.object(ct, "schedule_face_refs", lambda email: None),
        ]
        for p in self._p:
            p.start()
            self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(ct.router)
        app.dependency_overrides[require_nostr_auth] = lambda: "npub-test"
        self.app = app

    def post(self, c, text, name="contacts.vcf"):
        return c.post("/api/contacts/import-vcf", files={"file": (name, text.encode(), "text/vcard")})

    def test_petit_fichier_synchrone_avec_resultat(self):
        with TestClient(self.app) as c:
            r = self.post(c, vcf(5))
        b = r.json()
        self.assertEqual((r.status_code, b["state"], b["created"], b["updated"], b["errors"], b["total"]), (200, "done", 5, 0, 0, 5))

    def test_fiche_illisible_comptee_sans_bloquer_les_autres(self):
        with TestClient(self.app) as c:
            b = self.post(c, vcf(3) + "BEGIN:VCARD\nUID:z\nILLISIBLE\nEND:VCARD\n").json()
        self.assertEqual((b["created"], b["errors"]), (3, 1))

    def test_gros_fichier_en_tache_de_fond_avec_avancement(self):
        self.store.slow = 0.01
        n = ct.IMPORT_SYNC_MAX + 40
        with TestClient(self.app) as c:
            first = self.post(c, vcf(n)).json()
            self.assertEqual((first["state"], first["total"]), ("running", n))     # la requête rend la main tout de suite
            seen_progress, final = False, None
            deadline = time.time() + 20
            while time.time() < deadline:
                st = c.get("/api/contacts/import/status").json()
                if st["state"] == "running" and 0 < st["done"] < n:
                    seen_progress = True
                if st["state"] != "running":
                    final = st
                    break
                time.sleep(0.05)
        self.assertTrue(seen_progress, "l'avancement n'a jamais été visible en cours de route")
        self.assertEqual((final["state"], final["created"], final["done"], final["total"]), ("done", n, n, n))
        self.assertIsNotNone(final["finished"])

    def test_second_import_pendant_un_import_refuse(self):
        self.store.slow = 0.02
        with TestClient(self.app) as c:
            self.post(c, vcf(ct.IMPORT_SYNC_MAX + 20))
            r = self.post(c, vcf(3))
            self.assertEqual(r.status_code, 409)
            deadline = time.time() + 20
            while time.time() < deadline and c.get("/api/contacts/import/status").json()["state"] == "running":
                time.sleep(0.05)

    def test_fichier_vide_refuse_explicitement(self):
        with TestClient(self.app) as c:
            r = self.post(c, "ceci n'est pas une vCard")
        self.assertEqual(r.status_code, 400)

    def test_statut_inactif_sans_import(self):
        with TestClient(self.app) as c:
            self.assertEqual(c.get("/api/contacts/import/status").json(), {"state": "idle"})

    def test_panne_pendant_l_import_est_signalee_pas_silencieuse(self):
        with mock.patch.object(ct, "_import_cards", side_effect=RuntimeError("disque plein")):
            with TestClient(self.app) as c:
                self.post(c, vcf(ct.IMPORT_SYNC_MAX + 5))
                deadline, st = time.time() + 10, {}
                while time.time() < deadline:
                    st = c.get("/api/contacts/import/status").json()
                    if st["state"] != "running":
                        break
                    time.sleep(0.05)
        self.assertEqual(st["state"], "error")
        self.assertIn("disque plein", st["error"])


class SourceProgressTests(unittest.TestCase):
    def test_progression_enregistree_dans_la_source_et_effacee_a_la_fin(self):
        sources = [{"id": "s1", "label": "Nextcloud", "url": "u", "username": "a", "password": "secret", "last_sync": None}]
        store = {"v": sources}
        with mock.patch.object(ct, "_load_sources", lambda email: [dict(s) for s in store["v"]]), \
             mock.patch.object(ct, "_save_sources", lambda email, v: store.update(v=v)):
            ct._record_progress(EMAIL, "s1", {"state": "running", "phase": "apply", "total": 100, "done": 25})
            self.assertEqual(store["v"][0]["progress"]["done"], 25)
            pub = ct._public_source(store["v"][0])
            self.assertEqual(pub["progress"]["state"], "running")
            self.assertNotIn("password", pub)                       # jamais renvoyé à l'interface
            ct._record_sync(EMAIL, "s1", {"at": 1, "created": 100, "updated": 0, "deleted": 0, "kept_local": 0, "errors": 0})
            self.assertEqual(store["v"][0]["progress"]["state"], "done")
            ct._record_sync(EMAIL, "s1", {"at": 2, "error": "401 identifiants refusés"})
            self.assertEqual(store["v"][0]["progress"]["state"], "error")


if __name__ == "__main__":
    unittest.main()
