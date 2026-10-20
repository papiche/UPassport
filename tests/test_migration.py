"""
tests/test_migration.py — déménagement d'un MULTIPASS entre stations, clé identique (services/migration.py).

Deux « stations » (deux racines temporaires qu'on bascule), un bus de messages inter-NODE en
mémoire, IPFS et Qdrant simulés, la VRAIE signature NOSTR de la preuve. Aucun ~/.zen réel,
aucun réseau, aucun GPU.

    pytest tests/test_migration.py -v
"""

import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path

import httpx
import nostr_sdk

import ucloud_backup as ub
from services import cloud_storage as cs
from services import migration as mg
from services import node_share
from test_ucloud_backup_bundle import FakeQdrant

EMAIL = "move@example.com"
PASS = "4242"                                # PASS saisi avec le QR P1, vérifié par l'ANCIENNE station
PHOTO = "Qm" + "P" * 44                      # CID cité par un événement du compte
OLD_NODE, NEW_NODE = "11" * 32, "22" * 32
_TMP = Path(tempfile.mkdtemp(prefix="upassport-migration-test-"))
ROOTS = {"old": _TMP / "old", "new": _TMP / "new"}
NODE_HEX = {"old": OLD_NODE, "new": NEW_NODE}


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class MigrationCase(unittest.TestCase):
    def setUp(self):
        for r in ROOTS.values():
            shutil.rmtree(r, ignore_errors=True)
            (r / "nostr").mkdir(parents=True)
        self.here = "old"
        self.bus, self.spawned, self.ipns_calls, self.ipfs_store = [], [], [], {}
        self.run_calls, self.fixups_calls, self.ipfs_failing = [], [], set()
        self.zc_key_id, self.zc_release_rc = "k51ZENCARDID", 0
        self.fixups_result = (0, "OK:home.station\n", "")
        self.ipns_release = (0, "SEQ=574\nVALUE=/ipfs/QmOldUdrive\n", "")
        self.keys = nostr_sdk.Keys.generate()
        self.hex = self.keys.public_key().to_hex()
        for st in ("old", "new"):
            self.at(st)
            self._account()
        # même clé des deux côtés ; l'ancienne station a des données, la nouvelle des valeurs par défaut
        self.at("old")
        udir = cs.ensure_ucloud_dir(EMAIL)
        (udir / "blocked_senders.json").write_text('{"aa": 1}')
        (cs.GAME_NOSTR_PATH / EMAIL / ".mailjet").write_text('{"email_channel": true}')
        (cs.GAME_NOSTR_PATH / EMAIL / ".pass").write_text(PASS + "\n")
        self.at("new")
        (cs.GAME_NOSTR_PATH / EMAIL / ".mailjet").write_text('{"email_channel": false}')
        self.pass_alerts = []

        self.q = FakeQdrant()
        self._client = httpx.Client
        tr = httpx.MockTransport(self.q.handler)
        ub.httpx.Client = lambda **kw: self._client(transport=tr, **{k: v for k, v in kw.items() if k != "transport"})

        self._saved = (node_share._node_secret, node_share._intercom_send, mg._run_ipns, mg._spawn,
                       cs.ipfs_add_bytes, cs.ipfs_cat, mg.subprocess.run, mg._run_fixups, mg._captain_email,
                       ub.STRFRY_DIR, mg.account_pins.STRFRY_DIR)
        node_share._node_secret = lambda: {"HEX": NODE_HEX[self.here], "NSEC": "x"}
        node_share._intercom_send = lambda to, ch, payload, ttl: self.bus.append((to, ch, json.loads(json.dumps(payload)), self.here)) or True
        mg._run_ipns = self._fake_ipns
        mg._spawn = lambda cmd, name: self.spawned.append(cmd)
        cs.ipfs_add_bytes = lambda data, filename="", pin=True: self.ipfs_store.setdefault(f"Qm{len(self.ipfs_store):04d}", data) and f"Qm{len(self.ipfs_store) - 1:04d}"
        cs.ipfs_cat = lambda cid, max_bytes=0: self.ipfs_store[cid]
        mg._run_fixups = lambda email: self.fixups_calls.append(email) or self.fixups_result
        mg._captain_email = lambda: "captain@example.com"
        self._notify = mg._notify_pass_lock
        mg._notify_pass_lock = lambda email, attempts: self.pass_alerts.append((email, attempts))
        # JAMAIS le vrai mailjet.sh : les emails sont enregistrés (gabarit, destinataire, objet, variables)
        self._mail = mg._mail
        self.mails = []
        mg._mail = lambda template, dest, subject, ctx: self.mails.append((template, dest, subject, dict(ctx))) or "ok"
        # strfry SIMULÉ (export des événements du compte / import, journal des pins) — jamais la vraie base
        self.sf = Path(tempfile.mkdtemp(dir=_TMP))
        self.imported = self.sf / "imported.jsonl"
        (self.sf / "strfry").write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == scan ]]; then\n"
            "  case \"$2\" in\n"
            "    *authors*) echo '{\"id\":\"a1\",\"kind\":1,\"content\":\"photo /ipfs/" + PHOTO + "\"}'; echo '{\"id\":\"a2\",\"kind\":3}';;\n"
            "    *'#p'*) echo '{\"id\":\"d1\",\"kind\":1059}';;\n"
            "  esac\n"
            "elif [[ \"$1\" == import ]]; then cat > \"" + str(self.imported) + "\"; fi\n")
        (self.sf / "strfry").chmod(0o755)
        ub.STRFRY_DIR = self.sf
        mg.account_pins.STRFRY_DIR = self.sf
        real_run = self._saved[6]

        def fake_run(cmd, **kw):
            """ipfs (pin/ls/add) et nostr_unfollow.sh simulés ; tout le reste (strfry factice…) s'exécute vraiment."""
            name = Path(str(cmd[0])).name
            if name == "ipfs" or name == "nostr_unfollow.sh":
                self.run_calls.append([name] + [str(c) for c in cmd[1:]])
                R = type("R", (), {})
                r = R(); r.returncode = 0; r.stdout = "" if kw.get("text") else b""; r.stderr = r.stdout
                if name == "ipfs" and cmd[1:3] == ["pin", "ls"]:
                    r.returncode = 1                       # rien d'épinglé : tout doit être récupéré
                if name == "ipfs" and cmd[1:3] == ["key", "import"]:
                    r.stdout = self.zc_key_id if kw.get("text") else self.zc_key_id.encode()
                if name == "ipfs" and cmd[1:3] == ["pin", "add"] and cmd[-1] in self.ipfs_failing:
                    r.returncode = 1
                return r
            return real_run(cmd, **kw)
        mg.subprocess.run = fake_run

    def tearDown(self):
        ub.httpx.Client = self._client
        (node_share._node_secret, node_share._intercom_send, mg._run_ipns, mg._spawn,
         cs.ipfs_add_bytes, cs.ipfs_cat, mg.subprocess.run, mg._run_fixups, mg._captain_email,
         ub.STRFRY_DIR, mg.account_pins.STRFRY_DIR) = self._saved
        mg._notify_pass_lock = self._notify
        mg._mail = self._mail

    # ── aides ────────────────────────────────────────────────────────────────
    def at(self, station):
        self.here = station
        cs.GAME_NOSTR_PATH = ROOTS[station] / "nostr"

    def _account(self):
        d = cs.GAME_NOSTR_PATH / EMAIL
        d.mkdir(parents=True, exist_ok=True)
        (d / ".secret.nostr").write_text(
            f"NSEC={self.keys.secret_key().to_bech32()}; NPUB={self.keys.public_key().to_bech32()}; HEX={self.hex}")

    def _fake_ipns(self, args):
        self.ipns_calls.append(args)
        if "--key" in args:                                     # IPNS de la ZenCard (deux noms pour la même clé)
            if args[0] == "release":
                if self.zc_release_rc:
                    return (1, "", "ERROR:KEY_RM échec")
                return (0, "SEQ=88\nVALUE=/ipfs/QmZenCardTW\n", "")
            return (0, "SEQ=89\n", "")
        return self.ipns_release if args[0] == "release" else (0, "SEQ=575\n", "")

    def last(self, channel):
        return next(m for m in reversed(self.bus) if m[1] == channel)

    def run_to_bundle(self):
        self.at("new")
        mg.request(EMAIL, OLD_NODE, PASS)
        self.at("old")
        mg.serve_request(self.last("migrate_request")[2], NEW_NODE)

    def run_to_imported(self):
        self.run_to_bundle()
        self.at("new")
        mg.receive_bundle(self.last("migrate_bundle")[2], OLD_NODE)


