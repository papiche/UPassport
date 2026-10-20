"""
tests/test_ucloud_backup_bundle.py — paquet de sauvegarde / déménagement (ucloud_backup.py).

Le paquet porte, en plus de l'index et du trousseau : la mémoire de BRO (Qdrant
memory_<hex16>), les petits réglages du compte et le manifeste du uDRIVE.
Qdrant est SIMULÉ (aucun GPU, aucun service) ; `GAME_NOSTR_PATH` est redirigé
vers un répertoire temporaire : aucun ~/.zen réel n'est touché.

    pytest tests/test_ucloud_backup_bundle.py -v
"""

import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import httpx

import ucloud_backup as ub
from services import cloud_storage as cs

EMAIL = "move@example.com"
HEXPUB = "ab" * 32
MEM = f"memory_{HEXPUB[:16]}"

_TMP = Path(tempfile.mkdtemp(prefix="upassport-bundle-test-"))
cs.GAME_NOSTR_PATH = _TMP / "nostr"


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def _make_account(email=EMAIL, hex_pub=HEXPUB) -> Path:
    d = cs.GAME_NOSTR_PATH / email
    d.mkdir(parents=True, exist_ok=True)
    (d / ".secret.nostr").write_text(f"NSEC=nsec1fake; NPUB=npub1fake; HEX={hex_pub}")
    return d


class FakeQdrant:
    """Qdrant minimal en mémoire, branché sur httpx.Client via MockTransport."""

    def __init__(self):
        self.collections = {}   # nom -> {"size": int, "points": {id: point}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")   # collections/<nom>[/points[/scroll]]
        name = parts[1]
        body = json.loads(request.content) if request.content else {}
        if len(parts) == 2:
            if request.method == "GET":
                c = self.collections.get(name)
                if not c:
                    return httpx.Response(404, json={})
                return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"size": c["size"]}}}}})
            self.collections[name] = {"size": body["vectors"]["size"], "points": {}}
            return httpx.Response(200, json={"result": True})
        c = self.collections.get(name)
        if not c:
            return httpx.Response(404, json={})
        if parts[2:] == ["points", "scroll"]:
            pts = list(c["points"].values())
            return httpx.Response(200, json={"result": {"points": pts, "next_page_offset": None}})
        if parts[2] == "points" and request.method == "PUT":
            for p in body["points"]:
                c["points"][p["id"]] = p
            return httpx.Response(200, json={"result": True})
        return httpx.Response(400, json={})


class BundleTestCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(cs.GAME_NOSTR_PATH, ignore_errors=True)
        self.user = _make_account()
        self.q = FakeQdrant()
        self._client = httpx.Client
        transport = httpx.MockTransport(self.q.handler)
        ub.httpx.Client = lambda **kw: self._client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
        self.out = Path(tempfile.mkdtemp(dir=_TMP))

    def tearDown(self):
        ub.httpx.Client = self._client

    def _seed_old_station(self):
        udir = cs.ensure_ucloud_dir(EMAIL)
        (udir / "face_ref_sharing.json").write_text('{"enabled": false}')
        (udir / "blocked_senders.json").write_text('{"aa": 1}')
        (self.user / ".mailjet").write_text('{"email_channel": true}')
        (self.user / ".storage_quota").write_text("12345")
        (udir / "dav_token").write_text("secret-token")
        ud = self.user / "APP" / "uDRIVE"
        ud.mkdir(parents=True)
        (ud / "manifest.json").write_text('{"final_cid": "QmUdrive", "total_files": 3}')
        self.q.collections[MEM] = {"size": 4, "points": {
            1: {"id": 1, "vector": [0.1, 0.2, 0.3, 0.4], "payload": {"content": "aime le thé", "status": "active"}},
            2: {"id": 2, "vector": [0.4, 0.3, 0.2, 0.1], "payload": {"status": "deleted", "deleted_by": "user"}},
        }}


