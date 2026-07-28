"""Part 5 route/UI tests for the Settings webhooks area: admin-only access, the
secret never leaking into HTML or audit_log, URL uniqueness across clients, and the
test-ping verdict (a scenario that accepts a wrong secret is 'insecure', not verified).
"""
import pytest
from werkzeug.security import generate_password_hash

import database as db
import app as flask_app
import webhooks

CSRF = "test-csrf-token"


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    for t in ["approval_history", "performance_metrics", "post_media", "content_posts",
              "client_media", "brand_voices", "client_webhooks", "clients", "users",
              "trends", "audit_log"]:
        try:
            conn.execute("DELETE FROM %s" % t)   # raw-query-ok: test teardown
        except Exception:
            pass
    conn.commit(); conn.close()
    admin = db.create_user("admin@t.co", generate_password_hash("pw"), role="admin")
    ca = db.create_client({"name": "A"})
    cb = db.create_client({"name": "B"})
    cu = db.create_user("holly@t.co", generate_password_hash("pw"), role="client", client_id=ca)
    return dict(admin=admin, client_user=cu, ca=ca, cb=cb)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid); s["_fresh"] = True; s["_csrf_token"] = CSRF


def test_client_role_gets_403_on_every_settings_route(client, data):
    login(client, data["client_user"])
    for p in ("/settings/webhooks", "/settings/api-keys", "/settings/general"):
        assert client.get(p).status_code == 403
    ca = data["ca"]
    assert client.post("/settings/webhooks/save", data={
        "csrf_token": CSRF, "client_id": ca, "webhook_url": "https://x",
        "webhook_secret": "s", "platforms": "facebook"}).status_code == 403
    for verb in ("test", "disable", "enable", "delete"):
        assert client.post("/settings/webhooks/%s/%s" % (ca, verb),
                           data={"csrf_token": CSRF}).status_code == 403


def test_admin_can_open_settings(client, data):
    login(client, data["admin"])
    assert client.get("/settings/webhooks").status_code == 200
    assert client.get("/settings/api-keys").status_code == 200


def test_secret_never_in_html_or_audit(client, data):
    login(client, data["admin"])
    client.post("/settings/webhooks/save", data={
        "csrf_token": CSRF, "client_id": data["ca"], "webhook_url": "https://hook/aaa",
        "webhook_secret": "topsecret9999", "platforms": "facebook"})
    html = client.get("/settings/webhooks").get_data(as_text=True)
    assert "topsecret9999" not in html          # full secret never rendered
    assert "…9999" in html                       # masked last-4 is shown
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM audit_log").fetchall()   # raw-query-ok: test
    conn.close()
    assert rows and all("topsecret9999" not in ((r["metadata"] or "") + (r["reason"] or "")) for r in rows)


def test_url_cannot_be_shared_across_clients(client, data):
    login(client, data["admin"])
    client.post("/settings/webhooks/save", data={
        "csrf_token": CSRF, "client_id": data["ca"], "webhook_url": "https://hook/shared",
        "webhook_secret": "s1aaaa", "platforms": "facebook"})
    client.post("/settings/webhooks/save", data={
        "csrf_token": CSRF, "client_id": data["cb"], "webhook_url": "https://hook/shared",
        "webhook_secret": "s2bbbb", "platforms": "facebook"})
    assert db.get_client_webhook(data["ca"]) is not None
    assert db.get_client_webhook(data["cb"]) is None       # rejected, not saved


def test_test_ping_marks_insecure_when_wrong_secret_accepted(client, data, monkeypatch):
    login(client, data["admin"])
    db.upsert_client_webhook(data["ca"], "https://hook/a", "s1aaaa", "facebook")
    monkeypatch.setattr(webhooks, "_http_post", lambda url, p, secret=None: (True, 200, None))
    r = client.post("/settings/webhooks/%s/test" % data["ca"], data={"csrf_token": CSRF})
    assert r.get_json()["status"] == "insecure"
    assert db.get_client_webhook(data["ca"])["status"] == "insecure"


def test_test_ping_verified_when_wrong_secret_rejected(client, data, monkeypatch):
    login(client, data["admin"])
    db.upsert_client_webhook(data["ca"], "https://hook/a", "s1aaaa", "facebook")
    monkeypatch.setattr(webhooks, "_http_post",
                        lambda url, p, secret=None: (True, 200, None) if secret == "s1aaaa" else (False, 403, "HTTP 403"))
    r = client.post("/settings/webhooks/%s/test" % data["ca"], data={"csrf_token": CSRF})
    assert r.get_json()["status"] == "verified"
    assert db.get_client_webhook(data["ca"])["status"] == "verified"
