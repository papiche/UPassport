"""Compteur d'échecs PASS côté serveur (routers/identity.py::_check_pass)."""
import asyncio
import json

import pytest

from core.config import settings
from routers import identity

EMAIL = "test@example.org"


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GAME_PATH", tmp_path)
    alerts = []

    async def fake_alert(email, attempts, ip):
        alerts.append((email, attempts))

    monkeypatch.setattr(identity, "_notify_pass_alert", fake_alert)
    monkeypatch.setattr(identity, "log_user_event", lambda *a, **k: None)
    monkeypatch.setattr(identity, "log_node_event", lambda *a, **k: None)
    d = tmp_path / "nostr" / EMAIL
    d.mkdir(parents=True)
    (d / ".pass").write_text("1234\n")
    return d, alerts


def _run(code):
    async def go():
        r = identity._check_pass(EMAIL, code, "test")
        await asyncio.sleep(0)  # laisse tourner la tâche d'alerte éventuelle
        return r
    return asyncio.run(go())


def _body(resp):
    return json.loads(bytes(resp.body))


def test_good_pass_resets_counter(account):
    d, _ = account
    assert _body(_run("0000"))["attempts_left"] == 2
    assert _run("1234") is None
    assert _body(_run("0000"))["attempts_left"] == 2


def test_lock_after_three_failures(account):
    d, alerts = account
    assert _body(_run("0000"))["attempts_left"] == 2
    assert _body(_run("1111"))["attempts_left"] == 1
    last = _body(_run("2222"))
    assert last["locked"] is True and last["error"] == "INVALID_PASS"
    assert not (d / ".pass").exists()
    assert alerts == [(EMAIL, 3)]
    # PASS invalidé : même le bon code ne passe plus
    assert _body(_run("1234"))["error"] == "PASS_UNAVAILABLE"


def test_old_failures_expire(account):
    d, _ = account
    (d / ".pass.fails").write_text(json.dumps({"count": 2, "last": 0}))
    assert _body(_run("0000"))["attempts_left"] == 2


def test_disabled_flag_wins(account):
    d, _ = account
    (d / ".pass.disabled").touch()
    assert _body(_run("1234"))["error"] == "PASS_DISABLED"


def test_g1_onboard_recovery_flow(account):
    """Page /g1 : email existant → 409 need_pass, mauvais PASS → 401 avec
    essais restants, bon PASS → billet (recovered + QR P1)."""
    import httpx
    from fastapi import FastAPI

    d, _ = account
    (d / "G1PUBNOSTR").write_text("G1PUB")
    (d / ".multipass.json").write_text(json.dumps({
        "email": EMAIL, "pass": "1234", "ssss": "M-TestPartP1", "npub": "npub1x"}))
    app = FastAPI()
    app.include_router(identity.router)
    form = {"email": EMAIL, "lang": "fr", "lat": "0.00", "lon": "0.00"}

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r1 = await c.post("/g1/onboard", data=form)
            r2 = await c.post("/g1/onboard", data={**form, "pass_code": "9999"})
            r3 = await c.post("/g1/onboard", data={**form, "pass_code": "1234"})
            return r1, r2, r3

    r1, r2, r3 = asyncio.run(go())
    assert r1.status_code == 409 and r1.json()["need_pass"] is True
    assert r2.status_code == 401 and r2.json()["attempts_left"] == 2
    body = r3.json()
    assert r3.status_code == 200 and body["recovered"] is True and body["pass"] == "1234"
    assert body.get("p1_qr", "").startswith("data:image/png;base64,")
