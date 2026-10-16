"""
tests/test_cloud_storage.py — Cloud personnel chiffré (services/cloud_storage.py).

Repris des tests du démon `ucloudd` (test_index.py / test_uenc_roundtrip.py /
test_dav_smoke.py) après l'intégration dans UPassport. Trois familles :

  * persistance  — index/keyring : atomicité, 0600, flock, arborescence,
                   purge des clés orphelines, schéma de partage NIP-17 ;
  * contrat UENC — roundtrip, InvalidTag sur clé fausse / blob altéré, IV uniques ;
  * pile DAV     — PUT/GET/COPY/MOVE/DELETE avec IPFS MOCKÉ mais chiffrement
                   RÉEL : le test central vérifie que le blob « stocké » est
                   opaque, donc que le chiffrement a bien lieu AVANT l'upload.

`GAME_NOSTR_PATH` et `CACHE_DIR` sont redirigés vers un répertoire temporaire :
aucun ~/.zen/game/nostr réel n'est touché.

    pytest tests/test_cloud_storage.py -v
"""

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from services import cloud_storage as cs

EMAIL = "dav@example.com"
TOKEN = "f" * 64
HEXPUB = "ab" * 32
PLAINTEXT = "MARQUEUR-CLAIR-ucloud — contenu de test accentué\n".encode("utf-8") * 4

_TMP = Path(tempfile.mkdtemp(prefix="upassport-cloud-test-"))

