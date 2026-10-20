"""degafam.html (assistant « Quitter Google et Apple ») joué dans Chromium, API simulée."""
import json
import mimetypes
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                     # pragma: no cover
    sync_playwright = None

from core.config import settings

EARTH = settings.BASE_DIR.parent / "UPlanet" / "earth"
BASE = "http://station.test"
CREDS = {"dav_url": "https://u.test/dav/", "caldav_url": "https://u.test/caldav/", "carddav_url": "https://u.test/carddav/",
         "email": "alice@exemple.org", "token": "SECRET-TOKEN-123"}


def status(photos=0, contacts=0, agenda=0):
    return {"email": "alice@exemple.org", "enrolled": True, **{k: v for k, v in CREDS.items() if k.endswith("_url")},
            "steps": {"photos": {"done": photos >= 20, "count": photos, "started": True},
                      "contacts": {"done": contacts > 0, "count": contacts, "started": True},
                      "agenda": {"done": agenda > 0, "count": agenda, "started": True},
                      "amis": {"done": False, "count": 0, "started": False}}}


@unittest.skipIf(sync_playwright is None, "playwright indisponible")
class DegafamUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        except Exception:
            try:
                cls.browser = cls.pw.chromium.launch(args=["--no-sandbox"])
            except Exception as exc:                    # pragma: no cover
                cls.pw.stop()
                raise unittest.SkipTest(f"Chromium indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 420, "height": 900})
        self.page = self.ctx.new_page()
        self.errors, self.calls, self.current = [], [], status()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.add_init_script("window.userPubkey='ab'.repeat(32); window.nostr={signEvent: async e => Object.assign(e,{id:'x',sig:'y'})};")

        def earth(route):
            name = route.request.url.split("/earth/", 1)[1].split("?")[0].split("#")[0]
            f = EARTH / name
            route.fulfill(status=200 if f.is_file() else 404, content_type=mimetypes.guess_type(name)[0] or "text/plain",
                          body=f.read_bytes() if f.is_file() else b"")
        self.page.route("**/*", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
        self.page.route(f"{BASE}/earth/**", earth)
        self.page.route("**/api/degafam/status", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(self.current)))

        def reveal(route):
            self.calls.append(route.request.url.rsplit("/", 1)[1])
            route.fulfill(status=200, content_type="application/json", body=json.dumps(CREDS))
        self.page.route("**/api/cloud/reveal", reveal)

    def tearDown(self):
        self.ctx.close()

    def open(self, hash_=""):
        self.page.goto(f"{BASE}/earth/degafam.html{hash_}")
        self.page.evaluate("window.userPubkey = 'ab'.repeat(32)")      # common.js remet la clé à null au chargement
        self.page.wait_for_selector(".dg-step[id^=step-]", timeout=8000)

    def test_la_premiere_etape_non_faite_est_ouverte_et_le_mot_de_passe_cache(self):
        self.current = status(photos=5)
        self.open()
        self.assertTrue(self.page.is_visible("#step-photos .dg-body"))
        self.assertFalse(self.page.is_visible("#step-contacts .dg-body"))
        self.page.click("#step-photos [data-creds]")
        self.page.wait_for_selector("#step-photos .dg-creds")
        self.assertNotIn("SECRET-TOKEN-123", self.page.inner_text("#step-photos"))     # jamais affiché en clair
        self.assertEqual(self.page.get_attribute("#step-photos [data-copy='SECRET-TOKEN-123']", "data-copy"), "SECRET-TOKEN-123")
        self.assertEqual(self.calls, ["reveal"])
        self.assertIn("5 / 20", self.page.inner_text("#step-photos .dg-live"))

    def test_etapes_faites_cochees_et_suivante_ouverte(self):
        self.current = status(photos=134, contacts=3)
        self.open()
        self.assertEqual(self.page.locator(".dg-step.done").count(), 2)
        self.assertTrue(self.page.is_visible("#step-agenda .dg-body"))
        self.assertEqual(self.page.locator("#bar i.on").count(), 2)

    def test_ancre_ouvre_la_bonne_etape_et_android_ios_changent_les_pas(self):
        self.open("#contacts")
        self.assertTrue(self.page.is_visible("#step-contacts .dg-body"))
        self.page.click("#step-contacts [data-os=android]")
        self.assertIn("DAVx", self.page.inner_text("#step-contacts"))
        self.page.click("#step-contacts [data-os=ios]")
        self.assertIn("CardDAV", self.page.inner_text("#step-contacts"))
        self.assertEqual(self.errors, [])

    def test_tout_fait_felicite(self):
        st = status(photos=50, contacts=1, agenda=1)
        st["steps"]["amis"]["done"] = True
        self.current = st
        self.open()
        self.assertIn("quitté les GAFAM", self.page.inner_text("#root"))


if __name__ == "__main__":
    unittest.main()
