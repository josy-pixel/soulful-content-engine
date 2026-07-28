"""Part 2 data-layer tests for per-client webhooks: the repository + the view/base
boundary + one-active-per-client + secret masking. Dispatch/routing behaviour is
tested in Part 5.
"""
import pytest
import database as db
from webhooks import mask_secret


@pytest.fixture()
def cid():
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
    return db.create_client({"name": "C"})


def test_upsert_creates_and_reads_via_view(cid):
    assert db.get_client_webhook(cid) is None
    db.upsert_client_webhook(cid, "https://hook.make/abc", "secret1234", "facebook")
    w = db.get_client_webhook(cid)
    assert w and w["webhook_url"] == "https://hook.make/abc"
    assert w["status"] == "untested" and w["platforms_enabled"] == "facebook"


def test_upsert_updates_one_active_and_resets_status(cid):
    db.upsert_client_webhook(cid, "https://a", "s1aaaa", "facebook")
    db.record_webhook_result(cid, success=True)
    assert db.get_client_webhook(cid)["status"] == "verified"
    db.upsert_client_webhook(cid, "https://b", "s2bbbb", "facebook,instagram")
    w = db.get_client_webhook(cid)
    assert w["webhook_url"] == "https://b" and w["status"] == "untested"
    assert w["platforms_enabled"] == "facebook,instagram"
    assert len([x for x in db.get_all_client_webhooks() if x["client_id"] == cid]) == 1


def test_record_result_persists_error(cid):
    db.upsert_client_webhook(cid, "https://a", "s1aaaa", "facebook")
    db.record_webhook_result(cid, success=False, error="HTTP 500")
    w = db.get_client_webhook(cid)
    assert w["status"] == "failing" and w["last_error"] == "HTTP 500"


def test_disable_delete_and_reonboard(cid):
    db.upsert_client_webhook(cid, "https://a", "s1aaaa", "facebook")
    db.set_client_webhook_enabled(cid, False)
    assert db.get_client_webhook(cid)["status"] == "disabled"
    db.delete_client_webhook(cid)
    assert db.get_client_webhook(cid) is None                        # hidden by the view
    assert db.get_client_webhook_including_deleted(cid) is not None   # base still has it
    db.upsert_client_webhook(cid, "https://new", "s3cccc", "facebook")  # unique index freed
    assert db.get_client_webhook(cid)["webhook_url"] == "https://new"


def test_mask_secret():
    assert mask_secret("supersecret1234") == "…1234"
    assert mask_secret("ab") == "…"
    assert mask_secret("") == ""