# Redirection AVANT toute écriture : les helpers de cloud_storage lisent ces
# globals à chaque appel, un monkeypatch de module suffit donc.
cs.GAME_NOSTR_PATH = _TMP / "nostr"
cs.CACHE_DIR = _TMP / "cache"


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════════
# Persistance : chemins, index, keyring, verrou
# ═════════════════════════════════════════════════════════════════════════════
class TestPathHelpers(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(cs.normalize_path(""), "/")
        self.assertEqual(cs.normalize_path("/"), "/")
        self.assertEqual(cs.normalize_path("a/b"), "/a/b")
        self.assertEqual(cs.normalize_path("/a//b/"), "/a/b")
        self.assertEqual(cs.normalize_path("/a/./b"), "/a/b")

    def test_normalize_rejects_traversal(self):
        """Une traversée doit lever, jamais résoudre silencieusement."""
        for bad in ("/a/../../etc/passwd", "../secret", "/a/.."):
            with self.assertRaises(ValueError):
                cs.normalize_path(bad)

    def test_parent_and_basename(self):
        self.assertEqual(cs.parent_path("/a/b/c.txt"), "/a/b")
        self.assertEqual(cs.parent_path("/a"), "/")
        self.assertEqual(cs.parent_path("/"), "/")
        self.assertEqual(cs.base_name("/a/b/c.txt"), "c.txt")
        self.assertEqual(cs.base_name("/"), "")


class TestIndexIO(unittest.TestCase):
    def setUp(self):
        self.email = f"io-{time.time_ns()}@example.com"

    def test_load_missing_returns_empty(self):
        idx = cs.load_index(self.email)
        self.assertEqual(idx["entries"], {})
        self.assertEqual(idx["version"], cs.INDEX_VERSION)

    def test_roundtrip_and_permissions(self):
        idx = cs.empty_index("deadbeef")
        idx["entries"]["/a.txt"] = {"type": "file", "cid": "Qm1", "size_plain": 3}
        cs.save_index(self.email, idx)

        back = cs.load_index(self.email)
        self.assertEqual(back["entries"]["/a.txt"]["cid"], "Qm1")
        self.assertEqual(back["owner_hex"], "deadbeef")
        db = cs.ucloud_dir(self.email) / "ucloud.db"
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        for side in ("ucloud.db-wal", "ucloud.db-shm"):
            if (cs.ucloud_dir(self.email) / side).exists():
                self.assertEqual((cs.ucloud_dir(self.email) / side).stat().st_mode & 0o777, 0o600)

    def test_keyring_separate_table_and_export(self):
        """Le keyring est une TABLE DISTINCTE : l'index complet n'expose aucune clé
        (et l'export JSON de l'index non plus)."""
        cs.save_index(self.email, cs.empty_index())
        cs.save_keyring(self.email, {"Qm1": {"key_hex": "ab" * 32}})
        idx_json = json.dumps(cs.load_index(self.email))
        self.assertNotIn("key_hex", idx_json)
        self.assertNotIn("ab" * 32, idx_json)
        self.assertEqual(cs.get_key_hex(self.email, "Qm1"), "ab" * 32)
        self.assertIsNone(cs.get_key_hex(self.email, "Qm-absent"))
        out = _TMP / f"export-{time.time_ns()}"
        res = cs.ucloud_db.export_json(cs.ucloud_dir(self.email), out)
        self.assertEqual(res["keys"], 1)
        self.assertNotIn("key_hex", (out / "index.json").read_text())
        self.assertEqual((out / "keyring.json").stat().st_mode & 0o777, 0o600)

    def test_migration_from_legacy_json(self):
        """Un cloud existant (index.json + keyring.json) est importé au premier accès,
        dossiers implicites matérialisés ; les anciens fichiers sont conservés renommés."""
        d = cs.ensure_ucloud_dir(self.email)
        (d / "index.json").write_text(json.dumps({"version": 1, "owner_hex": "ab" * 32, "entries": {
            "/a/b/c.txt": {"type": "file", "cid": "Qm1", "size_plain": 5, "mime": "text/plain",
                           "faceid": {"status": "faces"}}}}))
        (d / "keyring.json").write_text(json.dumps({"Qm1": {"key_hex": "cd" * 32}}))
        idx = cs.load_index(self.email)
        self.assertEqual(idx["owner_hex"], "ab" * 32)
        self.assertEqual(sorted(idx["entries"]), ["/a", "/a/b", "/a/b/c.txt"])
        self.assertEqual(cs.get_key_hex(self.email, "Qm1"), "cd" * 32)
        self.assertEqual(cs.db_children(self.email, "/a"), ["b"])
        self.assertEqual(cs.ucloud_db.stats(d)["faceid"], {})   # pas une image
        self.assertFalse((d / "index.json").exists())
        self.assertTrue(list(d.glob("index.json.migrated-*")))

    def test_atomic_write_leaves_no_tmp(self):
        for i in range(5):
            idx = cs.load_index(self.email)
            idx["entries"][f"/f{i}.txt"] = {"type": "file", "cid": f"Qm{i}"}
            cs.save_index(self.email, idx)
        leftovers = [p.name for p in cs.ucloud_dir(self.email).iterdir() if ".tmp." in p.name]
        self.assertEqual(leftovers, [], "aucun fichier temporaire ne doit subsister")

    def test_corrupt_index_raises_loudly(self):
        """Un index illisible doit lever, pas retourner un index vide en
        silence (ce qui masquerait une perte de données)."""
        cs.ensure_ucloud_dir(self.email)
        (cs.ucloud_dir(self.email) / "index.json").write_text("{ ceci n'est pas du json")
        with self.assertRaises(RuntimeError):
            cs.load_index(self.email)


class TestTree(unittest.TestCase):
    def setUp(self):
        self.idx = cs.empty_index()
        for p in ("/Documents/rapport.pdf", "/Documents/notes/a.txt", "/photo.jpg"):
            self.idx["entries"][p] = {"type": "file", "cid": "Qm"}

    def test_list_children_root(self):
        self.assertEqual(cs.list_children(self.idx, "/"), ["Documents", "photo.jpg"])

    def test_list_children_implicit_dir(self):
        """Un dossier jamais créé par MKCOL doit tout de même être listé."""
        self.assertEqual(cs.list_children(self.idx, "/Documents"), ["notes", "rapport.pdf"])
        self.assertEqual(cs.list_children(self.idx, "/Documents/notes"), ["a.txt"])

    def test_ensure_parent_dirs(self):
        idx = cs.empty_index()
        cs.ensure_parent_dirs(idx, "/a/b/c/d.txt")
        self.assertEqual(sorted(idx["entries"]), ["/a", "/a/b", "/a/b/c"])
        self.assertTrue(all(e["type"] == "dir" for e in idx["entries"].values()))

    def test_ensure_parent_dirs_conflict(self):
        idx = cs.empty_index()
        idx["entries"]["/a"] = {"type": "file", "cid": "Qm"}
        with self.assertRaises(ValueError):
            cs.ensure_parent_dirs(idx, "/a/b.txt")

    def test_remove_subtree(self):
        removed = cs.remove_subtree(self.idx, "/Documents")
        self.assertEqual(len(removed), 2)
        self.assertEqual(sorted(self.idx["entries"]), ["/photo.jpg"])

    def test_prune_keyring_drops_orphans(self):
        idx = cs.empty_index()
        idx["entries"]["/a.txt"] = {"type": "file", "cid": "QmLIVE"}
        keyring = {"QmLIVE": {"key_hex": "11" * 32}, "QmDEAD": {"key_hex": "22" * 32}}
        pruned = cs.prune_keyring(idx, keyring)
        self.assertIn("QmLIVE", pruned)
        self.assertNotIn("QmDEAD", pruned, "une clé orpheline doit être détruite")

    def test_prune_keyring_keeps_shared_cid(self):
        """Une COPY partage le CID : la clé survit à la suppression d'une
        seule des deux entrées."""
        idx = cs.empty_index()
        idx["entries"]["/a.txt"] = {"type": "file", "cid": "QmS"}
        idx["entries"]["/b.txt"] = {"type": "file", "cid": "QmS"}
        cs.remove_subtree(idx, "/a.txt")
        self.assertIn("QmS", cs.prune_keyring(idx, {"QmS": {"key_hex": "3" * 64}}))


class TestLock(unittest.TestCase):
    def test_mutual_exclusion(self):
        email = f"lock-{time.time_ns()}@example.com"
        order = []

        def worker(tag, hold):
            with cs.index_lock(email, timeout=10):
                order.append(f"{tag}-in")
                time.sleep(hold)
                order.append(f"{tag}-out")

        t1 = threading.Thread(target=worker, args=("A", 0.3))
        t2 = threading.Thread(target=worker, args=("B", 0.0))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join()
        t2.join()
        # Aucune imbrication : chaque section critique se ferme avant la suivante.
        self.assertEqual(order, ["A-in", "A-out", "B-in", "B-out"])

    def test_lock_file_mode(self):
        email = f"lockmode-{time.time_ns()}@example.com"
        with cs.index_lock(email):
            pass
        self.assertEqual(cs.lock_path(email).stat().st_mode & 0o777, 0o600)


class TestSharedEntrySchema(unittest.TestCase):
    """Le schéma d'index accueille un partage NIP-17 sans refonte."""

    def test_nip17_share_entry_survives_roundtrip(self):
        email = f"share-{time.time_ns()}@example.com"
        idx = cs.empty_index()
        idx["entries"]["/Shared/npub1abc/photo.jpg"] = {
            "type": "file",
            "origin": "nip17-share",
            "from_npub": "npub1abc",
            "cid": "QmShared",
            "enc": "uenc-aes256gcm",
            "iv_hex": "00" * 12,
            "x_hash": "aa" * 32,
            "ox_hash": "bb" * 32,
            "mime": "image/jpeg",
            "size_plain": None,
            "received_at": 1234567999,
            "readonly": True,
        }
        cs.save_index(email, idx)
        back = cs.load_index(email)
        entry = back["entries"]["/Shared/npub1abc/photo.jpg"]
        # Le payload NIP-17 {cid, decryption-key, decryption-nonce, file-type,
        # x, ox} trouve sa place : clé → keyring, nonce → iv_hex, x/ox →
        # x_hash/ox_hash, file-type → mime.
        for field in ("origin", "from_npub", "x_hash", "ox_hash", "readonly"):
            self.assertIn(field, entry)
        self.assertIsNone(entry["size_plain"])
        self.assertEqual(cs.list_children(back, "/"), ["Shared"])
        self.assertEqual(cs.list_children(back, "/Shared"), ["npub1abc"])


# ═════════════════════════════════════════════════════════════════════════════
# Contrat UENC (Astroport.ONE/tools/uenc_codec.py, importé et non vendorisé)
# ═════════════════════════════════════════════════════════════════════════════
class TestUencContract(unittest.TestCase):
    def test_roundtrip(self):
        key = os.urandom(32).hex()
        payload, iv_hex = cs.uenc_codec.encrypt_aes256gcm(PLAINTEXT, key)
        self.assertEqual(payload[:4], b"UENC", "en-tête magique du format UENC")
        self.assertNotIn(b"MARQUEUR-CLAIR-ucloud", payload)
        self.assertEqual(len(iv_hex), 24, "IV de 12 octets")
        self.assertEqual(cs.uenc_codec.decrypt_aes256gcm(payload, key), PLAINTEXT)

    def test_wrong_key_raises(self):
        key = os.urandom(32).hex()
        payload, _ = cs.uenc_codec.encrypt_aes256gcm(PLAINTEXT, key)
        with self.assertRaises(Exception):
            cs.uenc_codec.decrypt_aes256gcm(payload, "00" * 32)

    def test_tampered_blob_raises(self):
        key = os.urandom(32).hex()
        payload, _ = cs.uenc_codec.encrypt_aes256gcm(PLAINTEXT, key)
        tampered = bytearray(payload)
        tampered[-1] ^= 0xFF
        with self.assertRaises(Exception):
            cs.uenc_codec.decrypt_aes256gcm(bytes(tampered), key)

    def test_iv_is_unique_per_encryption(self):
        key = os.urandom(32).hex()
        ivs = {cs.uenc_codec.encrypt_aes256gcm(PLAINTEXT, key)[1] for _ in range(8)}
        self.assertEqual(len(ivs), 8, "un IV ne doit jamais être réutilisé")


# ═════════════════════════════════════════════════════════════════════════════
# Pile DAV complète — IPFS mocké, chiffrement réel
# ═════════════════════════════════════════════════════════════════════════════
class FakeIPFS:
    """Stockage en mémoire imitant l'API `add`/`cat` du daemon local."""

    def __init__(self):
        self.blobs = {}

    def add_bytes(self, payload, filename="blob.uenc", pin=True):
        import hashlib

        cid = "Qm" + hashlib.sha256(payload).hexdigest()[:44]
        self.blobs[cid] = payload
        return cid

    def cat(self, cid, max_bytes=None):
        if cid not in self.blobs:
            raise cs.IPFSError(f"ERROR:ucloud:cid inconnu {cid}")
        return self.blobs[cid]


def call_wsgi(app, method, path, body=b"", headers=None, auth=(EMAIL, TOKEN)):
    """Invoque une application WSGI et retourne (status_int, headers, body)."""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        # SCRIPT_NAME reproduit le montage `app.mount("/dav", …)` : a2wsgi pose
        # root_path='/dav' et PATH_INFO = le reste. On vérifie donc aussi que
        # les href générés par wsgidav restent corrects sous le préfixe.
        "SCRIPT_NAME": "/dav",
        "QUERY_STRING": "",
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "54321",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": str(len(body)),
        # Requis par wsgidav pour COPY/MOVE (comparaison d'hôte du Destination).
        "HTTP_HOST": "127.0.0.1:54321",
    }
    if auth:
        raw = f"{auth[0]}:{auth[1]}".encode()
        environ["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(raw).decode()
    for k, v in (headers or {}).items():
        environ["HTTP_" + k.upper().replace("-", "_")] = v

    captured = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = response_headers

    chunks = app(environ, start_response)
    out = b"".join(chunks)
    if hasattr(chunks, "close"):
        chunks.close()
    return int(captured["status"].split()[0]), captured.get("headers", []), out


class TestDavSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from wsgidav.wsgidav_app import WsgiDAVApp

        # MULTIPASS de test + token DAV (0600) dans le tmpdir.
        nostr_dir = cs.nostr_dir(EMAIL)
        nostr_dir.mkdir(parents=True, exist_ok=True)
        (nostr_dir / ".secret.nostr").write_text(
            f"NSEC=nsec1x; NPUB=npub1x; HEX={HEXPUB};\n"
        )
        cs.ensure_ucloud_dir(EMAIL)
        cs.token_path(EMAIL).write_text(
            json.dumps({"token": TOKEN, "hex": HEXPUB, "created_at": 1})
        )
        os.chmod(cs.token_path(EMAIL), 0o600)

        # IPFS mocké — le chiffrement, lui, reste réel.
        cls.fake = FakeIPFS()
        cls._real_add, cls._real_cat = cs.ipfs_add_bytes, cs.ipfs_cat
        cs.ipfs_add_bytes = cls.fake.add_bytes
        cs.ipfs_cat = cls.fake.cat

        cs.ensure_cache_dir()
        cls.app = WsgiDAVApp(cs.build_wsgi_dav_config())

    @classmethod
    def tearDownClass(cls):
        cs.ipfs_add_bytes, cs.ipfs_cat = cls._real_add, cls._real_cat

    # ── Authentification ─────────────────────────────────────────────────────
    def test_01_unauthenticated_is_401(self):
        status, _, _ = call_wsgi(self.app, "PROPFIND", "/", auth=None)
        self.assertEqual(status, 401)

    def test_02_bad_token_is_401(self):
        status, _, _ = call_wsgi(self.app, "PROPFIND", "/", auth=(EMAIL, "mauvais"))
        self.assertEqual(status, 401)

    def test_03_propfind_root(self):
        status, _, body = call_wsgi(self.app, "PROPFIND", "/", headers={"Depth": "1"})
        self.assertEqual(status, 207)
        self.assertIn(b"multistatus", body)
        self.assertIn(b"/dav/", body, "les href doivent porter le préfixe de montage")

    # ── PUT / GET ────────────────────────────────────────────────────────────
    def test_04_mkcol_then_put_then_get(self):
        status, _, _ = call_wsgi(self.app, "MKCOL", "/Documents")
        self.assertEqual(status, 201)

        status, _, _ = call_wsgi(self.app, "PUT", "/Documents/f.txt", PLAINTEXT)
        self.assertEqual(status, 201, "création → 201")

        status, _, body = call_wsgi(self.app, "GET", "/Documents/f.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, PLAINTEXT, "round-trip GET doit être intègre")

        status, _, _ = call_wsgi(self.app, "PUT", "/Documents/f.txt", PLAINTEXT)
        self.assertEqual(status, 204, "mise à jour → 204")

    def test_05_blob_stored_is_encrypted(self):
        """LE test central : ce qui part vers IPFS est opaque."""
        self.assertTrue(self.fake.blobs, "un blob doit avoir été stocké")
        for cid, payload in self.fake.blobs.items():
            self.assertEqual(payload[:4], b"UENC", f"{cid} doit être au format UENC")
            self.assertNotIn(
                b"MARQUEUR-CLAIR-ucloud",
                payload,
                "le clair NE DOIT PAS apparaître dans le blob poussé sur IPFS",
            )

    def test_06_index_and_keyring_are_split(self):
        idx = cs.load_index(EMAIL)
        entry = idx["entries"]["/Documents/f.txt"]
        self.assertEqual(entry["type"], "file")
        self.assertEqual(entry["enc"], "uenc-aes256gcm")
        self.assertEqual(entry["size_plain"], len(PLAINTEXT))
        self.assertTrue(entry["cid"])
        self.assertNotIn("key_hex", entry, "aucune clé ne doit vivre dans l'index")

        key_hex = cs.get_key_hex(EMAIL, entry["cid"])
        self.assertEqual(len(key_hex), 64)
        self.assertEqual((cs.ucloud_dir(EMAIL) / "ucloud.db").stat().st_mode & 0o777, 0o600)

    def test_07_wrong_key_gives_500_not_plaintext(self):
        """Clé corrompue → 500, et surtout aucun clair partiel dans le corps."""
        idx = cs.load_index(EMAIL)
        cid = idx["entries"]["/Documents/f.txt"]["cid"]
        good = cs.load_keyring(EMAIL)
        cs.save_keyring(EMAIL, {cid: {"key_hex": "00" * 32}})
        try:
            status, _, body = call_wsgi(self.app, "GET", "/Documents/f.txt")
            self.assertEqual(status, 500)
            self.assertNotIn(b"MARQUEUR-CLAIR-ucloud", body)
        finally:
            cs.save_keyring(EMAIL, good)

    def test_08_missing_key_gives_500(self):
        good = cs.load_keyring(EMAIL)
        cs.save_keyring(EMAIL, {})
        try:
            status, _, body = call_wsgi(self.app, "GET", "/Documents/f.txt")
            self.assertEqual(status, 500)
            self.assertNotIn(b"MARQUEUR-CLAIR-ucloud", body)
        finally:
            cs.save_keyring(EMAIL, good)

    # ── Verbes DAV ───────────────────────────────────────────────────────────
    def test_09_copy_move_delete(self):
        status, _, _ = call_wsgi(
            self.app,
            "COPY",
            "/Documents/f.txt",
            headers={"Destination": "http://127.0.0.1:54321/dav/Documents/copie.txt"},
        )
        self.assertEqual(status, 201)

        status, _, body = call_wsgi(self.app, "GET", "/Documents/copie.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, PLAINTEXT, "COPY partage le CID et la clé")

        status, _, _ = call_wsgi(self.app, "DELETE", "/Documents/copie.txt")
        self.assertEqual(status, 204)
        status, _, _ = call_wsgi(self.app, "GET", "/Documents/copie.txt")
        self.assertEqual(status, 404)

    def test_10_readonly_share_rejects_writes(self):
        """Une entrée reçue par partage NIP-17 est en lecture seule."""
        idx = cs.load_index(EMAIL)
        src = idx["entries"]["/Documents/f.txt"]
        idx["entries"]["/Shared/npub1abc/photo.jpg"] = {
            "type": "file",
            "origin": "nip17-share",
            "from_npub": "npub1abc",
            "cid": src["cid"],
            "enc": "uenc-aes256gcm",
            "iv_hex": src["iv_hex"],
            "x_hash": "aa" * 32,
            "ox_hash": "bb" * 32,
            "mime": "image/jpeg",
            "size_plain": None,
            "received_at": 1234567999,
            "readonly": True,
        }
        cs.save_index(EMAIL, idx)

        status, _, _ = call_wsgi(self.app, "GET", "/Shared/npub1abc/photo.jpg")
        self.assertEqual(status, 200, "un partage reste lisible")
        status, _, _ = call_wsgi(self.app, "PUT", "/Shared/npub1abc/photo.jpg", b"x")
        self.assertEqual(status, 403, "un partage n'est pas modifiable")
        status, _, _ = call_wsgi(self.app, "DELETE", "/Shared/npub1abc/photo.jpg")
        self.assertEqual(status, 403)

    def test_11_size_limit_enforced(self):
        oversized = b"A" * (cs.MAX_FILE_SIZE + 1024)
        status, _, _ = call_wsgi(self.app, "PUT", "/Documents/big.bin", oversized)
        self.assertEqual(status, 403)
        self.assertNotIn(
            "/Documents/big.bin",
            cs.load_index(EMAIL)["entries"],
            "le placeholder doit être annulé (rollback end_write)",
        )

    def test_12_cache_is_purged_after_get(self):
        """Aucun clair ne doit subsister sur disque après la réponse."""
        call_wsgi(self.app, "GET", "/Documents/f.txt")
        leftovers = (
            [p for p in cs.CACHE_DIR.iterdir() if p.name.startswith("ucloud-")]
            if cs.CACHE_DIR.exists()
            else []
        )
        self.assertEqual(leftovers, [], "le cache éphémère doit être vide")

    def test_13_path_traversal_rejected(self):
        status, _, _ = call_wsgi(self.app, "GET", "/Documents/../../../etc/passwd")
        self.assertIn(status, (400, 403, 404))

    def test_14_multi_user_isolation(self):
        """Un autre MULTIPASS ne voit jamais l'arborescence du premier : la
        racine DAV est résolue depuis l'email authentifié, pas depuis le chemin."""
        other = "autre@example.com"
        other_token = "e" * 64
        d = cs.nostr_dir(other)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".secret.nostr").write_text(f"NSEC=nsec1y; NPUB=npub1y; HEX={'cd' * 32};\n")
        cs.ensure_ucloud_dir(other)
        cs.token_path(other).write_text(
            json.dumps({"token": other_token, "hex": "cd" * 32, "created_at": 1})
        )

        status, _, _ = call_wsgi(
            self.app, "GET", "/Documents/f.txt", auth=(other, other_token)
        )
        self.assertEqual(status, 404, "le fichier d'un autre MULTIPASS est invisible")

    def test_15_token_helpers(self):
        """create/load/revoke : le token est un secret 256 bits en 0600."""
        email = f"tok-{time.time_ns()}@example.com"
        token = cs.create_dav_token(email, HEXPUB)
        self.assertEqual(len(token), 64)
        self.assertEqual(cs.token_path(email).stat().st_mode & 0o777, 0o600)
        self.assertEqual(cs.verify_basic_credentials(email, token), email)
        self.assertIsNone(cs.verify_basic_credentials(email, "mauvais"))
        # Un nom d'utilisateur qui est un chemin ne doit jamais être résolu.
        self.assertIsNone(cs.verify_basic_credentials("../../etc", token))
        self.assertTrue(cs.revoke_dav_token(email))
        self.assertIsNone(cs.verify_basic_credentials(email, token))



# ═════════════════════════════════════════════════════════════════════════════
# Partage FaceCloud — share_copy (copie rechiffrée vers un autre MULTIPASS)
# ═════════════════════════════════════════════════════════════════════════════
class TestShareCopy(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIPFS()
        self._real = (cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis)
        cs.ipfs_add_bytes = self.fake.add_bytes
        cs.ipfs_cat = self.fake.cat
        self.faceid_calls = []
        cs._trigger_faceid_analysis = lambda *a, **k: self.faceid_calls.append(a)
        stamp = time.time_ns()
        self.owner = f"owner-{stamp}@example.com"
        self.friend = f"friend-{stamp}@example.com"

    def tearDown(self):
        cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis = self._real

    def test_copy_shares_the_same_encrypted_block_idempotent_and_silent(self):
        cs.ingest_plaintext(self.owner, "/Photos/a.txt", b"photo-claire", content_type="text/plain")
        src = cs.get_entry(cs.load_index(self.owner), "/Photos/a.txt")

        res = cs.share_copy(self.owner, "/Photos/a.txt", self.friend, "ab" * 32, "fred")
        self.assertEqual(res["status"], "copied")
        self.assertTrue(res["path"].startswith("/Reçues/fred/"))
        dst = cs.get_entry(cs.load_index(self.friend), res["path"])
        self.assertEqual(dst["cid"], src["cid"], "secret unifié : MÊME CID, un seul bloc sur IPFS")
        self.assertEqual(cs.get_key_hex(self.friend, dst["cid"]), cs.get_key_hex(self.owner, src["cid"]))
        self.assertEqual(len(self.fake.blobs), 1, "aucun nouveau blob n'a été créé pour la copie")
        self.assertEqual(dst["origin"], "facecloud-share")
        self.assertNotIn("geo", dst)
        self.assertEqual(cs.read_plaintext(self.friend, res["path"])[0], b"photo-claire")

        again = cs.share_copy(self.owner, "/Photos/a.txt", self.friend, "ab" * 32, "fred")
        self.assertEqual(again, {"status": "duplicate", "path": res["path"]})

    def test_shared_cid_stays_pinned_until_nobody_references_it(self):
        unpinned = []
        real = cs.ipfs_unpin
        cs.ipfs_unpin = lambda cid: unpinned.append(cid) or True
        try:
            cs.ingest_plaintext(self.owner, "/Photos/s.txt", b"partage", content_type="text/plain")
            cid = cs.get_entry(cs.load_index(self.owner), "/Photos/s.txt")["cid"]
            path = cs.share_copy(self.owner, "/Photos/s.txt", self.friend, "ab" * 32, "fred")["path"]
            cs.delete_paths(self.owner, ["/Photos/s.txt"])
            self.assertNotIn(cid, unpinned, "le destinataire référence encore ce CID : l'épinglage reste")
            self.assertEqual(cs.read_plaintext(self.friend, path)[0], b"partage", "sa copie reste lisible")
            cs.delete_paths(self.friend, [path])
            self.assertIn(cid, unpinned, "plus personne ne le référence : dépinglé")
            self.assertIsNone(cs.get_key_hex(self.friend, cid))
        finally:
            cs.ipfs_unpin = real

    def test_received_image_never_triggers_faceid(self):
        cs.ingest_plaintext(self.owner, "/Photos/b.jpg", b"\xff\xd8pas-une-vraie-image",
                            content_type="image/jpeg")
        self.faceid_calls.clear()
        cs.share_copy(self.owner, "/Photos/b.jpg", self.friend, "cd" * 32, "fred")
        self.assertEqual(self.faceid_calls, [],
                         "une copie reçue analysée renverrait la photo à l'expéditeur (ping-pong)")

    def test_same_name_different_content_is_not_overwritten(self):
        cs.ingest_plaintext(self.owner, "/A/x.txt", b"un", content_type="text/plain")
        cs.ingest_plaintext(self.owner, "/B/x.txt", b"deux", content_type="text/plain")
        p1 = cs.share_copy(self.owner, "/A/x.txt", self.friend, "ef" * 32, "fred")["path"]
        p2 = cs.share_copy(self.owner, "/B/x.txt", self.friend, "ef" * 32, "fred")["path"]
        self.assertNotEqual(p1, p2)
        self.assertTrue(p2.endswith("/x-2.txt"))


# ═════════════════════════════════════════════════════════════════════════════
# Durcissement (audit 2026-10-13) : ingestion, verrous, NIP-98, client WebDAV
# ═════════════════════════════════════════════════════════════════════════════
def _jpeg(orientation=1, gps=True, icc=None, size=(60, 40)):
    from PIL import Image
    im = Image.new("RGB", size, (200, 30, 30))
    ex = im.getexif()
    if orientation != 1:
        ex[0x0112] = orientation
    if gps:
        from PIL import ExifTags
        g = ex.get_ifd(ExifTags.IFD.GPSInfo)
        g[1], g[2], g[3], g[4] = "N", (48.0, 51.0, 0.0), "E", (2.0, 21.0, 0.0)
    buf = io.BytesIO()
    kw = {"exif": ex.tobytes()}
    if icc:
        kw["icc_profile"] = icc
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


class _IngestBase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIPFS()
        self._real = (cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis, cs.ipfs_unpin)
        cs.ipfs_add_bytes = self.fake.add_bytes
        cs.ipfs_cat = self.fake.cat
        self.faceid_calls, self.unpinned = [], []
        cs._trigger_faceid_analysis = lambda *a, **k: self.faceid_calls.append(a)
        cs.ipfs_unpin = lambda cid: self.unpinned.append(cid) or True
        self.email = f"h{time.time_ns()}@example.com"

    def tearDown(self):
        cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis, cs.ipfs_unpin = self._real


class TestStorageQuota(_IngestBase):
    """Offre 10 Go / 128 Go (ZenCard) : on refuse le NOUVEAU au-delà du quota, jamais de suppression."""

    def setUp(self):
        super().setUp()
        self.q = cs.storage_quota
        self._small = self.q.SMALL_FREE
        self.nd = cs.nostr_dir(self.email)
        cs.ensure_ucloud_dir(self.email)
        self.q.forget(self.nd)

    def tearDown(self):
        self.q.SMALL_FREE = self._small
        self.q.forget(self.nd)
        super().tearDown()

    def limit(self, nbytes):
        (self.nd / ".storage_quota").write_text(str(nbytes))
        self.q.forget(self.nd)

    def test_default_tiers_and_zencard(self):
        st = self.q.status(self.nd, fresh=True)
        self.assertEqual((st["tier"], st["quota"], st["zen_week"]), ("multipass", 10 * self.q.GO, 1))
        self.assertEqual(st["upgrade"]["quota"], 128 * self.q.GO)
        card = self.nd.parent.parent / "players" / self.email
        card.mkdir(parents=True, exist_ok=True)
        (card / "secret.dunikey").write_text("x")
        try:
            st = self.q.status(self.nd, fresh=True)
            self.assertEqual((st["tier"], st["quota"], st["zen_week"]), ("zencard", 128 * self.q.GO, 5))
            self.assertNotIn("upgrade", st)
        finally:
            (card / "secret.dunikey").unlink()

    def test_grace_zone_then_refusal_and_existing_kept(self):
        self.q.SMALL_FREE = 0
        a, b, c = _photo(1), _photo(2), _photo(3)
        # Forfait = la moitié du total : a+b dépassent le forfait (grâce) sans atteindre le seuil de refus (×2).
        self.limit((len(a) + len(b) + len(c) - 1) // 2)
        cs.ingest_plaintext(self.email, "/Photos/a.jpg", a, content_type="image/jpeg")
        cs.ingest_plaintext(self.email, "/Photos/b.jpg", b, content_type="image/jpeg")
        st = self.q.status(self.nd, fresh=True)
        self.assertTrue(st["over"], "au-delà du forfait : période de grâce")
        self.assertFalse(st["full"], "mais pas encore bloqué")
        with self.assertRaises(self.q.QuotaExceeded):
            cs.ingest_plaintext(self.email, "/Photos/c.jpg", c, content_type="image/jpeg")
        self.assertIsNone(cs.db_entry(self.email, "/Photos/c.jpg"), "refusé : rien d'indexé")
        self.assertIsNotNone(cs.db_entry(self.email, "/Photos/a.jpg"), "l'existant n'est jamais supprimé")
        self.assertNotIn("hard", " ".join(st.keys()), "le seuil de refus n'est jamais exposé")
        self.assertTrue(all(v != st["quota"] * 2 for v in st.values() if isinstance(v, int) and v), "ni le double du forfait")

    def test_small_files_always_accepted(self):
        self.limit(10)
        cs.ingest_plaintext(self.email, "/Contacts/x.vcf", b"BEGIN:VCARD\nEND:VCARD\n", content_type="text/vcard")
        self.assertIsNotNone(cs.db_entry(self.email, "/Contacts/x.vcf"))

    def test_received_photo_reports_quota_status(self):
        self.q.SMALL_FREE = 0
        self.limit(2)
        res = cs.ingest_received(self.email, _photo(4), "image/jpeg", "p.jpg", "ab" * 32, "Ami")
        self.assertEqual(res["status"], "quota")

    def test_usage_file_written_for_economy(self):
        cs.ingest_plaintext(self.email, "/Photos/a.jpg", _photo(5), content_type="image/jpeg")
        (self.nd / "APP" / "uDRIVE").mkdir(parents=True)
        (self.nd / "APP" / "uDRIVE" / "f.bin").write_bytes(b"x" * 1000)
        st = self.q.record(self.nd)
        import json as _j
        on_disk = _j.loads((self.nd / ".storage_usage.json").read_text())
        self.assertEqual(on_disk["udrive"], 1000)
        self.assertEqual(on_disk["used"], st["ucloud"] + 1000)
        self.assertGreater(st["ucloud"], 0)


class TestIngestHardening(_IngestBase):
    def test_exif_gps_and_icc_survive_rotation(self):
        icc = b"\x00" * 128
        src = _jpeg(orientation=6, gps=True)
        e = cs.ingest_plaintext(self.email, "/Photos/r.jpg", src, content_type="image/jpeg")
        self.assertIsNotNone(e.get("geo"), "GPS lu AVANT la normalisation (rotation)")
        self.assertAlmostEqual(e["geo"]["lat"], 48.85, places=2)
        stored, _ = cs.read_plaintext(self.email, "/Photos/r.jpg")
        from PIL import Image
        im = Image.open(io.BytesIO(stored))
        self.assertEqual(im.size, (40, 60), "image redressée")
        self.assertEqual(im.getexif().get(0x0112, 1), 1)
        self.assertTrue(im.getexif().get_ifd(0x8825), "EXIF GPS conservé dans l'image stockée")
        self.assertNotEqual(e["sha256_plain"], e["sha256_orig"])
        self.assertEqual(e["sha256_orig"], __import__("hashlib").sha256(src).hexdigest())

    def test_generic_mime_is_resolved(self):
        e = cs.ingest_plaintext(self.email, "/Photos/g.jpg", _jpeg(gps=False),
                                content_type="application/octet-stream")
        self.assertEqual(e["mime"], "image/jpeg")
        self.assertEqual(len(self.faceid_calls), 1, "FaceID déclenché malgré octet-stream")
        e2 = cs.ingest_plaintext(self.email, "/Photos/noext", _jpeg(gps=False), content_type="")
        self.assertEqual(e2["mime"], "image/jpeg", "reconnu par les octets")

    def test_junk_and_unreadable_never_reach_faceid(self):
        cs.ingest_plaintext(self.email, "/Photos/._IMG.jpg", b"\x00\x05\x16\x07appledouble",
                            content_type="image/jpeg")
        cs.ingest_plaintext(self.email, "/Photos/faux.jpg", b"pas une image", content_type="image/jpeg")
        self.assertEqual(self.faceid_calls, [])
        idx = cs.load_index(self.email)
        self.assertEqual(idx["entries"]["/Photos/faux.jpg"]["faceid"]["status"], "unreadable")

    def test_unique_is_race_free(self):
        errs = []

        def put(i):
            try:
                cs.ingest_plaintext(self.email, "/Reçues/x/IMG_0001.jpg", f"contenu-{i}".encode(),
                                    content_type="text/plain", unique=True)
            except Exception as exc:       # pragma: no cover
                errs.append(exc)
        ts = [threading.Thread(target=put, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        files = [k for k, v in cs.load_index(self.email)["entries"].items() if v.get("type") == "file"]
        self.assertEqual(len(files), 8, "aucun écrasement : 8 contenus → 8 fichiers")

    def test_failed_commit_unpins_blob(self):
        cs.ingest_plaintext(self.email, "/A/b", b"un fichier", content_type="text/plain")
        with self.assertRaises(ValueError):          # /A/b est un fichier, pas un dossier
            cs.ingest_plaintext(self.email, "/A/b/c.txt", b"x", content_type="text/plain")
        self.assertEqual(len(self.unpinned), 1, "blob orphelin dépinglé")

    def test_large_import_is_readable(self):
        big = os.urandom(21 * 1024 * 1024)
        cs.ingest_plaintext(self.email, "/Photos/pano.bin", big, content_type="application/x-big")
        entry = cs.get_entry(cs.load_index(self.email), "/Photos/pano.bin")
        self.assertGreater(cs.cipher_limit(entry), cs.MAX_FILE_SIZE)
        self.assertEqual(cs.read_plaintext(self.email, "/Photos/pano.bin")[0], big)

    def test_exact_max_size_cipher_fits_default_limit(self):
        self.assertGreaterEqual(cs.MAX_FILE_SIZE + cs.UENC_OVERHEAD, cs.MAX_FILE_SIZE + 34)

    def test_children_map_matches_list_children(self):
        for p in ("/a/b/c.txt", "/a/d.txt", "/z.txt"):
            cs.ingest_plaintext(self.email, p, b"x", content_type="text/plain")
        idx = cs.load_index(self.email)
        cmap = cs.build_children_map(idx)
        for d in ("/", "/a", "/a/b"):
            self.assertEqual(cmap.get(d, []), cs.list_children(idx, d))


class TestAuthAndLocks(unittest.TestCase):
    def test_basic_auth_non_ascii_password_is_refused_not_500(self):
        email = f"na{time.time_ns()}@example.com"
        cs.ensure_ucloud_dir(email)
        cs.token_path(email).write_text(json.dumps({"token": "a" * 64}))
        self.assertIsNone(cs.verify_basic_credentials(email, "é" * 10))
        self.assertEqual(cs.verify_basic_credentials(email, "a" * 64), email)

    def test_hex_map_cache(self):
        email = f"hx{time.time_ns()}@example.com"
        h = f"{time.time_ns():064x}"[-64:]
        cs.nostr_dir(email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(email) / ".secret.nostr").write_text(f"NSEC=nsec1zzz; NPUB=npub1x; HEX={h};\n")
        self.assertEqual(cs._parse_secret_nostr(cs.nostr_dir(email) / ".secret.nostr"),
                         {"HEX": h, "NPUB": "npub1x"}, "le NSEC n'est jamais conservé")
        cs._HEX_MAP_AT = 0.0
        self.assertEqual(cs.email_for_hex(h), email)

    def test_webdav_sources_concurrent_updates_are_not_lost(self):
        email = f"ws{time.time_ns()}@example.com"
        ts = [threading.Thread(target=cs.add_webdav_source,
                               args=(email, f"s{i}", "https://x/", "u", "p", "", "/Photos/Import_S"))
              for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        srcs = cs.load_webdav_sources(email)
        self.assertEqual(len(srcs), 12, "aucune source perdue (read-modify-write sous verrou)")
        prefixes = [s["dest_prefix"] for s in srcs]
        self.assertEqual(len(set(prefixes)), 12, "un dossier de destination par source")
        cs.record_webdav_sync(email, srcs[0]["id"], {"imported": 3})
        self.assertEqual(len(cs.load_webdav_sources(email)), 12)

    def test_dest_prefix_is_confined_under_photos(self):
        self.assertTrue(cs.sanitize_dest_prefix("/Contacts", "abcdef123", []).startswith("/Photos/Import_"))
        self.assertTrue(cs.sanitize_dest_prefix("/../etc", "abcdef123", []).startswith("/Photos/Import_"))
        self.assertEqual(cs.sanitize_dest_prefix("/Photos/Mes", "abcdef123", []), "/Photos/Mes")

    def test_nip98_binding_and_replay(self):
        from services.nostr import check_nip98_binding
        ev = lambda i, m="POST", u="https://h/api/cloud/files/delete": {
            "id": i, "tags": [["u", u], ["method", m]]}
        check_nip98_binding(ev("1" * 64), "POST", "/api/cloud/files/delete")
        with self.assertRaises(ValueError):                       # rejeu
            check_nip98_binding(ev("1" * 64), "POST", "/api/cloud/files/delete")
        with self.assertRaises(ValueError):                       # autre méthode
            check_nip98_binding(ev("2" * 64, m="GET"), "POST", "/api/cloud/files/delete")
        with self.assertRaises(ValueError):                       # autre route
            check_nip98_binding(ev("3" * 64, u="https://h/api/cloud/status"), "POST", "/api/cloud/enroll")
        check_nip98_binding(ev("4" * 64, m="GET", u="https://h/x/dav/a%20b"), "GET", "/dav/a b")
        check_nip98_binding(ev("4" * 64, m="GET", u="https://h/x/dav/a%20b"), "GET", "/dav/a b")  # GET rejouable
        check_nip98_binding({"id": "5" * 64, "tags": []}, "POST", "/n/importe/quoi")   # tolérant sans tags


class TestDavHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from wsgidav.wsgidav_app import WsgiDAVApp
        cls.email = f"dh{time.time_ns()}@example.com"
        cs.nostr_dir(cls.email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(cls.email) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={'cd' * 32};\n")
        cs.ensure_ucloud_dir(cls.email)
        cs.token_path(cls.email).write_text(json.dumps({"token": TOKEN}))
        cls.fake = FakeIPFS()
        cls._real = (cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis)
        cs.ipfs_add_bytes, cs.ipfs_cat = cls.fake.add_bytes, cls.fake.cat
        cs._trigger_faceid_analysis = lambda *a, **k: None
        cls.app = staticmethod(cs._strip_duplicate_date_header(WsgiDAVApp(cs.build_wsgi_dav_config())))

    @classmethod
    def tearDownClass(cls):
        cs.ipfs_add_bytes, cs.ipfs_cat, cs._trigger_faceid_analysis = cls._real

    def call(self, method, path, body=b"", headers=None):
        return call_wsgi(self.app, method, path, body, auth=(self.email, TOKEN), headers=headers)

    def test_if_none_match_star_refuses_overwrite(self):
        self.assertEqual(self.call("PUT", "/n.txt", b"un", {"If-None-Match": "*"})[0], 201)
        self.assertEqual(self.call("PUT", "/n.txt", b"deux", {"If-None-Match": "*"})[0], 412)
        self.assertEqual(self.call("GET", "/n.txt")[2], b"un", "contenu d'origine intact")

    def test_mkcol_existing_is_405(self):
        self.assertEqual(self.call("MKCOL", "/dossier")[0], 201)
        self.assertEqual(self.call("MKCOL", "/dossier")[0], 405)

    def test_get_has_download_headers_and_no_duplicate_date(self):
        self.call("PUT", "/p.html", b"<script>1</script>", {"Content-Type": "text/html"})
        st, hdrs, _ = self.call("GET", "/p.html")
        h = {k.lower(): v for k, v in hdrs}
        self.assertEqual(st, 200)
        self.assertEqual(h["content-disposition"], "attachment")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", h["content-security-policy"])
        self.assertNotIn("date", h)

    def test_oversize_declared_length_is_refused_early(self):
        def lying(environ, start_response):       # annonce 20 Mo+1, n'en envoie qu'un octet
            environ["CONTENT_LENGTH"] = str(cs.MAX_FILE_SIZE + 1)
            return self.app(environ, start_response)
        st, _, _ = call_wsgi(lying, "PUT", "/gros.bin", b"x", auth=(self.email, TOKEN))
        self.assertEqual(st, 403)
        self.assertNotIn("/gros.bin", cs.load_index(self.email)["entries"])

    def test_octet_stream_photo_is_typed_as_image(self):
        self.call("PUT", "/ph.jpg", _jpeg(gps=False), {"Content-Type": "application/octet-stream"})
        self.assertEqual(cs.load_index(self.email)["entries"]["/ph.jpg"]["mime"], "image/jpeg")

    def test_implicit_dir_and_propfind(self):
        idx = cs.load_index(self.email)
        idx["entries"]["/imp/licite/x.txt"] = {"type": "file", "cid": None, "mtime": 1}
        cs.save_index(self.email, idx)
        self.assertEqual(self.call("PROPFIND", "/imp", headers={"Depth": "1"})[0], 207)

    def test_stale_ghost_placeholders_are_purged(self):
        idx = cs.load_index(self.email)
        idx["entries"]["/fantome.txt"] = {"type": "file", "cid": None, "mtime": 1}
        cs.save_index(self.email, idx)
        self.call("PUT", "/nouveau.txt", b"x")
        self.assertNotIn("/fantome.txt", cs.load_index(self.email)["entries"])


class TestWebdavClientHardening(unittest.TestCase):
    def test_child_url_encodes_segments(self):
        from services import webdav_client as wc
        self.assertEqual(wc._child_url("https://h/dav", "a b/Photo: x#1?.jpg"),
                         "https://h/dav/a%20b/Photo%3A%20x%231%3F.jpg")
        self.assertEqual(wc._child_url("https://h/dav/", ""), "https://h/dav/")

    def test_internal_addresses_are_refused(self):
        import asyncio
        from services import webdav_client as wc
        for url in ("http://127.0.0.1:5001/", "http://169.254.169.254/latest", "ftp://x/", "http://[::1]/"):
            with self.assertRaises(wc.WebdavError, msg=url):
                asyncio.run(wc.validate_remote_url(url))

    def test_walk_keeps_newest_when_capped_and_survives_cycles(self):
        import asyncio
        from services import webdav_client as wc
        tree = {"": [{"name": y, "path": y, "is_dir": True, "size": 0, "mtime": None} for y in ("2007", "2026", "loop")],
                "loop": [{"name": "loop", "path": "loop", "is_dir": True, "size": 0, "mtime": None}]}
        for y in ("2007", "2026"):
            tree[y] = [{"name": f"{i}.jpg", "path": f"{y}/{i}.jpg", "is_dir": False, "size": 1, "mtime": None}
                       for i in range(3)]
        real = wc.list_folder

        async def fake(root, u, p, path="", timeout=20.0):
            return tree.get(path.strip("/"), [])
        wc.list_folder = fake
        try:
            out = asyncio.run(wc.walk_files("https://h/", "u", "p", "", max_files=4))
        finally:
            wc.list_folder = real
        paths = [e["path"] for e in out]
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(p.startswith(("2026/", "2007/")) for p in paths))
        self.assertEqual(paths[:3], ["2026/2.jpg", "2026/1.jpg", "2026/0.jpg"], "les plus récentes d'abord")


class TestBatchAndReadCache(_IngestBase):
    def test_ingest_many_writes_index_once(self):
        writes = []
        real = cs.ucloud_db.transaction
        from contextlib import contextmanager

        @contextmanager
        def counting(udir):
            writes.append(1)
            with real(udir) as conn:
                yield conn
        cs.ucloud_db.transaction = counting
        try:
            items = [{"path": f"/B/f{i}.txt", "plaintext": f"c{i}".encode(), "content_type": "text/plain"}
                     for i in range(20)]
            res = cs.ingest_many(self.email, items)
        finally:
            cs.ucloud_db.transaction = real
        self.assertEqual(len(writes), 1, "UNE transaction pour 20 fichiers")
        self.assertEqual([r["path"] for r in res], [f"/B/f{i}.txt" for i in range(20)])
        for i in (0, 19):
            self.assertEqual(cs.read_plaintext(self.email, f"/B/f{i}.txt")[0], f"c{i}".encode())
        self.assertEqual(len(cs.load_keyring(self.email)), 20)

    def test_batch_same_path_unique_and_overwrite(self):
        res = cs.ingest_many(self.email, [
            {"path": "/U/a.txt", "plaintext": b"1", "content_type": "text/plain", "unique": True},
            {"path": "/U/a.txt", "plaintext": b"2", "content_type": "text/plain", "unique": True}])
        self.assertEqual([r["path"] for r in res], ["/U/a.txt", "/U/a-2.txt"])
        # écrasement dans le même lot (unique=False) : le 1er blob est dépinglé, sa clé retirée
        self.unpinned.clear()
        res = cs.ingest_many(self.email, [
            {"path": "/V/a.txt", "plaintext": b"1", "content_type": "text/plain"},
            {"path": "/V/a.txt", "plaintext": b"2", "content_type": "text/plain"}])
        self.assertEqual(self.unpinned, [res[0]["cid"]])
        self.assertNotIn(res[0]["cid"], cs.load_keyring(self.email))
        self.assertEqual(cs.read_plaintext(self.email, "/V/a.txt")[0], b"2")

    def test_batch_failure_unpins_everything(self):
        cs.ingest_plaintext(self.email, "/F/file", b"x", content_type="text/plain")
        self.unpinned.clear()
        with self.assertRaises(ValueError):
            cs.ingest_many(self.email, [
                {"path": "/G/ok.txt", "plaintext": b"1", "content_type": "text/plain"},
                {"path": "/F/file/bad.txt", "plaintext": b"2", "content_type": "text/plain"}])
        self.assertEqual(len(self.unpinned), 2)
        self.assertNotIn("/G/ok.txt", cs.load_index(self.email)["entries"], "tout-ou-rien")

    def test_ro_cache_is_shared_then_invalidated_by_a_write(self):
        cs.ingest_plaintext(self.email, "/R/a.txt", b"a", content_type="text/plain")
        i1, i2 = cs.index_ro(self.email), cs.index_ro(self.email)
        self.assertIs(i1, i2, "même objet tant que le fichier n'a pas changé")
        cs.ingest_plaintext(self.email, "/R/b.txt", b"b", content_type="text/plain")
        i3 = cs.index_ro(self.email)
        self.assertIsNot(i1, i3)
        self.assertIn("/R/b.txt", i3["entries"])
        self.assertEqual(cs.db_children(self.email, "/R"), ["a.txt", "b.txt"])
        cid = i3["entries"]["/R/b.txt"]["cid"]
        self.assertEqual(len(cs.get_key_hex(self.email, cid)), 64)
        self.assertEqual(cs.index_ro(f"nouveau{time.time_ns()}@x.org")["entries"], {})


class TestFacesFollowPaths(_IngestBase):
    """Les points Qdrant suivent les chemins : MOVE → source_path mis à jour,
    DELETE / remplacement → visages oubliés."""
    def setUp(self):
        super().setUp()
        self.calls = []
        self._qp = cs._qdrant_post
        cs._qdrant_post = lambda coll, route, body: self.calls.append((coll, route, body)) or True
        cs.nostr_dir(self.email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(self.email) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={'12' * 32};\n")

    def tearDown(self):
        cs._qdrant_post = self._qp
        super().tearDown()

    def test_overwrite_with_other_content_forgets_old_faces(self):
        cs.ingest_plaintext(self.email, "/P/a.txt", b"v1", content_type="text/plain")
        self.assertEqual(self.calls, [])
        cs.ingest_plaintext(self.email, "/P/a.txt", b"v1", content_type="text/plain")   # même contenu
        self.assertEqual(self.calls, [])
        cs.ingest_plaintext(self.email, "/P/a.txt", b"v2", content_type="text/plain")
        coll, route, body = self.calls[0]
        self.assertEqual((coll, route), ("faces_" + "12" * 32, "points/delete"))
        self.assertEqual(body["filter"]["must"][0]["match"]["any"], ["/P/a.txt"])

    def test_subtree_paths_and_forget_batches(self):
        for p in ("/D/x.txt", "/D/s/y.txt", "/E/z.txt"):
            cs.ingest_plaintext(self.email, p, p.encode(), content_type="text/plain")
        idx = cs.load_index(self.email)
        self.assertEqual(sorted(cs.subtree_paths(idx, "/D")), ["/D/s/y.txt", "/D/x.txt"])
        cs.faces_forget_paths(self.email, [f"/p/{i}.jpg" for i in range(450)])
        self.assertEqual(len(self.calls), 3, "lots de 200")

    def test_follow_moves(self):
        cs.faces_follow_moves(self.email, {"/a.jpg": "/b.jpg", "/c.jpg": "/c.jpg"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2]["payload"], {"source_path": "/b.jpg"})


class TestNip98Middleware(unittest.TestCase):
    """Nip98BindingMiddleware : liaison sur toutes les routes, rejeu sur le cloud."""
    def setUp(self):
        import asyncio
        from services import nostr as nz
        self.nz, self.asyncio = nz, asyncio
        self._real = nz._decode_and_verify_nip98_event
        self.event = {}
        nz._decode_and_verify_nip98_event = lambda h: self.event
        calls = self.calls = []

        async def app(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        self.mw = nz.Nip98BindingMiddleware(app)

    def tearDown(self):
        self.nz._decode_and_verify_nip98_event = self._real

    def req(self, method, path, eid, tags, auth=True):
        self.event = {"id": eid, "tags": tags}
        out = []

        async def send(m):
            out.append(m)

        async def receive():
            return {"type": "http.request", "body": b""}
        headers = [(b"authorization", b"Nostr xxx")] if auth else []
        self.asyncio.run(self.mw({"type": "http", "method": method, "path": path, "headers": headers},
                                 receive, send))
        return out[0]["status"]

    def test_binding_applies_everywhere(self):
        t = [["u", "https://h/api/upload"], ["method", "POST"]]
        self.assertEqual(self.req("POST", "/api/upload", "a" * 64, t), 200)
        self.assertEqual(self.req("POST", "/api/upload", "b" * 64, [["u", "https://h/api/autre"], ["method", "POST"]]), 401)
        self.assertEqual(self.req("DELETE", "/api/upload", "c" * 64, t), 401, "autre méthode")
        self.assertEqual(self.req("POST", "/api/upload", "d" * 64, []), 200, "client sans tags toléré")

    def test_replay_only_on_cloud_prefixes(self):
        t = [["u", "https://h/api/upload"], ["method", "POST"]]
        self.assertEqual(self.req("POST", "/api/upload", "e" * 64, t), 200)
        self.assertEqual(self.req("POST", "/api/upload", "e" * 64, t), 200, "hors cloud : pas d'anti-rejeu")
        c = [["u", "https://h/api/cloud/files/delete"], ["method", "POST"], ["nonce", "1"]]
        self.assertEqual(self.req("POST", "/api/cloud/files/delete", "f" * 64, c), 200)
        self.assertEqual(self.req("POST", "/api/cloud/files/delete", "f" * 64, c), 401, "rejeu refusé")

    def test_dav_and_unauthenticated_are_passed_through(self):
        self.assertEqual(self.req("PUT", "/dav/x", "9" * 64, [["method", "GET"]]), 200, "géré par le middleware DAV")
        self.assertEqual(self.req("GET", "/n", "8" * 64, [], auth=False), 200)

    def test_invalid_event_is_left_to_the_route(self):
        def boom(h):
            raise ValueError("bad sig")
        self.nz._decode_and_verify_nip98_event = boom
        self.assertEqual(self.req("POST", "/api/x", "7" * 64, []), 200)


class TestEditWebdavSource(unittest.TestCase):
    def setUp(self):
        import asyncio
        from routers import cloud
        from services import webdav_client as wc
        self.asyncio, self.cloud, self.wc = asyncio, cloud, wc
        self.email = f"es{time.time_ns()}@example.com"
        self.h = f"{time.time_ns():064x}"[-64:]
        cs.nostr_dir(self.email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(self.email) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={self.h};\n")
        cs._HEX_MAP_AT = 0.0
        self.src = cs.add_webdav_source(self.email, "Vieux", "https://a.example/dav/", "u", "secret", "Photos", "/Photos/Import_a")
        self._real = (wc.validate_remote_url, wc.list_folder)
        self.checked = []

        async def ok_url(url):
            return None

        async def ok_list(url, user, pwd, path="", timeout=20.0):
            self.checked.append((url, user, pwd, path))
            return []
        wc.validate_remote_url, wc.list_folder = ok_url, ok_list

    def tearDown(self):
        self.wc.validate_remote_url, self.wc.list_folder = self._real

    def edit(self, **kw):
        args = dict(label=None, url=None, username=None, password=None, remote_path=None)   # appel direct : pas de Form()
        args.update(kw)
        return self.asyncio.run(self.cloud.edit_webdav_source(None, self.src["id"], npub=self.h, **args))

    def test_label_only_keeps_password_and_does_not_reconnect(self):
        r = self.edit(label="Nouveau nom")
        self.assertEqual(r.status_code, 200)
        cur = cs.get_webdav_source(self.email, self.src["id"])
        self.assertEqual((cur["label"], cur["password"], cur["dest_prefix"]), ("Nouveau nom", "secret", "/Photos/Import_a"))
        self.assertEqual(self.checked, [], "rien à revalider")
        self.assertNotIn("password", json.loads(r.body)["source"])

    def test_new_folder_blank_password_is_validated_with_stored_password(self):
        r = self.edit(remote_path="/Photos/2026/", password="")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.checked, [("https://a.example/dav/", "u", "secret", "Photos/2026")])
        cur = cs.get_webdav_source(self.email, self.src["id"])
        self.assertEqual((cur["remote_path"], cur["password"]), ("Photos/2026", "secret"))
        self.assertIsNone(cur["last_sync"])

    def test_refused_connection_changes_nothing(self):
        async def boom(url, user, pwd, path="", timeout=20.0):
            raise self.wc.WebdavError("identifiants refusés (401)")
        self.wc.list_folder = boom
        r = self.edit(password="mauvais", url="https://b.example/dav/")
        self.assertEqual(r.status_code, 400)
        cur = cs.get_webdav_source(self.email, self.src["id"])
        self.assertEqual((cur["url"], cur["password"]), ("https://a.example/dav/", "secret"))

    def test_browse_reuses_stored_password(self):
        r = self.asyncio.run(self.cloud.browse_webdav(None, url="https://a.example/dav/", username="", password="",
                                                      path="Photos", source_id=self.src["id"], npub=self.h))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.checked[-1][1:3], ("u", "secret"))

    def test_unknown_source_is_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.asyncio.run(self.cloud.edit_webdav_source(None, "inconnue", label="x", url=None, username=None, password=None, remote_path=None, npub=self.h))
        self.assertEqual(ctx.exception.status_code, 404)


class TestCirclesRouter(unittest.TestCase):
    """Cercles N1/N2 : la nature des liens, et un cercle 2 PAGINÉ (100 amis × 100 amis)."""
    def setUp(self):
        import asyncio
        from routers import facecloud as fc
        self.asyncio, self.fc = asyncio, fc
        fc._circles_cache.clear()
        h = lambda i: f"{i:064x}"
        self.h, self.me = h, h(1)
        self.follows = [h(i) for i in range(10, 110)]            # je suis 100 personnes
        self.followers = [h(i) for i in range(60, 160)]          # 100 me suivent : 50 mutuels, 50 « in »
        self.theirs = [h(i) for i in range(1000, 1100)] + [h(10), h(1)]   # les 100 amis d'UN ami (+ un N1 + moi)
        self.calls = {"n1": 0}
        self._real = (fc.get_n1_follows, fc.get_followers, fc.fetch_nostr_profiles)

        async def n1(k):
            self.calls["n1"] += 1
            return self.follows if k == self.me else self.theirs

        async def fol(k):
            return self.followers

        async def prof(keys):
            return {k: {"name": f"N{int(k, 16)}", "display_name": "Marie Dupont" if int(k, 16) == 1005 else "",
                        "email": "", "npub": "npub" + k[-6:], "picture": "http://x/y.png"} for k in keys}
        fc.get_n1_follows, fc.get_followers, fc.fetch_nostr_profiles = n1, fol, prof

    def tearDown(self):
        self.fc.get_n1_follows, self.fc.get_followers, self.fc.fetch_nostr_profiles = self._real

    def test_circle1_relations_and_no_avatar_url(self):
        r = json.loads(self.asyncio.run(self.fc.circles(None, refresh=0, npub=self.me)).body)
        rel = {l["pubkey"]: l["rel"] for l in r["links"]}
        self.assertEqual(rel[self.h(10)], "out")          # je le suis seulement
        self.assertEqual(rel[self.h(70)], "mutual")       # dans les deux listes
        self.assertEqual(rel[self.h(150)], "in")          # il me suit seulement
        self.assertEqual(r["counts"], {"out": 50, "in": 50, "mutual": 50})
        self.assertNotIn("picture", json.dumps(r), "aucune URL d'avatar : la page ne charge rien d'extérieur")

    def test_circle2_is_paginated_filtered_and_cached(self):
        via = self.h(10)
        r = json.loads(self.asyncio.run(self.fc.circles_via(None, via, q="", offset=0, limit=30, npub=self.me)).body)
        self.assertEqual((len(r["items"]), r["total"], r["total_friends"]), (30, 100, 100))
        self.assertNotIn(self.me, [i["pubkey"] for i in r["items"]], "ni moi ni l'introducteur")
        r2 = json.loads(self.asyncio.run(self.fc.circles_via(None, via, q="", offset=90, limit=30, npub=self.me)).body)
        self.assertEqual(len(r2["items"]), 10, "dernière page")
        hit = json.loads(self.asyncio.run(self.fc.circles_via(None, via, q="marie", offset=0, limit=30, npub=self.me)).body)
        self.assertEqual([i["display_name"] for i in hit["items"]], ["Marie Dupont"])
        self.assertEqual(hit["items"][0]["rel"], "none", "inconnu de mon réseau")
        self.assertEqual(self.calls["n1"], 2, "mes follows + ceux de l'ami, pas un appel par requête")


def _photo(seed: int, size=(160, 120), quality=90, rot=False):
    """Image à motifs aléatoires déterministes (un dégradé uni donnerait des dHash dégénérés)."""
    import random
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    im = Image.new("RGB", (320, 240), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x, y = rnd.randrange(320), rnd.randrange(240)
        d.ellipse([x, y, x + rnd.randrange(10, 90), y + rnd.randrange(10, 90)],
                  fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    if size != (320, 240):
        im = im.resize(size)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class TestGalleryAndDuplicates(_IngestBase):
    def setUp(self):
        super().setUp()
        self.udir = cs.ensure_ucloud_dir(self.email)

    def put(self, path, data, **kw):
        return cs.ingest_plaintext(self.email, path, data, content_type="image/jpeg", trigger_faceid=False, **kw)

    def test_phash_is_stable_under_resize_and_recompression_but_not_across_images(self):
        a = cs.compute_phash(_photo(1))
        self.assertEqual(len(a), 16)
        small = cs.compute_phash(_photo(1, size=(80, 60), quality=40))
        dist = lambda x, y: bin(int(x, 16) ^ int(y, 16)).count("1")
        self.assertLessEqual(dist(a, small), 6, "même photo réduite et recompressée")
        self.assertGreater(dist(a, cs.compute_phash(_photo(2))), 12, "autre photo")

    def test_exact_and_near_duplicates_and_keep_choice(self):
        from routers import cloud
        same = _photo(1)
        self.put("/Photos/a.jpg", same)
        self.put("/Photos/copie/a (1).jpg", same)                       # exact : mêmes octets
        self.put("/Photos/mini.jpg", _photo(1, size=(80, 60), quality=40))   # quasi : réduite
        self.put("/Photos/autre.jpg", _photo(2))
        res = cloud._dup_groups(self.email)
        self.assertEqual(len(res["exact"]), 1)
        self.assertEqual(sorted(f["path"] for f in res["exact"][0]["files"]), ["/Photos/a.jpg", "/Photos/copie/a (1).jpg"])
        self.assertEqual(res["exact"][0]["keep"], "/Photos/a.jpg", "à égalité : le chemin le plus court")
        self.assertEqual(len(res["near"]), 1, "la version réduite est un quasi-doublon")
        near = res["near"][0]
        self.assertIn("/Photos/mini.jpg", [f["path"] for f in near["files"]])
        self.assertNotEqual(near["keep"], "/Photos/mini.jpg", "on garde la plus lourde (la plus fidèle)")
        self.assertNotIn("/Photos/autre.jpg", json.dumps(res))

    def test_received_copy_is_never_the_one_kept(self):
        from routers import cloud
        same = _photo(3)
        self.put("/Photos/mienne.jpg", same)
        self.put("/Reçues/Maman/2026/10/x.jpg", same, extra={"origin": "facecloud-share", "from_name": "Maman"})
        g = cloud._dup_groups(self.email)["exact"][0]
        self.assertEqual(g["keep"], "/Photos/mienne.jpg")

    def test_page_filters_counts_and_order(self):
        for i in range(25):
            self.put(f"/Photos/p{i:02d}.jpg", _photo(100 + i))
        cs.ucloud_db.patch_entry(self.udir, "/Photos/p03.jpg", lambda e: e.update(faceid={"status": "faces", "count": 2}, taken_at=2000000000) or True)
        cs.ucloud_db.patch_entry(self.udir, "/Photos/p04.jpg", lambda e: e.update(faceid={"status": "no_face"}) or True)
        rows, total = cs.ucloud_db.page(self.udir, "all", "", 0, 10)
        self.assertEqual((len(rows), total), (10, 25))
        self.assertEqual(rows[0][0], "/Photos/p03.jpg", "date de prise de vue la plus récente d'abord")
        self.assertEqual(cs.ucloud_db.page(self.udir, "faces")[1], 1)
        self.assertEqual(cs.ucloud_db.page(self.udir, "no_face")[1], 1)
        self.assertEqual(cs.ucloud_db.page(self.udir, "all", "p1_")[1], 0, "_ est littéral dans la recherche")
        self.assertEqual(cs.ucloud_db.page(self.udir, "all", "p1")[1], 10)
        c = cs.ucloud_db.gallery_counts(self.udir)
        self.assertEqual((c["all"], c["faces"], c["no_face"], c["waiting"]), (25, 1, 1, 23))
        page2, _ = cs.ucloud_db.page(self.udir, "all", "", 10, 10)
        self.assertEqual(len(page2), 10)
        self.assertFalse({p for p, _ in rows} & {p for p, _ in page2}, "pages disjointes")

    def test_have_by_hash_matches_original_bytes_even_when_rotated(self):
        import hashlib
        data = _jpeg(orientation=6, gps=False)
        self.put("/Photos/r.jpg", data)
        h = hashlib.sha256(data).hexdigest()
        self.assertEqual(cs.db_present_hashes(self.email, [h, "0" * 64, "pas-un-hash"]), [h])

    def test_thumb_cache_is_encrypted_on_disk(self):
        cs.thumb_cache_put(self.email, "QmCID", 240, b"\xff\xd8JPEG-MARQUEUR")
        self.assertEqual(cs.thumb_cache_get(self.email, "QmCID", 240), b"\xff\xd8JPEG-MARQUEUR")
        raw = b"".join(f.read_bytes() for f in (cs.ucloud_dir(self.email) / "thumbcache").glob("*.uenc"))
        self.assertNotIn(b"MARQUEUR", raw, "jamais de clair sur disque")
        self.assertIsNone(cs.thumb_cache_get(self.email, "QmCID", 400))

    def test_v1_database_is_upgraded_in_place(self):
        import sqlite3
        d = cs.ucloud_dir(f"old{time.time_ns()}@x.org")
        d.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(d / "ucloud.db")
        c.executescript("""CREATE TABLE entries(path TEXT PRIMARY KEY, parent TEXT NOT NULL, type TEXT NOT NULL, cid TEXT, mime TEXT,
            size INTEGER, mtime INTEGER, taken_at INTEGER, sha_orig TEXT, sha_plain TEXT, faceid_status TEXT, origin TEXT, data TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE keyring(cid TEXT PRIMARY KEY, data TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
            INSERT INTO meta VALUES('migrated','1');
            INSERT INTO entries(path,parent,type,cid,mime,data) VALUES('/a.jpg','/','file','Qm1','image/jpeg','{"type":"file","cid":"Qm1","mime":"image/jpeg"}');""")
        c.commit(); c.close()
        conn = cs.ucloud_db.connect(d)
        self.assertIn("phash", {r[1] for r in conn.execute("PRAGMA table_info(entries)")})
        self.assertEqual(cs.ucloud_db.page(d, "all")[1], 1)


class TestNodeShareUnifiedSecret(_IngestBase):
    """Entre stations : même CID, même clé ; la station destinataire vérifie puis ÉPINGLE."""
    def setUp(self):
        super().setUp()
        from services import node_share
        self.ns = node_share
        self.owner = f"o{time.time_ns()}@example.com"
        self.rcpt = f"r{time.time_ns()}@example.com"
        for e, h in ((self.owner, "11" * 32), (self.rcpt, "22" * 32)):
            cs.nostr_dir(e).mkdir(parents=True, exist_ok=True)
            (cs.nostr_dir(e) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={h};\n")
        cs._HEX_MAP_AT = 0.0
        self.pinned, self.sent = [], []
        self._real2 = (cs.ipfs_pin, node_share._intercom_send, node_share.node_hex_for)
        cs.ipfs_pin = lambda cid, timeout=300.0: self.pinned.append(cid) or True
        node_share._intercom_send = lambda to, ch, payload, ttl: self.sent.append((ch, payload)) or True
        node_share.node_hex_for = lambda node: "33" * 32

    def tearDown(self):
        cs.ipfs_pin, self.ns._intercom_send, self.ns.node_hex_for = self._real2
        super().tearDown()

    def test_send_does_not_reencrypt_and_receive_pins_the_same_block(self):
        cs.ingest_plaintext(self.owner, "/Photos/x.jpg", _photo(7), content_type="image/jpeg", trigger_faceid=False)
        src = cs.db_entry(self.owner, "/Photos/x.jpg")
        blobs_before = len(self.fake.blobs)
        res = self.ns.send(self.owner, "Fred", {"email": self.rcpt, "hex": "22" * 32, "node": "N"}, ["/Photos/x.jpg"])
        self.assertEqual(res, {"status": "sent", "photos": 1})
        self.assertEqual(len(self.fake.blobs), blobs_before, "aucun blob créé à l'envoi")
        msg = self.sent[0][1]
        self.assertEqual(msg["items"][0]["cid"], src["cid"])
        self.assertEqual(msg["items"][0]["key"], cs.get_key_hex(self.owner, src["cid"]))

        out = self.ns.receive(msg)
        self.assertEqual((out["status"], out["copied"], out["error"]), ("ok", 1, 0))
        self.assertEqual(self.pinned, [src["cid"]], "la station destinataire épingle le bloc reçu")
        got = cs.get_entry(cs.load_index(self.rcpt), next(iter(p for p, e in cs.load_index(self.rcpt)["entries"].items() if e.get("cid"))))
        self.assertEqual(got["cid"], src["cid"])
        self.assertEqual(got["size_plain"], src["size_plain"], "métadonnées recalculées, pas prises sur parole")
        self.assertEqual(len(self.fake.blobs), blobs_before)

    def test_receive_refuses_a_wrong_key_or_hash(self):
        cs.ingest_plaintext(self.owner, "/Photos/y.jpg", _photo(8), content_type="image/jpeg", trigger_faceid=False)
        src = cs.db_entry(self.owner, "/Photos/y.jpg")
        base = {"to_email": self.rcpt, "to_hex": "22" * 32, "from_hex": "11" * 32, "from_name": "Fred"}
        bad_key = dict(base, items=[{"cid": src["cid"], "key": "00" * 32, "sha": src["sha256_plain"], "name": "y.jpg"}])
        bad_sha = dict(base, items=[{"cid": src["cid"], "key": cs.get_key_hex(self.owner, src["cid"]), "sha": "f" * 64, "name": "y.jpg"}])
        for msg in (bad_key, bad_sha):
            self.assertEqual(self.ns.receive(msg)["error"], 1)
        self.assertEqual([e for e in cs.load_index(self.rcpt)["entries"].values() if e.get("cid")], [], "rien n'est rangé")


class TestSendersRoutes(_IngestBase):
    def test_senders_listing_and_purge_all_not_only_loaded_ones(self):
        import asyncio
        from routers import cloud
        h = f"{time.time_ns():064x}"[-64:]
        cs.nostr_dir(self.email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(self.email) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={h};\n")
        cs._HEX_MAP_AT = 0.0
        for i in range(5):
            cs.ingest_plaintext(self.email, f"/Reçues/Maman/2026/10/p{i}.jpg", _photo(300 + i), content_type="image/jpeg",
                                trigger_faceid=False, extra={"origin": "facecloud-share", "from_hex": "ab" * 32, "from_name": "Maman"})
        cs.ingest_plaintext(self.email, "/Photos/mienne.jpg", _photo(399), content_type="image/jpeg", trigger_faceid=False)
        r = json.loads(asyncio.run(cloud.cloud_senders(None, npub=h)).body)
        self.assertEqual(r["senders"], [{"hex": "ab" * 32, "name": "Maman", "count": 5}])
        p = json.loads(asyncio.run(cloud.cloud_senders_purge(None, hex="ab" * 32, name="", npub=h)).body)
        self.assertEqual(p["deleted"], 5)
        self.assertEqual(cs.ucloud_db.page(cs.ucloud_dir(self.email))[1], 1, "ses photos à elle ne sont pas touchées")

    def test_have_endpoint(self):
        import asyncio, hashlib
        from routers import cloud
        h = f"{time.time_ns():064x}"[-64:]
        cs.nostr_dir(self.email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(self.email) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={h};\n")
        cs._HEX_MAP_AT = 0.0
        data = _photo(5)
        cs.ingest_plaintext(self.email, "/Photos/a.jpg", data, content_type="image/jpeg", trigger_faceid=False)
        sha = hashlib.sha256(data).hexdigest()
        r = json.loads(asyncio.run(cloud.cloud_have(None, hashes=json.dumps([sha, "1" * 64]), npub=h)).body)
        self.assertEqual(r["present"], [sha])


class TestMapView(_IngestBase):
    def setUp(self):
        super().setUp()
        self.udir = cs.ensure_ucloud_dir(self.email)

    def photo(self, path, lat_ref="N", lat=(48.0, 51.0, 0.0), taken="2025:07:14 10:00:00", seed=1):
        from PIL import Image, ExifTags
        im = Image.new("RGB", (40, 30), (seed * 9 % 255, 40, 90))
        ex = im.getexif()
        g = ex.get_ifd(ExifTags.IFD.GPSInfo)
        g[1], g[2], g[3], g[4] = lat_ref, lat, "E", (2.0, 21.0, 0.0)
        ex.get_ifd(ExifTags.IFD.Exif)[0x9003] = taken
        buf = io.BytesIO()
        im.save(buf, "JPEG", exif=ex.tobytes())
        return cs.ingest_plaintext(self.email, path, buf.getvalue(), content_type="image/jpeg", trigger_faceid=False)

    def test_points_months_and_range(self):
        from routers import cloud
        for i in range(3):
            self.photo(f"/Photos/paris{i}.jpg", seed=i, taken=f"2025:07:1{i} 10:00:00")
        self.photo("/Photos/nice.jpg", lat=(43.0, 42.0, 0.0), taken="2024:01:05 12:00:00", seed=9)
        cs.ingest_plaintext(self.email, "/Photos/sans_gps.jpg", _photo(50), content_type="image/jpeg", trigger_faceid=False)
        pts = cs.ucloud_db.geo_points(self.udir)
        self.assertEqual(sorted(p["n"] for p in pts), [1, 3], "un lieu par case : 3 photos à Paris, 1 à Nice")
        jul = int(time.mktime(time.strptime("2025-07-01", "%Y-%m-%d")))
        aug = int(time.mktime(time.strptime("2025-08-01", "%Y-%m-%d")))
        only = cs.ucloud_db.geo_points(self.udir, jul, aug)
        self.assertEqual([p["n"] for p in only], [3], "entre deux dates")
        self.assertEqual(dict(cs.ucloud_db.geo_months(self.udir)), {"2025-07": 3, "2024-01": 1})
        rows, total = cs.ucloud_db.page(self.udir, since=jul, until=aug, lat=only[0]["lat"], lon=only[0]["lon"])
        self.assertEqual(total, 3)
        self.assertEqual(cs.ucloud_db.page(self.udir, lat=0.0, lon=0.0)[1], 0)

    def test_events_group_by_gap_and_memories_by_day(self):
        ud = cs.ucloud_db
        # Soirée 2023-06-10 (4 photos en 2 h) + autre soirée 2023-06-11 + photo isolée 2022-06-10
        for i in range(4):
            self.photo(f"/Photos/s{i}.jpg", seed=i, taken=f"2023:06:10 2{i % 4}:00:00" if i < 4 else "")
        for i in range(3):
            self.photo(f"/Photos/t{i}.jpg", seed=20 + i, taken=f"2023:06:12 1{i}:00:00")
        self.photo("/Photos/old.jpg", seed=40, taken="2022:06:10 09:00:00")
        evs, total = ud.events(self.udir, min_photos=3)
        self.assertEqual(total, 2, "deux événements d'au moins 3 photos : l'isolée est exclue")
        self.assertEqual([e["n"] for e in evs], [3, 4], "récent d'abord")
        self.assertTrue(evs[0]["since"] > evs[1]["since"])
        self.assertIsNotNone(evs[0]["lat"])
        rows, tot = ud.page(self.udir, since=evs[1]["since"], until=evs[1]["until"])
        self.assertEqual(tot, 4, "l'intervalle d'un événement rouvre exactement ses photos")
        mem = ud.memories(self.udir, 6, 10, 2025)
        self.assertEqual([m["year"] for m in mem], [2023, 2022])
        self.assertEqual([m["ago"] for m in mem], [2, 3])
        self.assertEqual(mem[0]["n"], 4)
        self.assertEqual(ud.memories(self.udir, 6, 10, 2022), [], "pas de souvenir de l'année en cours ni future")

    def test_entry_shape_for_photos_info(self):
        self.photo("/Photos/a.jpg", seed=5)
        e = cs.ucloud_db.get_entry(cs.ucloud_db.connect(self.udir), "/Photos/a.jpg")
        self.assertEqual(e.get("type"), "file")
        self.assertTrue(e.get("cid"))
        self.assertTrue(str(e.get("mime")).startswith("image/"))
        from routers import cloud
        row = cloud._file_row("/Photos/a.jpg", e)
        self.assertEqual(row["path"], "/Photos/a.jpg")
        self.assertTrue(row["taken_at"])

    def test_v2_database_gets_coordinates_backfilled(self):
        import sqlite3
        d = cs.ucloud_dir(f"v2{time.time_ns()}@x.org")
        d.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(d / "ucloud.db")
        c.executescript("""CREATE TABLE entries(path TEXT PRIMARY KEY, parent TEXT NOT NULL, type TEXT NOT NULL, cid TEXT, mime TEXT,
            size INTEGER, mtime INTEGER, taken_at INTEGER, sha_orig TEXT, sha_plain TEXT, faceid_status TEXT, origin TEXT, data TEXT NOT NULL, phash INTEGER) WITHOUT ROWID;
            CREATE TABLE keyring(cid TEXT PRIMARY KEY, data TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
            INSERT INTO meta VALUES('migrated','1');""")
        c.execute("INSERT INTO entries(path,parent,type,cid,mime,mtime,data) VALUES('/a.jpg','/','file','Q','image/jpeg',1700000000,?)",
                  ('{"type":"file","cid":"Q","mime":"image/jpeg","geo":{"lat":48.85,"lon":2.35,"umap_key":"48.85,2.35"}}',))
        c.commit(); c.close()
        pts = cs.ucloud_db.geo_points(d)
        self.assertEqual((pts[0]["lat"], pts[0]["lon"], pts[0]["n"]), (48.85, 2.35, 1))

    def test_scan_enriches_legacy_photos_with_date_and_place(self):
        import ucloud_dedupe
        from PIL import Image, ExifTags
        im = Image.new("RGB", (40, 30), (10, 200, 10))
        ex = im.getexif()
        g = ex.get_ifd(ExifTags.IFD.GPSInfo)
        g[1], g[2], g[3], g[4] = "N", (45.0, 45.0, 0.0), "E", (4.0, 50.0, 0.0)
        ex.get_ifd(ExifTags.IFD.Exif)[0x9003] = "2019:05:20 09:30:00"
        buf = io.BytesIO(); im.save(buf, "JPEG", exif=ex.tobytes())
        e = cs.ingest_plaintext(self.email, "/Photos/vieille.jpg", buf.getvalue(), content_type="image/jpeg", trigger_faceid=False)
        # simule une photo importée AVANT l'extraction EXIF : ni date, ni lieu, ni empreinte
        cs.ucloud_db.patch_entry(self.udir, "/Photos/vieille.jpg", lambda x: [x.pop(k, None) for k in ("taken_at", "geo", "phash")] and True)
        self.assertEqual(cs.ucloud_db.geo_points(self.udir), [])
        out = ucloud_dedupe.scan(self.email, workers=1)
        self.assertEqual((out["done"], out["failed"]), (1, 0))
        got = cs.db_entry(self.email, "/Photos/vieille.jpg")
        self.assertEqual(got["geo"]["lat"], 45.75)
        self.assertTrue(got["taken_at"] and got["phash"])
        self.assertEqual(len(cs.ucloud_db.geo_points(self.udir)), 1, "la photo apparaît sur la carte")


class TestDailyOrchestrator(_IngestBase):
    """ucloud_daily.py : import → enrich → describe (budget) → embed → backup, étapes isolées et non bloquantes."""

    def setUp(self):
        super().setUp()
        import ucloud_daily
        from services import analysis
        self.d, self.analysis = ucloud_daily, analysis
        self.udir = cs.ensure_ucloud_dir(self.email)
        for i in range(3):
            cs.ingest_plaintext(self.email, f"/Photos/p{i}.jpg", _photo(900 + i), content_type="image/jpeg", trigger_faceid=False)
        self._resolve = analysis.resolve
        analysis.resolve = lambda task, mode=None: {"effective": "direct", "why": "ok", "direct_ready": True, "relay_ready": False}

    def tearDown(self):
        self.analysis.resolve = self._resolve
        super().tearDown()

    def test_budget_is_a_share_of_the_station_budget_within_bounds(self):
        d = self.d
        self.assertEqual(d.describe_limit(1), d.DESCRIBE_MAX, "un seul compte : plafonné")
        self.assertEqual(d.describe_limit(10), 240)
        self.assertEqual(d.describe_limit(500), d.DESCRIBE_MIN, "beaucoup de comptes : plancher")
        self.assertEqual(d.describe_limit(10, override=7), 7)

    def test_plan_lists_what_is_needed_and_why_the_rest_is_skipped(self):
        plan = {s: (a, r) for s, a, r in self.d.plan(self.email)}
        self.assertIsNone(plan["import"][0], "aucune source WebDAV")
        self.assertIn("aucune source", plan["import"][1])
        self.assertEqual(plan["describe"][0][:3], ["ucloud_describe.py", "scan", self.email], "3 photos sans description")
        self.assertIn("--limit", plan["describe"][0])
        self.assertIsNone(plan["embed"][0], "rien de décrit : rien à vectoriser")
        self.assertIsNotNone(plan["backup"][0])
        (self.udir / "webdav_sources.json").write_text('[{"id": "a"}]')
        self.assertEqual(dict((s, a) for s, a, _r in self.d.plan(self.email))["import"][0], "webdav_import.py")
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "La mer", "ai", "m")
        self.assertIsNotNone(dict((s, a) for s, a, _r in self.d.plan(self.email))["embed"], "une description nouvelle : à vectoriser")
        self.assertEqual([st for st, _a, _r in self.d.plan(self.email, only=["describe", "backup"])], ["describe", "backup"], "--only filtre les étapes")

    def test_describe_skipped_when_no_ai_path(self):
        self.analysis.resolve = lambda task, mode=None: {"effective": "none", "why": "Ni accès direct, ni relais", "direct_ready": False, "relay_ready": False}
        plan = {s: (a, r) for s, a, r in self.d.plan(self.email)}
        self.assertIsNone(plan["describe"][0])
        self.assertIn("Ni accès direct", plan["describe"][1])

    def test_run_executes_steps_in_order_and_a_failure_does_not_stop_the_next(self):
        import subprocess as sp
        (self.udir / "webdav_sources.json").write_text('[{"id": "a"}]')
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "La mer", "ai", "m")
        calls = []

        class R:
            def __init__(self, c): self.returncode = c

        def fake_run(cmd, timeout=None, **kw):
            script = os.path.basename(cmd[1])
            calls.append(script)
            return R({"webdav_import.py": 1, "ucloud_describe.py": 75}.get(script, 0))
        real = sp.run
        self.d.subprocess.run = fake_run
        try:
            out = self.d.run(self.email)
        finally:
            self.d.subprocess.run = real
        self.assertEqual(calls, ["webdav_import.py", "ucloud_describe.py", "ucloud_embed.py", "ucloud_backup.py"])
        self.assertEqual(out["steps"]["import"], "échec(1)")
        self.assertEqual(out["steps"]["describe"], "occupé", "code 75 : déjà en cours, pas un échec")
        self.assertEqual(out["steps"]["backup"], "ok", "les étapes suivantes continuent après un échec")
        self.assertEqual(out["steps"]["enrich"], "skip")

    def test_heavy_steps_are_serialised_between_accounts_but_backup_never_waits(self):
        import subprocess as sp
        calls = []

        class R:
            returncode = 0

        def fake_run(cmd, timeout=None, **kw):
            calls.append(os.path.basename(cmd[1]))
            return R()
        real_run = sp.run                                           # AVANT de patcher : sp est le même module que self.d.subprocess
        self.d.subprocess.run = fake_run
        wait_before, self.d.HEAVY_WAIT = self.d.HEAVY_WAIT, 0
        other = cs.try_lock_nowait(self.d.heavy_lock_path())          # un AUTRE MULTIPASS occupe le GPU
        try:
            out = self.d.run(self.email)
        finally:
            os.close(other)
            self.d.HEAVY_WAIT = wait_before
            self.d.subprocess.run = real_run
        self.assertEqual(out["steps"]["describe"], "attente", "reporté à demain : verrou non obtenu")
        self.assertNotIn("ucloud_describe.py", calls)
        self.assertEqual(out["steps"]["backup"], "ok", "la sauvegarde n'attend pas le GPU")
        # verrou libre : les étapes lourdes passent, puis le verrou est relâché pour les autres comptes
        self.d.subprocess.run = fake_run
        try:
            out2 = self.d.run(self.email)
        finally:
            self.d.subprocess.run = real_run
        self.assertEqual(out2["steps"]["describe"], "ok")
        again = cs.try_lock_nowait(self.d.heavy_lock_path())
        self.assertIsNotNone(again, "verrou des étapes lourdes relâché à la fin")
        os.close(again)

    def test_second_instance_is_refused_by_the_lock(self):
        lock_fd = cs.try_lock_nowait(self.udir / ".daily.lock")
        try:
            self.assertEqual(self.d.run(self.email)["status"], "busy")
        finally:
            os.close(lock_fd)


class TestNostrcardRefreshConformity(unittest.TestCase):
    """Conformité code ↔ Astroport.ONE : le traitement quotidien du .ucloud ne doit JAMAIS bloquer _12345.sh
    (`pgrep -f NOSTRCARD.refresh.sh`) ni les autres MULTIPASS : processus autonome, pas de sous-shell."""
    SCRIPT = Path(__file__).resolve().parents[2] / "Astroport.ONE" / "RUNTIME" / "NOSTRCARD.refresh.sh"

    def block(self):
        if not self.SCRIPT.exists():
            self.skipTest("Astroport.ONE absent de cet environnement")
        text = self.SCRIPT.read_text(encoding="utf-8")
        start = text.index('UCLOUD_DAILY_PY="${HOME}/.zen/UPassport/ucloud_daily.py"')
        return text[start:text.index("IPNS update skipped", start)]

    def test_daily_job_is_a_detached_autonomous_process(self):
        b = self.block()
        self.assertIn("setsid nohup python3", b)
        self.assertIn("200>&-", b, "le verrou hérité du script est libéré")
        self.assertIn("< /dev/null", b)
        code = [l for l in b.splitlines() if not l.strip().startswith("#")]
        self.assertFalse(any(l.strip().startswith("(") for l in code), "pas de sous-shell « ( … ) & » : il garderait la ligne de commande du script")

    def test_every_background_job_of_the_script_is_detached(self):
        """Revue complète : AUCUNE tâche d'arrière-plan du script ne garde sa ligne de commande (pgrep -f de _12345.sh)."""
        import re as _re
        if not self.SCRIPT.exists():
            self.skipTest("Astroport.ONE absent de cet environnement")
        offenders = []
        joined = self.SCRIPT.read_text(encoding="utf-8").replace("\\\n", " ")        # commandes sur plusieurs lignes (« \\ »)
        for n, line in enumerate(joined.splitlines(), 1):
            code = line.split("  #")[0].strip()
            if code.startswith("#"):
                continue
            bare = _re.sub(r"&&|[0-9]*>&[0-9-]*|&>", " ", code)
            if _re.search(r"&\s*$", bare) and "setsid nohup" not in code:
                offenders.append((n, code[:90]))
        self.assertEqual(offenders, [], "tâche d'arrière-plan non détachée (utiliser « setsid nohup … 200>&- & ») : %s" % offenders)

    def test_domain_scrapers_run_through_the_standalone_helper(self):
        if not self.SCRIPT.exists():
            self.skipTest("Astroport.ONE absent de cet environnement")
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("tools/domain_scraper_run.sh", text)
        helper = self.SCRIPT.parents[1] / "tools" / "domain_scraper_run.sh"
        self.assertTrue(helper.is_file() and os.access(helper, os.X_OK))
        self.assertNotIn("NOSTRCARD.refresh.sh", "\n".join(l for l in helper.read_text(encoding="utf-8").splitlines() if "pgrep" not in l and not l.startswith("#")),
                         "la ligne de commande du helper ne doit pas contenir le nom du script de rafraîchissement")

    def test_missing_ucloud_daily_is_reported_not_silently_replaced(self):
        b = self.block()
        self.assertIn('log "ERROR"', b)
        self.assertNotIn("webdav_import.py", b, "plus de repli silencieux sur l'ancien comportement")


class TestVideoFun(_IngestBase):
    """/VideoFUN/ : les vidéos du Studio rangées dans l'ucloud (DAV), par ami, avec leurs métadonnées."""

    def setUp(self):
        super().setUp()
        from services import videofun
        self.vf = videofun
        self.mp4 = Path(tempfile.mkdtemp()) / "scene.mp4"
        self.mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2048))
        self.h = f"{time.time_ns():064x}"[-64:]
        cs.nostr_dir(self.email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(self.email) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={self.h};\n")
        cs._HEX_MAP_AT = 0.0

    def test_archive_creates_dav_folder_per_friend_and_lists_with_metadata(self):
        out = self.vf.archive(self.email, self.mp4, {"to": "Léa", "contact": "uid-1", "title": "Anniversaire / pirate", "cast": ["Léa"], "max_seconds": 8})
        self.assertTrue(out["path"].startswith("/VideoFUN/Léa/") and out["path"].endswith("Anniversaire pirate.mp4"), out["path"])
        e = cs.db_entry(self.email, out["path"])
        self.assertEqual(e["mime"], "video/mp4")
        self.assertEqual(cs.db_entry(self.email, "/VideoFUN")["type"], "dir", "dossiers DAV créés")
        self.assertEqual(cs.db_entry(self.email, "/VideoFUN/Léa")["type"], "dir")
        vids = self.vf.list_videos(self.email)
        self.assertEqual(len(vids), 1)
        v = vids[0]
        self.assertEqual((v["to"], v["contact"], v["cast"], v["folder"]), ("Léa", "uid-1", ["Léa"], "Léa"))
        data, mime = self.vf.read_video(self.email, out["path"])
        self.assertEqual(data, self.mp4.read_bytes())

    def test_same_title_twice_never_overwrites(self):
        a = self.vf.archive(self.email, self.mp4, {"to": "Tim", "title": "Gag"})
        self.mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2048))              # une AUTRE vidéo du même jour et du même titre
        b = self.vf.archive(self.email, self.mp4, {"to": "Tim", "title": "Gag"})
        self.assertNotEqual(a["path"], b["path"], "suffixe -2 : jamais d'écrasement")
        self.assertEqual(len(self.vf.list_videos(self.email)), 2)

    def test_paths_outside_videofun_are_refused_and_ensure_root_is_idempotent(self):
        cs.ingest_plaintext(self.email, "/Photos/secret.jpg", b"x" * 400, content_type="image/jpeg", trigger_faceid=False)
        with self.assertRaises(ValueError):
            self.vf.read_video(self.email, "/Photos/secret.jpg")
        with self.assertRaises(ValueError):
            self.vf.in_root("/VideoFUN/../Photos/secret.jpg")
        self.assertEqual(self.vf.ensure_root(self.email), "/VideoFUN")
        self.assertEqual(self.vf.ensure_root(self.email), "/VideoFUN")
        self.assertEqual(self.vf.list_videos(self.email), [], "un dossier vide : aucune vidéo")

    def test_delete_and_non_video_files_are_not_listed(self):
        out = self.vf.archive(self.email, self.mp4, {"to": "Emma", "title": "Merci"})
        cs.ingest_plaintext(self.email, "/VideoFUN/Emma/notes.txt", b"hello" * 100, content_type="text/plain", trigger_faceid=False)
        self.assertEqual([v["name"] for v in self.vf.list_videos(self.email)], [Path(out["path"]).name])
        self.assertTrue(self.vf.delete_video(self.email, out["path"]))
        self.assertEqual(self.vf.list_videos(self.email), [])

    def test_cloud_routes_list_read_and_delete_videofun(self):
        import asyncio
        import json as _j
        from fastapi import HTTPException
        from routers import cloud
        out = self.vf.archive(self.email, self.mp4, {"to": "Léa", "title": "Pirate"})
        d = _j.loads(asyncio.run(cloud.cloud_videofun(None, npub=self.h)).body)
        self.assertEqual(d["root"], "/VideoFUN")
        self.assertEqual([v["path"] for v in d["videos"]], [out["path"]])
        r = asyncio.run(cloud.cloud_videofun_file(None, path=out["path"], npub=self.h))
        self.assertEqual(r.body, self.mp4.read_bytes())
        self.assertEqual(r.media_type, "video/mp4")
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(cloud.cloud_videofun_file(None, path="/Photos/x.jpg", npub=self.h))
        self.assertEqual(cm.exception.status_code, 400)
        asyncio.run(cloud.cloud_videofun_delete(None, path=out["path"], npub=self.h))
        self.assertEqual(_j.loads(asyncio.run(cloud.cloud_videofun(None, npub=self.h)).body)["videos"], [])

    def test_friend_card_gathers_videos_received_photos_and_shares(self):
        import asyncio
        import json as _j
        import tempfile
        from pathlib import Path as P
        from routers import cloud
        from services import story_share
        self.vf.archive(self.email, self.mp4, {"to": "Léa", "contact": "uid-1", "title": "Pirate"})
        self.vf.archive(self.email, self.mp4, {"to": "Tom", "title": "Autre"})
        cs.ingest_plaintext(self.email, "/Reçues/Léa/2026/10/a.jpg", b"\xff\xd8\xff" + os.urandom(300), content_type="image/jpeg", trigger_faceid=False)
        cs.ingest_plaintext(self.email, "/Reçues/Tom/2026/10/b.jpg", b"\xff\xd8\xff" + os.urandom(300), content_type="image/jpeg", trigger_faceid=False)
        lib = P(tempfile.mkdtemp())
        story_share.create(lib, "", "", "Pirate", "léa", 14, "Moi", {"kind": "ucloud", "email": self.email, "path": "/VideoFUN/Léa/x.mp4"})
        story_share.create(lib, "", "", "Autre", "Tom", 14, "Moi", {"kind": "ucloud", "email": self.email, "path": "/VideoFUN/Tom/y.mp4"})
        from services import friend_card
        c = friend_card.card(self.email, "Léa", "", "", lib)
        self.assertEqual(c["received"]["count"], 1)
        self.assertEqual([v["title"] for v in c["videos"]], ["Pirate"])
        self.assertEqual([s["title"] for s in c["shares"]], ["Pirate"], "nom comparé sans la casse, jamais les liens d'un autre ami")
        self.assertEqual(friend_card.card(self.email, "Léa", "", "", None)["shares"], [])
        by_uid = friend_card.card(self.email, "Autre nom", "", "uid-1", lib)
        self.assertEqual([v["title"] for v in by_uid["videos"]], ["Pirate"], "l'UID de contact retrouve ses vidéos même si le prénom change")
        d = _j.loads(asyncio.run(cloud.cloud_friend(None, name="Léa", hex="", contact="", npub=self.h)).body)
        self.assertEqual(d["received"]["count"], 1)

    def test_send_to_friend_copies_the_same_block_into_his_videofun_received(self):
        """Envoi cloud à cloud : même CID, même clé, rangé dans /VideoFUN/Reçues/<expéditeur>/ du destinataire (visible dans son onglet Vidéos)."""
        friend = f"ami{time.time_ns()}@example.org"
        fh = f"{time.time_ns():064x}"[-64:]
        cs.nostr_dir(friend).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(friend) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1y; HEX={fh};\n")
        out = self.vf.archive(self.email, self.mp4, {"to": "Ami", "title": "Pour toi"})
        res = self.vf.send_to_friend(self.email, out["path"], "Moi", pubkey=fh)
        self.assertEqual((res["mode"], res["status"]), ("local", "copied"))
        self.assertTrue(res["path"].startswith("/VideoFUN/Reçues/Moi/"), res["path"])
        got = self.vf.list_videos(friend)
        self.assertEqual([(v["received"], v["from_name"], v["to"]) for v in got], [(True, "Moi", "")])
        self.assertEqual(cs.db_entry(friend, res["path"])["cid"], cs.db_entry(self.email, out["path"])["cid"], "même bloc, rien de rechiffré")
        self.assertEqual(self.vf.send_to_friend(self.email, out["path"], "Moi", pubkey=fh)["status"], "duplicate", "idempotent par contenu")
        from services import friend_card
        c = friend_card.card(friend, "Moi")
        self.assertEqual([v["title"] for v in c["videos_received"]], [Path(res["path"]).stem])
        self.assertEqual(c["videos"], [], "une vidéo reçue n'est pas une vidéo « pour » lui")

    def test_send_to_friend_refuses_unknown_person_and_non_videos(self):
        out = self.vf.archive(self.email, self.mp4, {"to": "Ami", "title": "Pour toi"})
        with self.assertRaises(LookupError):
            self.vf.send_to_friend(self.email, out["path"], "Moi", pubkey="ee" * 32)
        cs.ingest_plaintext(self.email, "/VideoFUN/notes.txt", b"hello" * 100, content_type="text/plain", trigger_faceid=False)
        with self.assertRaises(ValueError):
            self.vf.send_to_friend(self.email, "/VideoFUN/notes.txt", "Moi", pubkey="ee" * 32)
        with self.assertRaises(ValueError):
            self.vf.send_to_friend(self.email, "/Photos/x.jpg", "Moi", pubkey="ee" * 32)

    def test_poster_frame_from_bytes_and_cache_cleanup(self):
        import shutil
        import subprocess as sp
        import tempfile
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg absent")
        src = self.mp4.with_name("real.mp4")
        sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=12:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
        out = Path(tempfile.mkdtemp()) / "p.jpg"
        self.vf.poster_frame(src.read_bytes(), None, out)
        self.assertEqual(out.read_bytes()[:2], b"\xff\xd8")
        with self.assertRaises(RuntimeError):
            self.vf.poster_frame(b"pas une video", None, Path(tempfile.mkdtemp()) / "q.jpg")

    def test_target_path_cleans_names(self):
        day = time.strftime("%Y-%m-%d", time.localtime(1_700_000_000))
        self.assertEqual(self.vf.target_path("../Léa/..", 'A "super" <titre>: ok', 1_700_000_000), f"/VideoFUN/Léa/{day} A super titre ok.mp4")
        self.assertEqual(self.vf.target_path("", "", 1_700_000_000), f"/VideoFUN/Divers/{day} Vidéo FUN.mp4")


class TestImportDatedDestination(unittest.TestCase):
    """webdav_import : rangement /YYYY/MM par la date de PRISE DE VUE EXIF, sinon par la date du serveur."""

    def entry(self):
        return {"path": "/Photos/IMG_1.jpg", "name": "IMG_1.jpg", "mtime": "Sat, 05 Mar 2022 10:00:00 GMT"}

    def test_exif_date_wins_over_server_date(self):
        import webdav_import as wi
        ts = int(time.mktime(time.strptime("2015:07:14 10:00:00", "%Y:%m:%d %H:%M:%S")))
        self.assertEqual(wi._dated_dest("/Photos/Import", self.entry(), ts), "/Photos/Import/2015/07/IMG_1.jpg")

    def test_server_date_when_no_exif_or_implausible(self):
        import webdav_import as wi
        self.assertEqual(wi._dated_dest("/Photos/Import", self.entry()), "/Photos/Import/2022/03/IMG_1.jpg")
        self.assertEqual(wi._dated_dest("/Photos/Import", self.entry(), 0), "/Photos/Import/2022/03/IMG_1.jpg", "1970 : horloge de l'appareil non réglée")
        self.assertEqual(wi._dated_dest("/Photos/Import", self.entry(), time.time() + 10 * 86400 * 365), "/Photos/Import/2022/03/IMG_1.jpg", "date dans le futur")
        self.assertTrue(wi._plausible_taken(time.time() - 3600))
        self.assertFalse(wi._plausible_taken(None))

    def test_real_exif_is_read_from_original_bytes(self):
        import webdav_import as wi
        from PIL import Image
        im = Image.new("RGB", (40, 30), (1, 2, 3))
        ex = im.getexif()
        ex.get_ifd(0x8769)[0x9003] = "2012:12:25 18:30:00"
        buf = io.BytesIO()
        im.save(buf, "JPEG", exif=ex.tobytes())
        taken = cs._inspect_image(buf.getvalue()).get("taken_at")
        self.assertEqual(wi._dated_dest("/P", self.entry(), taken), "/P/2012/12/IMG_1.jpg")


class TestAnalysisAndDescriptions(_IngestBase):
    def setUp(self):
        super().setUp()
        self.udir = cs.ensure_ucloud_dir(self.email)
        self.h = f"{time.time_ns():064x}"[-64:]
        cs.nostr_dir(self.email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(self.email) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={self.h};\n")
        cs._HEX_MAP_AT = 0.0

    def photos(self, n=3):
        for i in range(n):
            cs.ingest_plaintext(self.email, f"/Photos/p{i}.jpg", _photo(700 + i), content_type="image/jpeg", trigger_faceid=False)

    def test_counts_and_status_explain_each_task(self):
        import asyncio
        from routers import cloud
        self.photos(4)
        cs.ucloud_db.patch_entry(self.udir, "/Photos/p0.jpg", lambda e: e.update(faceid={"status": "faces", "count": 1}) or True)
        cs.ucloud_db.patch_entry(self.udir, "/Photos/p1.jpg", lambda e: e.update(faceid={"status": "pending"}) or True)
        cs.set_photo_description(self.email, "/Photos/p2.jpg", "Une plage au coucher du soleil", "ai", "minicpm")
        c = cs.ucloud_db.analysis_counts(self.udir)
        self.assertEqual((c["images"], c["faces_done"], c["faces_pending"], c["faces_waiting"], c["described"]), (4, 1, 1, 2, 1))
        st = asyncio.run(cloud.cloud_analysis(None, npub=self.h))
        d = json.loads(st.body)
        self.assertEqual(d["faces"]["done"], 1)
        self.assertEqual(d["describe"]["pending"], 3)
        self.assertEqual(d["enrich"]["pending"], 0, "les empreintes sont calculées à l'envoi")
        self.assertIn(d["mode"]["describe"]["effective"], ("direct", "relay", "none"))
        self.assertEqual(d["mode"]["enrich"]["effective"], "local")
        self.assertIn("why", d["mode"]["faces"])

    def test_describe_verbose_prints_each_photo_and_summary(self):
        import contextlib, io, ucloud_describe
        self.addCleanup(os.environ.pop, "FACECLOUD_VERBOSE", None)
        self.photos(2)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "ancienne", "ai", "minicpm-v4.6:latest")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ucloud_describe.scan(self.email, mode="direct", redo=True, verbose=True,
                                 runner=lambda plain: {"description": "Une plage.\nMots-clés : mer", "model": "m2"})
        out = buf.getvalue()
        self.assertIn("1 photo(s) à décrire", out)
        self.assertIn("[1/1] /Photos/p0.jpg", out)
        self.assertIn("Une plage.", out)
        self.assertIn("terminé", out)
        os.environ.pop("FACECLOUD_VERBOSE", None)
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            ucloud_describe.scan(self.email, mode="direct", redo=True, runner=lambda plain: {"description": "x", "model": "m"})
        self.assertNotIn("à décrire", quiet.getvalue(), "sans --verbose : seulement le JSON final")

    def test_trace_vv_gives_one_timestamped_chronology_on_stderr(self):
        import contextlib, io, re as _re, ucloud_describe
        self.addCleanup(os.environ.pop, "FACECLOUD_VERBOSE", None)
        self.photos(3)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "écrite par moi", "user")
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            ucloud_describe.scan(self.email, mode="direct", verbose=2,
                                 runner=lambda plain: {"description": "Une plage.\nMots-clés : mer", "model": "m2"})
        log = err.getvalue()
        lines = [l for l in log.splitlines() if l.startswith("[")]
        self.assertTrue(all(_re.match(r"^\[\d\d:\d\d:\d\d\.\d{3}\] \w+: ", l) for l in lines), "horodatage HH:MM:SS.mmm + composant")
        self.assertIn("chemin=direct", log)
        self.assertIn("sélection initiale : 2", log, "la photo décrite à la main n'est pas dans la sélection « sans description »")
        self.assertIn("écartées", log, "pourquoi des photos sont écartées (description écrite à la main)")
        self.assertIn("[1/2]", log)
        self.assertIn("ancienne description", log)
        self.assertIn("progression écrite", log)
        self.assertNotIn("Une plage.", log, "les descriptions vont sur stdout, le journal sur stderr")
        self.assertIn("Une plage.", out.getvalue())

    def test_no_trace_without_verbose(self):
        import contextlib, io, ucloud_describe
        os.environ.pop("FACECLOUD_VERBOSE", None)
        self.photos(1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ucloud_describe.scan(self.email, mode="direct", runner=lambda plain: {"description": "x", "model": "m"})
        self.assertEqual(err.getvalue(), "")

    def test_search_ignores_accents_case_plurals_and_needs_every_word(self):
        self.photos(4)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "Des moutons sur la plage.\nMots-clés : mouton, mer, été", "ai", "gemma3:12b")
        cs.set_photo_description(self.email, "/Photos/p1.jpg", "Une cuisine avec un réfrigérateur ouvert", "ai", "gemma3:12b")
        cs.set_photo_description(self.email, "/Photos/p2.jpg", "Mariage de Léa à l'église", "user")
        ud = cs.ucloud_db
        found = lambda q: sorted(p for p, _e in ud.page(self.udir, q=q)[0])
        self.assertEqual(found("MOUTON"), ["/Photos/p0.jpg"], "casse")
        self.assertEqual(found("moutons"), ["/Photos/p0.jpg"], "pluriel")
        self.assertEqual(found("refrigerateur"), ["/Photos/p1.jpg"], "accents")
        self.assertEqual(found("Léa eglise"), ["/Photos/p2.jpg"], "accents des deux côtés")
        self.assertEqual(found("moutons plage"), ["/Photos/p0.jpg"], "tous les mots")
        self.assertEqual(found("moutons cuisine"), [], "un mot absent : aucune photo")
        self.assertEqual(found("été"), ["/Photos/p0.jpg"])

    def test_normalised_search_column_is_backfilled_on_old_databases(self):
        import sqlite3
        self.photos(1)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "Éléphant à la plage", "ai", "m")
        conn = cs.ucloud_db.connect(self.udir)
        conn.execute("UPDATE entries SET desc_text='Éléphant à la plage' WHERE path='/Photos/p0.jpg'")   # ancien format
        conn.execute("DELETE FROM meta WHERE key='desc_norm'")
        cs.ucloud_db._ensure_columns(conn)
        self.assertEqual(conn.execute("SELECT desc_text FROM entries WHERE path='/Photos/p0.jpg'").fetchone()[0], "elephant a la plage")
        self.assertEqual([p for p, _e in cs.ucloud_db.page(self.udir, q="elephant")[0]], ["/Photos/p0.jpg"])

    def test_stale_descriptions_are_those_of_another_ai_model_only(self):
        self.photos(3)
        ud = cs.ucloud_db
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "vieille description", "ai", "minicpm-v4.6:latest")
        cs.set_photo_description(self.email, "/Photos/p1.jpg", "bonne description", "ai", "gemma3:12b")
        cs.set_photo_description(self.email, "/Photos/p2.jpg", "écrite par moi", "user")
        self.assertEqual(ud.describe_stale(self.udir, "gemma3:12b"), ["/Photos/p0.jpg"])
        self.assertEqual(ud.describe_stale_count(self.udir, "gemma3:12b"), 1)

    # ── Recherche par le sens (embeddings) — Ollama remplacé par un faux modèle à « concepts » ──────────────
    CONCEPTS = [{"mer", "plage", "sable", "ocean", "vacances"}, {"mouton", "moutons", "ferme", "animaux", "pre"},
                {"cuisine", "frigo", "refrigerateur", "gaz"}, {"mariage", "eglise", "fete"}]

    def fake_embed(self, texts, model=None, timeout=0):
        import numpy as np
        from services import ucloud_db
        out = []
        for t in texts:
            v = np.full(len(self.CONCEPTS) + 1, 0.01, dtype=np.float32)
            for w in ucloud_db.norm_text(t).replace(".", " ").replace(",", " ").split():
                for i, group in enumerate(self.CONCEPTS):
                    if w in group:
                        v[i] += 1.0
            out.append(v / np.linalg.norm(v))
        return np.array(out)

    def index_all(self):
        import ucloud_embed as script
        from services import ucloud_embed as ue
        real = ue.embed_texts
        ue.embed_texts = self.fake_embed
        try:
            return script.scan(self.email, model=ue.MODEL)
        finally:
            ue.embed_texts = real

    def test_semantic_search_finds_by_meaning_without_shared_words(self):
        from services import ucloud_embed as ue
        self.photos(4)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "Des moutons dans un pré.\nMots-clés : ferme, animaux", "ai", "m")
        cs.set_photo_description(self.email, "/Photos/p1.jpg", "Du sable et la mer au soleil", "ai", "m")
        cs.set_photo_description(self.email, "/Photos/p2.jpg", "Un frigo ouvert à côté du gaz", "ai", "m")
        self.assertEqual(ue.counts(self.udir)["pending"], 3)
        st = self.index_all()
        self.assertEqual((st["total"], st["done"]), (3, 3))
        self.assertEqual(ue.counts(self.udir), {"described": 3, "indexed": 3, "pending": 0})
        q = self.fake_embed(["vacances à la plage"])[0]
        ranked = ue.search(self.udir, "vacances à la plage", q)
        self.assertEqual([p for p, _s in ranked], ["/Photos/p1.jpg"], "aucun mot commun avec « sable et la mer » : trouvé par le sens, rien d'autre")
        q = self.fake_embed(["animaux de la ferme"])[0]
        self.assertEqual(ue.search(self.udir, "animaux de la ferme", q)[0][0], "/Photos/p0.jpg")

    def test_keyword_match_outranks_similarity_and_works_without_ollama(self):
        from services import ucloud_embed as ue
        self.photos(3)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "La mer", "ai", "m")
        cs.set_photo_description(self.email, "/Photos/p1.jpg", "Une plage bretonne", "ai", "m")
        self.index_all()
        q = self.fake_embed(["plage"])[0]
        self.assertEqual(ue.search(self.udir, "plage", q)[0][0], "/Photos/p1.jpg", "le mot exact passe devant la simple ressemblance")
        self.assertEqual([p for p, _s in ue.search(self.udir, "bretonne", None)], ["/Photos/p1.jpg"], "sans embedding : mots seulement")

    def test_description_edit_and_new_photo_make_embeddings_pending(self):
        from services import ucloud_embed as ue
        self.photos(2)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "La mer", "ai", "m")
        self.index_all()
        self.assertEqual(ue.counts(self.udir)["pending"], 0)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "Une cuisine", "user")
        self.assertEqual(ue.counts(self.udir)["pending"], 1, "description corrigée : à re-vectoriser")
        cs.set_photo_description(self.email, "/Photos/p1.jpg", "Un mariage", "ai", "m")
        self.assertEqual(len(ue.pending(self.udir)), 2)
        self.assertEqual(ue.counts(self.udir, model="autre-modele")["indexed"], 0, "autre modèle : tout est à refaire")

    def test_search_respects_filters_and_cache_follows_index(self):
        from services import ucloud_embed as ue
        self.photos(2)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "La mer", "ai", "m")
        cs.set_photo_description(self.email, "/Photos/p1.jpg", "La plage", "ai", "m")
        self.index_all()
        q = self.fake_embed(["mer"])[0]
        self.assertEqual(len(ue.search(self.udir, "mer", q)), 2)
        self.assertEqual(ue.search(self.udir, "mer", q, since=int(time.time()) + 10_000), [], "filtre de dates")
        cs.delete_paths(self.email, ["/Photos/p0.jpg"], True)
        self.assertEqual([p for p, _s in ue.search(self.udir, "mer", q)], ["/Photos/p1.jpg"], "une photo supprimée ne ressort plus")

    def test_matrix_cache_reloads_when_another_process_adds_vectors(self):
        from services import ucloud_embed as ue
        self.photos(2)
        cs.set_photo_description(self.email, "/Photos/p0.jpg", "La mer", "ai", "m")
        cs.set_photo_description(self.email, "/Photos/p1.jpg", "Une plage", "ai", "m")
        q = self.fake_embed(["mer"])[0]
        self.assertEqual(ue.search(self.udir, "zzz", q), [], "rien d'indexé : matrice vide mise en cache")
        self.index_all()                                  # ne touche NI l'index NI sa révision
        self.assertEqual(len(ue.search(self.udir, "zzz", q)), 2, "la matrice est rechargée")

    def test_embed_scan_limited_to_given_cids_and_interrupt_is_clean(self):
        import json as _j, ucloud_embed as script
        from services import ucloud_embed as ue
        self.photos(3)
        for i in range(3):
            cs.set_photo_description(self.email, f"/Photos/p{i}.jpg", f"La mer {i}", "ai", "m")
        cid1 = cs.db_entry(self.email, "/Photos/p1.jpg")["cid"]
        real = ue.embed_texts
        ue.embed_texts = self.fake_embed
        try:
            st = script.scan(self.email, only_cids={cid1})
            self.assertEqual((st["total"], st["done"]), (1, 1), "seulement la photo qui vient d'être décrite")
            self.assertEqual(ue.counts(self.udir)["indexed"], 1)

            def boom(texts, model=None, timeout=0):
                raise KeyboardInterrupt
            ue.embed_texts = boom
            with self.assertRaises(KeyboardInterrupt):
                script.scan(self.email)
        finally:
            ue.embed_texts = real
        prog = _j.loads(script.progress_path(self.email).read_text())
        self.assertFalse(prog["running"], "Ctrl-C : l'état ne reste pas « en cours »")
        self.assertEqual(prog["error"], "interrompu")
        self.assertEqual(ue.counts(self.udir)["indexed"], 1, "ce qui était fait est conservé")
        ue.embed_texts = self.fake_embed
        try:
            self.assertEqual(script.scan(self.email)["total"], 2, "le verrou est libéré : on peut relancer et finir")
        finally:
            ue.embed_texts = real

    def test_text_for_puts_keywords_first(self):
        from services import ucloud_embed as ue
        self.assertEqual(ue.text_for("Une cuisine claire.\nMots-clés : cuisine, gaz, frigo"), "cuisine, gaz, frigo. Une cuisine claire.")
        self.assertEqual(ue.text_for("Juste une phrase"), "Juste une phrase")

    def test_user_description_is_never_overwritten_by_ai(self):
        self.photos(1)
        self.assertEqual(cs.set_photo_description(self.email, "/Photos/p0.jpg", "Mariage de Léa, juin 2019", "user"), "set")
        self.assertEqual(cs.set_photo_description(self.email, "/Photos/p0.jpg", "Une église", "ai", "m"), "kept_user")
        e = cs.db_entry(self.email, "/Photos/p0.jpg")
        self.assertEqual((e["desc"]["text"], e["desc"]["src"]), ("Mariage de Léa, juin 2019", "user"))
        self.assertEqual(cs.set_photo_description(self.email, "/Photos/p0.jpg", "", "user"), "cleared")
        self.assertNotIn("desc", cs.db_entry(self.email, "/Photos/p0.jpg"))
        self.assertEqual(cs.set_photo_description(self.email, "/Photos/inconnue.jpg", "x", "user"), "missing")

    def test_search_finds_description_and_scan_describes_direct(self):
        import ucloud_describe
        self.photos(3)
        runner = lambda plain: {"description": "Deux chiens jouent dans la neige.", "model": "fake-vision"}
        out = ucloud_describe.scan(self.email, mode="direct", runner=runner)
        self.assertEqual((out["done"], out["failed"], out["mode"]), (3, 0, "direct"))
        self.assertEqual(cs.ucloud_db.page(self.udir, q="neige")[1], 3, "la recherche porte sur la description")
        self.assertEqual(cs.ucloud_db.page(self.udir, q="chats")[1], 0)
        self.assertEqual(cs.db_entry(self.email, "/Photos/p1.jpg")["desc"]["src"], "ai")
        self.assertEqual(ucloud_describe.scan(self.email, mode="direct", runner=runner)["total"], 0, "rien à refaire")

    def test_scan_stops_early_when_the_model_is_down(self):
        import ucloud_describe
        self.photos(8)
        out = ucloud_describe.scan(self.email, mode="direct", runner=lambda plain: {"error": "connexion refusée"})
        self.assertEqual(out["error"], "model_unavailable")
        self.assertLess(out["done"], 8, "arrêt après 5 échecs d'affilée, pas 8 appels inutiles")

    def test_apply_relay_result(self):
        import ucloud_describe
        self.photos(1)
        rc = ucloud_describe.apply_result({"email": self.email, "path": "/Photos/p0.jpg", "status": "ok",
                                           "description": "Un chat sur un canapé.", "model": "m", "task": "describe"})
        self.assertEqual(rc, 0)
        self.assertEqual(cs.db_entry(self.email, "/Photos/p0.jpg")["desc"]["text"], "Un chat sur un canapé.")
        self.assertEqual(ucloud_describe.apply_result({"email": self.email, "path": "/Photos/p0.jpg", "status": "failed"}), 1)

    def test_put_desc_route_and_mode_config(self):
        import asyncio
        from routers import cloud
        from services import analysis
        self.photos(1)
        r = json.loads(asyncio.run(cloud.cloud_photo_desc(None, path="/Photos/p0.jpg", text="  Pique-nique   chez  Mamie ", npub=self.h)).body)
        self.assertEqual(r["text"], "Pique-nique chez Mamie")
        real = analysis.CONFIG_FILE
        analysis.CONFIG_FILE = _TMP / f"mode-{time.time_ns()}.json"
        try:
            self.assertEqual(analysis.get_mode(), "auto")
            analysis.set_mode("relay")
            self.assertEqual(analysis.resolve("faces")["effective"], "relay" if analysis.relay_ready() else "relay")
            with self.assertRaises(ValueError):
                analysis.set_mode("nimporte")
            analysis.set_mode("direct")
            self.assertEqual(analysis.resolve("describe")["effective"], "direct")
        finally:
            analysis.CONFIG_FILE = real

    def test_refresh_time_info(self):
        from services import analysis
        d = cs.nostr_dir(self.email)
        (d / ".refresh_time").write_text("10:06")
        (d / ".todate").write_text("20200101")
        info = analysis.refresh_info(d)
        self.assertEqual((info["time"], info["done_today"]), ("10:06", False))
        (d / ".todate").write_text(time.strftime("%Y%m%d"))
        self.assertTrue(analysis.refresh_info(d)["done_today"])
        self.assertIsNone(analysis.refresh_info(d / "inexistant")["time"])


