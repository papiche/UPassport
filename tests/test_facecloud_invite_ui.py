"""
tests/test_facecloud_invite_ui.py — l'écran d'accueil d'un INVITÉ (ucloud.html#demarrer&claim=…), joué dans Chromium.

L'API est simulée. On vérifie ce que l'invité VOIT : son cadeau d'abord (pas le discours générique), les pochettes,
le compteur, l'email demandé tout de suite pour un lien, le bouton qui annonce les étapes, la touche Entrée.
"""
import json
import mimetypes
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                     # pragma: no cover
    sync_playwright = None

from core.config import settings

EARTH = settings.BASE_DIR.parent / "UPlanet" / "earth"
BASE = "http://station.test"
TOKEN = "K" * 32
PIXEL = ("data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


@unittest.skipIf(sync_playwright is None, "playwright indisponible")
class InviteUI(unittest.TestCase):
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
        self.ctx = self.browser.new_context(viewport={"width": 420, "height": 900}, reduced_motion="reduce")
        self.page = self.ctx.new_page()
        self.errors, self.starts = [], []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.invitation = {"from_name": "Fred", "photos": 23, "previews": [PIXEL] * 4, "email": "", "open": True,
                           "expires_at": 2_000_000_000, "has_account": False}

        def earth(route):
            name = route.request.url.split("/earth/", 1)[1].split("?")[0].split("#")[0]
            f = EARTH / name
            if f.is_file():
                route.fulfill(status=200, content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                              body=f.read_bytes())
            else:
                route.fulfill(status=404, body="")
        self.page.route("**/*", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
        self.page.route(f"{BASE}/earth/**", earth)

        def inv(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(self.invitation))
        self.page.route("**/api/facecloud/invitation/*", inv)

        def start(route):
            self.starts.append(route.request.post_data)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "exists", "email": "a@b.fr"}))
        self.page.route("**/api/facecloud/start", start)

    def tearDown(self):
        self.ctx.close()

    def open(self):
        self.page.goto(f"{BASE}/earth/ucloud.html#demarrer&claim={TOKEN}")
        self.page.wait_for_selector("#invite:not([hidden])", timeout=8000)

    def test_le_cadeau_passe_avant_le_discours(self):
        self.open()
        self.assertFalse(self.page.is_visible("#welcome h1"))
        self.assertEqual(self.page.locator(".fc-pola").count(), 4)
        self.assertEqual(self.page.inner_text(".fc-pola-more"), "+19")
        self.page.wait_for_function("document.querySelector('.fc-invite-n').textContent === '23'")
        self.assertIn("Fred", self.page.inner_text("#invite-title"))
        self.assertIn("reconnu", self.page.inner_text("#invite-title"))
        self.assertIn("rendez-lui la pareille", self.page.inner_text("#invite-recip"))
        self.page.screenshot(path="/tmp/claude-1000/-home-fred-workspace-AAA/8591e304-d93b-4c65-a8b5-38894847f904/scratchpad/invite.png")
        self.assertEqual(self.errors, [])

    def test_lien_ouvert_demande_l_email_tout_de_suite(self):
        self.open()
        self.assertTrue(self.page.is_visible("#invite-email"))
        self.assertIn("recevoir", self.page.inner_text("#btn-claim").lower())

    def test_invitation_par_email_n_en_demande_pas(self):
        self.invitation.update(open=False, email="a•••@b.fr")
        self.open()
        self.assertFalse(self.page.is_visible("#invite-email"))
        self.assertEqual(self.page.inner_text("#btn-claim"), "Recevoir mes photos")

    def test_un_seul_clic_cree_le_compte_et_entree_aussi(self):
        self.open()
        self.page.click("#btn-claim")                       # sans email : rien ne part
        self.assertEqual(self.starts, [])
        self.page.fill("#invite-email", "alice@exemple.org")
        self.page.press("#invite-email", "Enter")
        self.page.wait_for_timeout(300)
        self.assertEqual(len(self.starts), 1)
        self.assertIn("alice@exemple.org", self.starts[0])
        self.assertIn(TOKEN, self.starts[0])

    def test_invitation_expiree_laisse_creer_un_compte(self):
        self.page.route("**/api/facecloud/invitation/*", lambda r: r.fulfill(
            status=404, content_type="application/json", body=json.dumps({"error": "Cette invitation n'existe pas ou a expiré."})))
        self.page.goto(f"{BASE}/earth/ucloud.html#demarrer&claim={TOKEN}")
        self.page.wait_for_selector("#login-error:not([hidden])", timeout=8000)
        self.assertIn("expiré", self.page.inner_text("#login-error"))
        self.assertTrue(self.page.is_visible("#start"))


if __name__ == "__main__":
    unittest.main()
