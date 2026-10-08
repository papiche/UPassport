"""Le Studio web (story_asset.validate_storyboard, appelé par /api/story) et le moteur de rendu
(generate_scene.sh --check) doivent accepter et refuser EXACTEMENT les mêmes storyboards :
sinon story.html enregistre une scène que le rendu refusera, ou l'inverse."""
import json
import subprocess
import sys

import pytest

from core.config import settings

GEN = settings.TOOLS_PATH.parent / "IA" / "generators"
SCENE = GEN / "generate_scene.sh"

pytestmark = pytest.mark.skipif(not SCENE.exists(), reason="Astroport.ONE absent")

CASES = {
    "prompt": {"shots": [{"prompt": "a"}]},
    "video": {"shots": [{"video": "ipfs://QmX"}]},
    "card": {"shots": [{"card": {"title": "t"}}]},
    "screen": {"shots": [{"screen": "https://x"}]},
    "screen+presenter": {"cast": {"lea": {}}, "shots": [{"screen": "https://x", "presenter": "lea", "prompt": "Lea says"}]},
    "cast declared": {"cast": {"lea": {}}, "shots": [{"cast": ["lea"], "prompt": "a"}]},
    # refusés
    "empty shots": {"shots": []},
    "no shots": {"cast": {"lea": {}}},
    "shot without content": {"shots": [{"duration": 5}]},
    "screen+presenter, no prompt": {"cast": {"lea": {}}, "shots": [{"screen": "https://x", "presenter": "lea"}]},
    "first continue": {"shots": [{"continue": True, "prompt": "a"}]},
    "cast missing": {"shots": [{"cast": ["lea"], "prompt": "a"}]},
    "presenter missing": {"shots": [{"prompt": "a"}, {"screen": "https://x", "presenter": "le", "prompt": "p"}]},
}


def _python_ok(sb):
    sys.path.insert(0, str(settings.TOOLS_PATH))
    import story_asset
    try:
        story_asset.validate_storyboard(json.dumps(sb))
        return True
    except story_asset.AssetError:
        return False


def _bash_ok(path):
    r = subprocess.run(["bash", str(SCENE), "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert r.returncode in (0, 2), r.stderr
    return r.returncode == 0


@pytest.mark.parametrize("name", list(CASES))
def test_same_verdict(name, tmp_path):
    f = tmp_path / "sb.json"
    f.write_text(json.dumps(CASES[name]))
    assert _python_ok(CASES[name]) == _bash_ok(f), name


@pytest.mark.parametrize("path", sorted(GEN.glob("storyboard*.example.json")) + sorted((GEN / "storyboards").glob("*.json")),
                         ids=lambda p: p.name)
def test_shipped_storyboards_are_valid(path):
    """Les exemples livrés doivent passer (un « presenter » mal orthographié a déjà cassé g1billet_spot)."""
    assert _bash_ok(path) and _python_ok(json.loads(path.read_text()))
