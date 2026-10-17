"""
tests/test_visio.py — salles de visio privées (/api/visio/room) et NIP-98 sur /api/myGPS.

Vraies clés et vraies signatures NIP-98 (nostr_sdk), application complète (Nip98BindingMiddleware
comprise) ; aucun réseau, aucun compte réel.
"""

import base64
import importlib.util
import json
import sys
import time
from unittest.mock import patch

import httpx
import nostr_sdk
import pytest
import pytest_asyncio
from httpx import ASGITransport

spec = importlib.util.spec_from_file_location("main_app_visio", "54321.py")
main_app = importlib.util.module_from_spec(spec)
sys.modules["main_app_visio"] = main_app
spec.loader.exec_module(main_app)
app = main_app.app

from routers import visio  # noqa: E402


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_limits():
    visio._hits.clear()
    yield
    visio._hits.clear()


def nip98(keys, method, path, created_at=None):
    tags = [nostr_sdk.Tag.parse(["u", "http://test" + path]), nostr_sdk.Tag.parse(["method", method])]
    b = nostr_sdk.EventBuilder(nostr_sdk.Kind(27235), "").tags(tags)
    if created_at:
        b = b.custom_created_at(nostr_sdk.Timestamp.from_secs(created_at))
    ev = b.finalize(keys).as_json()
    return {"Authorization": "Nostr " + base64.b64encode(ev.encode()).decode()}


@pytest.mark.asyncio
async def test_room_requires_nip98(client):
    assert (await client.post("/api/visio/room")).status_code == 401


@pytest.mark.asyncio
async def test_room_is_random_private_and_well_formed(client):
    keys = nostr_sdk.Keys.generate()
    with patch.object(visio, "get_env_from_mysh", return_value="https://vdo.copylaradio.com"):
        a = (await client.post("/api/visio/room", headers=nip98(keys, "POST", "/api/visio/room"))).json()
        b = (await client.post("/api/visio/room", headers=nip98(keys, "POST", "/api/visio/room"))).json()
    assert a["room"] != b["room"] and a["password"] != b["password"]
    assert len(a["room"]) == 20 and len(a["password"]) == 16
    assert a["join_url"] == f"https://vdo.copylaradio.com/?room={a['room']}#p={a['password']}"
    assert "password=" not in a["join_url"]                      # le secret n'est pas dans la partie envoyée au serveur
    keys_pub = keys.public_key().to_hex()
    assert keys_pub[:10] not in a["room"]                       # plus dérivée de l'npub public
    assert a["expires_at"] > time.time() + 3600


@pytest.mark.asyncio
async def test_room_rejects_event_bound_to_another_route(client):
    keys = nostr_sdk.Keys.generate()
    r = await client.post("/api/visio/room", headers=nip98(keys, "POST", "/api/autre"))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_rate_limit_per_key(client):
    keys, other = nostr_sdk.Keys.generate(), nostr_sdk.Keys.generate()
    for _ in range(visio.PER_KEY_PER_HOUR):
        assert (await client.post("/api/visio/room", headers=nip98(keys, "POST", "/api/visio/room"))).status_code == 200
    assert (await client.post("/api/visio/room", headers=nip98(keys, "POST", "/api/visio/room"))).status_code == 429
    assert (await client.post("/api/visio/room", headers=nip98(other, "POST", "/api/visio/room"))).status_code == 200


@pytest.mark.asyncio
async def test_base_must_be_https(client):
    with patch.object(visio, "get_env_from_mysh", return_value="http://evil.example"):
        assert (await client.get("/api/visio/config")).json() == {"base": visio.DEFAULT_BASE}


# ── /api/myGPS : NIP-98 d'abord, NIP-42 en repli ─────────────────────────────────
@pytest.mark.asyncio
async def test_mygps_nip98_needs_no_relay_and_derives_npub(client):
    keys = nostr_sdk.Keys.generate()
    async def boom(*a, **k):
        raise AssertionError("NIP-42 ne doit pas être consulté quand NIP-98 est valide")
    with patch("routers.geo.verify_nostr_auth", boom):
        r = await client.get("/api/myGPS", headers=nip98(keys, "GET", "/api/myGPS"))
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "unknown"                      # inconnu ici : réponse de roaming normale
    assert "debug" not in r.json()


@pytest.mark.asyncio
async def test_mygps_nip98_with_matching_and_mismatching_npub(client):
    keys, other = nostr_sdk.Keys.generate(), nostr_sdk.Keys.generate()
    with patch("routers.geo.verify_nostr_auth", side_effect=AssertionError("pas de NIP-42")):
        ok = await client.get("/api/myGPS", params={"npub": keys.public_key().to_bech32()},
                              headers=nip98(keys, "GET", "/api/myGPS"))
        bad = await client.get("/api/myGPS", params={"npub": other.public_key().to_bech32()},
                               headers=nip98(keys, "GET", "/api/myGPS"))
    assert ok.status_code == 200
    assert bad.status_code == 403


@pytest.mark.asyncio
async def test_mygps_legacy_nip42_fallback_still_works_and_is_logged(client, caplog):
    keys = nostr_sdk.Keys.generate()
    async def refused(*a, **k):
        return False
    with patch("routers.geo.verify_nostr_auth", refused), caplog.at_level("WARNING"):
        r = await client.get("/api/myGPS", params={"npub": keys.public_key().to_bech32()})
    assert r.status_code == 403
    assert r.json()["error"]["nip42_challenge"]                 # le client historique reçoit son défi
    assert any("NIP42-FALLBACK path=/api/myGPS" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_mygps_without_npub_nor_header_is_422(client):
    assert (await client.get("/api/myGPS")).status_code == 422