class TestHappyPath(MigrationCase):
    def test_full_migration(self):
        self.at("new")
        r = mg.request(EMAIL, OLD_NODE, PASS)
        self.assertEqual(r["status"], "requested")
        to, ch, payload, frm = self.last("migrate_request")
        self.assertEqual((to, ch, frm), (OLD_NODE, "migrate_request", "new"))
        self.assertEqual(payload["hex"], self.hex)

        self.at("old")
        mg.serve_request(payload, NEW_NODE)
        self.assertTrue(mg.is_frozen(EMAIL))                                 # compte gelé dès l'export
        self.assertEqual(mg.load_state(EMAIL)["state"], "bundled")
        bundle = self.last("migrate_bundle")
        self.assertEqual(bundle[0], NEW_NODE)

        self.at("new")
        res = mg.receive_bundle(bundle[2], OLD_NODE)
        self.assertEqual(res["status"], "imported")
        self.assertEqual((cs.GAME_NOSTR_PATH / EMAIL / ".mailjet").read_text(), '{"email_channel": true}')  # réglages repris
        self.assertEqual(json.loads((cs.ucloud_dir(EMAIL) / "blocked_senders.json").read_text()), {"aa": 1})
        self.assertTrue(any("pin" in c for c in self.spawned[0]))            # épinglage détaché

        self.at("old")
        ack = self.last("migrate_ack")
        self.assertEqual(ack[0], OLD_NODE)
        mg.serve_ack(ack[2], NEW_NODE)
        self.assertEqual(self.ipns_calls[-1], ["release", EMAIL])
        st = mg.load_state(EMAIL)
        self.assertEqual((st["state"], st["seq"], st["value"]), ("committed", "574", "/ipfs/QmOldUdrive"))

        self.at("new")
        commit = self.last("migrate_commit")
        mg.receive_commit(commit[2], OLD_NODE)
        self.assertEqual(mg.load_state(EMAIL)["state"], "claiming")
        out = mg.finish(EMAIL)
        self.assertEqual(self.ipns_calls[-1], ["claim", EMAIL, "QmOldUdrive", "--after", "574"])
        self.assertEqual(out["status"], "claimed")
        self.assertEqual(out["pin"]["status"], "done")
        self.assertEqual(mg.load_state(EMAIL)["state"], "done")
        self.assertEqual(self.fixups_calls, [EMAIL])                         # mises à jour publiques ciblées
        to, ch, payload, frm = self.last("migrate_pinned")
        self.assertEqual((to, frm, payload["ok"], payload["failed"]), (OLD_NODE, "new", True, 0))

    def test_cli_status_never_prints_the_bundle_key(self):
        import io
        import sys
        import multipass_migrate
        self.run_to_bundle()
        self.at("old")
        self.assertIn("key", mg.load_state(EMAIL))                          # gardée pour pouvoir renvoyer le paquet
        buf, argv, out = io.StringIO(), sys.argv, sys.stdout
        sys.argv, sys.stdout = ["multipass_migrate.py", "--status", EMAIL], buf
        try:
            self.assertEqual(multipass_migrate.main(), 0)
        finally:
            sys.argv, sys.stdout = argv, out
        shown = json.loads(buf.getvalue())
        self.assertNotIn("key", shown)
        self.assertEqual(shown["state"], "bundled")


