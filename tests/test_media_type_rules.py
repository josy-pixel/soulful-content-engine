"""A photo post must not carry a video.

Sending one produces a generic failure at the network hours later — it happened
to two different clients. Both facts are known at the moment they are put
together, so the app says so then, and again before publishing in case the
content type was changed afterwards.
"""
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db
import media_rules
import webhooks


CSRF = "test-csrf-token"


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["post_media", "content_posts", "client_media", "client_webhooks",
              "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    admin = db.create_user("mr-admin@t.co", generate_password_hash("pw"), role="admin")
    cid = db.create_client({"name": "Rules Client"})
    db.upsert_client_webhook(cid, "https://hook.eu1.make.com/rules-test", "s3cret",
                             "facebook,instagram")
    return dict(admin=admin, cid=cid)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


# ── the table itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("content_type,kind,allowed", [
    ("photo", "image", True),
    ("photo", "video", False),
    ("video", "video", True),
    ("video", "image", False),
    ("reel", "video", True),
    ("reel", "image", False),
    ("story", "video", True),
    ("story", "image", False),
    ("carousel", "image", True),
    ("carousel", "video", True),
])
def test_what_each_kind_of_post_accepts(content_type, kind, allowed):
    ok, why = media_rules.check(content_type, kind)
    assert ok is allowed
    if not allowed:
        assert content_type in why          # the message names the post type


def test_an_unknown_content_type_does_not_block_work():
    """A type this table has not caught up with must not stop anyone publishing."""
    assert media_rules.check("something-new", "video")[0] is True


@pytest.mark.parametrize("name,kind", [
    ("clip.mp4", "video"), ("clip.MOV", "video"), ("a.webm", "video"),
    ("photo.jpg", "image"), ("logo.PNG", "image"),
    ("s3://clients/1/x.mp4", "video"),
    ("https://bucket/x.jpg?sig=abc", "image"),
])
def test_a_file_is_judged_by_its_extension(name, kind):
    assert media_rules.kind_of_filename(name) == kind


# ── attaching ────────────────────────────────────────────────────────────────

def test_attaching_a_video_to_a_photo_post_is_refused(client, data):
    post = db.create_post({"client_id": data["cid"], "platform": "instagram",
                           "content_type": "photo", "topic": "t", "caption": "c"})
    media = db.add_media(data["cid"], "clip.mp4", "clip.mp4", "video", 10)
    login_as(client, data["admin"])
    r = client.post("/api/content/%d/media" % post, json={"media_id": media},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 409
    assert "video" in r.get_json()["error"].lower()
    assert db.get_post_media(post) == []          # nothing was attached


def test_attaching_an_image_to_a_reel_is_refused(client, data):
    post = db.create_post({"client_id": data["cid"], "platform": "instagram",
                           "content_type": "reel", "topic": "t", "caption": "c"})
    media = db.add_media(data["cid"], "shot.jpg", "shot.jpg", "image", 10)
    login_as(client, data["admin"])
    assert client.post("/api/content/%d/media" % post, json={"media_id": media},
                       headers={"X-CSRF-Token": CSRF}).status_code == 409


def test_the_matching_kind_attaches_normally(client, data):
    post = db.create_post({"client_id": data["cid"], "platform": "instagram",
                           "content_type": "reel", "topic": "t", "caption": "c"})
    media = db.add_media(data["cid"], "clip.mp4", "clip.mp4", "video", 10)
    login_as(client, data["admin"])
    assert client.post("/api/content/%d/media" % post, json={"media_id": media},
                       headers={"X-CSRF-Token": CSRF}).status_code == 200
    assert len(db.get_post_media(post)) == 1


def test_a_carousel_takes_either(client, data):
    post = db.create_post({"client_id": data["cid"], "platform": "instagram",
                           "content_type": "carousel", "topic": "t", "caption": "c"})
    img = db.add_media(data["cid"], "a.jpg", "a.jpg", "image", 10)
    vid = db.add_media(data["cid"], "b.mp4", "b.mp4", "video", 10)
    login_as(client, data["admin"])
    for m in (img, vid):
        assert client.post("/api/content/%d/media" % post, json={"media_id": m},
                           headers={"X-CSRF-Token": CSRF}).status_code == 200


# ── publishing ───────────────────────────────────────────────────────────────

def test_publishing_a_photo_post_holding_a_video_is_refused(data):
    """The content type can be changed after the media was attached."""
    post_id = db.create_post({"client_id": data["cid"], "platform": "facebook",
                              "content_type": "photo", "topic": "t", "caption": "c",
                              "image_url": "s3://clients/1/clip.mp4"})
    ok, message = webhooks.dispatch_post(db.get_post(post_id))
    assert ok is False
    assert "video" in message.lower()
    assert "content type" in message.lower()      # says what to do about it


def test_an_external_link_is_left_alone(data, monkeypatch):
    """A pasted YouTube link is not our media and is not ours to judge."""
    sent = {}
    monkeypatch.setattr(webhooks, "_http_post",
                        lambda url, payload, secret=None: (sent.update(payload) or (True, 200, None)))
    post_id = db.create_post({"client_id": data["cid"], "platform": "facebook",
                              "content_type": "photo", "topic": "t", "caption": "c",
                              "image_url": "https://youtu.be/xyz"})
    ok, _ = webhooks.dispatch_post(db.get_post(post_id))
    assert ok is True
