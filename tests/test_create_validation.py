"""A post may only be created with a platform and content type the app knows.

Both values end up in pages and in the payload sent to the publishing scenario.
The live hole this closes: /api/save-caption stored whatever platform it was
given, so a client user could store markup as a "platform" and have it run in an
admin's browser on any page that wrote it as HTML.
"""
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db


CSRF = "test-csrf-token"
H = {"X-CSRF-Token": CSRF}
MACHINE = {"X-Secret": "ci-webhook-secret"}
EVIL = '<img src=x onerror=alert(document.domain)>'


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["post_media", "approval_history", "performance_metrics",
              "content_posts", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    cid = db.create_client({"name": "Validation Client"})
    member = db.create_user("validation-client@t.co", generate_password_hash("pw"),
                            role="client", client_id=cid)
    return dict(cid=cid, member=member)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


def _save(client, data, **over):
    body = {"client_id": data["cid"], "platform": "instagram",
            "topic": "T", "caption": "C"}
    body.update(over)
    return client.post("/api/save-caption", headers=H, json=body)


def test_save_caption_refuses_an_unknown_platform(client, data):
    login_as(client, data["member"])
    r = _save(client, data, platform=EVIL)
    assert r.status_code == 400
    assert "Unknown platform" in r.get_json()["error"]
    assert db.get_posts() == []


def test_save_caption_refuses_an_unknown_content_type(client, data):
    login_as(client, data["member"])
    r = _save(client, data, content_type=EVIL)
    assert r.status_code == 400
    assert "Unknown content type" in r.get_json()["error"]
    assert db.get_posts() == []


def test_save_caption_normalises_case_and_keeps_the_default_content_type(client, data):
    """The caption generator sends no content type; that must keep working, and a
    capitalised platform is the same platform, not a reason to refuse."""
    login_as(client, data["member"])
    r = _save(client, data, platform=" Instagram ")
    assert r.status_code == 200, r.data
    post = db.get_post(r.get_json()["post_id"])
    assert post["platform"] == "instagram"
    assert post["content_type"] == "photo"

    r = _save(client, data, platform="facebook", content_type="Reel")
    assert r.status_code == 200, r.data
    assert db.get_post(r.get_json()["post_id"])["content_type"] == "reel"


def test_machine_create_refuses_an_unknown_platform(client, data):
    r = client.post("/api/content", headers=MACHINE, json={
        "client_id": data["cid"], "platform": EVIL, "topic": "T"})
    assert r.status_code == 400
    assert db.get_posts() == []

    r = client.post("/api/content", headers=MACHINE, json={
        "client_id": data["cid"], "platform": "facebook", "topic": "T",
        "content_type": "video"})
    assert r.status_code == 201, r.data


def test_report_page_escapes_what_it_writes_as_html(client, data):
    """The report writes the platform breakdown and the model's report into the
    page with innerHTML; both must pass through the escape first."""
    admin = db.create_user("validation-admin@t.co", generate_password_hash("pw"), role="admin")
    login_as(client, admin)
    html = client.get("/report").data.decode()
    assert "escapeHtml(p.platform)" in html
    assert "return escapeHtml(md)" in html
