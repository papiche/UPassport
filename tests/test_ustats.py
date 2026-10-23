from services import ustats


def test_makecoord_comme_my_sh():
    assert ustats.makecoord("43.9") == "43.90"
    assert ustats.makecoord(".5") == "0.50"
    assert ustats.makecoord("-0.5") == "-0.50"
    assert ustats.makecoord("abc") == ""
    assert ustats.makecoord(None) == ""


def test_haversine():
    assert ustats.haversine_km(0, 0, 0, 0) == 0
    assert abs(ustats.haversine_km(48.8566, 2.3522, 51.5074, -0.1278) - 344) < 3  # Paris-Londres


def _data():
    return {"PLAYERs": [{"LAT": "43.92", "LON": "2.14"}, {"LAT": "10.00", "LON": "10.00"}],
            "NOSTR": [], "SWARM": [],
            "UMAPs": [{"LAT": "43.92", "LON": "2.14"}, {"LAT": "44.00", "LON": "2.00"}]}


def test_filter_area_boite_centree():
    out = ustats.filter_area(_data(), "43.92", "2.14", "1")
    assert len(out["PLAYERs"]) == 1 and len(out["UMAPs"]) == 2
    assert out["CENTER"] == {"LAT": "43.92", "LON": "2.14", "DEG": "1.00"}
    assert out["CLOSEST_UMAPs"][0]["LAT"] == "43.92"
    assert float(out["CLOSEST_UMAPs"][0]["DISTANCE_KM"]) == 0


def test_filter_area_sans_coordonnees_renvoie_tout():
    d = _data()
    assert ustats.filter_area(d, None, None, None) is d
