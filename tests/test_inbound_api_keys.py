"""Per-client keys on the machine callbacks.

Both inbound endpoints were guarded by one shared secret: every client's scenario
held the same string, and the post it named was taken at face value. So a scenario
could report on any client's post. A key now identifies who is calling, and the
caller never states which client they are.
"""
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db


@pytest.fixture()
def data(monkeypatch):
    monkeypatch.setenv("MAKE_WEBHOOK_SECRET", "ci-webhook-secret")
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["performance_metrics", "approval_history", "content_posts",
              "client_api_keys", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user("key-admin@t.co", generate_password_hash("pw"), role="admin")
    ca = db.create_client({"name": "Key Client A"})
    cb = db.create_client({"name": "Key Client B"})
    post_a = db.create_post({"client_id": ca, "platform": "facebook", "topic": "A",
                             "caption": "a", "status": "approved"})
    post_b = db.create_post({"client_id": cb, "platform": "facebook", "topic": "B",
                             "caption": "b", "status": "approved"})
    _, key_a = db.create_client_api_key(ca, "A scenario")
    return dict(admin=admin, ca=ca, cb=cb, post_a=post_a, post_b=post_b, key_a=key_a)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


# ── the key identifies the caller ────────────────────────────────────────────

def test_a_key_can_report_its_own_clients_post(client, data):
    r = client.post("/webhook/publish",
                    json={"post_id": data["post_a"], "posted_url": "https://fb.com/1"},
                    headers={"X-Api-Key": data["key_a"]})
    assert r.status_code == 200
    assert db.get_post(data["post_a"])["status"] == "posted"


def test_a_key_cannot_reach_another_clients_post(client, data):
    """The cross-tenant hole this whole change exists to close."""
    r = client.post("/webhook/publish",
                    json={"post_id": data["post_b"], "posted_url": "https://fb.com/2"},
                    headers={"X-Api-Key": data["key_a"]})
    assert r.status_code == 404                      # not 403 — no confirming it exists
    assert db.get_post(data["post_b"])["status"] == "approved"


def test_an_unknown_key_is_refused(client, data):
    r = client.post("/webhook/publish", json={"post_id": data["post_a"]},
                    headers={"X-Api-Key": "sce_not-a-real-key"})
    assert r.status_code == 403


def test_a_revoked_key_stops_working(client, data):
    key_id, raw = db.create_client_api_key(data["ca"], "temporary")
    assert client.post("/webhook/publish", json={"post_id": data["post_a"]},
                       headers={"X-Api-Key": raw}).status_code == 200
    db.revoke_client_api_key(key_id)
    assert client.post("/webhook/publish", json={"post_id": data["post_a"]},
                       headers={"X-Api-Key": raw}).status_code == 403


def test_offering_a_bad_key_does_not_fall_back_to_the_shared_secret(client, data):
    """Otherwise a wrong key would be silently upgraded to unscoped access."""
    r = client.post("/webhook/publish",
                    json={"post_id": data["post_b"], "secret": "ci-webhook-secret"},
                    headers={"X-Api-Key": "sce_wrong"})
    assert r.status_code == 403


# ── what the database keeps ──────────────────────────────────────────────────

def test_the_raw_key_is_never_stored(data):
    rows = db.get_client_api_keys(data["ca"])
    assert rows
    for r in rows:
        assert data["key_a"] not in str(r.values())
        assert r["last4"] == data["key_a"][-4:]


def test_using_a_key_records_when(client, data):
    before = db.get_client_api_keys(data["ca"])[0]
    assert before["last_used_at"] is None
    client.post("/webhook/publish", json={"post_id": data["post_a"]},
                headers={"X-Api-Key": data["key_a"]})
    assert db.get_client_api_keys(data["ca"])[0]["last_used_at"] is not None


# ── the legacy secret, and its limits ────────────────────────────────────────

def test_the_shared_secret_still_works_while_scenarios_migrate(client, data):
    r = client.post("/webhook/publish",
                    json={"post_id": data["post_a"], "secret": "ci-webhook-secret"})
    assert r.status_code == 200


def test_the_shared_secret_can_be_switched_off(client, data, monkeypatch):
    monkeypatch.setenv("LEGACY_INBOUND_SECRET", "false")
    r = client.post("/webhook/publish",
                    json={"post_id": data["post_a"], "secret": "ci-webhook-secret"})
    assert r.status_code == 403


def test_a_secret_in_the_query_string_is_no_longer_accepted(client, data):
    """Query strings end up in access logs, proxies and browser history."""
    r = client.post("/webhook/publish?secret=ci-webhook-secret",
                    json={"post_id": data["post_a"]})
    assert r.status_code == 403


# ── the same rules on the metrics endpoint ───────────────────────────────────

def test_metrics_accept_the_owning_clients_key(client, data):
    r = client.post("/api/performance",
                    json={"post_id": data["post_a"], "likes": 5},
                    headers={"X-Api-Key": data["key_a"]})
    assert r.status_code == 200


def test_metrics_refuse_another_clients_post(client, data):
    r = client.post("/api/performance",
                    json={"post_id": data["post_b"], "likes": 5},
                    headers={"X-Api-Key": data["key_a"]})
    assert r.status_code == 404
