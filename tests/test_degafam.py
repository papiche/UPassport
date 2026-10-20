"""Série « se dégafamiser » : décisions d'envoi d'après l'état réel, calendrier, opt-out, contenu des messages."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import degafam as dg  # noqa: E402

EMAIL = "alice.martin@exemple.org"
DAY = 86400


def state(photos=0, contacts=False, agenda=False, amis=False, enrolled=False):
    return {"dav_enrolled": enrolled,
            "photos": {"done": photos >= dg.PHOTOS_DONE, "count": photos, "started": photos > 0},
            "contacts": {"done": contacts, "count": int(contacts), "started": enrolled},
            "agenda": {"done": agenda, "count": int(agenda), "started": enrolled},
            "amis": {"done": amis, "count": 0, "started": False}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "nostr" / EMAIL).mkdir(parents=True)
        (self.root / "nostr" / EMAIL / "HEX").write_text("a" * 64)
        self.udir = self.root / "udir"
        self.udir.mkdir()
        for target, value in (("_nostr", lambda e: self.root / "nostr" / e), ("_udir", lambda e: self.udir)):
            p = mock.patch.object(dg, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.set_age(3 * DAY)

    def set_age(self, seconds):
        import os
        t = time.time() - seconds
        os.utime(self.root / "nostr" / EMAIL / "HEX", (t, t))

    def prefs(self, data):
        (self.root / "nostr" / EMAIL / ".mailjet").write_text(json.dumps(data))


class TestDecide(Base):
    def test_premiere_etape_non_faite(self):
        self.assertEqual(dg.decide(EMAIL, st=state())[0], "photos")

    def test_jamais_ce_qui_est_deja_fait(self):
        self.assertEqual(dg.decide(EMAIL, st=state(photos=50))[0], "contacts")
        self.assertEqual(dg.decide(EMAIL, st=state(photos=50, contacts=True))[0], "agenda")
        self.assertEqual(dg.decide(EMAIL, st=state(photos=50, contacts=True, agenda=True))[0], "amis")

    def test_tout_fait_on_passe_aux_decouvertes(self):
        step, _why = dg.decide(EMAIL, st=state(photos=50, contacts=True, agenda=True, amis=True))
        self.assertEqual(step, "zelkova")

    def test_un_message_par_etape_puis_on_passe(self):
        dg._save_progress(EMAIL, {"sent": {"photos": int(time.time()) - 5 * DAY}, "last": int(time.time()) - 5 * DAY, "count": 1})
        self.assertEqual(dg.decide(EMAIL, st=state())[0], "contacts")        # photos déjà proposée : on ne relance pas

    def test_espacement_minimal(self):
        dg._save_progress(EMAIL, {"sent": {"photos": 1}, "last": int(time.time()) - DAY, "count": 1})
        step, why = dg.decide(EMAIL, st=state())
        self.assertIsNone(step)
        self.assertIn("moins de", why)

    def test_plafond_de_messages(self):
        dg._save_progress(EMAIL, {"sent": {}, "last": int(time.time()) - 30 * DAY, "count": dg.MAX_MAILS})
        self.assertIsNone(dg.decide(EMAIL, st=state())[0])

    def test_compte_trop_recent_le_pass_vient_d_arriver(self):
        self.set_age(3 * 3600)
        step, why = dg.decide(EMAIL, st=state())
        self.assertIsNone(step)
        self.assertIn("trop récent", why)

    def test_anciens_comptes_pas_inondes(self):
        self.set_age((dg.MAX_AGE_DAYS + 1) * DAY)
        self.assertIsNone(dg.decide(EMAIL, st=state())[0])

    def test_desabonnements_respectes(self):
        for prefs in ({"channels": ["email"]}, {"channels": ["all"]}, {"flux": {"onboarding": False}},
                      {"flux_channels": {"onboarding": {"email": False}}}):
            self.prefs(prefs)
            self.assertIsNone(dg.decide(EMAIL, st=state())[0], prefs)
        self.prefs({"flux": {"zine": False}})                       # un AUTRE flux coupé ne nous concerne pas
        self.assertEqual(dg.decide(EMAIL, st=state())[0], "photos")


class TestRun(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(dg, "state", lambda e: state())
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        s = mock.patch.object(dg, "_send", lambda e, subj, body: self.sent.append((e, subj, body)) or True)
        s.start()
        self.addCleanup(s.stop)

    def test_envoi_puis_silence_jusqu_au_prochain_creneau(self):
        if True:
            r = dg.run(EMAIL)
            self.assertEqual((r["status"], r["step"]), ("sent", "photos"))
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(dg.run(EMAIL)["status"], "skip")           # le jour même : rien de plus
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(dg.load_progress(EMAIL)["count"], 1)

    def test_echec_d_envoi_ne_compte_pas(self):
        with mock.patch.object(dg, "_send", lambda *a: False):
            self.assertEqual(dg.run(EMAIL)["status"], "error")
        self.assertEqual(dg.load_progress(EMAIL)["count"], 0)           # repris demain

    def test_dry_run_n_ecrit_rien(self):
        if True:
            self.assertEqual(dg.run(EMAIL, dry_run=True)["status"], "dry-run")
        self.assertEqual(self.sent, [])
        self.assertFalse((self.udir / "degafam.json").exists())

    def test_apres_les_etapes_zelkova_puis_coracle_et_rien_d_autre(self):
        done = state(photos=99, contacts=True, agenda=True, amis=True)
        self.assertEqual(dg.decide(EMAIL, st=done)[0], "zelkova")
        dg._save_progress(EMAIL, {"sent": {"zelkova": 1}, "last": int(time.time()) - 5 * DAY, "count": 5})
        self.assertEqual(dg.decide(EMAIL, st=done)[0], "coracle")
        dg._save_progress(EMAIL, {"sent": {"zelkova": 1, "coracle": 1}, "last": int(time.time()) - 5 * DAY, "count": 6})
        self.assertIsNone(dg.decide(EMAIL, st=done)[0])
        self.assertEqual(dg.ALL_STEPS, ("photos", "contacts", "agenda", "amis", "zelkova", "coracle"))

    def test_serie_terminee_quand_tout_est_fait(self):
        with mock.patch.object(dg, "state", lambda e: state(photos=99, contacts=True, agenda=True, amis=True)):
            dg._save_progress(EMAIL, {"sent": {"zelkova": 1, "coracle": 1}, "last": 1, "count": 6})
            dg.run(EMAIL)
            self.assertTrue(dg.load_progress(EMAIL).get("finished"))


class TestMessages(Base):
    def test_contenu_et_chiffres_reels(self):
        st = state(photos=134, contacts=True)
        for step in dg.ALL_STEPS:
            subject, body = dg.compose(step, EMAIL, st)
            self.assertTrue(subject)
            self.assertIn({"amis": "ucloud.html", "zelkova": "z.astroport.one", "coracle": "coracle"}.get(step, "degafam.html"), body)
            self.assertIn("Bonjour Alice", body)
            self.assertIn("moncompte.html#notifications", body, "chaque message permet de se désabonner")
        self.assertIn("134", dg.compose("contacts", EMAIL, st)[1])

    def test_jamais_d_identifiant_ni_de_jeton_dans_un_email(self):
        for step in dg.ALL_STEPS:
            body = dg.compose(step, EMAIL, state())[1].lower()
            self.assertNotIn("dav_token", body)
            self.assertNotIn("mot de passe :", body)

    def test_prenom_absurde_ignore(self):
        self.assertEqual(dg._first_name("xk42@x.fr"), "")
        self.assertEqual(dg._first_name("jean-pierre@x.fr"), "Jean")

    def test_pas_un_mot_sur_les_applications_pas_pretes(self):
        for step in dg.ALL_STEPS:
            body = dg.compose(step, EMAIL, state())[1].lower()
            for banned in ("wotx", "troczen", "nostr tube", "minelife", "nextcloud"):
                self.assertNotIn(banned, body, (step, banned))

    def test_html_echappe(self):
        out = dg.layout("<script>x</script>", "ok", ["a"], "Go", "https://x/?a=1&b=2", "ps")
        self.assertNotIn("<script>x", out)
        self.assertIn("a=1&amp;b=2", out)


if __name__ == "__main__":
    unittest.main()
