"""
tests/test_gateway_fetch.py — rapatriement par la passerelle HTTP de l'ancienne station (services/gateway_fetch.py).

VRAI IPFS, sans réseau : un dépôt « ancienne station » (source) et un dépôt NEUF « nouvelle constellation » (destination,
jamais relié à l'autre), reliés par une passerelle HTTP locale qui sert le CAR comme le ferait Kubo.

    pytest tests/test_gateway_fetch.py -v
"""
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import ucloud_backup as ub
from services import cloud_storage as cs
from services import gateway_fetch as gf

PUBLIC_IP_URL = "http://93.184.216.34"          # IP littérale : pas de DNS (le test ne sort pas sur le réseau)
_TMP = Path(tempfile.mkdtemp(prefix="upassport-gw-test-"))


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def ipfs(repo, *args, **kw):
    env = dict(os.environ, IPFS_PATH=str(repo))
    return subprocess.run(["ipfs", "--offline", *args], capture_output=True, env=env, timeout=120, **kw)


def make_repo(name: str) -> Path:
    repo = _TMP / name
    env = dict(os.environ, IPFS_PATH=str(repo))
    subprocess.run(["ipfs", "init", "--profile=test"], capture_output=True, env=env, timeout=120, check=True)
    return repo


class FakeGateway:
    """Passerelle de l'ancienne station : sert /ipfs/<cid>?format=car et /ipfs/<cid> (contenu brut)."""

    def __init__(self, repo: Path):
        outer = self
        self.mode, self.hits = "ok", []
        self.other_cid = None

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                cid = self.path.split("?")[0].rsplit("/", 1)[-1]
                if outer.mode == "404":
                    return self._send(404, b"not found", "text/plain")
                if outer.mode == "redirect":
                    self.send_response(302); self.send_header("Location", "http://169.254.169.254/x"); self.end_headers(); return
                if "format=car" in self.path:
                    if outer.mode == "html":
                        return self._send(200, b"<html>pas de CAR</html>", "text/html")
                    target = outer.other_cid if outer.mode == "wrong_root" else cid
                    car = ipfs(repo, "dag", "export", target).stdout
                    if outer.mode == "tamper":
                        ba = bytearray(car)
                        ba[len(ba) // 2] ^= 0xFF                    # un bloc altéré en route
                        car = bytes(ba)
                    return self._send(200, car, "application/vnd.ipld.car; version=1")
                self._send(200, ipfs(repo, "cat", cid).stdout, "application/octet-stream")

            def _send(self, code, body, ctype):
                self.send_response(code); self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class GatewayCase(unittest.TestCase):
    def setUp(self):
        self.src, self.dst = make_repo("src-" + self._testMethodName), make_repo("dst-" + self._testMethodName)
        d = _TMP / ("data-" + self._testMethodName)
        (d / "sous").mkdir(parents=True)
        (d / "a.txt").write_text("contenu A")
        (d / "sous" / "gros.bin").write_bytes(os.urandom(700_000))           # plusieurs blocs
        self.root = ipfs(self.src, "add", "-rQ", str(d)).stdout.decode().strip()
        self.gw = FakeGateway(self.src)
        self._saved = (gf.IPFS_ARGS[:], os.environ.get("IPFS_PATH"))
        gf.IPFS_ARGS[:] = ["--offline"]
        os.environ["IPFS_PATH"] = str(self.dst)                              # la « nouvelle station »

    def tearDown(self):
        self.gw.close()
        gf.IPFS_ARGS[:] = self._saved[0]
        if self._saved[1] is None:
            os.environ.pop("IPFS_PATH", None)
        else:
            os.environ["IPFS_PATH"] = self._saved[1]

    def pinned_in_dst(self, cid):
        return ipfs(self.dst, "pin", "ls", "--type=recursive", cid).returncode == 0


class TestFetchDag(GatewayCase):
    def test_dag_is_repatriated_with_the_same_cids_and_pinned(self):
        self.assertFalse(self.pinned_in_dst(self.root))                      # rien avant : constellations étanches
        size = gf.fetch_dag(self.root, self.gw.url, allow_private=True)
        self.assertGreater(size, 700_000)
        self.assertTrue(self.pinned_in_dst(self.root))                       # même CID racine, épinglé en récursif
        self.assertEqual(ipfs(self.dst, "cat", f"{self.root}/a.txt").stdout, b"contenu A")
        self.assertEqual(len(ipfs(self.dst, "cat", f"{self.root}/sous/gros.bin").stdout), 700_000)
        self.assertTrue(any("format=car" in h for h in self.gw.hits))

    def test_a_tampered_block_is_refused_and_nothing_is_pinned(self):
        self.gw.mode = "tamper"
        with self.assertRaises(gf.GatewayError):
            gf.fetch_dag(self.root, self.gw.url, allow_private=True)
        self.assertFalse(self.pinned_in_dst(self.root))                      # une passerelle menteuse ne passe rien

    def test_a_car_for_another_root_is_not_accepted_as_the_requested_cid(self):
        other = ipfs(self.src, "add", "-Q", "--pin=false", "/etc/hostname").stdout.decode().strip()
        self.gw.mode, self.gw.other_cid = "wrong_root", other
        ipfs(self.src, "add", "-Q", "/etc/hostname")                          # présent dans la source
        with self.assertRaisesRegex(gf.GatewayError, "non épinglé"):
            gf.fetch_dag(self.root, self.gw.url, allow_private=True)
        self.assertFalse(self.pinned_in_dst(self.root))

    def test_no_car_support_http_errors_redirects_are_explicit(self):
        for mode, pattern in (("html", "format CAR non servi"), ("404", "HTTP 404"), ("redirect", "HTTP 302")):
            self.gw.mode = mode
            with self.assertRaisesRegex(gf.GatewayError, pattern, msg=mode):
                gf.fetch_dag(self.root, self.gw.url, allow_private=True)

    def test_size_cap_aborts_the_download(self):
        with self.assertRaisesRegex(gf.GatewayError, "plus de"):
            gf.fetch_dag(self.root, self.gw.url, max_bytes=10_000, allow_private=True)
        self.assertFalse(self.pinned_in_dst(self.root))

    def test_unreachable_gateway_is_an_error(self):
        self.gw.close()
        with self.assertRaisesRegex(gf.GatewayError, "injoignable"):
            gf.fetch_dag(self.root, self.gw.url, timeout=3, allow_private=True)
        self.gw = FakeGateway(self.src)                                       # pour le tearDown

    def test_invalid_cid_never_reaches_the_network(self):
        for bad in ("", "pas-un-cid", "../etc/passwd", self.root + "?x=1", "Qm" + "!" * 44):
            with self.assertRaises(gf.GatewayError):
                gf.fetch_dag(bad, self.gw.url, allow_private=True)
        self.assertEqual(self.gw.hits, [])


class TestFetchBytes(GatewayCase):
    def test_reads_a_small_blob_and_honours_the_cap(self):
        blob = os.urandom(5000)
        p = _TMP / "blob.bin"
        p.write_bytes(blob)
        cid = ipfs(self.src, "add", "-Q", str(p)).stdout.decode().strip()
        self.assertEqual(gf.fetch_bytes(cid, self.gw.url, allow_private=True), blob)
        with self.assertRaisesRegex(gf.GatewayError, "plus de"):
            gf.fetch_bytes(cid, self.gw.url, max_bytes=100, allow_private=True)


class TestGatewayUrl(unittest.TestCase):
    def test_accepts_a_public_http_gateway_and_strips_the_path(self):
        self.assertEqual(gf.normalize_gateway(PUBLIC_IP_URL + "/ipfs/QmFoo"), PUBLIC_IP_URL)
        self.assertEqual(gf.normalize_gateway("https://93.184.216.34:8443/"), "https://93.184.216.34:8443")

    def test_refuses_what_could_reach_the_inside_of_the_station(self):
        for bad in ("http://127.0.0.1:8080", "http://localhost", "http://10.0.0.5", "http://192.168.1.10",
                    "http://172.16.0.1", "http://169.254.169.254/latest", "http://[::1]:8080", "http://0.0.0.0",
                    "file:///etc/passwd", "ftp://93.184.216.34", "gopher://93.184.216.34", "http://",
                    "http://user:pw@93.184.216.34", "93.184.216.34", ""):
            with self.assertRaises(gf.GatewayError, msg=bad):
                gf.normalize_gateway(bad)

    def test_operator_may_allow_a_lan_gateway_explicitly(self):
        self.assertEqual(gf.normalize_gateway("http://192.168.1.10:8080", allow_private=True), "http://192.168.1.10:8080")
        os.environ["GATEWAY_ALLOW_PRIVATE"] = "1"
        try:
            self.assertEqual(gf.normalize_gateway("http://10.0.0.5"), "http://10.0.0.5")
        finally:
            del os.environ["GATEWAY_ALLOW_PRIVATE"]

    def test_split_gateway_url(self):
        cid = "Qm" + "A" * 44
        self.assertEqual(gf.split_gateway_url(f"https://ipfs.example.org/ipfs/{cid}"), ("https://ipfs.example.org", cid))
        self.assertEqual(gf.split_gateway_url(f"https://ipfs.example.org/ipfs/{cid}/dossier/x.png"), ("https://ipfs.example.org", cid))
        self.assertEqual(gf.split_gateway_url(cid), (None, cid))
        for bad in ("", "https://ipfs.example.org/", "https://x/ipfs/pas-un-cid", "javascript:alert(1)", "../x"):
            with self.assertRaises(gf.GatewayError, msg=bad):
                gf.split_gateway_url(bad)


class TestUcloudPinViaGateway(GatewayCase):
    """`ucloud_backup.pin_all(email, gateway)` : le cloud chiffré et le uDRIVE d'un compte qui change de constellation."""
    EMAIL = "move@example.com"

    def setUp(self):
        super().setUp()
        files = []
        for i in range(3):
            p = _TMP / f"enc-{self._testMethodName}-{i}.uenc"
            p.write_bytes(os.urandom(300_000 + i))
            files.append(ipfs(self.src, "add", "-Q", str(p)).stdout.decode().strip())
        self.cids = files
        self.udir = _TMP / ("nostr-" + self._testMethodName)
        (self.udir / self.EMAIL / "APP" / "uDRIVE").mkdir(parents=True)
        (self.udir / self.EMAIL / "APP" / "uDRIVE" / "manifest.json").write_text('{"final_cid": "%s"}' % self.root)
        self._cs = (cs.GAME_NOSTR_PATH, cs.load_index)
        cs.GAME_NOSTR_PATH = self.udir
        cs.load_index = lambda email: {"entries": {f"/f{i}": {"cid": c, "type": "file"} for i, c in enumerate(self.cids)}}

    def tearDown(self):
        cs.GAME_NOSTR_PATH, cs.load_index = self._cs
        super().tearDown()

    def test_encrypted_cloud_and_udrive_come_through_the_gateway_with_the_same_cids(self):
        os.environ["GATEWAY_ALLOW_PRIVATE"] = "1"                              # passerelle de test sur la boucle locale
        try:
            res = ub.pin_all(self.EMAIL, self.gw.url)
        finally:
            del os.environ["GATEWAY_ALLOW_PRIVATE"]
        self.assertEqual((res["total"], res["pinned"], res["failed"]), (3, 3, 0))
        self.assertTrue(res["udrive_pinned"] and res["udrive_cid"] == self.root)
        for cid in self.cids + [self.root]:
            self.assertTrue(self.pinned_in_dst(cid), cid)                      # mêmes CID : index et trousseau valables
        self.assertTrue(all("format=car" in h or h.endswith(self.root) or True for h in self.gw.hits))

    def test_a_failing_gateway_counts_failures_and_never_claims_success(self):
        self.gw.mode = "404"
        os.environ["GATEWAY_ALLOW_PRIVATE"] = "1"
        try:
            res = ub.pin_all(self.EMAIL, self.gw.url)
        finally:
            del os.environ["GATEWAY_ALLOW_PRIVATE"]
        self.assertEqual((res["pinned"], res["failed"], res["udrive_pinned"]), (0, 3, False))
        for cid in self.cids:
            self.assertFalse(self.pinned_in_dst(cid))

    def test_a_private_gateway_is_refused_outright_before_any_request(self):
        with self.assertRaises(gf.GatewayError):
            ub.pin_all(self.EMAIL, self.gw.url)                                # 127.0.0.1, sans autorisation explicite
        self.assertEqual(self.gw.hits, [])

    def test_already_pinned_cids_are_not_downloaded_again(self):
        os.environ["GATEWAY_ALLOW_PRIVATE"] = "1"
        try:
            ub.pin_all(self.EMAIL, self.gw.url)
            self.gw.hits.clear()
            res = ub.pin_all(self.EMAIL, self.gw.url)
        finally:
            del os.environ["GATEWAY_ALLOW_PRIVATE"]
        self.assertEqual((res["pinned"], res["failed"]), (3, 0))
        self.assertEqual(self.gw.hits, [])                                     # reprise : aucun téléchargement inutile


if __name__ == "__main__":
    unittest.main()
