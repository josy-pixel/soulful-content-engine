"""The client's own media library.

The page and the tenant checks already existed; what did not exist was a way in,
and a delete that tells you what it costs. Deleting a media row also pulls the
file out of every post that uses it — silently, before this.
"""
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db
import s3_media


CSRF = "test-csrf-token"


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["post_media", "content_posts", "client_media", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user("ml-admin@t.co", generate_password_hash("pw"), role="admin")
    ca = db.create_client({"name": "Lib Client A"})
    cb = db.create_client({"name": "Lib Client B"})
    user_a = db.create_user("ml-a@t.co", generate_password_hash("pw"), role="client", client_id=ca)
    return dict(admin=admin, ca=ca, cb=cb, user_a=user_a)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


def attach(post_id, media_id):
    db.attach_media_to_post(post_id, media_id)


# ── the way in ───────────────────────────────────────────────────────────────

def test_a_client_lands_in_their_own_library(client, data):
    login_as(client, data["user_a"])
    r = client.get("/media")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/clients/%d/gallery" % data["ca"])


def test_a_client_cannot_open_another_clients_library(client, data):
    login_as(client, data["user_a"])
    assert client.get("/clients/%d/gallery" % data["cb"]).status_code == 403


def test_the_gallery_page_actually_renders(client, data, monkeypatch):
    """The page itself, not just the redirect into it.

    Testing only the redirects let a TypeError in the view reach production as a
    500 on the very page the feature is about.
    """
    monkeypatch.setattr(s3_media, "presign_view", lambda key, expires=None: "https://x/y")
    db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    db.add_media(data["ca"], "b.mp4", "b.mp4", "video", 20, storage="s3", s3_key="clients/1/b.mp4")
    login_as(client, data["admin"])
    r = client.get("/clients/%d/gallery" % data["ca"])
    assert r.status_code == 200
    assert b"a.jpg" in r.data and b"b.mp4" in r.data
    assert b"not used" in r.data                 # the usage badge rendered


def test_the_gallery_renders_for_a_client_user_too(client, data):
    login_as(client, data["user_a"])
    assert client.get("/clients/%d/gallery" % data["ca"]).status_code == 200


def test_an_admin_gets_a_picker_when_there_are_several_clients(client, data):
    login_as(client, data["admin"])
    r = client.get("/media")
    assert r.status_code == 200
    assert b"Lib Client A" in r.data and b"Lib Client B" in r.data


# ── knowing what is safe to remove ───────────────────────────────────────────

def test_unused_media_is_reported_as_unused(data):
    mid = db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    rows = db.get_client_media_with_usage(data["ca"])
    assert rows[0]["uses"] == 0 and rows[0]["posted_uses"] == 0


def test_usage_counts_the_posts_that_actually_use_it(data):
    mid = db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    p1 = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "one",
                         "caption": "c", "status": "draft"})
    p2 = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "two",
                         "caption": "c", "status": "posted"})
    attach(p1, mid)
    attach(p2, mid)
    rows = db.get_client_media_with_usage(data["ca"])
    assert rows[0]["uses"] == 2
    assert rows[0]["posted_uses"] == 1


def test_a_deleted_post_does_not_make_a_file_look_busy(data):
    """Otherwise the library would refuse to tidy up files nothing really uses.

    Posts have a deleted_at column but nothing writes it yet — the trash for posts
    was approved and never built — so the deleted state is set here directly. When
    that feature lands this test should switch to calling it.
    """
    mid = db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    p = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "gone",
                        "caption": "c", "status": "posted"})
    attach(p, mid)

    conn = db.get_db()
    conn.execute("UPDATE content_posts SET deleted_at='2026-01-01' WHERE id=?", (p,))  # raw-query-ok: simulating a state no code path can produce yet
    conn.commit()
    conn.close()

    rows = db.get_client_media_with_usage(data["ca"])
    assert rows[0]["uses"] == 0 and rows[0]["posted_uses"] == 0


def test_the_usage_endpoint_names_the_posts(client, data):
    mid = db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    p = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "Summer promo",
                        "caption": "c", "status": "draft"})
    attach(p, mid)
    login_as(client, data["user_a"])
    body = client.get("/api/media/%d/usage" % mid).get_json()
    assert body["uses"] == 1
    assert body["posts"][0]["topic"] == "Summer promo"
    assert body["client_may_delete"] is True


# ── the guard ────────────────────────────────────────────────────────────────

def test_a_client_may_delete_their_own_unused_file(client, data, monkeypatch):
    monkeypatch.setattr(s3_media, "delete", lambda key: True)
    mid = db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    login_as(client, data["user_a"])
    r = client.delete("/api/media/%d" % mid, headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 200
    assert db.get_media(mid) is None


def test_a_client_may_still_delete_a_file_used_only_by_a_draft(client, data, monkeypatch):
    monkeypatch.setattr(s3_media, "delete", lambda key: True)
    mid = db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    p = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "draft",
                        "caption": "c", "status": "draft"})
    attach(p, mid)
    login_as(client, data["user_a"])
    assert client.delete("/api/media/%d" % mid, headers={"X-CSRF-Token": CSRF}).status_code == 200


def test_a_client_cannot_delete_a_file_used_by_a_published_post(client, data):
    """Removing it changes nothing on the network — it only destroys the record."""
    mid = db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    p = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "live",
                        "caption": "c", "status": "posted"})
    attach(p, mid)
    login_as(client, data["user_a"])
    r = client.delete("/api/media/%d" % mid, headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 409
    assert db.get_media(mid) is not None            # still there
    assert db.get_post_media(p)                     # and still attached to the post


def test_an_admin_can_delete_it_deliberately(client, data, monkeypatch):
    monkeypatch.setattr(s3_media, "delete", lambda key: True)
    mid = db.add_media(data["ca"], "a.jpg", "a.jpg", "image", 10)
    p = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "live",
                        "caption": "c", "status": "posted"})
    attach(p, mid)
    login_as(client, data["admin"])
    assert client.delete("/api/media/%d" % mid, headers={"X-CSRF-Token": CSRF}).status_code == 200
    assert db.get_media(mid) is None