class TestNetworkProfiles(unittest.TestCase):
    """Profils kind 0 lus sur plusieurs relais ; vignettes servies par la station."""
    def setUp(self):
        import asyncio
        from services import nostr_profiles as np
        from core.config import settings
        self.asyncio, self.np, self.settings = asyncio, np, settings
        self._real = (np.CACHE_DIR, settings.NOSTR_RELAYS, np.swarm_relays, np.PROFILE_TTL)
        np.CACHE_DIR = _TMP / f"profiles-{time.time_ns()}"
        np.swarm_relays = lambda: []
        self.me = "aa" * 32
        self.a, self.b, self.c = "11" * 32, "22" * 32, "33" * 32
        self.queries = []

    def tearDown(self):
        self.np.CACHE_DIR, self.settings.NOSTR_RELAYS, self.np.swarm_relays, self.np.PROFILE_TTL = self._real

    def serve(self, events):
        """Faux relais : répond aux REQ par les événements correspondant au filtre, puis EOSE."""
        import websockets

        async def handler(ws):
            async for raw in ws:
                msg = json.loads(raw)
                if msg[0] != "REQ":
                    continue
                flt = msg[2]
                self.queries.append(flt)
                for ev in events:
                    if ev["kind"] in flt.get("kinds", []) and (not flt.get("authors") or ev["pubkey"] in flt["authors"]):
                        await ws.send(json.dumps(["EVENT", msg[1], ev]))
                await ws.send(json.dumps(["EOSE", msg[1]]))
        return websockets.serve(handler, "127.0.0.1", 0)

    def test_newest_profile_wins_across_relays_and_declared_relays_are_used(self):
        # deux relais (deux ports) : on vérifie l'union des relais et la fraîcheur du profil
        async def scenario():
            ev = lambda pk, name, ts, pic="": {"kind": 0, "pubkey": pk, "created_at": ts, "content": json.dumps({"name": name, "picture": pic, "lud16": f"{name}@x.org"}), "tags": [], "id": "0" * 64, "sig": "0" * 128}
            async with self.serve([ev(self.b, "Béa", 50), ev(self.a, "Nouveau", 200, "http://x/y.png")]) as s2:
                port2 = s2.sockets[0].getsockname()[1]
                decl = {"kind": 10002, "pubkey": self.me, "created_at": 1, "content": "", "tags": [["r", f"ws://127.0.0.1:{port2}"]], "id": "0" * 64, "sig": "0" * 128}
                async with self.serve([ev(self.a, "Ancien", 100), decl]) as s1:
                    port1 = s1.sockets[0].getsockname()[1]
                    self.settings.NOSTR_RELAYS = f"ws://127.0.0.1:{port1}"
                    first = await self.np.read_profiles([self.a, self.b, self.c, "pas-une-cle"], self.me)
                    n_queries = len(self.queries)
                    second = await self.np.read_profiles([self.a, self.b], self.me)
                    return first, second, n_queries, len(self.queries), port1, port2
        first, second, q1, q2, p1, p2 = self.asyncio.run(scenario())
        self.assertEqual(first["profiles"][self.a]["name"], "Nouveau", "le profil le plus récent gagne, quel que soit le relais")
        self.assertEqual(first["profiles"][self.b]["name"], "Béa")
        self.assertEqual(first["missing"], [self.c])
        self.assertEqual(first["relays"], [f"ws://127.0.0.1:{p1}", f"ws://127.0.0.1:{p2}"], "station d'abord, puis relais déclaré (NIP-65)")
        self.assertEqual(first["profiles"][self.a]["email"], "Nouveau@x.org")
        self.assertEqual((second["cached"], q2), (2, q1), "cache disque : plus aucune requête aux relais")
        self.assertEqual(self.np.cached_profile(self.c), None)

    def test_dead_relay_is_ignored(self):
        self.settings.NOSTR_RELAYS = "ws://127.0.0.1:9"
        res = self.asyncio.run(self.np.read_profiles([self.a], self.me))
        self.assertEqual((res["profiles"], res["missing"]), ({}, [self.a]))

    def test_avatar_is_downloaded_once_resized_and_failures_are_remembered(self):
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (500, 300), (200, 10, 10)).save(buf, "PNG")
        calls = []

        async def fake(url):
            calls.append(url)
            return buf.getvalue() if "ok" in url else None
        real = self.np._download
        self.np._download = fake
        try:
            jpeg = self.asyncio.run(self.np.avatar_jpeg(self.a, "http://h/ok.png"))
            self.assertEqual(Image.open(io.BytesIO(jpeg)).size, (96, 96))
            self.asyncio.run(self.np.avatar_jpeg(self.a, "http://h/ok.png"))
            self.assertEqual(len(calls), 1, "téléchargée UNE fois")
            self.assertIsNone(self.asyncio.run(self.np.avatar_jpeg(self.b, "http://h/cassee.png")))
            self.assertIsNone(self.asyncio.run(self.np.avatar_jpeg(self.b, "http://h/cassee.png")))
            self.assertEqual(len(calls), 2, "l'échec est mémorisé : pas de nouvel essai immédiat")
        finally:
            self.np._download = real

    def test_download_refuses_internal_addresses(self):
        for url in ("http://127.0.0.1:5001/api/v0/id", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd"):
            self.assertIsNone(self.asyncio.run(self.np._download(url)), url)


class TestCloudRoutes(unittest.TestCase):
    def test_delete_root_is_refused(self):
        import asyncio
        from routers import cloud
        email = f"rt{time.time_ns()}@example.com"
        h = "ef" * 32
        cs.nostr_dir(email).mkdir(parents=True, exist_ok=True)
        (cs.nostr_dir(email) / ".secret.nostr").write_text(f"NSEC=n; NPUB=npub1x; HEX={h};\n")
        cs._HEX_MAP_AT = 0.0
        fake = FakeIPFS()
        real = (cs.ipfs_add_bytes, cs.ipfs_cat)
        cs.ipfs_add_bytes, cs.ipfs_cat = fake.add_bytes, fake.cat
        try:
            cs.ingest_plaintext(email, "/Photos/a.txt", b"a", content_type="text/plain")
            for bad in ('["/"]', '[""]', '["/Photos/../.."]'):
                r = asyncio.run(cloud.delete_cloud_files(None, paths=bad, npub=h))
                self.assertEqual(r.status_code, 400, bad)
            self.assertIn("/Photos/a.txt", cs.load_index(email)["entries"], "rien n'a été supprimé")
            r = asyncio.run(cloud.delete_cloud_files(None, paths='["/Photos/a.txt"]', npub=h))
            self.assertEqual(r.status_code, 200)
        finally:
            cs.ipfs_add_bytes, cs.ipfs_cat = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
