"""Authorization tests for the roles & client-portal layer (Stage 1).

The forged-client_id case is the reason this suite exists: it cannot be exercised
through a browser, only by POSTing another client's id in the request body.
"""
import pytest
from werkzeug.security import generate_password_hash

import database as db
import app as flask_app


@pytest.fixture()
def data():
    """Fresh, deterministic tenant graph: 1 admin, 2 clients, 1 client user bound
    to client A, one post per client."""
    db.init_db()  # ensures schema + migrations (new user columns) exist
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["approval_history", "performance_metrics", "post_media",
              "content_posts", "client_media", "brand_voices", "clients",
              "users", "trends"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user("admin@t.co", generate_password_hash("pw"), role="admin")
    ca = db.create_client({"name": "Client A"})
    cb = db.create_client({"name": "Client B"})
    client_user = db.create_user("holly@t.co", generate_password_hash("pw"),
                                 role="client", client_id=ca)
    post_a = db.create_post({"client_id": ca, "platform": "facebook",
                             "topic": "A topic", "caption": "A caption"})
    post_b = db.create_post({"client_id": cb, "platform": "facebook",
                             "topic": "B topic", "caption": "B caption"})
    return dict(admin=admin, client_user=client_user,
                client_a=ca, client_b=cb, post_a=post_a, post_b=post_b)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(client, user_id):
    with client.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True


# ── client user is confined to their own tenant ──

def test_client_cannot_view_other_clients_content(client, data):
    login_as(client, data["client_user"])
    assert client.get(f"/content/{data['post_b']}").status_code == 403


def test_client_can_view_own_content(client, data):
    login_as(client, data["client_user"])
    assert client.get(f"/content/{data['post_a']}").status_code == 200


def test_client_cannot_edit_other_clients_content(client, data):
    login_as(client, data["client_user"])
    r = client.post(f"/content/{data['post_b']}/edit",
                    data={"topic": "x", "caption": "y", "content_type": "photo"})
    assert r.status_code == 403


def test_forged_client_id_is_overwritten(client, data):
    """A client user POSTs another client's id in the body; the post must land
    under THEIR client_id, never the forged one."""
    login_as(client, data["client_user"])
    r = client.post("/api/save-caption", json={
        "client_id": data["client_b"],   # forged
        "platform": "facebook", "topic": "t", "caption": "c",
    })
    assert r.status_code == 200
    pid = r.get_json()["post_id"]
    assert db.get_post(pid)["client_id"] == data["client_a"]  # not client_b


def test_client_cannot_open_another_client_record(client, data):
    login_as(client, data["client_user"])
    assert client.get(f"/clients/{data['client_b']}").status_code == 403


def test_client_bounced_from_all_clients_list(client, data):
    login_as(client, data["client_user"])
    r = client.get("/clients")
    assert r.status_code in (301, 302)  # redirected to their own client page


def test_client_blocked_from_users(client, data):
    login_as(client, data["client_user"])
    assert client.get("/users").status_code == 403


def test_client_blocked_from_reports(client, data):
    login_as(client, data["client_user"])
    assert client.get("/report").status_code == 403


def test_client_cannot_post_to_make_webhook(client, data):
    login_as(client, data["client_user"])
    r = client.post("/webhook/publish", json={"post_id": data["post_a"]})
    assert r.status_code in (401, 403)  # X-Secret gate rejects (no secret)


# ── admin keeps full access ──

def test_admin_full_access(client, data):
    login_as(client, data["admin"])
    assert client.get(f"/content/{data['post_b']}").status_code == 200
    assert client.get("/clients").status_code == 200
    assert client.get("/report").status_code == 200
    assert client.get("/users").status_code == 200


# ── tenancy invariant (SQLite: enforced in code, not a DB CHECK) ──

def test_client_user_requires_client_id():
    with pytest.raises(ValueError):
        db.create_user("orphan@t.co", "h", role="client", client_id=None)


def test_non_client_cannot_have_client_id(data):
    with pytest.raises(ValueError):
        db.create_user("weird@t.co", "h", role="admin", client_id=data["client_a"])


# ── invite flow (step 3) ──

def test_admin_invite_creates_pending_client_user(client, data):
    login_as(client, data["admin"])
    r = client.post("/users/invite",
                    data={"email": "new@t.co", "client_id": data["client_a"]})
    assert r.status_code in (301, 302)
    u = db.get_user_by_email("new@t.co")
    assert u and u["role"] == "client" and u["client_id"] == data["client_a"]
    assert u["is_active"] == 0          # pending until the invite is consumed
    assert u["password_hash"] == ""     # no usable password yet
    assert u["invite_token_hash"]       # has a stored invite hash


def test_invite_acceptance_sets_password_and_activates(client, data):
    import hashlib
    from datetime import datetime, timedelta
    token = "known-test-token-123456"
    thash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    exp = (datetime.now() + timedelta(hours=72)).isoformat()
    uid = db.create_pending_client_user("invitee@t.co", data["client_a"], thash, exp)

    assert client.get("/invite/wrong-token").status_code in (301, 302)   # rejected
    assert client.get(f"/invite/{token}").status_code == 200             # form shown

    r = client.post(f"/invite/{token}",
                    data={"password": "longenough123", "confirm": "longenough123"})
    assert r.status_code in (301, 302)
    u = db.get_user_by_id(uid)
    assert u["is_active"] == 1
    assert u["password_hash"]                    # now set
    assert u["invite_token_hash"] is None        # single-use: consumed


def test_short_invite_password_rejected(client, data):
    import hashlib
    from datetime import datetime, timedelta
    token = "another-token-abcdef"
    thash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    exp = (datetime.now() + timedelta(hours=72)).isoformat()
    uid = db.create_pending_client_user("short@t.co", data["client_a"], thash, exp)
    client.post(f"/invite/{token}", data={"password": "short", "confirm": "short"})
    assert db.get_user_by_id(uid)["is_active"] == 0   # still not activated


def test_deactivated_user_is_bounced(client, data):
    db.set_user_active(data["client_user"], False)
    login_as(client, data["client_user"])
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (301, 302)   # before_request logs out an inactive session