class TestRefusals(MigrationCase):
    def test_proof_for_another_station_is_refused_and_nothing_frozen(self):
        self.at("new")
        mg.request(EMAIL, OLD_NODE, PASS)
        msg = self.last("migrate_request")[2]
        self.at("old")
        with self.assertRaisesRegex(mg.MigrationError, "autre station"):
            mg.serve_request(msg, "33" * 32)
        self.assertFalse(mg.is_frozen(EMAIL))
        self.assertEqual(mg.load_state(EMAIL), {})

    def test_proof_replay_refused(self):
        self.at("new")
        mg.request(EMAIL, OLD_NODE, PASS)
        msg = self.last("migrate_request")[2]
        self.at("old")
        mg.serve_request(msg, NEW_NODE)
        with self.assertRaisesRegex(mg.MigrationError, "rejeu"):
            mg.serve_request(msg, NEW_NODE)

    def test_proof_signed_by_another_key_refused(self):
        other = nostr_sdk.Keys.generate()
        self.at("new")
        d = cs.GAME_NOSTR_PATH / EMAIL
        (d / ".secret.nostr").write_text(f"NSEC={other.secret_key().to_bech32()}; HEX={self.hex}")  # NSEC ≠ HEX annoncé
        mg.request(EMAIL, OLD_NODE, PASS)
        msg = self.last("migrate_request")[2]
        self.at("old")
        with self.assertRaisesRegex(mg.MigrationError, "pas signée par la clé"):
            mg.serve_request(msg, NEW_NODE)
        self.assertFalse(mg.is_frozen(EMAIL))

    def test_stale_proof_refused(self):
        self.at("new")
        mg.request(EMAIL, OLD_NODE, PASS)
        msg = self.last("migrate_request")[2]
        self.at("old")
        mg.PROOF_MAX_AGE = -1
        try:
            with self.assertRaisesRegex(mg.MigrationError, "périmée"):
                mg.serve_request(msg, NEW_NODE)
        finally:
            mg.PROOF_MAX_AGE = 600

    def test_unknown_account_or_other_key_refused(self):
        self.at("new")
        mg.request(EMAIL, OLD_NODE, PASS)
        msg = self.last("migrate_request")[2]
        self.at("old")
        bad = dict(msg, hex="cd" * 32)
        with self.assertRaisesRegex(mg.MigrationError, "clé différente"):
            mg.serve_request(bad, NEW_NODE)

    def test_bundle_from_unexpected_node_refused(self):
        self.run_to_bundle()
        self.at("new")
        with self.assertRaisesRegex(mg.MigrationError, "non attendu"):
            mg.receive_bundle(self.last("migrate_bundle")[2], "44" * 32)
        self.assertEqual(mg.load_state(EMAIL)["state"], "requested")

    def test_tampered_bundle_refused_no_ack(self):
        self.run_to_bundle()
        msg = dict(self.last("migrate_bundle")[2], sha="00" * 32)
        self.at("new")
        n = len(self.bus)
        with self.assertRaisesRegex(mg.MigrationError, "empreinte"):
            mg.receive_bundle(msg, OLD_NODE)
        self.assertEqual(len(self.bus), n)                                   # aucun accusé
        self.assertEqual(mg.load_state(EMAIL)["state"], "error")
        self.assertEqual((cs.GAME_NOSTR_PATH / EMAIL / ".mailjet").read_text(), '{"email_channel": false}')

    def test_embedding_dimension_mismatch_means_no_ack(self):
        self.at("old")
        mem = f"memory_{self.hex[:16]}"
        self.q.collections[mem] = {"size": 4, "points": {1: {"id": 1, "vector": [0.1] * 4, "payload": {"content": "x"}}}}
        self.run_to_bundle()
        self.at("new")
        self.q.collections.clear()
        self.q.collections[mem] = {"size": 768, "points": {}}               # autre modèle d'embedding ici
        n = len(self.bus)
        with self.assertRaisesRegex(mg.MigrationError, "768"):
            mg.receive_bundle(self.last("migrate_bundle")[2], OLD_NODE)
        self.assertEqual(len(self.bus), n)

    def test_ack_with_wrong_cid_or_sender_refused(self):
        self.run_to_imported()
        ack = self.last("migrate_ack")[2]
        self.at("old")
        with self.assertRaisesRegex(mg.MigrationError, "non attendu"):
            mg.serve_ack(dict(ack, cid="QmAutre"), NEW_NODE)
        with self.assertRaisesRegex(mg.MigrationError, "non attendu"):
            mg.serve_ack(ack, "44" * 32)
        self.assertEqual(mg.load_state(EMAIL)["state"], "bundled")

    def test_ipns_release_failure_keeps_account_not_ceded(self):
        self.run_to_imported()
        ack = self.last("migrate_ack")[2]
        self.at("old")
        self.ipns_release = (1, "", "ERROR:KEY_RM ipfs key rm a échoué")
        n = len(self.bus)
        with self.assertRaisesRegex(mg.MigrationError, "NON cédé"):
            mg.serve_ack(ack, NEW_NODE)
        self.assertEqual(mg.load_state(EMAIL)["state"], "bundled")
        self.assertEqual(len(self.bus), n)                                   # pas de commit

    def test_key_already_released_still_commits_with_empty_sequence(self):
        self.run_to_imported()
        ack = self.last("migrate_ack")[2]
        self.at("old")
        self.ipns_release = (1, "", "ERROR:NO_KEY clé absente")
        mg.serve_ack(ack, NEW_NODE)
        self.assertEqual(self.last("migrate_commit")[2]["seq"], "")

    def test_ack_is_idempotent_and_never_releases_twice(self):
        self.run_to_imported()
        ack = self.last("migrate_ack")[2]
        self.at("old")
        mg.serve_ack(ack, NEW_NODE)
        mg.serve_ack(ack, NEW_NODE)
        self.assertEqual(sum(1 for c in self.ipns_calls if c[0] == "release"), 1)

    def test_request_without_known_source_is_an_error(self):
        self.at("new")
        with self.assertRaisesRegex(mg.MigrationError, "introuvable"):
            mg.request(EMAIL)

    def test_finish_without_record_publishes_nothing(self):
        self.ipns_release = (0, "SEQ=0\nVALUE=\n", "")
        self.run_to_imported()
        self.at("old")
        mg.serve_ack(self.last("migrate_ack")[2], NEW_NODE)
        self.at("new")
        mg.receive_commit(self.last("migrate_commit")[2], OLD_NODE)
        out = mg.finish(EMAIL)
        self.assertEqual(out["note"], "no_record")
        self.assertFalse(any(c[0] == "claim" for c in self.ipns_calls))

    def test_commit_malformed_refused(self):
        self.run_to_imported()
        self.at("new")
        with self.assertRaisesRegex(mg.MigrationError, "mal formé"):
            mg.receive_commit({"email": EMAIL, "hex": self.hex, "seq": "12", "value": "/ipfs/x; rm -rf /"}, OLD_NODE)


