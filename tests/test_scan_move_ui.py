"""
tests/test_scan_move_ui.py — l'onglet « Déménager » de /scan, joué dans un vrai Chromium (Playwright).

L'API est SIMULÉE (page.route) et le temps accéléré (page.clock) : on vérifie ce que la personne VOIT — validation du
QR P1 et du PASS, envoi, barre et étapes d'avancement qui évoluent, documents rapatriés, erreurs, reprise après
rechargement, annulation, relance — et que ni le QR ni le PASS ne restent dans la page ni dans le stockage du navigateur.

    pytest tests/test_scan_move_ui.py -v
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                     # pragma: no cover
    sync_playwright = None

from core.config import settings

TEMPLATE = settings.BASE_DIR / "templates" / "scan.html"
EARTH = settings.BASE_DIR.parent / "UPlanet" / "earth"
HEAD, NS = "1-" + "ab" * 64, "k51qzi5uqu5d" + "a" * 50
PASS = "4242"
EMAIL = "move@example.com"
BASE = "http://station.test"


def make_qr() -> str:
    out = subprocess.run([sys.executable, str(settings.TOOLS_PATH / "Mbase58.py"), "encode", f"{HEAD}:{NS}"],
                         capture_output=True, text=True, timeout=20).stdout.strip()
    return "M-" + out


STEPS = ["recognized", "accepted", "received", "published", "documents", "notified"]


def status(state, percent, done, active=None, error=None, pin=None, msg="", **kw):
    steps = []
    for i, sid in enumerate(STEPS):
        st = "error" if i == error else "done" if i < done else "active" if i == active else "pending"
        steps.append({"id": sid, "label": f"étape {sid}", "status": st})
    out = {"state": state, "role": "target", "percent": percent, "steps": steps, "message": msg, "pin": pin,
           "finished": state == "done", "failed": state in ("error", "pin_error"),
           "can_abort": state in ("requested", "error"), "can_retry_pin": state in ("claimed", "pin_error")}
    out.update(kw)
    return out


@unittest.skipIf(sync_playwright is None, "playwright indisponible")
class ScanMoveUI(unittest.TestCase):
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
        cls.qr = make_qr()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 420, "height": 900})
        self.page = self.ctx.new_page()
        self.errors, self.posts = [], []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.queue = []                                 # états renvoyés successivement par /status
        self.start_reply = (202, {"email": EMAIL, "ticket": "T" * 22, "status": status("requested", 5, 1, active=1)})
        self.status_code = 200

        html = TEMPLATE.read_text(encoding="utf-8")
        self.page.route("**/*", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
        def serve_js(name):
            body = (EARTH / name).read_text(encoding="utf-8", errors="replace")
            return lambda route: route.fulfill(status=200, content_type="application/javascript", body=body)
        for name in ("jsQR.min.js", "nacl-fast.min.js", "blake2b_browser.js"):
            self.page.route(f"**/earth/{name}", serve_js(name))
        self.page.route(f"{BASE}/scan*", lambda r: r.fulfill(status=200, content_type="text/html", body=html))

        def api_start(route):
            self.posts.append(("start", route.request.post_data_json))
            code, body = self.start_reply
            route.fulfill(status=code, content_type="application/json", body=json.dumps(body))

        def api_status(route):
            if self.status_code == 404:
                return route.fulfill(status=404, content_type="application/json", body='{"detail":"Aucun déménagement"}')
            st = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(st))

        def api_post(kind):
            def h(route):
                self.posts.append((kind, route.request.post_data_json))
                route.fulfill(status=202 if kind == "pin" else 200, content_type="application/json", body='{"ok": true}')
            return h
        self.page.route("**/api/migration/start", api_start)
        self.page.route("**/api/migration/status*", api_status)
        self.page.route("**/api/migration/pin", api_post("pin"))
        self.page.route("**/api/migration/abort", api_post("abort"))
        self.page.clock.install()

    def tearDown(self):
        self.ctx.close()

    # ── helpers ────────────────────────────────────────────────────────────────
    def open(self, hash_=""):
        self.page.goto(f"{BASE}/scan{hash_}")
        self.page.wait_for_selector("#tab-move")

    def fill_valid(self):
        self.page.fill("#mv-qr", self.qr)
        self.page.fill("#mv-pass", PASS)

    def tick(self, ms=3100):
        self.page.clock.run_for(ms)
        self.page.wait_for_timeout(50)

    def steps(self):
        return self.page.eval_on_selector_all("#mv-steps li", "els => els.map(e => e.className)")

    # ── onglets ────────────────────────────────────────────────────────────────
    def test_tabs_switch_and_deep_link(self):
        self.open()
        self.assertTrue(self.page.is_visible("#panel-billet"))
        self.assertFalse(self.page.is_visible("#panel-move"))
        self.page.click("#tab-move")
        self.assertTrue(self.page.is_visible("#panel-move"))
        self.assertFalse(self.page.is_visible("#panel-billet"))
        self.assertEqual(self.page.get_attribute("#tab-move", "aria-selected"), "true")
        self.page.click("#tab-billet")
        self.assertTrue(self.page.is_visible("#panel-billet"))
        self.open("#demenager")
        self.assertTrue(self.page.is_visible("#panel-move"))                        # lien direct /scan#demenager
        self.assertEqual(self.errors, [])

    def test_header_tells_the_truth_of_each_tab(self):
        """L'onglet Billet vérifie dans le navigateur ; l'onglet Déménager ENVOIE le QR et le PASS à la station."""
        self.open()
        self.assertIn("tout se passe dans ce navigateur", self.page.inner_text("#hd-sub"))
        self.page.click("#tab-move")
        sub = self.page.inner_text("#hd-sub")
        self.assertIn("envoyés à cette station", sub)
        self.assertNotIn("tout se passe dans ce navigateur", sub)                      # jamais l'affirmation inverse
        self.assertIn("Déménager", self.page.inner_text("#hd-title"))
        self.page.click("#tab-billet")
        self.assertIn("Scanner un", self.page.inner_text("#hd-title"))
        self.assertIn("tout se passe dans ce navigateur", self.page.inner_text("#hd-sub"))

    def test_password_fields_use_the_dark_field_style(self):
        self.open("#demenager")
        for sel in ("#mv-qr", "#mv-pass"):
            bg = self.page.eval_on_selector(sel, "e => getComputedStyle(e).backgroundColor")
            self.assertNotEqual(bg, "rgb(255, 255, 255)", sel)                          # plus de champ blanc sur fond sombre

    def test_billet_flow_is_still_there(self):
        self.open()
        for sel in ("#card-identify", "#btn-scan-billet", "#input-p1", "#input-p2", "#btn-redeem"):
            self.assertTrue(self.page.query_selector(sel) is not None, sel)

    # ── formulaire ─────────────────────────────────────────────────────────────
    def test_validation_of_qr_p1_and_pass(self):
        self.open("#demenager")
        self.assertTrue(self.page.is_disabled("#mv-start"))
        self.page.fill("#mv-qr", "n'importe quoi")
        self.assertIn("✗", self.page.inner_text("#mv-qr-state"))
        self.assertTrue(self.page.is_disabled("#mv-start"))
        self.page.fill("#mv-qr", self.qr)
        self.assertIn("✓ QR P1 reconnu", self.page.inner_text("#mv-qr-state"))
        self.assertTrue(self.page.is_disabled("#mv-start"))                          # PASS manquant
        for bad in ("12", "abcd", "12 4"):
            self.page.fill("#mv-pass", bad)
            self.assertTrue(self.page.is_disabled("#mv-start"), bad)
        self.page.fill("#mv-pass", PASS)
        self.assertFalse(self.page.is_disabled("#mv-start"))
        self.page.click("summary")                                                    # « Options avancées » (repliées)
        self.page.fill("#mv-from", "pas-un-hex")                                     # option avancée invalide
        self.assertTrue(self.page.is_disabled("#mv-start"))
        self.page.fill("#mv-from", "ab" * 32)
        self.assertFalse(self.page.is_disabled("#mv-start"))

    def test_qr_and_pass_fields_are_masked(self):
        self.open("#demenager")
        self.assertEqual(self.page.get_attribute("#mv-qr", "type"), "password")
        self.assertEqual(self.page.get_attribute("#mv-pass", "type"), "password")
        self.assertEqual(self.page.get_attribute("#mv-pass", "autocomplete"), "off")

    # ── envoi et suivi d'avancement ────────────────────────────────────────────
    def test_full_progress_reported_on_screen_then_done(self):
        self.queue = [
            status("requested", 5, 1, active=1, msg="En attente de l'ancienne station…"),
            status("imported", 40, 3, active=3, msg="Vos données sont arrivées…"),
            status("claimed", 55, 4, active=4, pin={"total": 100, "pinned": 10, "already": 0, "failed": 0},
                   msg="Votre compte est installé ici. Rapatriement de 100 documents en cours."),
            status("claimed", 80, 4, active=4, pin={"total": 100, "pinned": 60, "already": 0, "failed": 0},
                   msg="Rapatriement…"),
            status("done", 100, 5, pin={"total": 100, "pinned": 100, "already": 0, "failed": 0}, msg="Terminé : …",
                   notified={"player": "ok", "captain": "ok"}),
        ]
        self.queue[-1]["steps"][-1]["status"] = "done"
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        # le formulaire a disparu, QR et PASS ont été vidés de la page
        self.assertFalse(self.page.is_visible("#mv-form"))
        self.assertEqual(self.page.input_value("#mv-qr"), "")
        self.assertEqual(self.page.input_value("#mv-pass"), "")
        self.assertEqual(self.posts[0], ("start", {"qr": self.qr, "pass": PASS}))
        self.assertEqual(self.page.inner_text("#mv-sub"), EMAIL)
        # 1) demande envoyée
        self.tick(100)
        self.assertEqual(self.steps(), ["done", "active", "pending", "pending", "pending", "pending"])
        self.assertEqual(self.page.inner_text("#mv-pct"), "5 %")
        self.assertIn("ancienne station", self.page.inner_text("#mv-msg"))
        # 2) données reçues
        self.tick()
        self.assertEqual(self.steps()[:4], ["done", "done", "done", "active"])
        self.assertEqual(self.page.inner_text("#mv-pct"), "40 %")
        # 3) compte installé, documents en cours : progression chiffrée
        self.tick()
        self.assertEqual(self.steps()[4], "active")
        self.assertIn("10 / 100", self.page.inner_text("#mv-docs"))
        self.assertEqual(self.page.get_attribute("#mv-bar", "aria-valuenow"), "55")
        self.tick()
        self.assertIn("60 / 100", self.page.inner_text("#mv-docs"))
        self.assertGreater(float(self.page.eval_on_selector("#mv-bar-fill", "e => parseFloat(e.style.width)")), 70)
        # 4) terminé
        self.tick()
        self.assertEqual(self.steps(), ["done"] * 6)
        self.assertEqual(self.page.inner_text("#mv-pct"), "100 %")
        self.assertIn("terminé", self.page.inner_text("#mv-title").lower())
        self.assertIn("e-mail de confirmation vous a été envoyé", self.page.inner_text("#mv-msg"))
        self.assertTrue(self.page.is_visible("#mv-close"))
        self.assertFalse(self.page.is_visible("#mv-abort"))
        self.assertFalse(self.page.is_visible("#mv-retry"))
        n = len([1 for _ in self.queue])
        self.tick(20000)                                                              # plus aucune requête une fois fini
        self.assertEqual(self.errors, [])

    def test_nothing_secret_is_kept_in_the_browser_storage(self):
        self.queue = [status("requested", 5, 1, active=1)]
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        stored = self.page.evaluate("JSON.stringify([sessionStorage, localStorage])")
        self.assertNotIn(PASS, stored)
        self.assertNotIn(self.qr, stored)
        self.assertNotIn(self.qr[:30], stored)
        self.assertIn("T" * 22, stored)                                               # seul le ticket de suivi est gardé
        html = self.page.content()
        self.assertNotIn(self.qr, html)

    def test_start_error_is_shown_and_the_form_stays(self):
        self.start_reply = (409, {"code": "request_failed", "error": "PASS refusé : Code PASS incorrect (2 essais restants)."})
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_function("document.getElementById('mv-start-state').textContent.includes('✗')")
        self.assertIn("2 essais restants", self.page.inner_text("#mv-start-state"))
        self.assertTrue(self.page.is_visible("#mv-form"))
        self.assertEqual(self.page.input_value("#mv-pass"), "")                       # le PASS erroné n'est pas conservé
        self.assertTrue(self.page.is_disabled("#mv-start"))                           # il faut ressaisir

    # ── échec, relance, annulation, reprise ───────────────────────────────────
    def test_pin_error_offers_retry_and_posts_the_ticket(self):
        self.queue = [status("pin_error", 60, 4, error=4, pin={"total": 9, "pinned": 7, "already": 0, "failed": 2},
                             msg="Votre compte est installé, mais certains documents n'ont pas pu être rapatriés.")]
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        self.tick(100)
        self.assertEqual(self.steps()[4], "error")
        self.assertTrue(self.page.is_visible("#mv-retry"))
        self.assertIn("2 à rapatrier", self.page.inner_text("#mv-docs"))
        self.assertIn("mv-msg bad", self.page.get_attribute("#mv-msg", "class"))
        self.page.click("#mv-retry")
        self.page.wait_for_timeout(100)
        self.assertIn(("pin", {"email": EMAIL, "ticket": "T" * 22}), self.posts)

    def test_error_state_shows_the_reason_and_allows_abort_after_confirmation(self):
        self.queue = [status("error", 0, 2, error=2, errors=["import incomplet (state_files : 9 annoncés, 0 appliqués)"],
                             msg="Le déménagement a échoué avant tout changement chez l'ancienne station.")]
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        self.tick(100)
        self.assertIn("import incomplet", self.page.inner_text("#mv-msg"))
        self.assertTrue(self.page.is_visible("#mv-abort"))
        self.page.once("dialog", lambda d: d.accept())
        self.page.click("#mv-abort")
        self.page.wait_for_selector("#mv-form:not(.hidden)")
        self.assertIn(("abort", {"email": EMAIL, "ticket": "T" * 22}), self.posts)
        self.assertIn("annulée", self.page.inner_text("#mv-start-state"))

    def test_abort_is_not_sent_when_the_person_cancels_the_confirmation(self):
        self.queue = [status("requested", 5, 1, active=1)]
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        self.tick(100)
        self.page.once("dialog", lambda d: d.dismiss())
        self.page.click("#mv-abort")
        self.page.wait_for_timeout(100)
        self.assertFalse(any(k == "abort" for k, _ in self.posts))
        self.assertTrue(self.page.is_visible("#mv-progress"))

    def test_progress_resumes_after_a_reload_and_reopens_the_tab(self):
        self.queue = [status("claimed", 55, 4, active=4, pin={"total": 100, "pinned": 30, "already": 0, "failed": 0})]
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        self.page.reload()                                                             # même onglet, sessionStorage conservé
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        self.assertTrue(self.page.is_visible("#panel-move"))                           # l'onglet se rouvre tout seul
        self.page.clock.run_for(100)
        self.assertIn("30 / 100", self.page.inner_text("#mv-docs"))

    def test_a_vanished_move_sends_back_to_the_form_with_a_note(self):
        self.queue = [status("requested", 5, 1, active=1)]
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        self.status_code = 404
        self.tick()
        self.page.wait_for_selector("#mv-form:not(.hidden)")
        self.assertIn("n’existe plus", self.page.inner_text("#mv-start-state"))

    def test_lost_connection_is_reported_and_polling_continues(self):
        self.queue = [status("imported", 40, 3, active=3)]
        self.open("#demenager")
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        self.page.unroute("**/api/migration/status*")
        self.page.route("**/api/migration/status*", lambda r: r.abort())
        self.tick()
        self.assertIn("Connexion perdue", self.page.inner_text("#mv-msg"))
        self.page.unroute("**/api/migration/status*")
        self.page.route("**/api/migration/status*", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(status("claimed", 60, 4, active=4, msg="De retour"))))
        self.tick(5200)
        self.assertEqual(self.page.inner_text("#mv-msg"), "De retour")

    # ── mobile ─────────────────────────────────────────────────────────────────
    def test_no_horizontal_scroll_on_a_phone(self):
        self.page.set_viewport_size({"width": 360, "height": 780})
        self.queue = [status("claimed", 55, 4, active=4, pin={"total": 1234, "pinned": 600, "already": 0, "failed": 3},
                             msg="Votre compte est installé ici. Rapatriement de 1234 documents en cours — vous pouvez déjà vous servir de votre compte.")]
        self.open("#demenager")
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 360)
        self.fill_valid()
        self.page.click("#mv-start")
        self.page.wait_for_selector("#mv-progress:not(.hidden)")
        self.tick(100)
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 360)


if __name__ == "__main__":
    unittest.main()