class TestExport(BundleTestCase):
    def test_memory_settings_udrive_exported_dav_token_never(self):
        self._seed_old_station()
        counts = ub.export_bundle(EMAIL, self.out)
        self.assertEqual(counts["memories"], 2)
        self.assertEqual(counts["settings"], 3)                # pas .storage_quota (décision du Capitaine)
        self.assertNotIn(".storage_quota", {f.name for f in self.out.rglob("*")})
        self.assertEqual(counts["udrive"], 1)
        self.assertEqual(len(json.loads((self.out / "qdrant_lifeos.json").read_text())), 2)
        self.assertEqual((self.out / "settings" / ".mailjet").read_text(), '{"email_channel": true}')
        self.assertEqual(json.loads((self.out / "uDRIVE_manifest.json").read_text())["final_cid"], "QmUdrive")
        names = {f.name for f in self.out.rglob("*")}
        self.assertNotIn("dav_token", names)
        self.assertNotIn("secret-token", "".join(f.read_text(errors="ignore") for f in self.out.rglob("*") if f.is_file()))

    def test_no_memory_collection_is_not_an_error(self):
        counts = ub.export_bundle(EMAIL, self.out)
        self.assertNotIn("memories", counts)
        self.assertFalse((self.out / "qdrant_lifeos.json").exists())


class TestApply(BundleTestCase):
    def _migrate(self, **kw):
        """Exporte depuis l'ancienne station, repart d'un compte NEUF (même HEX)."""
        self._seed_old_station()
        ub.export_bundle(EMAIL, self.out)
        old_points = self.q.collections[MEM]["points"]
        shutil.rmtree(cs.GAME_NOSTR_PATH)
        _make_account()
        self.q.collections.clear()
        return old_points, ub.apply_bundle(EMAIL, self.out, **kw)

    def test_memory_restored_under_same_collection_with_tombstone(self):
        old, counts = self._migrate()
        self.assertEqual(counts["memories"], 2)
        self.assertEqual(counts["errors"], [])
        new = self.q.collections[MEM]
        self.assertEqual(new["size"], 4)                      # dimension lue dans le paquet
        self.assertEqual(new["points"][2]["payload"]["status"], "deleted")  # souvenir oublié conservé
        self.assertEqual(new["points"][1]["vector"], old[1]["vector"])

    def test_restore_is_a_merge_never_overwrites_local_memory(self):
        self._seed_old_station()
        ub.export_bundle(EMAIL, self.out)
        self.q.collections[MEM]["points"][1]["payload"]["content"] = "modifié ici depuis"
        counts = ub.apply_bundle(EMAIL, self.out)
        self.assertEqual(counts["memories"], 0)               # tout était déjà là
        self.assertEqual(self.q.collections[MEM]["points"][1]["payload"]["content"], "modifié ici depuis")

    def test_dimension_mismatch_is_reported_never_mixed(self):
        self._seed_old_station()
        ub.export_bundle(EMAIL, self.out)
        self.q.collections.clear()
        self.q.collections[MEM] = {"size": 768, "points": {}}  # autre modèle d'embedding
        counts = ub.apply_bundle(EMAIL, self.out)
        self.assertEqual(counts["memories"], 0)
        self.assertEqual(len(counts["errors"]), 1)
        self.assertIn("768", counts["errors"][0])
        self.assertEqual(self.q.collections[MEM]["points"], {})

    def test_settings_merge_by_default_overwrite_on_migration(self):
        self._seed_old_station()
        ub.export_bundle(EMAIL, self.out)
        (self.user / ".mailjet").write_text('{"email_channel": false}')   # valeur par défaut d'un compte neuf
        ub.apply_bundle(EMAIL, self.out)
        self.assertEqual((self.user / ".mailjet").read_text(), '{"email_channel": false}')
        ub.apply_bundle(EMAIL, self.out, overwrite_settings=True)
        self.assertEqual((self.user / ".mailjet").read_text(), '{"email_channel": true}')

    def test_no_qdrant_flag_skips_memory_and_faces(self):
        _, _ = self._migrate(with_qdrant=False)
        self.assertNotIn(MEM, self.q.collections)

    def test_dav_token_not_restored(self):
        self._migrate()
        self.assertFalse((cs.ucloud_dir(EMAIL) / "dav_token").exists())


class TestPinUdrive(BundleTestCase):
    def test_manifest_without_cid_is_ignored(self):
        self.assertEqual(ub.pin_udrive(EMAIL), {})
        ud = self.user / "APP" / "uDRIVE"
        ud.mkdir(parents=True)
        (ud / "manifest.json").write_text("{}")
        self.assertEqual(ub.pin_udrive(EMAIL), {})


