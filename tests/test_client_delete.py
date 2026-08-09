"""Deleting a client.

Soft delete, admin only. The claims worth testing are the ones that are easy to
believe and expensive to get wrong: that the client really disappears from every
reader, that their content can no longer reach the publish path, that a client
user cannot do it, and that nothing is actually destroyed.
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
    for t in ["approval_history", "performance_metrics", "post_media", "audit_log",
              "content_posts", "client_media", "brand_voices", "client_webhooks",
              "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user("cd-admin@t.co", generate_password_hash("pw"), role="admin")
    ca = db.create_client({"name": "Client A"})
    cb = db.create_client({"name": "Client B"})
    user_a = db.create_user("a-user@t.co", generate_password_hash("pw"),
                            role="client", client_id=ca)
    # A client user who is NOT affected by deleting client A, so the permission
    # check is tested on a live session rather than one already logged out.
    user_b = db.create_user("b-user@t.co", generate_password_hash("pw"),
                            role="client", client_id=cb)
    post_a = db.create_post({"client_id": ca, "platform": "facebook",
                             "topic": "A", "caption": "A", "status": "approved"})
    post_b = db.create_post({"client_id": cb, "platform": "facebook",
                             "topic": "B", "caption": "B"})
    db.upsert_client_webhook(ca, "https://hook.eu1.make.com/aaa", "secret-aaa",
                             "facebook")
    return dict(admin=admin, ca=ca, cb=cb, user_a=user_a, user_b=user_b,
                post_a=post_a, post_b=post_b)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


# ── permission: admin only ───────────────────────────────────────────────────

def test_client_user_cannot_delete_a_client(client, data):
    login_as(client, data["user_a"])
    r = client.post(f"/clients/{data['ca']}/delete", data={"csrf_token": CSRF})
    assert r.status_code == 403
    assert db.get_client(data["ca"]) is not None      # still there


def test_client_user_cannot_delete_even_their_own_client(client, data):
    """Scope does not grant deletion — 'their own' is not an exception."""
    login_as(client, data["user_a"])
    assert client.post(f"/clients/{data['ca']}/delete",
                       data={"csrf_token": CSRF}).status_code == 403


def test_client_user_cannot_restore(client, data):
    db.soft_delete_client(data["ca"])
    login_as(client, data["user_b"])          # a live client session, not a logged-out one
    assert client.post(f"/clients/{data['ca']}/restore",
                       data={"csrf_token": CSRF}).status_code == 403
    assert db.get_client(data["ca"]) is None  # still deleted


def test_the_deleted_clients_own_user_is_locked_out_entirely(client, data):
    """They do not even reach a 403 — deletion deactivated them, so the login
    guard bounces the session before any route runs."""
    db.soft_delete_client(data["ca"])
    login_as(client, data["user_a"])
    r = client.post(f"/clients/{data['ca']}/restore", data={"csrf_token": CSRF})
    assert r.status_code in (301, 302)
    assert "/login" in r.headers.get("Location", "")
    assert db.get_client(data["ca"]) is None


def test_delete_requires_csrf(client, data):
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/delete")     # no token
    assert r.status_code == 400
    assert db.get_client(data["ca"]) is not None


# ── what deletion does ───────────────────────────────────────────────────────

def test_admin_delete_hides_the_client_everywhere(client, data):
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/delete", data={"csrf_token": CSRF})
    assert r.status_code in (301, 302)

    assert db.get_client(data["ca"]) is None
    assert data["ca"] not in [c["id"] for c in db.get_clients()]
    assert data["cb"] in [c["id"] for c in db.get_clients()]      # untouched


def test_deleted_clients_content_cannot_reach_the_publish_path(client, data):
    """The security-critical one. webhook_publish and the machine content routes
    all load via get_post(), which inner-joins the active-clients view."""
    login_as(client, data["admin"])
    client.post(f"/clients/{data['ca']}/delete", data={"csrf_token": CSRF})

    assert db.get_post(data["post_a"]) is None          # invisible to the publisher
    assert db.get_post(data["post_b"]) is not None      # other client unaffected
    assert data["post_a"] not in [p["id"] for p in db.get_posts()]


def test_delete_switches_off_logins_and_the_webhook(client, data):
    login_as(client, data["admin"])
    client.post(f"/clients/{data['ca']}/delete", data={"csrf_token": CSRF})

    assert db.get_user_by_id(data["user_a"])["is_active"] == 0
    assert db.get_client_webhook(data["ca"]) is None


def test_nothing_is_destroyed(client, data):
    """Soft delete means the rows survive — that is what makes restore possible."""
    login_as(client, data["admin"])
    client.post(f"/clients/{data['ca']}/delete", data={"csrf_token": CSRF})

    assert db.get_client_including_deleted(data["ca"]) is not None
    assert db.get_post_including_deleted(data["post_a"]) is not None
    assert data["ca"] in [c["id"] for c in db.get_deleted_clients()]


def test_deleting_twice_is_refused(data):
    db.soft_delete_client(data["ca"])
    with pytest.raises(ValueError):
        db.soft_delete_client(data["ca"])


def test_delete_is_audited(data):
    db.soft_delete_client(data["ca"], actor_user_id=data["admin"], actor_role="admin")
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM audit_log WHERE entity_type='client' AND action='delete' "
        "AND entity_id=?", (data["ca"],)).fetchone()
    conn.close()
    assert row is not None and row["actor_role"] == "admin"


# ── restore ──────────────────────────────────────────────────────────────────

def test_restore_brings_the_client_and_its_content_back(client, data):
    login_as(client, data["admin"])
    client.post(f"/clients/{data['ca']}/delete", data={"csrf_token": CSRF})
    r = client.post(f"/clients/{data['ca']}/restore", data={"csrf_token": CSRF})
    assert r.status_code in (301, 302)

    assert db.get_client(data["ca"]) is not None
    assert db.get_post(data["post_a"]) is not None     # library comes back intact


def test_restore_does_not_silently_resume_publishing(client, data):
    """A restore must not reconnect a live page as a side effect."""
    login_as(client, data["admin"])
    client.post(f"/clients/{data['ca']}/delete", data={"csrf_token": CSRF})
    client.post(f"/clients/{data['ca']}/restore", data={"csrf_token": CSRF})

    assert db.get_client_webhook(data["ca"]) is None            # still off
    assert db.get_user_by_id(data["user_a"])["is_active"] == 0   # still off


def test_restoring_a_live_client_is_refused(data):
    with pytest.raises(ValueError):
        db.restore_client(data["ca"])


# ── the impact figures shown in the confirm ──────────────────────────────────

def test_clients_page_renders_with_delete_and_trash(client, data):
    """Catches Jinja errors, and checks a quote in a client name cannot break
    out of the confirm dialog's JS string."""
    tricky = db.create_client({"name": "O'Brien & Sons \"Media\""})
    db.soft_delete_client(data["cb"])
    login_as(client, data["admin"])

    r = client.get("/clients")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert f"/clients/{tricky}/delete" in body
    assert f"/clients/{data['cb']}/restore" in body
    assert "Deleted clients" in body
    # the raw apostrophe never reaches the JS string unescaped
    assert "O'Brien" not in body


def test_client_user_never_sees_a_delete_control(client, data):
    login_as(client, data["user_b"])
    r = client.get(f"/clients/{data['cb']}", follow_redirects=True)
    assert b"/delete" not in r.data.replace(b"/content/", b"")


def test_impact_counts_what_it_claims(data):
    im = db.get_client_deletion_impact(data["ca"])
    assert im["posts"] == 1
    assert im["users"] == 1
    assert im["has_webhook"] is True
    assert db.get_client_deletion_impact(data["cb"])["has_webhook"] is False