class FullFlowCase(MigrationCase):
    """Parcours complet jusqu'à l'épinglage + nettoyage de l'ancienne station."""

    def seed_old_account(self):
        self.at("old")
        d = cs.GAME_NOSTR_PATH / EMAIL
        for name, val in ((".account_created", "2026-01-02"), (".lastpayment", "2026-10-13"), (".pass", "4242"),
                          (".secret.disco", "/?salt=S&nostr=P"), (".keyformula", "nostr=legacy g1=isolated ipns=isolated"),
                          (".multipass.json", '{"nsec": "nsec1old"}')):
            (d / name).write_text(val)
        (d / "identity").mkdir(exist_ok=True)
        (d / "identity" / ".Core.md").write_text("ma biographie")
        cap = cs.GAME_NOSTR_PATH / "captain@example.com"
        cap.mkdir(parents=True, exist_ok=True)
        (cap / ".secret.nostr").write_text("NSEC=nsec1CAPTAIN; NPUB=npub1c; HEX=" + "cd" * 32)

    def run_to_commit(self):
        self.run_to_imported()
        self.at("old")
        mg.serve_ack(self.last("migrate_ack")[2], NEW_NODE)
        self.at("new")
        mg.receive_commit(self.last("migrate_commit")[2], OLD_NODE)

    def run_all(self):
        self.run_to_commit()
        self.at("new")
        return mg.finish(EMAIL)


class TestStateTravels(FullFlowCase):
    def test_state_pass_identity_and_events_arrive_secrets_do_not(self):
        self.seed_old_account()
        self.at("new")
        (cs.GAME_NOSTR_PATH / EMAIL / ".roaming").write_text("")
        self.run_to_imported()
        d = cs.GAME_NOSTR_PATH / EMAIL
        self.assertEqual((d / ".account_created").read_text(), "2026-01-02")          # sinon : compte pris pour NEUF
        self.assertEqual((d / ".lastpayment").read_text(), "2026-10-13")              # sinon : redevance refaite
        self.assertEqual((d / ".pass").read_text(), "4242")                           # le PASS voyage (déménagement)
        self.assertEqual((d / "identity" / ".Core.md").read_text(), "ma biographie")
        self.assertFalse((d / ".secret.disco").exists())                              # re-dérivé du QR, jamais emporté
        self.assertFalse((d / ".multipass.json").exists())
        self.assertFalse((d / ".keyformula").exists())
        self.assertIn('"id":"a1"', self.imported.read_text())                         # événements importés (vérifiés)
        self.assertEqual(len(self.imported.read_text().splitlines()), 3)

    def test_announced_counts_must_match_applied(self):
        self.seed_old_account()
        self.run_to_bundle()
        msg = self.last("migrate_bundle")[2]
        msg["counts"]["state_files"] = 99
        self.at("new")
        n = len(self.bus)
        with self.assertRaisesRegex(mg.MigrationError, "annoncés"):
            mg.receive_bundle(msg, OLD_NODE)
        self.assertEqual(len(self.bus), n)                                           # aucun accusé
        self.assertEqual(mg.load_state(EMAIL)["state"], "error")

    def test_incomplete_export_is_refused_and_account_unfrozen(self):
        self.seed_old_account()
        self.at("new")
        mg.request(EMAIL, OLD_NODE, PASS)
        msg = self.last("migrate_request")[2]
        self.at("old")
        real_export = ub.export_nostr_events
        ub.export_nostr_events = lambda hex_pub, out: (_ for _ in ()).throw(RuntimeError("lmdb verrouillée"))
        try:
            with self.assertRaisesRegex(mg.MigrationError, "export incomplet"):
                mg.serve_request(msg, NEW_NODE)
        finally:
            ub.export_nostr_events = real_export
        self.assertFalse(mg.is_frozen(EMAIL))                                        # le compte continue normalement
        self.assertEqual(mg.load_state(EMAIL)["state"], "export_error")
        self.assertFalse(any(m[1] == "migrate_bundle" for m in self.bus))


