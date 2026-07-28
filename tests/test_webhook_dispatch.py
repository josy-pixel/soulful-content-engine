"""Part 3 dispatch tests: routing is resolved ONLY from the post's own client_id,
refusals are loud, the legacy fallback is env-gated, outcomes are recorded, and the
secret never lands in audit_log. _http_post is stubbed so no network is touched.
"""
import json
import pytest
import database as db
import webhooks


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv('LEGACY_WEBHOOK_FALLBACK', raising=False)
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
    a = db.create_client({"name": "A"})
    b = db.create_client({"name": "B"})
    db.upsert_client_webhook(a, "https://A", "secretAAAA", "facebook")
    db.upsert_client_webhook(b, "https://B", "secretBBBB", "facebook,instagram")
    pa = db.create_post({"client_id": a, "platform": "facebook", "topic": "t", "caption": "c"})
    pb = db.create_post({"client_id": b, "platform": "facebook", "topic": "t", "caption": "c"})
    return dict(a=a, b=b, pa=pa, pb=pb)


def _stub(monkeypatch, ok=True, status=200, err=None, cap=None):
    def fake(url, payload, secret=None):
        if cap is not None:
            cap['url'] = url; cap['secret'] = secret; cap['payload'] = payload
        return ok, status, err
    monkeypatch.setattr(webhooks, '_http_post', fake)


def test_resolves_to_own_client_webhook_never_another(env, monkeypatch):
    cap = {}
    _stub(monkeypatch, cap=cap)
    ok, _ = webhooks.dispatch_post(db.get_post(env['pa']))
    assert ok and cap['url'] == 'https://A' and cap['secret'] == 'secretAAAA'
    cap.clear()
    webhooks.dispatch_post(db.get_post(env['pb']))
    assert cap['url'] == 'https://B' and cap['secret'] == 'secretBBBB'
    # the payload's client_id is the post's own — the only routing input
    assert cap['payload']['client_id'] == env['b']


def test_no_webhook_fails_and_does_not_fall_back(env, monkeypatch):
    monkeypatch.delenv('LEGACY_WEBHOOK_FALLBACK', raising=False)
    c = db.create_client({"name": "NoHook"})
    pid = db.create_post({"client_id": c, "platform": "facebook", "topic": "t", "caption": "c"})
    ok, msg = webhooks.dispatch_post(db.get_post(pid))
    assert not ok and 'No webhook configured' in msg


def test_platform_not_enabled_refused(env):
    pid = db.create_post({"client_id": env['a'], "platform": "instagram", "topic": "t", "caption": "c"})
    ok, msg = webhooks.dispatch_post(db.get_post(pid))
    assert not ok and 'Instagram is not connected' in msg


def test_disabled_webhook_refused(env):
    db.set_client_webhook_enabled(env['a'], False)
    ok, msg = webhooks.dispatch_post(db.get_post(env['pa']))
    assert not ok and 'disabled' in msg


def test_deleted_post_refused_at_dispatch(env):
    conn = db.get_db()
    conn.execute("UPDATE content_posts SET deleted_at=? WHERE id=?", ("2026-01-01", env['pa']))  # raw-query-ok: test
    conn.commit(); conn.close()
    stale = {"id": env['pa'], "client_id": env['a'], "platform": "facebook", "client_name": "A"}
    ok, msg = webhooks.dispatch_post(stale)
    assert not ok and 'deleted' in msg


def test_failed_dispatch_records_error_and_is_not_sent(env, monkeypatch):
    _stub(monkeypatch, ok=False, status=500, err='HTTP 500')
    ok, msg = webhooks.dispatch_post(db.get_post(env['pa']))
    assert not ok
    w = db.get_client_webhook(env['a'])
    assert w['status'] == 'failing' and w['last_error'] == 'HTTP 500'


def test_legacy_fallback_only_when_enabled(env, monkeypatch):
    monkeypatch.setenv('LEGACY_WEBHOOK_FALLBACK', 'true')
    monkeypatch.setenv('MAKE_WEBHOOK_URL', 'https://legacy')
    cap = {}
    _stub(monkeypatch, cap=cap)
    c = db.create_client({"name": "NoHook"})
    pid = db.create_post({"client_id": c, "platform": "facebook", "topic": "t", "caption": "c"})
    ok, msg = webhooks.dispatch_post(db.get_post(pid))
    assert ok and cap['url'] == 'https://legacy' and cap['secret'] is None   # legacy sends no X-Secret


def test_dispatch_audits_without_secret(env, monkeypatch):
    _stub(monkeypatch)
    webhooks.dispatch_post(db.get_post(env['pa']), actor_user_id=1, actor_role='admin')
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM audit_log WHERE action='dispatch'").fetchall()  # raw-query-ok: test
    conn.close()
    assert len(rows) >= 1
    row = dict(rows[-1])
    assert 'secretAAAA' not in (row.get('metadata') or '')
    assert 'secretAAAA' not in (row.get('reason') or '')
    assert json.loads(row['metadata'])['ok'] is True
