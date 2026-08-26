"""Who am I signed in as.

The sidebar showed a Sign out button and nothing about whose session it would end.
With an admin and a client user in the same product, and several browser profiles
in play, that is the one place the answer has to be visible.
"""
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["content_posts", "client_media", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    admin = db.create_user("boss@agency.co", generate_password_hash("pw"), role="admin")
    cid = db.create_client({"name": "Holly Talent"})
    member = db.create_user("holly@client.co", generate_password_hash("pw"),
                            role="client", client_id=cid)
    return dict(admin=admin, member=member, cid=cid)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True


def test_an_admin_sees_their_own_address_and_role(client, data):
    login_as(client, data["admin"])
    html = client.get("/").get_data(as_text=True)
    assert "boss@agency.co" in html
    assert "Administrator" in html


def test_a_client_user_sees_which_client_they_are_acting_for(client, data):
    """For a client user the useful answer is the brand, not the word 'client'."""
    login_as(client, data["member"])
    html = client.get("/").get_data(as_text=True)
    assert "holly@client.co" in html
    assert "Holly Talent" in html


def test_the_identity_sits_with_the_sign_out_control(client, data):
    login_as(client, data["admin"])
    html = client.get("/").get_data(as_text=True)
    footer = html.split('class="sidebar-footer"')[1]
    assert "boss@agency.co" in footer.split("Sign out")[0]


def test_a_signed_out_visitor_is_shown_no_identity(client, data):
    r = client.get("/", follow_redirects=True)
    assert "boss@agency.co" not in r.get_data(as_text=True)