class TestRoamingAndAbort(MigrationCase):
    def test_request_removes_roaming_marker_so_the_24h_purge_cannot_wipe_the_account(self):
        self.at("new")
        roaming = cs.GAME_NOSTR_PATH / EMAIL / ".roaming"
        roaming.write_text("")
        mg.request(EMAIL, OLD_NODE, PASS)
        self.assertFalse(roaming.exists())
        self.assertTrue(mg.load_state(EMAIL)["was_roaming"])

    def test_abort_restores_roaming_and_clears_state(self):
        self.at("new")
        roaming = cs.GAME_NOSTR_PATH / EMAIL / ".roaming"
        roaming.write_text("")
        mg.request(EMAIL, OLD_NODE, PASS)
        self.assertEqual(mg.abort(EMAIL)["status"], "aborted")
        self.assertTrue(roaming.exists())
        self.assertEqual(mg.load_state(EMAIL), {})

    def test_failed_send_puts_the_account_back_as_before(self):
        self.at("new")
        roaming = cs.GAME_NOSTR_PATH / EMAIL / ".roaming"
        roaming.write_text("")
        node_share._intercom_send = lambda *a, **k: False
        with self.assertRaisesRegex(mg.MigrationError, "impossible"):
            mg.request(EMAIL, OLD_NODE, PASS)
        self.assertTrue(roaming.exists())
        self.assertEqual(mg.load_state(EMAIL), {})

    def test_abort_refused_after_import(self):
        self.run_to_imported()
        with self.assertRaisesRegex(mg.MigrationError, "rien à annuler"):
            mg.abort(EMAIL)


