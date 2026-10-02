"""Router /api/story : bibliothèque, versions, copie, rendu avec suivi (IPFS, relais et ComfyUI simulés)."""
import base64
import importlib
import json
import os
import stat
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

FAKE_SCENE = """#!/bin/bash
# faux generate_scene.sh : écrit progress.json comme le vrai, un plan par appel
W=""; while getopts "w:" o; do W="$OPTARG"; done; shift $((OPTIND-1))
N=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))['shots']))" "$1")
mkdir -p "$W"
for ((i=0;i<N;i++)); do
  [ -s "$W/shot_0$i.mp4" ] || echo "clip$i" > "$W/shot_0$i.mp4"
  echo "part$i" > "$W/part_0$i.mp4"
  d=$(seq -s, 0 $i | sed 's/,$//')
  echo "{\\"stage\\":\\"shot\\",\\"shot\\":$i,\\"shots\\":$N,\\"message\\":\\"plan $i\\",\\"done\\":[$d]}" > "$W/progress.json"
done
echo "fin" > "$W/scene.mp4"
echo "{\\"stage\\":\\"done\\",\\"shot\\":-1,\\"shots\\":$N,\\"message\\":\\"terminé\\",\\"done\\":[$(seq -s, 0 $((N-1)))]}" > "$W/progress.json"
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENES_DIR", str(tmp_path / "scenes"))
    for m in ("story_asset", "services.story_render", "routers.story"):
        sys.modules.pop(m, None)
    from routers import story
    sr, sa = story.story_render, story.story_asset
    blobs, events = {}, []

    def fake_add(payload, name):
        cid = "Qm%044d" % len(blobs)
        blobs[cid] = payload
        return cid

    def fake_publish(nsec, tags, content):
        events.append({"id": "ev%03d" % len(events), "kind": sa.KIND, "pubkey": "a" * 64, "created_at": 1000 + len(events),
                       "tags": tags, "content": json.dumps(content)})
        return events[-1]["id"]

    monkeypatch.setattr(sa, "ipfs_add", fake_add)
    monkeypatch.setattr(sa, "ipfs_cat", lambda cid: blobs[cid])
    monkeypatch.setattr(sa, "ipfs_pin", lambda cid: None)
    monkeypatch.setattr(sa, "publish_event", fake_publish)
    monkeypatch.setattr(sa, "relay_events", lambda **f: [e for e in events if not f.get("tag_t") or ["t", f["tag_t"]] in e["tags"]])
    monkeypatch.setattr(sa, "captain_secret", lambda: "nsec-test")
    monkeypatch.setattr(sa, "captain_hex", lambda: "a" * 64)
    monkeypatch.setattr(sa, "coop_key", lambda: "22" * 32)
    monkeypatch.setattr(sa, "send_key", lambda *a: True)
    monkeypatch.setattr(story, "_require_captain", lambda npub: None if npub == "captain" else (_ for _ in ()).throw(
        story.HTTPException(status_code=403, detail="no")))

    gen = tmp_path / "gen"
    gen.mkdir()
    (gen / "generate_scene.sh").write_text(FAKE_SCENE)
    (gen / "generate_scene.sh").chmod(0o755)
    monkeypatch.setattr(sr, "GEN_DIR", gen)
    monkeypatch.setattr(sr, "_ensure_pump", lambda: None)  # le test pompe lui-même, pas de thread
    monkeypatch.setattr(sr.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "QmVideoCid\n", "stderr": ""})())

    app = FastAPI()
    app.include_router(story.router)
    app.dependency_overrides[story.require_nostr_auth] = lambda: "captain"
    c = TestClient(app)
    c.app_, c.story, c.sr, c.sa, c.events = app, story, sr, sa, events
    return c


def mk(client, typ, name, scope="private"):
    r = client.post("/api/story/assets", json={"type": typ, "name": name, "scope": scope})
    assert r.status_code == 200, r.text
    return r.json()["cid"]


def test_create_edit_versions_restore(client):
    cid = mk(client, "scene", "Demo")
    assets = client.get("/api/story/assets").json()["assets"]
    assert [(a["name"], a["scope"], a["mine"]) for a in assets] == [("Demo", "private", True)] and "key_hex" not in assets[0]
    sb = {"ratio": "9:16", "cast": {}, "shots": [{"prompt": "un"}, {"prompt": "deux"}]}
    r = client.put(f"/api/story/asset/{cid}", json={"description": "v2", "changes": {"storyboard.json": {"text": json.dumps(sb)}}})
    assert r.status_code == 200 and r.json()["version"] == 2
    new = r.json()["cid"]
    vs = client.get(f"/api/story/asset/{new}/versions").json()["versions"]
    assert [v["version"] for v in vs] == [2, 1] and vs[0]["latest"] and not vs[1]["latest"]
    old = client.get(f"/api/story/asset/{cid}").json()  # l'ancien CID reste lisible
    assert old["version"] == 1 and not old["is_latest"] and old["latest_cid"] == new
    assert client.put(f"/api/story/asset/{cid}", json={"description": "x"}).status_code == 200  # édition = repart de la dernière
    rr = client.post(f"/api/story/asset/{cid}/restore")
    assert rr.status_code == 200 and rr.json()["version"] == 4


def test_rejections(client):
    cid = mk(client, "scene", "Rej")
    put = lambda body: client.put(f"/api/story/asset/{cid}", json=body).status_code
    assert put({"changes": {"../evil": {"text": "x"}}}) == 400
    assert put({"changes": {"storyboard.json": {"text": "{"}}}) == 400
    assert put({"changes": {"storyboard.json": {"text": json.dumps({"shots": [{"cast": ["x"], "prompt": "a"}]})}}}) == 400
    assert put({}) == 400
    assert client.post("/api/story/assets", json={"type": "scene", "name": "../x"}).status_code == 400
    client.app_.dependency_overrides[client.story.require_nostr_auth] = lambda: "stranger"
    assert client.get("/api/story/assets").status_code == 403


def test_coop_visible_by_other_captain_and_fork(client, monkeypatch):
    cid = mk(client, "character", "lea", "coop")
    other = [dict(e, pubkey="b" * 64) for e in client.events]
    monkeypatch.setattr(client.sa, "relay_events", lambda **f: other)
    client.sa.KEYRING.unlink()
    a = client.get("/api/story/assets").json()["assets"]
    assert [(x["name"], x["mine"]) for x in a] == [("lea", False)]
    d = client.get(f"/api/story/asset/{a[0]['cid']}").json()
    assert not d["mine"] and any(f["path"].endswith("character.json") for f in d["files"])
    assert client.put(f"/api/story/asset/{a[0]['cid']}", json={"description": "pirate"}).status_code == 400
    f = client.post(f"/api/story/asset/{a[0]['cid']}/fork", json={"name": "lea2"})
    assert f.status_code == 200
    assert client.get(f"/api/story/asset/{f.json()['cid']}").json()["mine"]


def test_attach_character_and_render_job(client):
    ch = mk(client, "character", "ana")
    ch2 = client.put(f"/api/story/asset/{ch}", json={"changes": {"cast/ana/portrait.png": {"b64": base64.b64encode(b"PNG").decode()}}}).json()["cid"]
    sc = mk(client, "scene", "Clip")
    sb = {"cast": {}, "shots": [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]}
    sc2 = client.put(f"/api/story/asset/{sc}", json={"changes": {"storyboard.json": {"text": json.dumps(sb)}},
                                                       "attach": [{"cid": ch2}]}).json()["cid"]
    files = {f["path"]: f for f in client.get(f"/api/story/asset/{sc2}").json()["files"]}
    assert "cast/ana/portrait.png" in files and "ana" in json.loads(files["storyboard.json"]["text"])["cast"]

    job = client.post(f"/api/story/asset/{sc2}/render", json={}).json()
    assert job["status"] in ("running", "queued") and job["shots_total"] == 3
    for _ in range(100):
        j = client.get(f"/api/story/jobs/{job['id']}").json()
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    assert j["shots_done"] == 3 and j["stage"] == "done" and j["render"]["shots"] == ["part_00.mp4", "part_01.mp4", "part_02.mp4"]
    assert client.get(f"/api/story/jobs/{job['id']}/file", params={"name": "shot_01.mp4"}).content.startswith(b"clip1")
    assert client.get(f"/api/story/jobs/{job['id']}/file", params={"name": "../x"}).status_code == 400
    # le rendu est archivé et rattaché à SA version
    vs = client.get(f"/api/story/asset/{sc2}/versions").json()["versions"]
    assert [len(v["renders"]) for v in vs][0] == 1
    rid = vs[0]["renders"][0]["id"]
    assert client.get(f"/api/story/render/{sc2}/{rid}/file", params={"name": "scene.mp4"}).content == b"fin\n"
    # privé : rien d'annoncé dans l'événement
    assert "renders" not in json.loads(client.events[-1]["content"])


def test_coop_render_is_announced(client):
    sc = mk(client, "scene", "Pub", "coop")
    job = client.post(f"/api/story/asset/{sc}/render", json={}).json()
    for _ in range(100):
        j = client.get(f"/api/story/jobs/{job['id']}").json()
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert j["status"] == "done" and j["render"]["mp4_cid"] == "QmVideoCid"
    ev = json.loads(client.events[-1]["content"])
    assert ev["renders"][0]["mp4_cid"] == "QmVideoCid" and "dir" not in ev["renders"][0]
    assert ev["renders"][0]["version_cid"] == sc


def test_queue_one_at_a_time(client):
    sc = mk(client, "scene", "Q")
    a = client.post(f"/api/story/asset/{sc}/render", json={}).json()
    b = client.post(f"/api/story/asset/{sc}/render", json={}).json()
    assert a["status"] == "running" and b["status"] == "queued"
    c = client.post(f"/api/story/jobs/{b['id']}/cancel").json()
    assert c["status"] == "cancelled"
