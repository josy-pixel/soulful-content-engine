"""Writing once and publishing to several platforms.

A post row holds one platform, because content type, approval, the link it ends up
at and its metrics are all per-platform. So choosing several platforms creates
several posts rather than one row pretending to be many.
"""
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db


CSRF = "test-csrf-token"


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["post_media", "content_posts", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    admin = db.create_user("mp-admin@t.co", generate_password_hash("pw"), role="admin")
    cid = db.create_client({"name": "Multi Client"})
    other = db.create_client({"name": "Other Client"})
    member = db.create_user("mp-client@t.co", generate_password_hash("pw"),
                            role="client", client_id=cid)
    return dict(admin=admin, cid=cid, other=other, member=member)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


def form(**over):
    base = {"csrf_token": CSRF, "topic": "Launch week", "caption": "Hello there",
            "hashtags": "#a", "status": "draft"}
    base.update(over)
    return base


def test_two_platforms_make_two_posts(client, data):
    login_as(client, data["admin"])
    client.post("/content/new", data=form(client_id=data["cid"],
                                          platforms=["facebook", "instagram"]))
    posts = db.get_posts()
    assert sorted(p["platform"] for p in posts) == ["facebook", "instagram"]
    assert {p["topic"] for p in posts} == {"Launch week"}
    assert {p["caption"] for p in posts} == {"Hello there"}


def test_each_platform_keeps_its_own_content_type(client, data):
    """A reel is an Instagram thing; Facebook must not be told to publish one."""
    login_as(client, data["admin"])
    client.post("/content/new", data=form(
        client_id=data["cid"], platforms=["facebook", "instagram"],
        content_type_facebook="video", content_type_instagram="reel"))
    by_platform = {p["platform"]: p for p in db.get_posts()}
    assert by_platform["facebook"]["content_type"] == "video"
    assert by_platform["instagram"]["content_type"] == "reel"


def test_a_type_the_platform_does_not_have_falls_back(client, data):
    """Rather than publishing something that platform has no concept of."""
    login_as(client, data["admin"])
    client.post("/content/new", data=form(client_id=data["cid"], platforms=["facebook"],
                                          content_type_facebook="story"))
    post = db.get_posts()[0]
    assert post["content_type"] in flask_app.CONTENT_TYPES["facebook"]
    assert post["content_type"] != "story"


def test_one_platform_still_lands_on_that_post(client, data):
    login_as(client, data["admin"])
    r = client.post("/content/new", data=form(client_id=data["cid"], platforms=["facebook"]))
    assert r.status_code == 302
    assert "/content/" in r.headers["Location"]
    assert len(db.get_posts()) == 1


def test_no_platform_creates_nothing(client, data):
    login_as(client, data["admin"])
    client.post("/content/new", data=form(client_id=data["cid"], platforms=[]))
    assert db.get_posts() == []


def test_an_unknown_platform_is_ignored(client, data):
    login_as(client, data["admin"])
    client.post("/content/new", data=form(client_id=data["cid"],
                                          platforms=["facebook", "myspace"]))
    assert [p["platform"] for p in db.get_posts()] == ["facebook"]


def test_the_single_platform_field_still_works(client, data):
    """An older form, or anything posting the previous shape, keeps working."""
    login_as(client, data["admin"])
    client.post("/content/new", data=form(client_id=data["cid"], platform="facebook"))
    assert [p["platform"] for p in db.get_posts()] == ["facebook"]


def test_every_created_post_belongs_to_the_client_user_not_the_form(client, data):
    """The scope rule has to hold for each of them, not only the first."""
    login_as(client, data["member"])
    client.post("/content/new", data=form(client_id=data["other"],   # forged
                                          platforms=["facebook", "instagram"]))
    posts = db.get_posts()
    assert len(posts) == 2
    assert {p["client_id"] for p in posts} == {data["cid"]}
