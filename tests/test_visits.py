"""Compteur de visites de l'essaim : comptage par IP, anti-pollution, parole de l'essaim signée par les seules stations membres."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import visits as visits_router
from services import visits

NODE_A, NODE_B, STRANGER = "a" * 64, "b" * 64, "c" * 64


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(visits, "BASE", tmp_path / "visits")
    monkeypatch.setattr(visits, "SALT_FILE", tmp_path / ".visit_salt")
    monkeypatch.setattr(visits, "MAX_SITES", 3)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(visits_router.router)
    return TestClient(app)


def ev(pubkey, sites, created_at=1):
    return {"pubkey": pubkey, "created_at": created_at, "content": json.dumps({"sites": sites})}


@pytest.mark.parametrize("raw,expected", [("G1SMS.fr", "g1sms.fr"), ("g1sms.fr.", "g1sms.fr"), ("a.b-c.example.org", "a.b-c.example.org"),
                                          ("https://g1sms.fr", None), ("g1sms.fr/x", None), ("g1sms.fr:8080", None), ("../etc", None),
                                          ("localhost", None), ("", None), (None, None), (42, None), ("a" * 300 + ".fr", None)])
def test_normalize_site(raw, expected):
    assert visits.normalize_site(raw) == expected


def test_same_ip_counts_once_a_day_per_site_and_ip_is_never_stored():
    assert visits.count_visit("g1sms.fr", "1.2.3.4") is True
    assert visits.count_visit("g1sms.fr", "1.2.3.4") is False
    assert visits.count_visit("g1sms.fr", "5.6.7.8") is True
    assert visits.count_visit("autre.fr", "1.2.3.4") is True          # un site différent compte à part
    assert visits.station_totals()["g1sms.fr"]["total"] == 2
    stored = " ".join(p.read_text() for p in visits.BASE.rglob("*") if p.is_file())
    assert "1.2.3.4" not in stored and "5.6.7.8" not in stored
    assert oct(visits.SALT_FILE.stat().st_mode & 0o777) == "0o600"


def test_station_refuses_new_sites_beyond_the_cap_but_keeps_counting_known_ones():
    for i in range(3):
        assert visits.count_visit(f"s{i}.fr", "1.1.1.1") is True
    assert visits.count_visit("s9.fr", "1.1.1.1") is None
    assert visits.count_visit("s0.fr", "2.2.2.2") is True


def test_contribute_clicks_are_counted_per_placement_and_ip_and_do_not_change_visits():
    assert visits.count_visit("g1sms.fr", "1.1.1.1") is True
    assert visits.count_visit("g1sms.fr", "1.1.1.1", "contribute", "hero") is True
    assert visits.count_visit("g1sms.fr", "1.1.1.1", "contribute", "hero") is False      # même IP, même bouton, même jour
    assert visits.count_visit("g1sms.fr", "1.1.1.1", "contribute", "header") is True     # autre bouton
    assert visits.count_visit("g1sms.fr", "2.2.2.2", "contribute", "hero") is True       # autre IP
    t = visits.station_totals()["g1sms.fr"]
    assert t["total"] == 1 and t["events"] == {"contribute:hero": 2, "contribute:header": 1}


def test_click_placements_are_capped_per_site(monkeypatch):
    monkeypatch.setattr(visits, "MAX_EVENT_KEYS", 2)
    assert visits.count_visit("g1sms.fr", "1.1.1.1", "contribute", "a") is True
    assert visits.count_visit("g1sms.fr", "1.1.1.1", "contribute", "b") is True
    assert visits.count_visit("g1sms.fr", "1.1.1.1", "contribute", "c") is None
    assert visits.count_visit("g1sms.fr", "2.2.2.2", "contribute", "a") is True          # emplacement connu : toujours compté


def test_route_validates_click_events(client):
    post = lambda body, **kw: client.post("/visit", content=json.dumps(body), **kw)
    assert post({"site": "g1sms.fr", "event": "contribute", "label": "hero"}).json() == {"ok": True, "counted": True}
    assert post({"site": "g1sms.fr", "event": "contribute", "label": "hero"}).json()["counted"] is False
    assert post({"site": "g1sms.fr", "event": "inconnu", "label": "hero"}).status_code == 400
    assert post({"site": "g1sms.fr", "event": "contribute", "label": "Pas Valide!"}).status_code == 400
    assert post({"site": "g1sms.fr", "event": "contribute", "label": 3}).status_code == 400


def test_route_counts_and_checks_origin(client):
    assert client.post("/visit", content=json.dumps({"site": "g1sms.fr"}), headers={"origin": "https://g1sms.fr"}).json() == {"ok": True, "counted": True}
    assert client.post("/visit", content=json.dumps({"site": "g1sms.fr"}), headers={"origin": "https://g1sms.fr"}).json()["counted"] is False
    assert client.post("/visit", content=json.dumps({"site": "g1sms.fr"}), headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/visit", content=json.dumps({"site": "pas un site"})).status_code == 400
    assert client.post("/visit", content="pas du json").status_code == 400


def test_swarm_word_sums_member_stations_only_and_ignores_garbage():
    members = {NODE_A: "", NODE_B: ""}
    events = [ev(NODE_A, {"g1sms.fr": {"total": 10}}), ev(NODE_B, {"g1sms.fr": {"total": 5}, "x.org": {"total": 1}}),
              ev(STRANGER, {"g1sms.fr": {"total": 999999}}),                       # n'est pas une station de l'essaim
              ev(NODE_A, {"g1sms.fr": {"total": 12}}, created_at=2),               # plus récent : remplace le précédent
              ev(NODE_A, {"g1sms.fr": {"total": 7}}, created_at=1)]                # plus ancien : ignoré
    assert visits.aggregate(events, members) == {"g1sms.fr": {"total": 17, "stations": 2, "events": {}}, "x.org": {"total": 1, "stations": 1, "events": {}}}


@pytest.mark.parametrize("bad", [{"g1sms.fr": {"total": -1}}, {"g1sms.fr": {"total": True}}, {"g1sms.fr": {"total": "9"}},
                                 {"pas un site": {"total": 3}}, {"g1sms.fr": 3}])
def test_swarm_word_ignores_invalid_values(bad):
    assert visits.aggregate([ev(NODE_A, bad)], {NODE_A: ""}) == {}


def test_swarm_word_sums_click_events_and_ignores_invalid_keys():
    a = ev(NODE_A, {"g1sms.fr": {"total": 1, "events": {"contribute:hero": 3, "contribute:band": 1, "n'importe quoi": 9, "contribute:x": -1}}})
    b = ev(NODE_B, {"g1sms.fr": {"total": 1, "events": {"contribute:hero": 2}}})
    assert visits.aggregate([a, b], {NODE_A: "", NODE_B: ""})["g1sms.fr"]["events"] == {"contribute:hero": 5, "contribute:band": 1}


def test_swarm_word_survives_a_broken_event():
    broken = {"pubkey": NODE_A, "created_at": 1, "content": "pas du json"}
    assert visits.aggregate([broken, ev(NODE_B, {"g1sms.fr": {"total": 4}})], {NODE_A: "", NODE_B: ""}) == {"g1sms.fr": {"total": 4, "stations": 1, "events": {}}}


def test_only_first_bootstrap_station_speaks_for_the_swarm(tmp_path, monkeypatch):
    nodes = tmp_path / "A_boostrap_nodes.txt"
    nodes.write_text("# commentaire\n/ip4/1.2.3.4/tcp/4001/p2p/FIRST\n/ip4/5.6.7.8/tcp/4001/p2p/SECOND\n")
    monkeypatch.setattr(visits.settings, "GAME_PATH", tmp_path)
    monkeypatch.setattr(visits.settings, "ZEN_PATH", tmp_path / "zen")
    (tmp_path / "MY_boostrap_nodes.txt").write_text(nodes.read_text())
    monkeypatch.setattr(visits.settings, "IPFSNODEID", "FIRST")
    assert visits.is_bootstrap_station() is True
    monkeypatch.setattr(visits.settings, "IPFSNODEID", "SECOND")
    assert visits.is_bootstrap_station() is False
    monkeypatch.setattr(visits.settings, "IPFSNODEID", "")
    assert visits.is_bootstrap_station() is False


def test_oracle_permit_filters_stations_by_valid_credential(monkeypatch):
    import time
    from services import relay_io
    captains = ["1" * 64, "2" * 64, "3" * 64]
    now = int(time.time())
    creds = [{"tags": [["d", "PERMIT_DRAGON"], ["p", captains[0]]]},                                      # valide, sans expiration
             {"tags": [["d", "PERMIT_DRAGON"], ["p", captains[1]], ["expiration", str(now + 1000)]]},     # valide
             {"tags": [["d", "PERMIT_DRAGON"], ["p", captains[2]], ["expiration", str(now - 1000)]]}]     # expirée
    monkeypatch.setattr(relay_io, "query", lambda filt, **kw: creds)
    assert visits.credentialed_captains(captains, "PERMIT_DRAGON") == {captains[0], captains[1]}