class TestAccountState(BundleTestCase):
    """account_state.tgz : l'état du compte voyage, jamais les secrets re-dérivables du QR."""

    SECRETS = [".secret.nostr", ".secret.disco", ".secret.dunikey", ".secret.ipns", ".secret.love", ".multipass.json",
               ".keyformula", ".ssss.head.player.enc", ".ssss.player.key", "._SSSSQR.png", ".nostr.zine.html",
               ".MULTIPASS.CARD.png", ".welcome.html", ".roaming", ".migration.json", ".migrating", ".refresh_time",
               ".nip42_auth_abc"]
    STATE = [".account_created", ".lastpayment", ".n2_genesis_minted", ".milestone_6months_notified", ".mailjet",
             ".forum.duniter.org.cookie", ".birth_place"]

    def _seed(self):
        for n in self.SECRETS + self.STATE + [".pass"]:
            (self.user / n).write_text("contenu " + n)
        (self.user / "identity").mkdir()
        (self.user / "identity" / ".Core.md").write_text("biographie")
        (self.user / "HEX").write_text("public")            # fichier non caché : publié par NOSTRNS, pas dans l'état
        (self.user / ".ucloud").mkdir(exist_ok=True)
        (self.user / ".ucloud" / "ucloud.db").write_text("x")
        os.symlink("/etc/passwd", self.user / ".lien")      # un lien n'est jamais suivi

    def names(self, out):
        with tarfile.open(out / "account_state.tgz") as t:
            return {m.name for m in t.getmembers()}

    def test_secrets_never_exported_state_is_(self):
        self._seed()
        ub.export_account_state(EMAIL, self.out)
        got = self.names(self.out)
        for n in self.STATE + ["identity/.Core.md"]:
            self.assertIn(n, got)
        for n in self.SECRETS + [".pass", "HEX", ".ucloud/ucloud.db", ".lien"]:
            self.assertNotIn(n, got, f"{n} ne doit pas être dans l'état du compte")

    def test_pass_travels_only_in_a_migration_bundle(self):
        self._seed()
        ub.export_account_state(EMAIL, self.out, for_migration=False)
        self.assertNotIn(".pass", self.names(self.out))
        out2 = Path(tempfile.mkdtemp(dir=_TMP))
        ub.export_account_state(EMAIL, out2, for_migration=True)
        self.assertIn(".pass", self.names(out2))
        for n in self.SECRETS:
            self.assertNotIn(n, self.names(out2))

    def _evil_tar(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, data in ((".secret.nostr", b"NSEC=evil"), (".keyformula", b"nostr=evil"), (".migrating", b"x"),
                               ("../evil", b"x"), ("/tmp/abs", b"x"), ("HEX", b"pirate"), (".lastpayment", b"ok"),
                               ("identity/.Core.md", b"bio")):
                ti = tarfile.TarInfo(name); ti.size = len(data); t.addfile(ti, io.BytesIO(data))
            ln = tarfile.TarInfo(".lien"); ln.type = tarfile.SYMTYPE; ln.linkname = "/etc/passwd"; t.addfile(ln)
        f = Path(tempfile.mkdtemp(dir=_TMP)) / "evil.tgz"
        f.write_bytes(buf.getvalue())
        return f

    def test_import_refuses_secrets_traversal_and_links_even_if_the_archive_has_them(self):
        shutil.rmtree(self.user)
        self.user = _make_account()
        for migration in (False, True):
            n = ub.import_account_state(EMAIL, self._evil_tar(), for_migration=migration)
            self.assertEqual(n, 2)                                   # .lastpayment + identity/.Core.md seulement
            self.assertEqual((self.user / ".lastpayment").read_text(), "ok")
            self.assertFalse((self.user / ".keyformula").exists())
            self.assertFalse((self.user / ".migrating").exists())
            self.assertNotIn("pirate", (self.user / "HEX").read_text() if (self.user / "HEX").exists() else "")
            self.assertFalse((_TMP / "evil").exists())
            self.assertFalse((self.user / ".lien").exists())
            self.assertEqual(stat.S_IMODE(os.stat(self.user / ".lastpayment").st_mode), 0o600)
            (self.user / ".lastpayment").unlink()

    def test_normal_restore_merges_migration_overwrites(self):
        self._seed()
        ub.export_account_state(EMAIL, self.out, for_migration=True)
        (self.user / ".lastpayment").write_text("valeur locale")
        tgz = self.out / "account_state.tgz"
        ub.import_account_state(EMAIL, tgz, for_migration=False)
        self.assertEqual((self.user / ".lastpayment").read_text(), "valeur locale")      # fusion
        ub.import_account_state(EMAIL, tgz, for_migration=True)
        self.assertEqual((self.user / ".lastpayment").read_text(), "contenu .lastpayment")  # l'état d'origine fait foi

    def test_oversized_state_is_an_error_not_a_silent_truncation(self):
        self._seed()
        old = ub.ACCOUNT_STATE_MAX_BYTES
        ub.ACCOUNT_STATE_MAX_BYTES = 10
        try:
            with self.assertRaises(RuntimeError):
                ub.export_account_state(EMAIL, self.out)
        finally:
            ub.ACCOUNT_STATE_MAX_BYTES = old

    def test_state_errors_are_reported_in_counts(self):
        self._seed()
        old = ub.ACCOUNT_STATE_MAX_BYTES
        ub.ACCOUNT_STATE_MAX_BYTES = 10
        try:
            counts = ub.export_bundle(EMAIL, self.out)
        finally:
            ub.ACCOUNT_STATE_MAX_BYTES = old
        self.assertTrue(any(e.startswith("state:") for e in counts["errors"]))


class TestNostrEvents(BundleTestCase):
    """Événements NOSTR du déménagement : strfry SIMULÉ par un petit script."""

    def setUp(self):
        super().setUp()
        self.sf = Path(tempfile.mkdtemp(dir=_TMP))
        (self.sf / "strfry").write_text(
            "#!/bin/bash\n"
            "if [[ \"$1\" == scan ]]; then\n"
            "  case \"$2\" in\n"
            "    *authors*) echo '{\"id\":\"a1\",\"kind\":1}'; echo '{\"id\":\"a2\",\"kind\":3}';;\n"
            "    *'#p'*) echo '{\"id\":\"d1\",\"kind\":1059}';;\n"
            "  esac\n"
            "elif [[ \"$1\" == import ]]; then cat > \"$STRFRY_IMPORTED\"; fi\n")
        (self.sf / "strfry").chmod(0o755)
        self._dir = ub.STRFRY_DIR
        ub.STRFRY_DIR = self.sf
        self.imported = self.sf / "imported.jsonl"
        os.environ["STRFRY_IMPORTED"] = str(self.imported)

    def tearDown(self):
        ub.STRFRY_DIR = self._dir
        os.environ.pop("STRFRY_IMPORTED", None)
        super().tearDown()

    def test_export_authored_and_received_private_messages_only_for_migration(self):
        counts = ub.export_bundle(EMAIL, self.out, for_migration=True)
        self.assertEqual(counts["nostr_events"], 3)
        lines = (self.out / "nostr_events.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], ["a1", "a2", "d1"])
        out2 = Path(tempfile.mkdtemp(dir=_TMP))
        counts2 = ub.export_bundle(EMAIL, out2)                      # sauvegarde quotidienne : pas d'événements
        self.assertNotIn("nostr_events", counts2)
        self.assertFalse((out2 / "nostr_events.jsonl").exists())

    def test_import_goes_through_strfry_import_with_verification(self):
        ub.export_bundle(EMAIL, self.out, for_migration=True)
        res = ub.apply_bundle(EMAIL, self.out, with_qdrant=False, for_migration=True)
        self.assertEqual(res["events"], 3)
        self.assertEqual(len(self.imported.read_text().splitlines()), 3)

    def test_missing_strfry_is_reported_never_silent(self):
        ub.STRFRY_DIR = Path(tempfile.mkdtemp(dir=_TMP))             # pas de binaire
        counts = ub.export_bundle(EMAIL, self.out, for_migration=True)
        self.assertTrue(any("strfry introuvable" in e for e in counts["errors"]))
        self.assertNotIn("nostr_events", counts)

    def test_import_failure_is_reported(self):
        ub.export_bundle(EMAIL, self.out, for_migration=True)
        ub.STRFRY_DIR = Path(tempfile.mkdtemp(dir=_TMP))
        res = ub.apply_bundle(EMAIL, self.out, with_qdrant=False, for_migration=True)
        self.assertTrue(any(e.startswith("nostr_events:") for e in res["errors"]))


class TestZenCardState(BundleTestCase):
    def setUp(self):
        super().setUp()
        shutil.rmtree(cs.GAME_NOSTR_PATH.parent / "players", ignore_errors=True)   # hors GAME_NOSTR_PATH : nettoyé ici
        Path("/tmp/abs-zc").unlink(missing_ok=True)

    def pdir(self):
        return cs.GAME_NOSTR_PATH.parent / "players" / EMAIL

    def seed(self, g1="g1CardA"):
        d = self.pdir()
        (d / "ipfs" / "moa").mkdir(parents=True, exist_ok=True)
        for name, val in ((".g1pub", g1), ("secret.june", "SALT=x"), ("secret.dunikey", "sec"),
                          ("ipfs/moa/index.html", "<html>TW</html>"), ("U.SOCIETY", "2026")):
            (d / name).write_text(val)
        os.symlink("/etc/passwd", d / "lien")

    def test_only_a_migration_bundle_carries_the_zencard(self):
        self.seed()
        counts = ub.export_bundle(EMAIL, self.out)                       # sauvegarde quotidienne
        self.assertNotIn("zencard_files", counts)
        self.assertFalse((self.out / "zencard_state.tgz").exists())      # clé de la ZenCard : JAMAIS en sauvegarde lisible
        out2 = Path(tempfile.mkdtemp(dir=_TMP))
        counts2 = ub.export_bundle(EMAIL, out2, for_migration=True)
        self.assertEqual(counts2["zencard_files"], 5)                    # le lien symbolique est ignoré
        with tarfile.open(out2 / "zencard_state.tgz") as t:
            self.assertNotIn("lien", {m.name for m in t.getmembers()})

    def test_no_zencard_is_not_an_error(self):
        counts = ub.export_bundle(EMAIL, self.out, for_migration=True)
        self.assertNotIn("zencard_files", counts)
        self.assertNotIn("errors", counts)

    def test_roundtrip_restores_content_modes_and_society_link(self):
        self.seed()
        ub.export_zencard_state(EMAIL, self.out)
        shutil.rmtree(self.pdir())
        n = ub.import_zencard_state(EMAIL, self.out / "zencard_state.tgz")
        self.assertEqual(n, 5)
        self.assertEqual((self.pdir() / "ipfs" / "moa" / "index.html").read_text(), "<html>TW</html>")
        self.assertEqual(stat.S_IMODE(os.stat(self.pdir() / "secret.june").st_mode), 0o600)
        link = self.user / "U.SOCIETY"
        self.assertTrue(link.is_symlink() and Path(os.readlink(link)) == self.pdir() / "U.SOCIETY")

    def test_same_card_is_updated_a_different_one_is_never_overwritten(self):
        self.seed("g1CardA")
        ub.export_zencard_state(EMAIL, self.out)
        (self.pdir() / "secret.june").write_text("SALT=modifié")
        ub.import_zencard_state(EMAIL, self.out / "zencard_state.tgz")             # même .g1pub : mise à jour
        self.assertEqual((self.pdir() / "secret.june").read_text(), "SALT=x")
        (self.pdir() / ".g1pub").write_text("g1AutreCarte")
        (self.pdir() / "secret.june").write_text("A_GARDER")
        with self.assertRaisesRegex(RuntimeError, "différente"):
            ub.import_zencard_state(EMAIL, self.out / "zencard_state.tgz")
        self.assertEqual((self.pdir() / "secret.june").read_text(), "A_GARDER")

    def test_crafted_archive_cannot_escape_the_folder(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, data in ((".g1pub", b"g1CardA"), ("../evil", b"x"), ("/tmp/abs-zc", b"x"), ("ok.txt", b"ok")):
                ti = tarfile.TarInfo(name); ti.size = len(data); t.addfile(ti, io.BytesIO(data))
        f = Path(tempfile.mkdtemp(dir=_TMP)) / "evil.tgz"
        f.write_bytes(buf.getvalue())
        self.assertEqual(ub.import_zencard_state(EMAIL, f), 2)           # .g1pub + ok.txt seulement
        self.assertFalse((self.pdir().parent / "evil").exists())
        self.assertFalse(Path("/tmp/abs-zc").exists())


class TestZenCardContent(BundleTestCase):
    """Archive de DÉPART : le CONTENU de la ZenCard (TiddlyWiki, chaîne de versions, historiques), jamais de secret."""
    G1 = "g1ZenCardPub"
    SECRETS = (".pass", "secret.june", "secret.dunikey", "secret.player", "ssh.pub", "U.SOCIETY", "U.SOCIETY.end",
               ".ZENCard.html", "AstroID.png", "ID.png", ".playerns", ".player", ".pseudo", "GPS.json", ".umap", ".g1pub",
               "QR.png")

    def setUp(self):
        super().setUp()
        shutil.rmtree(cs.GAME_NOSTR_PATH.parent / "players", ignore_errors=True)
        self.fresh_root = Path(tempfile.mkdtemp(dir=_TMP))
        self._fresh = ub.FRESH_ROOT
        ub.FRESH_ROOT = self.fresh_root

    def tearDown(self):
        ub.FRESH_ROOT = self._fresh
        super().tearDown()

    def pdir(self):
        return cs.GAME_NOSTR_PATH.parent / "players" / EMAIL

    def seed(self, tw="MON TIDDLYWIKI", g1=None):
        d = self.pdir()
        (d / "ipfs" / "moa").mkdir(parents=True, exist_ok=True)
        (d / "ipfs" / "G1SSB").mkdir(parents=True, exist_ok=True)
        (d / "G1PalPay").mkdir(exist_ok=True)
        files = {".g1pub": g1 or self.G1, "ipfs/G1SSB/_g1.pubkey": g1 or self.G1, "ipfs/moa/index.html": tw,
                 "ipfs/moa/.chain.20261001": "v1", "ipfs/moa/.chain.20261009": "v2", "ipfs/x.rss.json": "{}",
                 "G1PalPay/histo.json": "[1,2]"}
        for n in self.SECRETS:
            files.setdefault(n, "SECRET-" + n)
        for n, v in files.items():
            (d / n).write_text(v)
        os.symlink("/etc/passwd", d / "ipfs" / "lien")

    def names(self, out):
        with tarfile.open(out / "zencard_content.tgz") as t:
            return {m.name for m in t.getmembers()}

    def test_export_carries_the_content_and_never_a_secret(self):
        self.seed()
        self.assertEqual(ub.export_zencard_content(EMAIL, self.out), 6)
        got = self.names(self.out)
        for n in ("ipfs/moa/index.html", "ipfs/moa/.chain.20261001", "ipfs/moa/.chain.20261009", "ipfs/x.rss.json",
                  "G1PalPay/histo.json", "ipfs/G1SSB/_g1.pubkey"):
            self.assertIn(n, got)
        for n in self.SECRETS + ("ipfs/lien",):
            self.assertNotIn(n, got, f"{n} ne doit pas être dans l'archive de départ")
        blob = b"".join(open(self.out / "zencard_content.tgz", "rb"))
        import gzip
        self.assertNotIn(b"SECRET-", gzip.decompress(blob))

    TW_LINKS = ('<a href="https://vdo.copylaradio.com/?room=abc">v</a> <a href="https://vdo.copylaradio.com]addsuffix{!!wikiURL}]">w</a> '
                'https://ipfs.copylaradio.com :5001 https://ipfs.copylaradio.com/ipns/12D3KooW '
                'https://relay.copylaradio.com https://a.b.copylaradio.com/x https://copylaradio.com/ '
                'contact@copylaradio.com https://ipfs.copylaradio.com.evil.org https://notcopylaradio.com ')

    def tw(self):
        return (self.pdir() / "ipfs" / "moa" / "index.html").read_bytes().decode()

    def test_every_link_to_the_origin_domain_follows_the_station_zone(self):
        self.seed(tw=self.TW_LINKS)
        os.chmod(self.pdir() / "ipfs" / "moa" / "index.html", 0o640)
        (self.pdir() / "G1PalPay" / "histo.json").write_text('{"u":"https://relay.copylaradio.com/x"}')
        res = ub.rewrite_zencard_links(EMAIL, "Astroport.One")
        self.assertEqual((res["links"], res["files"]), (8, 2))
        t = self.tw()
        for want in ("https://vdo.astroport.one/?room=abc", "https://vdo.astroport.one]addsuffix{!!wikiURL}]",
                     "https://ipfs.astroport.one :5001", "https://ipfs.astroport.one/ipns/12D3KooW",
                     "https://relay.astroport.one", "https://a.b.astroport.one/x", "https://astroport.one/"):
            self.assertIn(want, t)
        for untouched in ("contact@copylaradio.com", "https://ipfs.copylaradio.com.evil.org", "https://notcopylaradio.com"):
            self.assertIn(untouched, t)
        self.assertIn("relay.astroport.one", (self.pdir() / "G1PalPay" / "histo.json").read_text())
        self.assertEqual((self.pdir() / "ipfs" / "moa" / "index.html").stat().st_mode & 0o7777, 0o640)
        self.assertEqual((self.pdir() / "ipfs" / "moa" / ".chain.20261001").read_text(), "v1")

    def test_links_rewrite_is_idempotent_and_refuses_a_bad_zone(self):
        self.seed(tw=self.TW_LINKS)
        ub.rewrite_zencard_links(EMAIL, "astroport.one")
        once = self.tw()
        self.assertEqual(ub.rewrite_zencard_links(EMAIL, "astroport.one")["links"], 0)
        self.assertEqual(self.tw(), once)
        for bad in ("", "a/b.fr", "x y.fr", "nodot", "evil.fr/../x", "-a.fr"):
            with self.assertRaises(ValueError):
                ub.rewrite_zencard_links(EMAIL, bad)
        self.assertEqual(self.tw(), once)

    def test_links_unchanged_when_zone_is_the_original_domain(self):
        self.seed(tw=self.TW_LINKS)
        ub.rewrite_zencard_links(EMAIL, "copylaradio.com")
        self.assertEqual(self.tw(), self.TW_LINKS)

    def test_import_rewrites_links_and_a_failure_never_loses_the_content(self):
        self.seed(tw=self.TW_LINKS)
        ub.export_zencard_content(EMAIL, self.out)
        res = ub.import_zencard_content(EMAIL, self.out / "zencard_content.tgz", "astroport.one")
        self.assertEqual(res["links_rewritten"]["links"], 7)
        self.assertIn("https://ipfs.astroport.one", self.tw())
        res = ub.import_zencard_content(EMAIL, self.out / "zencard_content.tgz", "pas une zone")
        self.assertIn("links_error", res)
        self.assertIn("https://ipfs.copylaradio.com", self.tw())      # contenu restauré, liens d'origine

    def test_no_zencard_means_nothing_exported(self):
        self.assertEqual(ub.export_zencard_content(EMAIL, self.out), 0)
        self.assertFalse((self.out / "zencard_content.tgz").exists())

    def test_oversized_content_is_an_error_not_a_truncation(self):
        self.seed()
        old = ub.ZENCARD_MAX_BYTES
        ub.ZENCARD_MAX_BYTES = 5
        try:
            with self.assertRaises(RuntimeError):
                ub.export_zencard_content(EMAIL, self.out)
        finally:
            ub.ZENCARD_MAX_BYTES = old

    def recreated_zencard(self, g1=None):
        """Ce que VISA.new.sh refait sur la nouvelle station : mêmes identifiants, TiddlyWiki NEUF et clés/PASS d'ici."""
        shutil.rmtree(self.pdir(), ignore_errors=True)
        self.seed(tw="TW NEUF PAR DÉFAUT", g1=g1)
        for n in self.SECRETS:
            if n not in (".g1pub",):
                (self.pdir() / n).write_text("NOUVEAU-" + n)

    def test_import_replaces_the_fresh_tiddlywiki_keeps_keys_and_pass_and_sets_the_fresh_aside(self):
        self.seed()
        ub.export_zencard_content(EMAIL, self.out)
        self.recreated_zencard()
        res = ub.import_zencard_content(EMAIL, self.out / "zencard_content.tgz")
        d = self.pdir()
        self.assertEqual((d / "ipfs" / "moa" / "index.html").read_text(), "MON TIDDLYWIKI")           # contenu restauré
        self.assertEqual((d / "ipfs" / "moa" / ".chain.20261009").read_text(), "v2")                   # chaîne de versions
        self.assertEqual((d / "G1PalPay" / "histo.json").read_text(), "[1,2]")
        for n in (".pass", "secret.june", "secret.dunikey", "U.SOCIETY"):                              # rien d'autre touché
            self.assertEqual((d / n).read_text(), "NOUVEAU-" + n, n)
        fresh = Path(res["fresh_moved_to"])
        self.assertEqual(fresh.parent, self.fresh_root)                                               # mis de côté, pas détruit
        self.assertEqual((fresh / "ipfs" / "moa" / "index.html").read_text(), "TW NEUF PAR DÉFAUT")
        self.assertEqual(stat.S_IMODE(os.stat(fresh).st_mode), 0o700)
        self.assertEqual([p.name for p in d.iterdir() if p.name.startswith(".zc_restore_")], [])      # aucun reste

    def test_the_content_of_another_zencard_is_refused_and_nothing_changes(self):
        self.seed()
        ub.export_zencard_content(EMAIL, self.out)
        self.recreated_zencard(g1="g1UneAutreCarte")
        with self.assertRaisesRegex(RuntimeError, "AUTRE ZenCard"):
            ub.import_zencard_content(EMAIL, self.out / "zencard_content.tgz")
        self.assertEqual((self.pdir() / "ipfs" / "moa" / "index.html").read_text(), "TW NEUF PAR DÉFAUT")

    def test_import_refused_without_zencard_or_without_owner_mark(self):
        self.seed()
        ub.export_zencard_content(EMAIL, self.out)
        shutil.rmtree(self.pdir())
        with self.assertRaisesRegex(RuntimeError, "aucune ZenCard"):
            ub.import_zencard_content(EMAIL, self.out / "zencard_content.tgz")
        self.recreated_zencard()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            ti = tarfile.TarInfo("ipfs/moa/index.html"); ti.size = 3; t.addfile(ti, io.BytesIO(b"abc"))
        f = Path(tempfile.mkdtemp(dir=_TMP)) / "sans-marqueur.tgz"
        f.write_bytes(buf.getvalue())
        with self.assertRaisesRegex(RuntimeError, "sans marqueur"):
            ub.import_zencard_content(EMAIL, f)
        self.assertEqual((self.pdir() / "ipfs" / "moa" / "index.html").read_text(), "TW NEUF PAR DÉFAUT")

    def test_a_crafted_archive_cannot_write_secrets_or_escape(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, data in (("ipfs/G1SSB/_g1.pubkey", self.G1.encode()), ("ipfs/moa/index.html", b"CONTENU"),
                               ("secret.june", b"PIRATE"), (".pass", b"0000"), ("U.SOCIETY", b"FAUX"),
                               ("../evil", b"x"), ("/tmp/abs-zc2", b"x"), ("ipfs/../../evil2", b"x")):
                ti = tarfile.TarInfo(name); ti.size = len(data); t.addfile(ti, io.BytesIO(data))
        f = Path(tempfile.mkdtemp(dir=_TMP)) / "piege.tgz"
        f.write_bytes(buf.getvalue())
        self.recreated_zencard()
        res = ub.import_zencard_content(EMAIL, f)
        self.assertEqual(res["files"], 2)
        d = self.pdir()
        self.assertEqual((d / "ipfs" / "moa" / "index.html").read_text(), "CONTENU")
        self.assertEqual((d / "secret.june").read_text(), "NOUVEAU-secret.june")                      # jamais écrasés
        self.assertEqual((d / ".pass").read_text(), "NOUVEAU-.pass")
        self.assertEqual((d / "U.SOCIETY").read_text(), "NOUVEAU-U.SOCIETY")
        self.assertFalse((d.parent / "evil").exists() or (d.parent.parent / "evil").exists())
        self.assertFalse(Path("/tmp/abs-zc2").exists())

    def test_a_corrupt_archive_leaves_the_existing_content_untouched(self):
        self.seed()
        ub.export_zencard_content(EMAIL, self.out)
        data = (self.out / "zencard_content.tgz").read_bytes()
        bad = Path(tempfile.mkdtemp(dir=_TMP)) / "tronquee.tgz"
        bad.write_bytes(data[: len(data) // 2])
        self.recreated_zencard()
        with self.assertRaises(Exception):
            ub.import_zencard_content(EMAIL, bad)
        self.assertEqual((self.pdir() / "ipfs" / "moa" / "index.html").read_text(), "TW NEUF PAR DÉFAUT")
        self.assertEqual([p.name for p in self.pdir().iterdir() if p.name.startswith(".zc_restore_")], [])

    def test_the_real_cli_exports_then_imports(self):
        import subprocess
        self.seed()
        env = dict(os.environ, GAME_PATH=str(cs.GAME_NOSTR_PATH.parent), ZENCARD_FRESH_ROOT=str(self.fresh_root))
        script = str(Path(ub.__file__).resolve())
        r = subprocess.run([sys.executable, script, "zencard-export", EMAIL, str(self.out)], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((r.returncode, json.loads(r.stdout.strip().splitlines()[-1])), (0, {"files": 6}), r.stderr)
        self.recreated_zencard()
        r = subprocess.run([sys.executable, script, "zencard-import", EMAIL, str(self.out / "zencard_content.tgz")],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.pdir() / "ipfs" / "moa" / "index.html").read_text(), "MON TIDDLYWIKI")
        r = subprocess.run([sys.executable, script, "zencard-import", "inconnu@example.com", str(self.out / "zencard_content.tgz")],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(r.returncode, 0)                                                         # MULTIPASS absent : refusé


if __name__ == "__main__":
    unittest.main()
