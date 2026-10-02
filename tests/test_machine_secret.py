"""The machine secret stays on the machine side.

The Trends page rendered MAKE_WEBHOOK_SECRET into its own JavaScript so the
"Generate" button could call the X-Secret route. Every signed-in user — client
users included — could read it in the page source, and with it read or rewrite
any client's post through the pipeline API. The page now calls a session route,
and the X-Secret routes no longer accept the secret in the query string.
"""
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import claude_api as ai
import database as db

SECRET = "ci-webhook-secret"
CSRF = "test-csrf-token"


@pytest.fixture()
def data(monkeypatch):
    monkeypatch.setenv("MAKE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(ai, "generate_trends",
                        lambda summary, platform: ([{"platform": platform,
                                                     "trend_text": "A trend",
                                                     "category": "format"}], None))
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["approval_history", "content_posts", "trends", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user("ms-admin@t.co", generate_password_hash("pw"), role="admin")
    ca = db.create_client({"name": "Client A"})
    client_user = db.create_user("ms-holly@t.co", generate_password_hash("pw"),
                                 role="client", client_id=ca)
    post = db.create_post({"client_id": ca, "platform": "facebook",
                           "topic": "A topic", "caption": "A caption"})
    return dict(admin=admin, client_user=client_user, post=post)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(client, user_id):
    with client.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


# ── the page never carries the secret ──

@pytest.mark.parametrize("who", ["admin", "client_user"])
def test_trends_page_does_not_render_the_machine_secret(client, data, who):
    login_as(client, data[who])
    r = client.get("/trends")
    assert r.status_code == 200
    assert SECRET not in r.get_data(as_text=True)


def test_generate_button_is_only_shown_to_staff(client, data):
    login_as(client, data["admin"])
    assert 'id="generateBtn"' in client.get("/trends").get_data(as_text=True)
    login_as(client, data["client_user"])
    assert 'id="generateBtn"' not in client.get("/trends").get_data(as_text=True)


# ── the page's own route: session + CSRF, staff only ──

def test_staff_generates_trends_through_the_session_route(client, data):
    login_as(client, data["admin"])
    r = client.post("/trends/generate", json={"platform": "instagram"},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_session_route_requires_csrf(client, data):
    login_as(client, data["admin"])
    r = client.post("/trends/generate", json={"platform": "instagram"})
    assert r.status_code == 400


def test_client_user_cannot_generate_trends(client, data):
    login_as(client, data["client_user"])
    r = client.post("/trends/generate", json={"platform": "instagram"},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 403


def test_session_route_needs_a_login(client, data):
    r = client.post("/trends/generate", json={"platform": "instagram"})
    assert r.status_code in (302, 400, 401)


# ── X-Secret routes: header only ──

def test_machine_route_still_accepts_the_secret_header(client, data):
    r = client.post("/api/trends/generate", json={"platform": "instagram"},
                    headers={"X-Secret": SECRET})
    assert r.status_code == 200


@pytest.mark.parametrize("method,path", [
    ("get", "/api/content/{post}"),
    ("patch", "/api/content/{post}"),
    ("post", "/api/trends/generate"),
])
def test_secret_in_query_string_is_refused(client, data, method, path):
    url = path.format(post=data["post"]) + "?secret=" + SECRET
    r = getattr(client, method)(url, json={"caption": "changed"})
    assert r.status_code == 403
    assert db.get_post(data["post"])["caption"] == "A caption"


def test_secret_header_still_reads_a_post(client, data):
    r = client.get(f"/api/content/{data['post']}", headers={"X-Secret": SECRET})
    assert r.status_code == 200
