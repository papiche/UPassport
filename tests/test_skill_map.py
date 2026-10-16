"""« Qui d'autre maîtrise ça dans le coin ? » : annuaire par case (UMAP / SECTOR / REGION) vérifié contre les certificats 30503.
Relais simulé en mémoire (vrais événements signés), dérivation des clés de case simulée (keygen n'est pas l'objet du test)."""
import hashlib
import time

import pytest

from services import relay_io, skill_map


class FakeRelay:
    def __init__(self):
        self.events = {}

    def publish(self, raws, relays=None):
        for e in raws:
            if e["kind"] == 5:                                   # retrait : supprime les événements visés (a-tag kind:pubkey:d)
                for t in e["tags"]:
                    if t[0] == "a":
                        k, pk, d = t[1].split(":", 2)
                        for i, old in list(self.events.items()):
                            if old["kind"] == int(k) and old["pubkey"] == pk and skill_map._tag(old, "d") == d and old["pubkey"] == e["pubkey"]:
                                del self.events[i]
            else:                                                # remplaçable paramétré : un seul par (kind, auteur, d)
                for i, old in list(self.events.items()):
                    if old["kind"] == e["kind"] and old["pubkey"] == e["pubkey"] and skill_map._tag(old, "d") == skill_map._tag(e, "d"):
                        del self.events[i]
                self.events[e["id"]] = e
        return ["fake"]

    def query(self, filt, timeout=8.0, strict=False, relays=None):
        out = []
        for e in self.events.values():
            if "kinds" in filt and e["kind"] not in filt["kinds"]:
                continue
            if "authors" in filt and e["pubkey"] not in filt["authors"]:
                continue
            if any(k.startswith("#") and not any(t[0] == k[1:] and t[1] in v for t in e["tags"]) for k, v in filt.items()):
                continue
            out.append(e)
        return out


@pytest.fixture()
def world(monkeypatch, tmp_path):
    import nostr_sdk
    relay = FakeRelay()
    monkeypatch.setattr(relay_io, "publish", relay.publish)
    monkeypatch.setattr(relay_io, "query", relay.query)
    monkeypatch.setattr(skill_map, "cell_nsec", lambda level, name: nostr_sdk.SecretKey.parse(hashlib.sha256(f"{level}{name}".encode()).hexdigest()).to_bech32())
    skill_map._pub_cache.clear()
    members = {}

    def member(email, lat, lon):
        keys = nostr_sdk.Keys.generate()
        d = tmp_path / email
        d.mkdir()
        (d / ".secret.nostr").write_text(f"NSEC={keys.secret_key().to_bech32()}; NPUB=x; HEX=y;\n")
        (d / "GPS").write_text(f"LAT={lat}; LON={lon};\n")
        members[email] = keys.public_key().to_hex()
        return email
    monkeypatch.setattr(skill_map, "GAME_NOSTR", tmp_path)
    return type("W", (), {"relay": relay, "member": staticmethod(member), "hex": members})


def test_cell_names_follow_the_bash_scripts():
    assert skill_map.cell_names(43.92, 2.14) == {"umap": "_43.92_2.14", "sector": "_43.9_2.1", "region": "_43_2"}
    assert skill_map.cell_names(48.857, 2.3522)["umap"] == "_48.86_2.35"
    assert skill_map.cell_names(-0.001, 0)["umap"] == "_0.00_0.00"
    assert skill_map.skill_norm("Boulanger  Pâtissier!") == "boulanger-p-tissier"
    assert skill_map.permit_key("pain-au-levain", 2) == "PERMIT_PAIN_AU_LEVAIN_X2"
    assert skill_map.levels_for("sector") == ["sector", "region"] and skill_map.levels_for("none") == [] and skill_map.levels_for("umap") == ["umap", "sector", "region"]


def test_umap_salts_are_lat_and_lon_separately(monkeypatch):
    monkeypatch.setattr(skill_map, "swarm_secret", lambda: "S")
    assert skill_map.cell_salts("umap", "_43.92_2.14") == ("S43.92", "S2.14")
    assert skill_map.cell_salts("sector", "_43.9_2.1") == ("S_43.9_2.1", "S_43.9_2.1")
    assert skill_map.cell_salts("region", "_43_2") == ("S_43_2", "S_43_2")


