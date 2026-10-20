"""
tests/test_account_pins.py — épingler/rapatrier tous les documents IPFS d'un compte (services/account_pins.py).

IPFS et strfry sont SIMULÉS (aucun réseau, aucun démon) ; `GAME_NOSTR_PATH` redirigé vers un dossier temporaire.

    pytest tests/test_account_pins.py -v
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from services import account_pins as ap
from services import cloud_storage as cs

EMAIL, HEXPUB = "pin@example.com", "cd" * 32
_TMP = Path(tempfile.mkdtemp(prefix="upassport-pins-test-"))
cs.GAME_NOSTR_PATH = _TMP / "nostr"


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def cid(n: int) -> str:
    """CIDv0 plausible et UNIQUE par n (le zéro n'existe pas en base58 : 0 → A)."""
    return "Qm" + ("%044d" % n).replace("0", "A")


class PinsCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(cs.GAME_NOSTR_PATH, ignore_errors=True)
        self.d = cs.GAME_NOSTR_PATH / EMAIL
        (self.d / "APP" / "uDRIVE").mkdir(parents=True)
        (self.d / ".secret.nostr").write_text(f"NSEC=nsec1x; NPUB=npub1x; HEX={HEXPUB}")
        self.calls = []
        self.pinned_already, self.failing = set(), set()
        self._saved = (ap.subprocess.run, cs.load_index, ap.STRFRY_DIR, ap.shutil.disk_usage)
        ap.subprocess.run = self._fake_run
        cs.load_index = lambda email: {"entries": {"/a.jpg": {"cid": cid(1), "size": 1000, "type": "file"},
                                                  "/b.jpg": {"cid": cid(2), "size": 2000, "type": "file"},
                                                  "/dossier": {"type": "dir"}}}
        ap.STRFRY_DIR = _TMP / "no-strfry"                      # absent par défaut

    def tearDown(self):
        ap.subprocess.run, cs.load_index, ap.STRFRY_DIR, ap.shutil.disk_usage = self._saved

    def _fake_run(self, cmd, **kw):
        self.calls.append(list(cmd))
        R = type("R", (), {})
        r = R(); r.returncode = 0; r.stdout = b""; r.stderr = b""
        if cmd[:3] == ["ipfs", "pin", "ls"]:
            r.returncode = 0 if cmd[-1] in self.pinned_already else 1
        elif cmd[:3] == ["ipfs", "pin", "add"]:
            r.returncode = 1 if cmd[-1] in self.failing else 0
        elif cmd[1:2] == ["scan"]:
            r.stdout = (json.dumps({"id": "e1", "content": f"voir ipfs://{cid(30)} et /ipfs/{cid(31)}/x.mp4",
                                    "tags": [["url", f"https://g/ipfs/{cid(30)}"]]}) + "\n").encode()
        return r


class TestCollect(PinsCase):
    def test_all_sources_deduplicated_with_known_sizes(self):
        (self.d / "APP" / "uDRIVE" / "manifest.json").write_text(json.dumps({"final_cid": cid(10), "total_size": 5000}))
        (self.d / ".cookie_manifest.json").write_text(json.dumps({"a.com": {"cid": cid(20)}, "b.com": {"cid": cid(1)}}))
        sf = _TMP / "strfry-dir"; sf.mkdir(exist_ok=True); (sf / "strfry").write_text("x"); ap.STRFRY_DIR = sf
        found = ap.collect(EMAIL, roots=[f"/ipfs/{cid(99)}"])
        self.assertEqual(set(found), {cid(99), cid(1), cid(2), cid(10), cid(20), cid(30), cid(31)})
        self.assertEqual(found[cid(1)]["source"], "ucloud")             # première source gardée, pas « cookies »
        self.assertEqual((found[cid(1)]["size"], found[cid(2)]["size"], found[cid(10)]["size"]), (1000, 2000, 5000))
        self.assertEqual(found[cid(99)]["source"], "nostrns")
        self.assertEqual(found[cid(30)]["source"], "events")

    def test_missing_optional_sources_are_fine(self):
        found = ap.collect(EMAIL)
        self.assertEqual(set(found), {cid(1), cid(2)})

    def test_unreadable_ucloud_index_is_an_error_not_zero_files(self):
        def boom(email): raise RuntimeError("index corrompu")
        cs.load_index = boom
        with self.assertRaisesRegex(ap.PinError, "index ucloud illisible"):
            ap.collect(EMAIL)

    def test_strfry_failure_is_an_error(self):
        sf = _TMP / "strfry-ko"; sf.mkdir(exist_ok=True); (sf / "strfry").write_text("x"); ap.STRFRY_DIR = sf
        orig = self._fake_run
        def ko(cmd, **kw):
            r = orig(cmd, **kw)
            if cmd[1:2] == ["scan"]: r.returncode = 1; r.stderr = b"lmdb"
            return r
        ap.subprocess.run = ko
        with self.assertRaisesRegex(ap.PinError, "strfry scan"):
            ap.collect(EMAIL)


class TestSpace(PinsCase):
    def _usage(self, free):
        ap.shutil.disk_usage = lambda p: type("U", (), {"free": free})()

    def test_insufficient_space_is_refused_with_the_numbers(self):
        self._usage(1024 ** 3)                                          # 1 Go libre < 2 Go de marge
        with self.assertRaisesRegex(ap.PinError, "espace disque insuffisant"):
            ap.check_space({cid(1): {"source": "ucloud", "size": 1000}})

    def test_known_sizes_plus_reserve_must_fit(self):
        found = {cid(1): {"source": "ucloud", "size": 10 * 1024 ** 3}}
        self._usage(11 * 1024 ** 3)                                     # 10 Go × 1,1 + 2 Go = 13 Go nécessaires
        with self.assertRaises(ap.PinError):
            ap.check_space(found)
        self._usage(14 * 1024 ** 3)
        self.assertEqual(ap.check_space(found)["known_bytes"], 10 * 1024 ** 3)


class TestPinAll(PinsCase):
    def found(self):
        return {cid(1): {"source": "ucloud", "size": 1}, cid(99): {"source": "nostrns", "size": None},
                cid(10): {"source": "udrive", "size": 1}, cid(2): {"source": "ucloud", "size": 1}}

    def test_roots_first_already_skipped_failures_counted(self):
        self.pinned_already = {cid(2)}
        self.failing = {cid(1)}
        progress = []
        res = ap.pin_all(self.found(), progress.append, workers=1)
        adds = [c[-1] for c in self.calls if c[:3] == ["ipfs", "pin", "add"]]
        self.assertEqual(adds[:2], [cid(99), cid(10)])                  # racine NOSTRNS puis uDRIVE d'abord
        self.assertNotIn(cid(2), adds)                                  # déjà épinglé : sauté
        self.assertEqual((res["total"], res["pinned"], res["already"], res["failed"]), (4, 2, 1, 1))
        self.assertEqual(res["failed_cids"], [cid(1)])
        self.assertFalse(res["ok"])                                     # un échec = jamais « ok »
        self.assertEqual(progress[-1]["total"], 4)

    def test_all_good_is_ok(self):
        res = ap.pin_all(self.found(), workers=2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["pinned"], 4)

    def test_timeout_counts_as_failure(self):
        def slow(cmd, **kw):
            if cmd[:3] == ["ipfs", "pin", "add"]:
                import subprocess as sp
                raise sp.TimeoutExpired(cmd, 1)
            return self._fake_run.__func__(self, cmd, **kw) if False else type("R", (), {"returncode": 1, "stdout": b"", "stderr": b""})()
        ap.subprocess.run = slow
        res = ap.pin_all({cid(1): {"source": "ucloud", "size": 1}}, timeout=1, workers=1)
        self.assertEqual((res["failed"], res["ok"]), (1, False))


if __name__ == "__main__":
    unittest.main()