class TestZenCard(FullFlowCase):
    """La ZenCard (players/<email> : TiddlyWiki, secret.june, parts sociales) déménage avec le compte."""
    G1PUB = "g1ZenCardPub"

    def seed_zencard(self):
        self.at("old")
        pdir = cs.GAME_NOSTR_PATH.parent / "players" / EMAIL
        (pdir / "ipfs" / "moa").mkdir(parents=True)
        for name, val in ((".g1pub", self.G1PUB), (".playerns", "/ipns/k51ZENCARDID"), (".pass", "4242"),
                          ("secret.player", "-----BEGIN PRIVATE KEY-----"), ("secret.june", 'SALT="s"\nPEPPER="p"'),
                          ("secret.dunikey", "pub: x\nsec: y"), ("U.SOCIETY", "2026-01-02"),
                          ("ipfs/moa/index.html", "<html>mon TiddlyWiki</html>")):
            (pdir / name).write_text(val)
        os.symlink(str(pdir / "U.SOCIETY"), cs.GAME_NOSTR_PATH / EMAIL / "U.SOCIETY")

    def test_zencard_content_keys_and_ipns_travel(self):
        self.seed_old_account()
        self.seed_zencard()
        out = self.run_all()
        self.assertEqual(out["status"], "claimed")
        self.at("new")
        pdir = cs.GAME_NOSTR_PATH.parent / "players" / EMAIL
        self.assertEqual((pdir / "ipfs" / "moa" / "index.html").read_text(), "<html>mon TiddlyWiki</html>")   # contenu
        self.assertIn("SALT=", (pdir / "secret.june").read_text())
        self.assertEqual((pdir / "U.SOCIETY").read_text(), "2026-01-02")                                      # parts sociales
        self.assertEqual(stat.S_IMODE(os.stat(pdir / "secret.june").st_mode), 0o600)
        link = cs.GAME_NOSTR_PATH / EMAIL / "U.SOCIETY"
        self.assertTrue(link.is_symlink() and Path(os.readlink(link)) == pdir / "U.SOCIETY")
        imports = [c for c in self.run_calls if c[:3] == ["ipfs", "key", "import"]]
        self.assertEqual({c[3] for c in imports}, {EMAIL, self.G1PUB})                                        # deux noms
        # IPNS de la ZenCard : l'ancienne libère les DEUX noms, la nouvelle publie avec séquence supérieure
        self.assertIn(["release", EMAIL, "--key", EMAIL], self.ipns_calls)
        self.assertIn(["release", EMAIL, "--key", self.G1PUB], self.ipns_calls)
        self.assertIn(["claim", EMAIL, "QmZenCardTW", "--after", "88", "--key", EMAIL], self.ipns_calls)
        commit = self.last("migrate_commit")[2]
        self.assertEqual(commit["zencard"], {"seq": "88", "value": "/ipfs/QmZenCardTW"})
        adds = [c[-1] for c in self.run_calls if c[:3] == ["ipfs", "pin", "add"]]
        self.assertIn("QmZenCardTW", adds)                                                                    # contenu rapatrié

    def test_cleanup_moves_both_folders(self):
        self.seed_old_account()
        self.seed_zencard()
        self.run_all()
        self.at("old")
        res = mg.serve_pinned(self.last("migrate_pinned")[2], NEW_NODE)
        self.assertEqual(res["status"], "cleaned")
        self.assertFalse((cs.GAME_NOSTR_PATH / EMAIL).exists())
        self.assertFalse((cs.GAME_NOSTR_PATH.parent / "players" / EMAIL).exists())
        moved = Path(res["moved_to"])
        self.assertTrue((moved / "players" / "secret.june").exists())
        self.assertTrue((moved / "nostr" / ".account_created").exists())

    def test_a_different_zencard_already_here_is_never_overwritten(self):
        self.seed_old_account()
        self.seed_zencard()
        self.run_to_bundle()
        self.at("new")
        other = cs.GAME_NOSTR_PATH.parent / "players" / EMAIL
        other.mkdir(parents=True)
        (other / ".g1pub").write_text("g1AutreCarte")
        (other / "secret.june").write_text("A_GARDER")
        n = len(self.bus)
        with self.assertRaisesRegex(mg.MigrationError, "ZenCard différente"):
            mg.receive_bundle(self.last("migrate_bundle")[2], OLD_NODE)
        self.assertEqual((other / "secret.june").read_text(), "A_GARDER")
        self.assertEqual(len(self.bus), n)                                                                    # aucun accusé

    def test_wrong_key_id_after_import_means_no_ack(self):
        self.seed_old_account()
        self.seed_zencard()
        self.zc_key_id = "k51AUTRECLE"
        self.run_to_bundle()
        self.at("new")
        n = len(self.bus)
        with self.assertRaisesRegex(mg.MigrationError, "identifiant différent"):
            mg.receive_bundle(self.last("migrate_bundle")[2], OLD_NODE)
        self.assertEqual(len(self.bus), n)
        self.assertEqual(mg.load_state(EMAIL)["state"], "error")

    def test_release_is_idempotent_a_failure_never_forgets_the_first_sequence(self):
        self.seed_old_account()
        self.seed_zencard()
        self.run_to_imported()
        ack = self.last("migrate_ack")[2]
        self.at("old")
        self.zc_release_rc = 1
        with self.assertRaisesRegex(mg.MigrationError, "ZenCard impossible"):
            mg.serve_ack(ack, NEW_NODE)
        st = mg.load_state(EMAIL)
        self.assertEqual((st["state"], st["released_nostr"], st["seq"]), ("bundled", True, "574"))            # consigné
        self.zc_release_rc = 0
        before = sum(1 for c in self.ipns_calls if c == ["release", EMAIL])
        mg.serve_ack(ack, NEW_NODE)
        self.assertEqual(sum(1 for c in self.ipns_calls if c == ["release", EMAIL]), before)                  # pas refait
        commit = self.last("migrate_commit")[2]
        self.assertEqual((commit["seq"], commit["zencard"]["seq"]), ("574", "88"))


class TestPinAndCleanup(FullFlowCase):
    def test_pin_all_documents_then_confirm_then_old_station_moves_the_account(self):
        self.seed_old_account()
        out = self.run_all()
        self.assertEqual(out["pin"]["status"], "done")
        adds = [c[-1] for c in self.run_calls if c[:3] == ["ipfs", "pin", "add"]]
        self.assertIn("QmOldUdrive", adds)                                           # racine du NOSTRNS
        self.assertIn(PHOTO, adds)                                                   # pièce jointe d'un événement
        msg = self.last("migrate_pinned")[2]
        self.assertTrue(msg["ok"])
        self.at("old")
        res = mg.serve_pinned(msg, NEW_NODE)
        self.assertEqual(res["status"], "cleaned")
        self.assertFalse((cs.GAME_NOSTR_PATH / EMAIL).exists())                      # déplacé…
        moved = Path(res["moved_to"])
        self.assertEqual(moved.parent, _TMP / "tmp" / "migrated")                    # ~/.zen/tmp/migrated : purgé à 20h12
        self.assertTrue((moved / "nostr" / ".account_created").exists())             # déplacé, pas supprimé
        self.assertEqual(json.loads((moved / "nostr" / ".migration.json").read_text())["cleanup"], "done")
        unfollow = [c for c in self.run_calls if c[0] == "nostr_unfollow.sh"]
        self.assertEqual(len(unfollow), 1)
        self.assertEqual(unfollow[0][1], "-")                                        # clé du Capitaine par l'environnement
        self.assertNotIn("nsec1", " ".join(unfollow[0]))

    def test_a_failed_pin_blocks_every_cleanup(self):
        self.seed_old_account()
        self.ipfs_failing = {PHOTO}
        out = self.run_all()
        self.assertEqual(out["pin"]["status"], "pin_error")
        msg = self.last("migrate_pinned")[2]
        self.assertEqual((msg["ok"], msg["failed"]), (False, 1))
        self.assertEqual(mg.load_state(EMAIL)["pin_failed_cids"], [PHOTO])
        self.at("old")
        res = mg.serve_pinned(msg, NEW_NODE)
        self.assertEqual(res["status"], "pin_incomplete")
        self.assertTrue((cs.GAME_NOSTR_PATH / EMAIL).exists())                       # RIEN n'est nettoyé
        self.assertTrue(mg.is_frozen(EMAIL))
        self.assertFalse(any(c[0] == "nostr_unfollow.sh" for c in self.run_calls))

    def test_pin_is_resumable_after_a_failure(self):
        self.seed_old_account()
        self.ipfs_failing = {PHOTO}
        self.run_all()
        self.ipfs_failing = set()
        self.at("new")
        res = mg.pin(EMAIL)
        self.assertEqual(res["status"], "done")
        self.assertTrue(self.last("migrate_pinned")[2]["ok"])

    def test_not_enough_disk_space_is_visible_and_nothing_is_confirmed(self):
        self.seed_old_account()
        self.run_to_commit()
        self.at("new")
        real = mg.account_pins.check_space
        mg.account_pins.check_space = lambda found, repo=None: (_ for _ in ()).throw(mg.account_pins.PinError("espace disque insuffisant"))
        try:
            with self.assertRaisesRegex(mg.MigrationError, "espace disque insuffisant"):
                mg.finish(EMAIL)
        finally:
            mg.account_pins.check_space = real
        st = mg.load_state(EMAIL)
        self.assertEqual(st["state"], "pin_error")
        self.assertIn("espace disque", st["pin_error"])
        self.assertFalse(any(m[1] == "migrate_pinned" for m in self.bus))            # pas de confirmation mensongère
        self.assertEqual(mg.pin(EMAIL)["status"], "done")                            # reprise une fois la place faite

    def test_confirmation_from_the_wrong_node_or_state_is_refused(self):
        self.seed_old_account()
        self.run_all()
        msg = self.last("migrate_pinned")[2]
        self.at("old")
        with self.assertRaisesRegex(mg.MigrationError, "non attendue"):
            mg.serve_pinned(msg, "44" * 32)
        with self.assertRaisesRegex(mg.MigrationError, "tant que l'épinglage"):
            mg.cleanup_source(EMAIL)                                                 # jamais sans confirmation sans échec
        self.assertTrue((cs.GAME_NOSTR_PATH / EMAIL).exists())

    def test_frozen_states_are_exactly_the_incomplete_ones(self):
        self.seed_old_account()
        self.run_to_commit()
        self.at("new")
        self.assertEqual(mg.load_state(EMAIL)["state"], "claiming")
        mg.finish(EMAIL)
        self.assertEqual(mg.load_state(EMAIL)["state"], "done")


