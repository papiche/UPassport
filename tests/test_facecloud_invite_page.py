"""Page d'invitation /i/<jeton> (aperçu de lien) et entonnoir viral de FaceCloud."""
import asyncio
import base64
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from routers import facecloud as fc  # noqa: E402
from services import facecloud_funnel as ff  # noqa: E402

TOKEN = "T" * 32


def _face_data_url(color):
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color).save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


class FunnelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(ff, "LOG_PATH", Path(self.tmp.name) / "funnel.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)
        g = mock.patch.object(ff.settings, "GAME_PATH", Path(self.tmp.name) / "game")
        g.start()
        self.addCleanup(g.stop)


class TestFunnel(FunnelCase):
    def test_le_journal_ne_contient_ni_jeton_ni_email(self):
        ff.record("created", TOKEN, mode="link", sender="fred@exemple.org")
        raw = ff.LOG_PATH.read_text()
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("fred", raw)

    def test_distinct_par_invitation_et_par_etape(self):
        for _ in range(3):
            ff.record("visit", TOKEN)                 # recharger la page ne compte qu'une fois
        ff.record("visit", "U" * 32)
        self.assertEqual(ff.stats()["counts"]["visit"], 2)

    def test_generation_du_compte_cree_par_invitation(self):
        (ff.settings.GAME_PATH / "nostr" / "alice@x.fr").mkdir(parents=True)
        self.assertEqual(ff.generation_of("alice@x.fr"), 0)
        self.assertEqual(ff.remember_origin("alice@x.fr", 0, "fred"), 1)
        self.assertEqual(ff.generation_of("alice@x.fr"), 1)
        self.assertEqual(ff.remember_origin("bob@x.fr", ff.generation_of("alice@x.fr"), "alice"), 2)

    def test_k_mesure(self):
        # Fred (gen 0) invite 2 personnes ; Alice réclame et invite à son tour 1 personne ; Bob réclame sans inviter.
        ff.record("created", "a" * 24, gen=0, mode="link", sender="fred@x.fr")
        ff.record("created", "b" * 24, gen=0, mode="link", sender="fred@x.fr")
        ff.record("claimed", "a" * 24, gen=1, mode="link", sender="fred@x.fr", account="alice@x.fr")
        ff.record("claimed", "b" * 24, gen=1, mode="link", sender="fred@x.fr", account="bob@x.fr")
        ff.record("created", "c" * 24, gen=1, mode="link", sender="alice@x.fr")
        ff.record("claimed", "c" * 24, gen=2, mode="link", sender="alice@x.fr", account="carl@x.fr")
        v = ff.stats()["viral"]
        self.assertEqual(v["new_accounts"], 3)
        self.assertEqual(v["became_senders"], 1)                  # Alice seulement
        self.assertEqual(v["share_rate"], round(1 / 3, 3))
        self.assertEqual(v["invites_per_sender"], 1.0)
        self.assertEqual(ff.stats()["claimed_by_generation"], {"1": 2, "2": 1})

    def test_sans_donnee_pas_de_division_par_zero(self):
        s = ff.stats()
        self.assertIsNone(s["viral"]["k"])
        self.assertEqual(s["counts"]["claimed"], 0)

    def test_robots_reconnus(self):
        self.assertTrue(ff.is_bot("WhatsApp/2.23.20.0 A"))
        self.assertTrue(ff.is_bot("TelegramBot (like TwitterBot)"))
        self.assertTrue(ff.is_bot(""))
        self.assertFalse(ff.is_bot("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15 Safari/604.1"))


class TestInvitePage(FunnelCase):
    def _page(self, loaded, ua="WhatsApp/2.23"):
        request = mock.Mock()
        request.headers = {"user-agent": ua}

        async def load(_t):
            return loaded
        with mock.patch.object(fc, "_load_invite", load):
            return asyncio.run(fc.invitation_page(TOKEN, request))

    def _loaded(self, n=23):
        rec = {"owner_email": "fred@exemple.org", "from_name": "Fred", "items": [{}] * n, "open": True, "gen": 1}
        return {"local": None, "record": rec}

    def test_apercu_personnalise_pour_les_robots(self):
        r = self._page(self._loaded())
        body = r.body.decode()
        self.assertIn('property="og:title" content="Fred vous a reconnu·e sur 23 photos"', body)
        self.assertIn(f"/i/{TOKEN}/og.jpg", body)
        self.assertIn('content="summary_large_image"', body)
        self.assertEqual(r.headers["x-robots-tag"], "noindex, nofollow")
        self.assertEqual(ff.stats()["counts"]["preview"], 1)

    def test_un_humain_est_compte_et_renvoye_vers_l_accueil(self):
        r = self._page(self._loaded(), ua="Mozilla/5.0 (X11; Linux) Firefox/130.0")
        body = r.body.decode()
        self.assertIn("location.replace(", body)
        self.assertIn(f"/earth/ucloud.html#demarrer&claim={TOKEN}", body)
        self.assertEqual(ff.stats()["counts"]["visit"], 1)

    def test_invitation_inconnue_donne_un_apercu_generique_sans_fuite(self):
        body = self._page(None).body.decode()
        self.assertIn("Des photos de vous vous attendent", body)
        self.assertNotIn("Fred", body)

    def test_le_nom_est_echappe(self):
        loaded = self._loaded()
        loaded["record"]["from_name"] = '"><script>alert(1)</script>'
        body = self._page(loaded).body.decode()
        self.assertNotIn("<script>alert(1)", body)

    def test_lire_la_page_ne_reclame_pas_l_invitation(self):
        called = []
        with mock.patch.object(ff, "remember_origin", lambda *a, **k: called.append(a)):
            self._page(self._loaded())
        self.assertEqual(called, [])


class TestOgImage(unittest.TestCase):
    def test_mosaique_1200x630_et_floutee(self):
        # Un visage « lisible » : damier très contrasté. Après traitement, plus aucun contraste fin.
        buf = io.BytesIO()
        img = Image.new("RGB", (200, 200))
        px = img.load()
        for x in range(200):
            for y in range(200):
                px[x, y] = (255, 255, 255) if (x // 4 + y // 4) % 2 else (0, 0, 0)
        img.save(buf, "JPEG", quality=95)
        url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        out = Image.open(io.BytesIO(fc._og_image([url] * 4)))
        self.assertEqual(out.size, (1200, 630))
        x0, y0 = (1200 - (4 * 250 + 3 * 40)) // 2, (630 - 250) // 2 - 14       # 1re pastille, légèrement relevée
        tile = out.crop((x0 + 40, y0 + 40, x0 + 210, y0 + 210)).convert("L")      # sans le cadre blanc ni les coins
        lo, hi = tile.getextrema()
        self.assertLess(hi - lo, 40, "le damier doit être lissé en gris : aucun détail ne subsiste")

    def test_sans_apercu_image_generique(self):
        out = Image.open(io.BytesIO(fc._og_image([])))
        self.assertEqual(out.size, (1200, 630))

    def test_apercu_corrompu_ignore(self):
        out = Image.open(io.BytesIO(fc._og_image(["data:image/jpeg;base64,@@@", "nimporte quoi"])))
        self.assertEqual(out.size, (1200, 630))


if __name__ == "__main__":
    unittest.main()
