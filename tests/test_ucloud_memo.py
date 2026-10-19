"""Les agrégats du statut/des analyses sont mémoïsés par révision de la base :
toute écriture doit les rafraîchir, une lecture répétée ne doit pas recalculer."""
from services import ucloud_db


def _img(cid, **kw):
    e = {"type": "file", "cid": cid, "mime": "image/jpeg", "size": 10, "mtime": 1}
    e.update(kw)
    return e


def test_stats_and_counts_follow_writes(tmp_path):
    udir = tmp_path / ".ucloud"
    udir.mkdir()
    assert ucloud_db.stats(udir)["files"] == 0
    with ucloud_db.transaction(udir) as conn:
        ucloud_db.put_entry(conn, "/Photos/a.jpg", _img("Qm1"))
    assert ucloud_db.stats(udir)["files"] == 1
    assert ucloud_db.analysis_counts(udir)["images"] == 1
    with ucloud_db.transaction(udir) as conn:
        ucloud_db.put_entry(conn, "/Photos/b.jpg", _img("Qm2", faceid={"status": "faces"}))
    assert ucloud_db.stats(udir)["files"] == 2
    assert ucloud_db.analysis_counts(udir)["images"] == 2


def test_memo_does_not_recompute_without_write(tmp_path):
    udir = tmp_path / ".ucloud"
    udir.mkdir()
    calls = []
    f = lambda: calls.append(1) or len(calls)
    assert ucloud_db.memo(udir, "x", f) == ucloud_db.memo(udir, "x", f) == 1
    with ucloud_db.transaction(udir):
        pass
    assert ucloud_db.memo(udir, "x", f) == 2


def test_stats_result_is_not_shared_mutable(tmp_path):
    udir = tmp_path / ".ucloud"
    udir.mkdir()
    ucloud_db.stats(udir)["faceid"]["hack"] = 1
    assert "hack" not in ucloud_db.stats(udir)["faceid"]


def test_describe_pending_skips_photos_marked_unreadable(tmp_path):
    import time
    udir = tmp_path / ".ucloud"
    udir.mkdir()
    with ucloud_db.transaction(udir) as conn:
        ucloud_db.put_entry(conn, "/a.jpg", _img("Qm1"))
        ucloud_db.put_entry(conn, "/b.jpg", _img("Qm2"))
    assert set(ucloud_db.describe_pending(udir)) == {"/a.jpg", "/b.jpg"}
    ucloud_db.patch_entry(udir, "/a.jpg", lambda e: e.update(desc_fail={"at": int(time.time()), "reason": "x"}) or True)
    assert ucloud_db.describe_pending(udir) == ["/b.jpg"]
    old = int(time.time()) - 8 * 86400                       # au-delà de 7 jours : réessayée
    ucloud_db.patch_entry(udir, "/a.jpg", lambda e: e.update(desc_fail={"at": old, "reason": "x"}) or True)
    assert set(ucloud_db.describe_pending(udir)) == {"/a.jpg", "/b.jpg"}