class TestPassSecondFactor(MigrationCase):
    """Le PASS (saisi avec le QR P1) est vérifié par l'ANCIENNE station avant tout gel, avec le compteur partagé."""

    def request_with(self, pass_code):
        self.at("new")
        mg.request(EMAIL, OLD_NODE, pass_code)
        msg = self.last("migrate_request")[2]
        self.at("old")
        return msg

    def test_wrong_pass_is_refused_nothing_frozen_counter_counts(self):
        msg = self.request_with("0000")
        self.at("old")
        with self.assertRaisesRegex(mg.MigrationError, "PASS refusé.*2 essais restants"):
            mg.serve_request(msg, NEW_NODE)
        self.assertFalse(mg.is_frozen(EMAIL))
        self.assertEqual(mg.load_state(EMAIL), {})
        self.assertFalse(any(m[1] == "migrate_bundle" for m in self.bus))
        self.assertEqual(json.loads((cs.GAME_NOSTR_PATH / EMAIL / ".pass.fails").read_text())["count"], 1)

    def test_missing_pass_is_refused(self):
        msg = self.request_with("")
        with self.assertRaisesRegex(mg.MigrationError, "PASS refusé"):
            mg.serve_request(msg, NEW_NODE)
        self.assertFalse(mg.is_frozen(EMAIL))

    def test_three_failures_lock_the_pass_and_warn_the_captain(self):
        for code in ("0000", "1111", "2222"):
            msg = self.request_with(code)
            self.at("old")
            with self.assertRaises(mg.MigrationError):
                mg.serve_request(msg, NEW_NODE)
        self.assertEqual(self.pass_alerts, [(EMAIL, 3)])
        self.assertFalse((cs.GAME_NOSTR_PATH / EMAIL / ".pass").exists())            # PASS invalidé
        msg = self.request_with(PASS)                                                # même le bon ne passe plus
        with self.assertRaisesRegex(mg.MigrationError, "indisponible"):
            mg.serve_request(msg, NEW_NODE)
        self.assertFalse(mg.is_frozen(EMAIL))

    def test_good_pass_goes_through_and_resets_the_counter(self):
        msg = self.request_with("0000")
        with self.assertRaises(mg.MigrationError):
            mg.serve_request(msg, NEW_NODE)
        msg = self.request_with(PASS)
        self.at("old")
        mg.serve_request(msg, NEW_NODE)
        self.assertTrue(mg.is_frozen(EMAIL))
        self.assertFalse((cs.GAME_NOSTR_PATH / EMAIL / ".pass.fails").exists() and
                         (cs.GAME_NOSTR_PATH / EMAIL / ".pass.fails").read_text().strip())

    def test_the_pass_never_appears_in_clear_outside_the_encrypted_message(self):
        self.at("new")
        mg.request(EMAIL, OLD_NODE, PASS)
        self.assertNotIn(PASS, json.dumps(mg.load_state(EMAIL)))                     # jamais écrit dans l'état

    def test_pass_disabled_by_the_user_blocks_the_move(self):
        (cs.GAME_NOSTR_PATH.parent.parent / "old" / "nostr" / EMAIL / ".pass.disabled").touch()
        msg = self.request_with(PASS)
        with self.assertRaisesRegex(mg.MigrationError, "désactivée"):
            mg.serve_request(msg, NEW_NODE)


