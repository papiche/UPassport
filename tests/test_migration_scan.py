"""
tests/test_migration_scan.py — onglet « Déménager » de /scan : QR P1 + PASS (services/migration_api.py +
routers/migrate.py). Roaming, CLI et sous-processus SIMULÉS ; GAME_NOSTR_PATH redirigé vers un dossier temporaire.

    pytest tests/test_migration_scan.py -v
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from routers import migrate
from services import cloud_storage as cs
from services import migration, migration_api as api

EMAIL = "move@example.com"
HEAD = "1-" + "ab" * 64                       # part P1 « 1-<hex> »
NS = "k51qzi5uqu5d" + "a" * 50                 # NOSTRNS
PASS = "4242"
_TMP = Path(tempfile.mkdtemp(prefix="upassport-scan-test-"))
cs.GAME_NOSTR_PATH = _TMP / "nostr"


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def make_qr(head=HEAD, ns=NS) -> str:
    """« M-… » exactement comme make_NOSTRCARD.sh : Mbase58.py encode '<part>:<NOSTRNS>'."""
    out = subprocess.run([sys.executable, str(settings.TOOLS_PATH / "Mbase58.py"), "encode", f"{head}:{ns}"],
                         capture_output=True, text=True, timeout=20).stdout.strip()
    return "M-" + out


class ScanCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(cs.GAME_NOSTR_PATH, ignore_errors=True)
        (cs.GAME_NOSTR_PATH / EMAIL).mkdir(parents=True)
        api._by_ip.clear()
        api._by_email.clear()
        self.hydrate_calls, self.start_calls = [], []
        self._saved = (api._hydrate, api.start)
        api._hydrate = lambda head, ns: self.hydrate_calls.append((head, ns)) or {"valid": True, "email": EMAIL, "hex": "ab" * 32}
        api.start = lambda email, from_node=None, pass_code="": self.start_calls.append((email, from_node, pass_code)) or {"ok": True, "result": {}}

    def tearDown(self):
        api._hydrate, api.start = self._saved


class TestDecode(ScanCase):
    def test_roundtrip_with_the_real_encoder(self):
        self.assertEqual(api.decode_qr_p1(make_qr()), (HEAD, NS))

    def test_rejects_anything_that_is_not_a_multipass_p1(self):
        qr = make_qr()
        for bad in ("", "pas un qr", "M-", "M-abc", "X-" + qr[2:], qr[:60], qr + "0OIl", "M-" + "1" * 99,
                    "M-" + "z" * 401, qr.replace("M-", "m-", 1), " ; rm -rf / ", "../../etc/passwd"):
            with self.assertRaises(api.QrError, msg=bad[:20]):
                api.decode_qr_p1(bad)

    def test_tampered_qr_never_yields_a_valid_share(self):
        qr = make_qr()
        mutated = qr[:30] + ("2" if qr[30] != "2" else "3") + qr[31:]
        try:
            head, ns = api.decode_qr_p1(mutated)
        except api.QrError:
            return
        self.assertNotEqual((head, ns), (HEAD, NS))            # lu, mais jamais pris pour l'original


class TestStartFromQr(ScanCase):
    def test_success_returns_a_ticket_and_stores_only_its_hash(self):
        res = api.start_from_qr(make_qr(), PASS, None, "1.2.3.4")
        self.assertTrue(res["ok"])
        self.assertEqual((res["email"], len(res["ticket"]) >= 20), (EMAIL, True))
        self.assertEqual(self.hydrate_calls, [(HEAD, NS)])
        self.assertEqual(self.start_calls, [(EMAIL, None, PASS)])
        stored = (cs.GAME_NOSTR_PATH / EMAIL / ".migration.ticket").read_text()
        self.assertEqual(stored, hashlib.sha256(res["ticket"].encode()).hexdigest())
        self.assertNotEqual(stored, res["ticket"])
        self.assertEqual(oct((cs.GAME_NOSTR_PATH / EMAIL / ".migration.ticket").stat().st_mode & 0o777), "0o600")

    def test_junk_is_refused_before_any_work_is_done(self):
        for i, pc in enumerate(("", "12", "abcd", "12345678901", "12 34", "1234; rm")):
            self.assertEqual(api.start_from_qr(make_qr(), pc, None, f"1.1.1.{i}")["code"], "bad_pass")
        self.assertEqual(api.start_from_qr("M-faux", PASS, None, "2.2.2.2")["code"], "bad_qr")
        self.assertEqual((self.hydrate_calls, self.start_calls), ([], []))

    def test_an_account_already_hosted_here_is_refused_before_hydration(self):
        (cs.GAME_NOSTR_PATH / EMAIL / "NOSTRNS").write_text("/ipns/" + NS)
        res = api.start_from_qr(make_qr(), PASS, None, "1.1.1.1")
        self.assertEqual(res["code"], "hosted_here")
        self.assertEqual(self.hydrate_calls, [])                                   # jamais d'écrasement de ses clés
        (cs.GAME_NOSTR_PATH / EMAIL / ".roaming").write_text("")                    # un visiteur, lui, peut déménager
        self.assertTrue(api.start_from_qr(make_qr(), PASS, None, "1.1.1.2")["ok"])

    def test_hydration_failures_are_explicit(self):
        api._hydrate = lambda h, n: {"valid": False, "reason": "INVALID (SSSS mismatch)"}
        res = api.start_from_qr(make_qr(), PASS, None, "1.1.1.1")
        self.assertEqual(res["code"], "hydrate_failed")
        self.assertEqual(self.start_calls, [])
        api._hydrate = lambda h, n: {"valid": False, "reason": "INVALID (account already hosted on this station)"}
        self.assertEqual(api.start_from_qr(make_qr(), PASS, None, "1.1.1.3")["code"], "hosted_here")

    def test_a_refused_request_is_reported_and_issues_no_ticket(self):
        api.start = lambda *a, **k: {"ok": False, "error": "PASS refusé : Code PASS incorrect (2 essais restants)."}
        res = api.start_from_qr(make_qr(), PASS, None, "1.1.1.1")
        self.assertEqual((res["code"], "PASS" in res["error"]), ("request_failed", True))
        self.assertFalse((cs.GAME_NOSTR_PATH / EMAIL / ".migration.ticket").exists())

    def test_per_ip_and_per_account_throttles(self):
        for i in range(api.IP_LIMIT):
            api._by_email.clear()
            self.assertTrue(api.start_from_qr(make_qr(), PASS, None, "9.9.9.9")["ok"])
        self.assertEqual(api.start_from_qr(make_qr(), PASS, None, "9.9.9.9")["code"], "throttled")
        self.assertTrue(api.start_from_qr(make_qr(), PASS, None, "8.8.8.8").get("ok") is False)   # même compte, <60 s
        self.assertEqual(api.start_from_qr(make_qr(), PASS, None, "8.8.8.8")["code"], "throttled")


class TestHydrateProcess(ScanCase):
    def test_the_share_goes_through_stdin_never_argv_and_noise_after_json_is_ignored(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            out = "Saving file(s) to x\n" + json.dumps({"valid": True, "email": EMAIL, "hex": "ab" * 32}) + "\nbruit tardif\n"
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        api._hydrate = self._saved[0]
        with mock.patch.object(api.subprocess, "run", fake_run):
            res = api._hydrate(HEAD, NS)
        self.assertTrue(res["valid"])
        cmd, kw = calls[0]
        self.assertEqual(cmd[-3:], ["-", "", "json"])                               # « - » = part lue sur stdin
        self.assertEqual(kw["input"], HEAD + "\n")
        self.assertNotIn(HEAD, " ".join(cmd))                                       # jamais visible dans ps

    def test_unreadable_answer_is_an_error_not_a_success(self):
        api._hydrate = self._saved[0]
        with mock.patch.object(api.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "pas du json", "stderr": ""})()):
            self.assertFalse(api._hydrate(HEAD, NS)["valid"])


class TestTicket(ScanCase):
    def test_status_and_abort_need_the_right_ticket(self):
        ticket = api.start_from_qr(make_qr(), PASS, None, "1.1.1.1")["ticket"]
        migration.save_state(EMAIL, {"role": "target", "state": "requested", "from_node": "ab" * 32, "at": 1})
        self.assertEqual(api.status_for_ticket(EMAIL, ticket)["state"], "requested")
        self.assertIsNone(api.status_for_ticket(EMAIL, "mauvais"))
        self.assertIsNone(api.status_for_ticket("autre@example.com", ticket))
        self.assertIsNone(api.status_for_ticket(EMAIL, ""))
        aborted = []
        real_abort = api.abort
        api.abort = lambda email: aborted.append(email) or {"ok": True, "status": {"state": "none"}}   # CLI simulée
        try:
            self.assertIsNone(api.abort_for_ticket(EMAIL, "mauvais"))
            self.assertEqual(aborted, [])                                           # jamais lancée avec un mauvais ticket
            self.assertEqual(api.abort_for_ticket(EMAIL, ticket)["ok"], True)
        finally:
            api.abort = real_abort
        self.assertFalse((cs.GAME_NOSTR_PATH / EMAIL / ".migration.ticket").exists())     # ticket révoqué avec l'annulation


class TestRoutes(ScanCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(migrate.router)
        self.client = TestClient(app)

    def test_start_202_with_ticket_and_status_follows(self):
        r = self.client.post("/api/migration/start", json={"qr": make_qr(), "pass": PASS})
        self.assertEqual(r.status_code, 202)
        j = r.json()
        self.assertEqual(set(j), {"email", "ticket", "status"})
        migration.save_state(EMAIL, {"role": "target", "state": "claimed", "from_node": "ab" * 32, "at": 1,
                                     "pin": {"total": 10, "pinned": 3, "already": 0, "failed": 0}})
        s = self.client.get("/api/migration/status", params={"email": EMAIL, "ticket": j["ticket"]})
        self.assertEqual((s.status_code, s.json()["pin"]["total"]), (200, 10))
        blob = json.dumps(s.json())
        for secret in ("sha256", PASS, j["ticket"], HEAD):
            self.assertNotIn(secret, blob)

    def test_error_codes_map_to_http_statuses(self):
        cases = [({"qr": "M-faux", "pass": PASS}, 422), ({"qr": make_qr(), "pass": "x"}, 422), ({}, 422)]
        for body, status in cases:
            self.assertEqual(self.client.post("/api/migration/start", json=body).status_code, status, body)
        api._hydrate = lambda h, n: {"valid": False, "reason": "KO"}
        self.assertEqual(self.client.post("/api/migration/start", json={"qr": make_qr(), "pass": PASS}).status_code, 404)

    def test_unknown_account_and_wrong_ticket_look_the_same_no_oracle(self):
        a = self.client.get("/api/migration/status", params={"email": "inconnu@example.com", "ticket": "x"})
        b = self.client.get("/api/migration/status", params={"email": EMAIL, "ticket": "x"})
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
        self.assertEqual(a.status_code, 404)

    def test_the_pass_and_the_share_are_never_logged(self):
        with self.assertLogs(level="DEBUG") as cm:
            import logging
            logging.getLogger("probe").debug("sonde")
            self.client.post("/api/migration/start", json={"qr": make_qr(), "pass": PASS})
        text = "\n".join(cm.output)
        self.assertNotIn(PASS + '"', text)
        self.assertNotIn(HEAD, text)
        self.assertNotIn("ab" * 40, text)


class TestStaticGuards(unittest.TestCase):
    def test_public_routes_are_registered_in_the_app(self):
        src = (settings.BASE_DIR / "54321.py").read_text()
        self.assertIn("app.include_router(migrate.router)", src)

    def test_the_signer_stays_out_of_the_server(self):
        for f in ("services/migration_api.py", "routers/migrate.py"):
            src = (settings.BASE_DIR / f).read_text()
            for bad in ("import ucloud_backup", "make_proof", ".secret.nostr"):
                self.assertNotIn(bad, src, f"{f} : {bad}")

    def test_no_secret_in_command_line_arguments(self):
        src = (settings.BASE_DIR / "services/migration_api.py").read_text()
        self.assertIn('input=head + "\\n"', src)
        self.assertIn("stdin=(pass_code", src)


if __name__ == "__main__":
    unittest.main()
