"""Deleting a post from the Content Library.

Two bugs lived here and both are regression-tested:

1. delete_post() cleared approval_history and performance_metrics but not
   post_media, which holds a FOREIGN KEY to content_posts. With
   PRAGMA foreign_keys = ON, deleting any post that had media attached failed
   on a FK violation — a 500, post still there. That is exactly the shape of
   post the app produces, so delete was broken for the normal case.

2. The failure raised between get_db() and conn.close(), so the connection
   leaked while holding the write lock. The NEXT write then blocked on it and
   died with "database is locked". One failed delete poisoned the app.
"""
import pytest
from werkzeug.security import generate_password_hash

import database as db
import app as flask_app


CSRF = "test-csrf-token"


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["approval_history", "performance_metrics", "post_media",
              "content_posts", "client_media", "brand_voices", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    admin = db.create_user("del-admin@t.co", generate_password_hash("pw"), role="admin")
    client_id = db.create_client({"name": "Delete Co"})
    return dict(admin=admin, client_id=client_id)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


def make_post(client_id, with_media=True, status="draft"):
    post_id = db.create_post({"client_id": client_id, "platform": "facebook",
                              "content_type": "photo", "topic": "T", "caption": "C",
                              "image_url": "/uploads/1/x.jpg", "status": status})
    media_id = None
    if with_media:
        media_id = db.add_media(client_id, "x.jpg", "x.jpg", "image")
        db.attach_media_to_post(post_id, media_id)
    return post_id, media_id


def test_delete_post_with_attached_media(client, data):
    """The bug: this used to raise FOREIGN KEY constraint failed."""
    post_id, media_id = make_post(data["client_id"], with_media=True)
    db.add_performance(post_id, {"likes": 5})
    login_as(client, data["admin"])

    r = client.post(f"/content/{post_id}/delete", data={"csrf_token": CSRF})
    assert r.status_code in (301, 302)
    assert db.get_post(post_id) is None

    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM post_media WHERE post_id=?",
                        (post_id,)).fetchone()[0] == 0      # no orphan link rows
    assert conn.execute("SELECT COUNT(*) FROM approval_history WHERE post_id=?",
                        (post_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM performance_metrics WHERE post_id=?",
                        (post_id,)).fetchone()[0] == 0
    conn.close()


def test_delete_keeps_the_media_in_the_gallery(client, data):
    """Detaching media from a deleted post must not destroy the client's asset."""
    post_id, media_id = make_post(data["client_id"], with_media=True)
    login_as(client, data["admin"])
    client.post(f"/content/{post_id}/delete", data={"csrf_token": CSRF})
    assert db.get_media(media_id) is not None


def test_delete_without_media_still_works(client, data):
    post_id, _ = make_post(data["client_id"], with_media=False)
    login_as(client, data["admin"])
    client.post(f"/content/{post_id}/delete", data={"csrf_token": CSRF})
    assert db.get_post(post_id) is None


def test_a_failed_write_does_not_leave_the_db_locked(data):
    """The compounding bug: a raise between get_db() and close() leaked the
    connection with the write lock held, so the next write hit 'database is
    locked'. write_db() must always close, even on failure."""
    post_id, _ = make_post(data["client_id"], with_media=False)
    conn = db.get_db()
    # No soft-delete route exists yet (Stage 2 Part 3), so the test marks the row
    # deleted itself — that is the only way to make the write guard fire.
    conn.execute("UPDATE content_posts SET deleted_at=? WHERE id=?",  # raw-query-ok: test setup, must reach the base table
                 ("2026-01-01T00:00:00", post_id))
    conn.commit()
    conn.close()

    # The write guard fires on a soft-deleted row -> raises inside the write path.
    with pytest.raises(ValueError):
        db.update_post(post_id, {"topic": "new", "caption": "new"})

    # If the connection leaked, this next write would block then fail.
    other = db.create_post({"client_id": data["client_id"], "platform": "facebook",
                            "topic": "still writable", "caption": "c"})
    assert db.get_post(other) is not None
