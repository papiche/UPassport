"""
tests/test_account_migration_api.py — point d'entrée utilisateur du déménagement (routers/account.py +
services/migration_api.py). Sous-processus et IPFS SIMULÉS ; GAME_NOSTR_PATH redirigé vers un dossier temporaire.

    pytest tests/test_account_migration_api.py -v
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import account
from services import cloud_storage as cs
from services import migration, migration_api

EMAIL = "move@example.com"
_TMP = Path(tempfile.mkdtemp(prefix="upassport-migapi-test-"))
cs.GAME_NOSTR_PATH = _TMP / "nostr"


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class ApiCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(cs.GAME_NOSTR_PATH, ignore_errors=True)
        (cs.GAME_NOSTR_PATH / EMAIL).mkdir(parents=True)
        app = FastAPI()
        app.include_router(account.router)
        app.dependency_overrides[account.require_nostr_auth] = lambda: "npub-test"
        self.client = TestClient(app)
        self._email = mock.patch.object(account, "_email_for_authenticated_npub", return_value=EMAIL)
        self._email.start()
        self.cli_calls, self.cli_stdin = [], []

    def tearDown(self):
        self._email.stop()

    def cli(self, rc=0, out="", err=""):
        def fake(args, timeout=150, stdin=None):
            self.cli_calls.append(args)
            self.cli_stdin.append(stdin)
            return rc, out, err
        return mock.patch.object(migration_api, "_cli", fake)

    def set_state(self, **st):
        migration.save_state(EMAIL, st)


class TestStatus(ApiCase):
    def test_no_migration(self):
        r = self.client.get("/api/account/migration")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "none")

    def test_state_is_readable_and_never_leaks_package_material(self):
        self.set_state(role="target", state="claimed", from_node="ab" * 32, at=1, key="SECRET-KEY", cid="QmSecretBundle",
                       value="/ipfs/QmOld", pin={"total": 10, "pinned": 4, "already": 2, "failed": 0})
        j = self.client.get("/api/account/migration").json()
        self.assertEqual((j["state"], j["frozen"], j["pin"]["total"]), ("claimed", False, 10))
        self.assertEqual(j["from_node"], "ab" * 6)
        blob = json.dumps(j)
        for secret in ("SECRET-KEY", "QmSecretBundle", "/ipfs/QmOld"):
            self.assertNotIn(secret, blob)

    def test_frozen_matches_the_shell_helper(self):
        for state, frozen in (("requested", True), ("imported", True), ("claiming", True), ("error", True),
                              ("claimed", False), ("done", False), ("pin_error", False)):
            self.set_state(role="target", state=state)
            self.assertEqual(self.client.get("/api/account/migration").json()["frozen"], frozen, state)

    def test_source_side_state_is_not_exposed_as_a_target_migration(self):
        self.set_state(role="source", state="committed", key="SECRET")
        self.assertEqual(self.client.get("/api/account/migration").json()["state"], "none")


class TestStart(ApiCase):
    def test_start_sends_the_request_and_answers_202(self):
        with self.cli(0, json.dumps({"status": "requested", "from_node": "ab" * 32})):
            r = self.client.post("/api/account/migration", json={"pass": "4242"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["result"]["status"], "requested")
        self.assertEqual(self.cli_calls, [["--request", EMAIL, "--pass-stdin"]])
        self.assertEqual(self.cli_stdin, ["4242\n"])                          # le PASS passe par l'entrée standard
        self.assertNotIn("4242", " ".join(self.cli_calls[0]))                 # jamais en argument (visible dans ps)

    def test_from_node_is_forwarded_lowercased_and_validated(self):
        with self.cli(0, json.dumps({"status": "requested"})):
            self.client.post("/api/account/migration", json={"from_node": "AB" * 32, "pass": "4242"})
        self.assertEqual(self.cli_calls[-1], ["--request", EMAIL, "--pass-stdin", "--from-node", "ab" * 32])
        with self.cli(0, "{}"):
            r = self.client.post("/api/account/migration", json={"from_node": "pas-un-hex"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("invalide", r.json()["detail"])
        self.assertEqual(len(self.cli_calls), 1)                      # jamais lancé avec une valeur invalide

    def test_cli_error_becomes_a_readable_409(self):
        err = "Traceback…\nERROR:migration:station d'origine introuvable dans le swarm — la passer en argument\n"
        with self.cli(1, "", err):
            r = self.client.post("/api/account/migration", json={"pass": "4242"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"], "station d'origine introuvable dans le swarm — la passer en argument")

    def test_refuses_when_a_migration_is_already_running(self):
        self.set_state(role="target", state="imported")
        with self.cli(0, "{}"):
            r = self.client.post("/api/account/migration")
        self.assertEqual(r.status_code, 409)
        self.assertIn("déjà en cours", r.json()["detail"])
        self.assertEqual(self.cli_calls, [])


class TestAbortAndPin(ApiCase):
    def test_abort(self):
        with self.cli(0, json.dumps({"status": "aborted"})):
            r = self.client.delete("/api/account/migration")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cli_calls, [["--abort", EMAIL]])

    def test_abort_error_is_a_409_with_the_reason(self):
        with self.cli(1, "", "ERROR:migration:rien à annuler (état « target:done »)"):
            r = self.client.delete("/api/account/migration")
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "rien à annuler (état « target:done »)"))

    def test_pin_resume_only_when_pin_is_pending_and_runs_detached(self):
        self.set_state(role="target", state="imported")
        self.assertEqual(self.client.post("/api/account/migration/pin").status_code, 409)
        self.set_state(role="target", state="pin_error")
        with mock.patch.object(migration_api.subprocess, "Popen") as popen, \
                mock.patch.object(migration_api, "LOG_DIR", _TMP / "log"):
            r = self.client.post("/api/account/migration/pin")
        self.assertEqual(r.status_code, 202)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][-2:], ["--pin", EMAIL])
        self.assertTrue(kwargs["start_new_session"])                   # processus autonome, jamais sous le serveur


class TestProgressReport(ApiCase):
    """Ce que l'interface affiche : étapes ordonnées, pourcentage, message en français, boutons possibles."""

    def report(self, **st):
        self.set_state(role="target", from_node="ab" * 32, **st)
        return self.client.get("/api/account/migration").json()

    def statuses(self, j):
        return [s["status"] for s in j["steps"]]

    def test_each_state_has_the_expected_steps(self):
        expected = {
            "requested": ["done", "active", "pending", "pending", "pending", "pending"],
            "imported":  ["done", "done", "done", "active", "pending", "pending"],
            "claiming":  ["done", "done", "done", "active", "pending", "pending"],
            "claimed":   ["done", "done", "done", "done", "active", "pending"],
            "pin_error": ["done", "done", "done", "done", "error", "pending"],
            "error":     ["done", "done", "error", "pending", "pending", "pending"],
        }
        for state, steps in expected.items():
            self.assertEqual(self.statuses(self.report(state=state)), steps, state)
        done = self.report(state="done", notified_arrival={"player": "ok", "captain": "ok"})
        self.assertEqual(self.statuses(done), ["done"] * 6)

    def test_steps_keep_the_protocol_order_and_french_labels(self):
        j = self.report(state="requested")
        self.assertEqual([s["id"] for s in j["steps"]],
                         ["recognized", "accepted", "received", "published", "documents", "notified"])
        self.assertTrue(all(s["label"] for s in j["steps"]))

    def test_percent_never_goes_backwards_and_follows_the_document_pinning(self):
        marks = [self.report(state=s)["percent"] for s in ("requested", "imported", "claiming")]
        self.assertEqual(marks, sorted(marks))
        low = self.report(state="claimed", pin={"total": 100, "pinned": 10, "already": 0, "failed": 0})["percent"]
        mid = self.report(state="claimed", pin={"total": 100, "pinned": 40, "already": 10, "failed": 0})["percent"]
        end = self.report(state="claimed", pin={"total": 100, "pinned": 100, "already": 0, "failed": 0})["percent"]
        self.assertTrue(50 < low < mid < end < 100)
        self.assertEqual(self.report(state="done")["percent"], 100)

    def test_a_delivered_move_with_a_failed_email_is_flagged_not_hidden(self):
        j = self.report(state="done", notified_arrival={"player": "échec : mailjet.sh code 1", "captain": "ok"})
        self.assertEqual(j["steps"][-1]["status"], "warn")
        self.assertEqual(j["notified"], {"player": "échec", "captain": "ok"})
        self.assertTrue(j["finished"])

    def test_messages_and_actions(self):
        self.assertIn("ancienne station", self.report(state="requested")["message"])
        self.assertTrue(self.report(state="requested")["can_abort"])
        j = self.report(state="claimed", pin={"total": 120, "pinned": 5, "already": 0, "failed": 0})
        self.assertIn("120 documents", j["message"])
        self.assertTrue(j["can_retry_pin"])
        self.assertFalse(j["can_abort"])
        j = self.report(state="pin_error", pin={"total": 9, "pinned": 7, "already": 0, "failed": 2})
        self.assertTrue(j["failed"] and j["can_retry_pin"])
        j = self.report(state="error", errors=["import incomplet (state_files : 9 annoncés, 0 appliqués)"])
        self.assertIn("import incomplet", j["message"])
        self.assertTrue(j["failed"] and j["can_abort"])
        j = self.report(state="done")
        self.assertTrue(j["finished"] and not j["can_abort"] and not j["can_retry_pin"])

    def test_error_details_are_bounded_and_never_expose_package_material(self):
        j = self.report(state="error", errors=["x" * 900], key="SECRET", cid="QmSecret", value="/ipfs/QmV")
        self.assertLessEqual(len(j["errors"][0]), 300)
        blob = json.dumps(j)
        for secret in ("SECRET", "QmSecret", "/ipfs/QmV"):
            self.assertNotIn(secret, blob)


class TestSecurity(unittest.TestCase):
    def test_every_migration_route_requires_nip98(self):
        paths = {("/api/account/migration", "GET"), ("/api/account/migration", "POST"),
                 ("/api/account/migration", "DELETE"), ("/api/account/migration/pin", "POST")}
        seen = set()
        for route in account.router.routes:
            for method in route.methods:
                if (route.path, method) in paths:
                    deps = {d.call for d in route.dependant.dependencies}
                    self.assertIn(account.require_nostr_auth, deps, f"{method} {route.path} sans NIP-98")
                    seen.add((route.path, method))
        self.assertEqual(seen, paths)

    def test_the_server_never_imports_the_signer(self):
        src = Path(migration_api.__file__).read_text()
        self.assertNotIn("import ucloud_backup", src)                  # le NSEC ne vit jamais dans UPassport
        self.assertNotIn("make_proof", src)
        self.assertNotIn(".secret.nostr", src)


if __name__ == "__main__":
    unittest.main()
