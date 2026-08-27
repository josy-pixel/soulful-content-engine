"""Approving is the only thing that publishes — so nothing may be created past it.

The live symptom this locks down: a post created already "approved" was never sent
anywhere, because the dispatch hangs off the *transition* into approved and there was
no transition. It then looked ready, so its author finished the job by hand with the
"Posted" button — leaving a post marked posted that no network had ever seen.
"""
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db
import webhooks


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
    admin = db.create_user("gate-admin@t.co", generate_password_hash("pw"), role="admin")
    cid = db.create_client({"name": "Gate Client"})
    member = db.create_user("gate-client@t.co", generate_password_hash("pw"),
                            role="client", client_id=cid)
    return dict(admin=admin, cid=cid, member=member)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


@pytest.fixture()
def sent(monkeypatch):
    """Every dispatch that leaves the app, in order."""
    calls = []

    def fake(post, *a, **kw):
        calls.append(post["id"])
        return True, "Dispatched to this client's webhook."

    monkeypatch.setattr(webhooks, "dispatch_post", fake)
    return calls


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


def form(**over):
    base = {"csrf_token": CSRF, "topic": "Gate topic", "caption": "Gate caption",
            "status": "draft", "platforms": ["instagram"]}
    base.update(over)
    return base


def test_a_post_cannot_be_created_already_approved(client, data, sent):
    """The exact shape of the live incident."""
    login_as(client, data["admin"])
    r = client.post("/content/new", data=form(client_id=data["cid"], status="approved"))
    assert db.get_posts() == []          # nothing created, rather than created unsent
    assert sent == []
    assert b"cannot be created" in r.data


def test_a_post_cannot_be_created_already_posted(client, data, sent):
    """'Posted' as a starting point is a claim about the world, not a state."""
    login_as(client, data["admin"])
    client.post("/content/new", data=form(client_id=data["cid"], status="posted"))
    assert db.get_posts() == []
    assert sent == []


def test_creating_a_draft_creates_it_and_sends_nothing(client, data, sent):
    login_as(client, data["admin"])
    client.post("/content/new", data=form(client_id=data["cid"]))
    assert [p["status"] for p in db.get_posts()] == ["draft"]
    assert sent == []


def test_approving_is_what_dispatches(client, data, sent):
    """The one path to 'approved' — and it goes out."""
    login_as(client, data["admin"])
    client.post("/content/new", data=form(client_id=data["cid"]))
    post_id = db.get_posts()[0]["id"]
    client.post(f"/content/{post_id}/status",
                data={"csrf_token": CSRF, "status": "approved"})
    assert db.get_post(post_id)["status"] == "approved"
    assert sent == [post_id]


def test_the_create_form_offers_no_status_past_the_gate(client, data):
    """A control that exists in the form is a control someone will use."""
    login_as(client, data["admin"])
    body = client.get("/content/new").data.decode()
    _, _, after = body.partition('<select name="status"')
    options, _, _ = after.partition("</select>")
    assert 'value="draft"' in options
    assert 'value="approved"' not in options
    assert 'value="posted"' not in options


def test_save_caption_route_refuses_a_post_past_the_gate(client, data, sent):
    login_as(client, data["admin"])
    r = client.post("/api/save-caption", json={
        "client_id": data["cid"], "platform": "instagram",
        "topic": "T", "caption": "C", "status": "approved"})
    assert r.status_code == 400
    assert db.get_posts() == []
    assert sent == []
