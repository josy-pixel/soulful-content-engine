"""Part 1 (structural) tests: the filtered views hide soft-deleted rows from every
reader, the base tables stay reachable only via get_*_including_deleted, and writes
refuse to mutate a deleted row. Deletion ROUTES/rules are Part 3 — not tested here.
"""
import pytest
import database as db

DELETED_AT = '2026-01-01T00:00:00'


@pytest.fixture()
def freshdb():
    db.init_db()
    conn = db.get_db()
    for t in ["approval_history", "performance_metrics", "post_media", "content_posts",
              "client_media", "brand_voices", "clients", "users", "trends"]:
        try:
            conn.execute("DELETE FROM %s" % t)   # raw-query-ok: test teardown, not app code
        except Exception:
            pass
    conn.commit(); conn.close()
    cid = db.create_client({"name": "C"})
    pid = db.create_post({"client_id": cid, "platform": "facebook", "topic": "t", "caption": "c"})
    return {"cid": cid, "pid": pid}


def _soft_delete(table, row_id):
    conn = db.get_db()
    conn.execute("UPDATE %s SET deleted_at=? WHERE id=?" % table, (DELETED_AT, row_id))  # raw-query-ok: test
    conn.commit(); conn.close()


def test_view_columns_match_base_tables():
    db.init_db()
    conn = db.get_db()
    for base, view in (("content_posts", "v_content_active"), ("clients", "v_clients_active")):
        b = [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % base)]
        v = [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % view)]
        assert b == v, "%s columns drifted from %s:\n base=%s\n view=%s" % (view, base, b, v)
    conn.close()


def test_deleted_post_hidden_from_every_reader(freshdb):
    pid = freshdb["pid"]
    assert db.get_post(pid) is not None
    _soft_delete("content_posts", pid)
    assert db.get_post(pid) is None
    assert all(p["id"] != pid for p in db.get_posts())
    assert db.get_dashboard_stats()["total_posts"] == 0
    assert all(p["id"] != pid for p in db.get_scheduled_posts())


def test_deleted_post_visible_only_to_including_deleted(freshdb):
    pid = freshdb["pid"]
    _soft_delete("content_posts", pid)
    row = db.get_post_including_deleted(pid)
    assert row is not None and row["id"] == pid


def test_write_guard_refuses_deleted_post(freshdb):
    pid = freshdb["pid"]
    _soft_delete("content_posts", pid)
    with pytest.raises(ValueError):
        db.update_post_status(pid, "approved")
    with pytest.raises(ValueError):
        db.update_post(pid, {"topic": "x", "caption": "y"})
    with pytest.raises(ValueError):
        db.set_post_error(pid, "boom")


def test_deleted_client_hidden_and_write_guarded(freshdb):
    cid = freshdb["cid"]
    _soft_delete("clients", cid)
    assert db.get_client(cid) is None
    assert all(c["id"] != cid for c in db.get_clients())
    assert any(c["id"] == cid for c in db.get_clients_including_deleted())
    with pytest.raises(ValueError):
        db.update_client(cid, {"name": "x"})
