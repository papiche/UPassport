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
        self.assertEqual(cs.index_path(self.email).stat().st_mode & 0o777, 0o600)

    def test_keyring_separate_and_0600(self):
        """Le keyring est un FICHIER DISTINCT : une fuite de l'index seul
        n'expose aucune clé de déchiffrement."""
        cs.save_index(self.email, cs.empty_index())
        cs.save_keyring(self.email, {"Qm1": {"key_hex": "ab" * 32}})

        self.assertNotEqual(cs.index_path(self.email), cs.keyring_path(self.email))
        raw_index = cs.index_path(self.email).read_text()
        self.assertNotIn("key_hex", raw_index)
        self.assertNotIn("ab" * 32, raw_index)

        self.assertEqual(cs.keyring_path(self.email).stat().st_mode & 0o777, 0o600)
        self.assertEqual(cs.get_key_hex(self.email, "Qm1"), "ab" * 32)
        self.assertIsNone(cs.get_key_hex(self.email, "Qm-absent"))

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
        cs.index_path(self.email).write_text("{ ceci n'est pas du json")
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
        self.assertEqual(cs.keyring_path(EMAIL).stat().st_mode & 0o777, 0o600)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