class TestEmails(FullFlowCase):
    """Tout le monde est prévenu : la personne et le Capitaine d'ARRIVÉE (nouvelle station) quand tout est épinglé,
    le Capitaine de DÉPART quand les dossiers ont quitté sa station."""

    def test_arrival_notifies_the_person_and_the_new_captain_once_everything_is_pinned(self):
        self.seed_old_account()
        self.run_all()
        by_tpl = {m[0]: m for m in self.mails}
        self.assertEqual(set(by_tpl), {"migration_arrived_player.html", "migration_captain_arrived.html"})
        tpl, dest, subject, ctx = by_tpl["migration_arrived_player.html"]
        self.assertEqual(dest, EMAIL)
        self.assertIn("déménagé", subject)
        self.assertEqual(ctx["_EMAIL_"], EMAIL)
        self.assertRegex(ctx["_DOCS_"], r"^\d+ / \d+$")
        tpl, dest, subject, ctx = by_tpl["migration_captain_arrived.html"]
        self.assertEqual((dest, ctx["_FAILED_"], ctx["_STATUS_"]), ("captain@example.com", 0, "terminé"))
        self.assertEqual(mg.load_state(EMAIL)["notified_arrival"], {"player": "ok", "captain": "ok"})

    def test_a_resumed_pin_never_sends_the_arrival_twice(self):
        self.seed_old_account()
        self.ipfs_failing = {PHOTO}
        self.run_all()
        self.assertEqual([m[0] for m in self.mails], ["migration_captain_arrived.html"])   # incomplet : Capitaine seul
        self.assertIn("incomplet", self.mails[0][2].lower())
        self.assertNotIn("migration_arrived_player.html", [m[0] for m in self.mails])      # la personne n'est pas alarmée
        self.ipfs_failing = set()
        self.at("new")
        mg.pin(EMAIL)
        mg.pin(EMAIL) if mg.load_state(EMAIL)["state"] == "pin_error" else None
        self.assertEqual(sum(1 for m in self.mails if m[0] == "migration_arrived_player.html"), 1)

    def test_departure_notifies_the_old_captain_with_where_the_folders_went(self):
        self.seed_old_account()
        self.run_all()
        self.mails.clear()
        self.at("old")
        res = mg.serve_pinned(self.last("migrate_pinned")[2], NEW_NODE)
        self.assertEqual([m[0] for m in self.mails], ["migration_captain_departed.html"])
        tpl, dest, subject, ctx = self.mails[0]
        self.assertEqual((dest, ctx["_EMAIL_"], ctx["_MOVEDTO_"]), ("captain@example.com", EMAIL, res["moved_to"]))
        self.assertEqual(res["notified"], {"captain": "ok"})

    def test_a_failed_email_never_cancels_a_successful_move(self):
        self.seed_old_account()
        mg._mail = lambda *a, **k: "échec : mailjet.sh code 1"
        out = self.run_all()
        self.assertEqual(out["pin"]["status"], "done")                                     # le déménagement a réussi
        self.assertEqual(mg.load_state(EMAIL)["notified_arrival"]["player"], "échec : mailjet.sh code 1")   # consigné


class TestMailTemplates(unittest.TestCase):
    """Les trois emails sont lisibles dans une messagerie : HTML statique, tous les marqueurs fournis par le code."""
    NAMES = ("migration_arrived_player.html", "migration_captain_arrived.html", "migration_captain_departed.html")

    def test_static_html_without_hidden_content(self):
        for n in self.NAMES:
            src = (mg.MAIL_TEMPLATES / n).read_text(encoding="utf-8")
            for bad in ("<script", "display:none", "display: none", "lang-content", "onclick"):
                self.assertNotIn(bad, src, f"{n} : {bad}")
            self.assertIn("🇫🇷", src)
            self.assertIn("🇬🇧", src)                                                       # les deux langues, visibles

    def test_every_marker_is_filled_by_the_code(self):
        import re
        code = Path(mg.__file__).read_text(encoding="utf-8")
        for n in self.NAMES:
            src = re.sub(r"<!--.*?-->", "", (mg.MAIL_TEMPLATES / n).read_text(encoding="utf-8"), flags=re.S)
            for marker in set(re.findall(r"(?<![A-Za-z0-9])(_[A-Z]+_)(?![A-Za-z0-9])", src)):
                self.assertIn(f'"{marker}"', code, f"{n} : marqueur {marker} jamais fourni par migration.py")

    def test_rendered_email_has_no_leftover_marker_and_escapes_values(self):
        sent = {}
        orig = mg.subprocess.run
        mg.subprocess.run = lambda cmd, **kw: sent.setdefault("html", Path(cmd[-2]).read_text()) and type("R", (), {"returncode": 0})()
        mg.MAILJET = Path(__file__)                                                        # « existe »
        try:
            res = mg._mail("migration_captain_departed.html", "cap@x.org", "sujet",
                           {"_EMAIL_": "a<b>@x.org", "_TONODE_": "abc…", "_DATE_": "2026-10-20", "_MOVEDTO_": "/tmp/x&y"})
        finally:
            mg.subprocess.run = orig
        self.assertEqual(res, "ok")
        self.assertNotIn("_EMAIL_", sent["html"])
        self.assertIn("a&lt;b&gt;@x.org", sent["html"])                                    # valeurs échappées
        self.assertNotIn("<b>@x.org", sent["html"])


if __name__ == "__main__":
    unittest.main()