def test_neighbour_in_the_same_sector_is_found_with_the_certificate_level(world):
    a = world.member("a@x.org", 43.92, 2.14)
    b = world.member("b@x.org", 43.95, 2.18)                       # autre UMAP, même SECTOR (_43.9_2.1)
    skill_map.declare(a, "Boulanger", 1, "pain au levain", "🥖", "sector")
    skill_map.declare(b, "boulanger", 1, visibility="sector")
    found = skill_map.nearby("boulanger", 43.95, 2.18, "sector", 1, exclude_hex=world.hex[b])
    assert [f["hex"] for f in found] == [world.hex[a]] and found[0]["level"] == 1
    assert skill_map.nearby("boulanger", 43.95, 2.18, "umap", 1) == [] or all(f["hex"] != world.hex[a] for f in skill_map.nearby("boulanger", 43.95, 2.18, "umap", 1))
    assert skill_map.nearby("boulanger", 43.95, 2.18, "region", 1)[0]["hex"] in world.hex.values(), "visible aussi à l'échelle de la région"
    assert skill_map.nearby("menuisier", 43.95, 2.18, "sector") == []


def test_visibility_decides_which_cells_publish_and_none_removes_the_directory(world):
    a = world.member("a@x.org", 43.92, 2.14)
    n_cells = lambda: len([e for e in world.relay.events.values() if e["kind"] == 30078])
    skill_map.declare(a, "boulanger", 1, visibility="umap")
    assert n_cells() == 3
    skill_map.declare(a, "boulanger", 1, visibility="region")       # moins précis : l'annuaire fin est retiré
    assert n_cells() == 1
    assert skill_map.my_visibility(world.hex[a], 43.92, 2.14) == {"boulanger": "region"}
    skill_map.declare(a, "boulanger", 1, visibility="none")
    assert n_cells() == 0 and skill_map.skills_of(world.hex[a])[0]["skill"] == "boulanger", "le certificat reste, seul l'annuaire est retiré"
    with pytest.raises(ValueError):
        skill_map.declare(a, "x", 1)
    with pytest.raises(ValueError):
        skill_map.declare(a, "boulanger", 1, visibility="partout")


def test_forged_or_stale_directory_entries_are_ignored(world):
    """L'annuaire n'est qu'un index : sans certificat valide du membre, ou avec un certificat expiré, il n'apparaît pas ; le niveau vient du certificat."""
    import nostr_sdk
    a = world.member("a@x.org", 43.92, 2.14)
    skill_map.declare(a, "boulanger", 1, visibility="sector")
    ghost = nostr_sdk.Keys.generate().public_key().to_hex()          # annoncé par la case mais sans aucun certificat
    cell = skill_map.cell_names(43.92, 2.14)["sector"]
    ck = skill_map._cell_keys("sector", cell)
    world.relay.publish([skill_map.directory_event(ck, ghost, "boulanger", 9, "PERMIT_BOULANGER_X9")])
    found = skill_map.nearby("boulanger", 43.92, 2.14, "sector")
    assert [f["hex"] for f in found] == [world.hex[a]], "le fantôme (sans certificat) est écarté, même annoncé niveau 9"
    # certificat de niveau 2 signé par les pairs : le niveau annoncé par l'annuaire (1) n'a pas d'importance
    keys = skill_map.member_keys(a)
    world.relay.publish([skill_map.certificate_event(keys, "boulanger", 2)])
    assert skill_map.nearby("boulanger", 43.92, 2.14, "sector", min_level=2)[0]["level"] == 2
    assert skill_map.nearby("boulanger", 43.92, 2.14, "sector", min_level=3) == []
    # certificat expiré : le membre disparaît des résultats
    for e in list(world.relay.events.values()):
        if e["kind"] == 30503:
            e["tags"] = [t if t[0] != "expiration" else ["expiration", str(int(time.time()) - 10)] for t in e["tags"]]
    assert skill_map.nearby("boulanger", 43.92, 2.14, "sector") == []


def test_retract_removes_certificate_and_directory(world):
    a = world.member("a@x.org", 43.92, 2.14)
    skill_map.declare(a, "boulanger", 1, visibility="umap")
    out = skill_map.retract(a, "Boulanger")
    assert out["removed"] >= 4 and not world.relay.events
    assert skill_map.skills_of(world.hex[a]) == []


def test_declare_without_position_asks_for_one(world, tmp_path):
    a = world.member("a@x.org", 1, 1)
    (tmp_path / a / "GPS").unlink()
    with pytest.raises(ValueError):
        skill_map.declare(a, "boulanger", 1, visibility="sector")
    assert skill_map.declare(a, "boulanger", 1, visibility="sector", lat=10.5, lon=20.5)["cells"]["sector"] == "_10.5_20.5"
    assert skill_map.declare(a, "boulanger", 1, visibility="none")["cells"] == {}, "visibilité nulle : aucune case, et pas d erreur sans position"
