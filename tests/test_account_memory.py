"""« Ce que BRO sait de moi » — couche service (services/memory_status.py), sous-processus simulé."""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import memory_status as ms  # noqa: E402

BIG_ID = 1023336186787698967  # > 2^53 : JavaScript l'arrondirait


def _points():
    return json.dumps([
        {"id": BIG_ID, "payload": {"content": "Allergique aux noix", "kind": "fact", "source": "chat",
                                   "visibility": "private", "confidence": 0.9, "timestamp": "2026-10-19T10:00:00Z"}},
        {"id": 7, "payload": {"content": "Aime le jazz à Marciac", "kind": "trait", "source": "mastodon",
                              "visibility": "love", "timestamp": "2026-10-18T10:00:00Z"}},
    ])


def test_ids_envoyes_en_chaine():
    with mock.patch.object(ms, "_cli", return_value=(0, _points(), "")):
        res = ms.list_memory_items("u@x")
    assert res["ok"] and res["total"] == 2
    assert res["items"][0]["id"] == str(BIG_ID) and isinstance(res["items"][0]["id"], str)


def test_filtre_visibilite_et_recherche_sans_accent():
    with mock.patch.object(ms, "_cli", return_value=(0, _points(), "")):
        assert [i["kind"] for i in ms.list_memory_items("u@x", visibility="love")["items"]] == ["trait"]
        assert len(ms.list_memory_items("u@x", q="ALLERGIQUE")["items"]) == 1
        assert len(ms.list_memory_items("u@x", q="marciaç")["items"]) == 1   # accents et cédille ignorés
        assert len(ms.list_memory_items("u@x", q="marciac")["items"]) == 1
        assert len(ms.list_memory_items("u@x", q="blues")["items"]) == 0


def test_erreur_qdrant_jamais_prise_pour_aucun_souvenir():
    trace = 'Traceback (most recent call last):\n  File "x.py", line 1, in <module>\nRuntimeError: collection memory_x : 768 dimensions\n'
    with mock.patch.object(ms, "_cli", return_value=(1, "", trace)):
        res = ms.list_memory_items("u@x")
    assert res["ok"] is False
    assert res["error"] == "RuntimeError: collection memory_x : 768 dimensions"   # dernière ligne, pas la trace


def test_validation_du_texte_avant_tout_appel():
    with mock.patch.object(ms, "_cli", side_effect=AssertionError("ne doit pas être appelé")):
        assert not ms.add_memory_item("u@x", "a")["ok"]
        assert not ms.add_memory_item("u@x", "x" * 601)["ok"]
        assert not ms.update_memory_item("u@x", 5, content=" ")["ok"]


def test_secret_refuse_message_clair():
    with mock.patch.object(ms, "_cli", return_value=(1, "", "[memory_manager] 🚫 souvenir refusé pour u@x : secret")):
        res = ms.add_memory_item("u@x", "mon mot de passe est Xk29!fjd8392")
    assert res == {"ok": False, "error": "contenu refusé (secret ou mot de passe)"}


def test_type_et_visibilite_inconnus_ramenes_aux_valeurs_sures():
    seen = {}
    def fake(script, args, timeout=30):
        seen["args"] = args
        return 0, "42", ""
    with mock.patch.object(ms, "_cli", fake):
        ms.add_memory_item("u@x", "Un souvenir valable", kind="bidule", visibility="public")
    a = seen["args"]
    assert a[a.index("--kind") + 1] == "fact" and a[a.index("--visibility") + 1] == "private"
    assert a[a.index("--source") + 1] == "manual"


def test_suppression_borne_et_vide():
    with mock.patch.object(ms, "_cli", side_effect=AssertionError("rien à supprimer")):
        assert ms.delete_memory_items("u@x", []) == {"ok": True, "deleted": 0}


def test_activer_une_source_en_pause_refuse_sans_rien_ecrire():
    status = {"ok": True, "sources": [{"source": "love_dm", "paused": True}, {"source": "chat", "paused": False}]}
    with mock.patch.object(ms, "memory_sources", return_value=status), \
         mock.patch.object(ms, "_cli", side_effect=AssertionError("ne doit pas écrire le consentement")):
        res = ms.set_memory_source("u@x", "love_dm", True)
    assert res["ok"] is False and res["paused"] is True


def test_desactiver_une_source_en_pause_reste_possible():
    called = {}
    def fake(script, args, timeout=30):
        called["args"] = args
        return 0, '{"love_dm": false}', ""
    with mock.patch.object(ms, "_cli", fake):
        assert ms.set_memory_source("u@x", "love_dm", False)["ok"] is True
    assert "love_dm=off" in called["args"]
