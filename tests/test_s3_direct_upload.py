"""Direct-to-storage upload.

The bytes go from the browser to S3 without passing through this server, so the
app's job shrinks to two things: hand out a permit that cannot be pointed at
someone else's files, and refuse to record a file that never actually arrived.
Those two are what these tests hold down, plus the rule that a post never stores
a link that expires.
"""
import pytest
from werkzeug.security import generate_password_hash

import database as db
import app as flask_app
import s3_media
import webhooks


CSRF = "test-csrf-token"


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["post_media", "content_posts", "client_media", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user("s3-admin@t.co", generate_password_hash("pw"), role="admin")
    ca = db.create_client({"name": "S3 Client A"})
    cb = db.create_client({"name": "S3 Client B"})
    return dict(admin=admin, ca=ca, cb=cb)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


@pytest.fixture()
def s3_on(monkeypatch):
    """Pretend storage is configured, without touching the network."""
    uploaded = {}

    monkeypatch.setattr(s3_media, "enabled", lambda: True)
    monkeypatch.setattr(s3_media, "presign_upload",
                        lambda key, ct=None: {"url": "https://bucket.example/", "fields": {"key": key}})
    monkeypatch.setattr(s3_media, "presign_view",
                        lambda key, expires=None: f"https://bucket.example/{key}?signed=yes")
    monkeypatch.setattr(s3_media, "head",
                        lambda key: uploaded.get(key))
    return uploaded


# ── the permit ───────────────────────────────────────────────────────────────

def test_permit_key_is_scoped_to_the_client(client, data, s3_on):
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/media/presign",
                    json={"filename": "clip.mp4"},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 200
    assert r.get_json()["key"].startswith(f"clients/{data['ca']}/")


def test_permit_refuses_a_disallowed_file_type(client, data, s3_on):
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/media/presign",
                    json={"filename": "payload.exe"},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 400


def test_permit_never_reuses_the_original_filename(client, data, s3_on):
    """The caller's name is not part of the key, so it cannot collide or traverse."""
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/media/presign",
                    json={"filename": "../../etc/passwd.jpg"},
                    headers={"X-CSRF-Token": CSRF})
    key = r.get_json()["key"]
    assert ".." not in key and key.endswith(".jpg")


def test_upload_is_refused_when_storage_is_not_configured(client, data, monkeypatch):
    monkeypatch.setattr(s3_media, "enabled", lambda: False)
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/media/presign",
                    json={"filename": "clip.mp4"},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 503


# ── registering what was uploaded ────────────────────────────────────────────

def test_a_file_that_never_arrived_is_not_recorded(client, data, s3_on):
    """The browser saying 'done' is not evidence. Storage is asked directly."""
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/media/complete",
                    json={"key": f"clients/{data['ca']}/ghost.mp4", "filename": "ghost.mp4"},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 400
    assert db.get_client_media(data["ca"]) == []


def test_cannot_register_a_key_belonging_to_another_client(client, data, s3_on):
    """Even with a real object behind it — the prefix is re-derived, not trusted."""
    foreign = f"clients/{data['cb']}/secret.mp4"
    s3_on[foreign] = {"size": 10, "content_type": "video/mp4"}
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/media/complete",
                    json={"key": foreign, "filename": "secret.mp4"},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 403
    assert db.get_client_media(data["ca"]) == []


def test_a_real_upload_is_recorded_against_storage(client, data, s3_on):
    key = f"clients/{data['ca']}/abc123.mp4"
    s3_on[key] = {"size": 4096, "content_type": "video/mp4"}
    login_as(client, data["admin"])
    r = client.post(f"/clients/{data['ca']}/media/complete",
                    json={"key": key, "filename": "holiday clip.mp4"},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 200

    rows = db.get_client_media(data["ca"])
    assert len(rows) == 1
    assert rows[0]["storage"] == "s3"
    assert rows[0]["s3_key"] == key
    assert rows[0]["file_size"] == 4096          # the size storage reported, not a claim
    assert rows[0]["media_type"] == "video"


def test_size_comes_from_storage_not_from_the_caller(client, data, s3_on):
    key = f"clients/{data['ca']}/sized.mp4"
    s3_on[key] = {"size": 999, "content_type": "video/mp4"}
    login_as(client, data["admin"])
    client.post(f"/clients/{data['ca']}/media/complete",
                json={"key": key, "filename": "sized.mp4", "file_size": 1},
                headers={"X-CSRF-Token": CSRF})
    assert db.get_client_media(data["ca"])[0]["file_size"] == 999


# ── references never expire ──────────────────────────────────────────────────

def test_a_post_stores_a_reference_not_a_signed_link(data, s3_on):
    """A signed link dies in an hour; a post outlives that by months."""
    media_id = db.add_media(data["ca"], "abc.mp4", "abc.mp4", "video", 10,
                            storage="s3", s3_key="clients/1/abc.mp4")
    ref = flask_app._media_ref(db.get_media(media_id))
    assert ref == "s3://clients/1/abc.mp4"
    assert "signed=yes" not in ref


def test_local_media_keeps_its_existing_reference(data):
    """Nothing about files already on disk changes."""
    media_id = db.add_media(data["ca"], "old.jpg", "old.jpg", "image", 10)
    with flask_app.app.test_request_context():
        ref = flask_app._media_ref(db.get_media(media_id))
    assert ref == f"/uploads/{data['ca']}/old.jpg"


# ── what Make receives ───────────────────────────────────────────────────────

def test_payload_resolves_a_reference_into_a_fetchable_link(monkeypatch):
    monkeypatch.setattr(s3_media, "presign_view",
                        lambda key, expires=None: f"https://bucket.example/{key}?signed=yes")
    monkeypatch.setenv("APP_URL", "https://app.example.com")
    payload = webhooks._build_payload({
        "id": 1, "client_id": 1, "platform": "facebook", "topic": "t",
        "caption": "c", "image_url": "s3://clients/1/abc.mp4",
    })
    assert payload["image_url"] == "https://bucket.example/clients/1/abc.mp4?signed=yes"


def test_payload_makes_local_media_absolute_too(monkeypatch):
    """Make must never have to glue a domain on; both storages arrive ready."""
    monkeypatch.setenv("APP_URL", "https://app.example.com")
    payload = webhooks._build_payload({
        "id": 1, "client_id": 1, "platform": "facebook", "topic": "t",
        "caption": "c", "image_url": "/uploads/1/old.jpg",
    })
    assert payload["image_url"] == "https://app.example.com/uploads/1/old.jpg"


def test_payload_leaves_an_external_link_alone(monkeypatch):
    monkeypatch.setenv("APP_URL", "https://app.example.com")
    payload = webhooks._build_payload({
        "id": 1, "client_id": 1, "platform": "facebook", "topic": "t",
        "caption": "c", "image_url": "https://youtu.be/xyz",
    })
    assert payload["image_url"] == "https://youtu.be/xyz"
