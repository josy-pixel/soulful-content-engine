"""Bulk weekly generation: who may run it, and what one request is allowed to cost.

The first version wrote the whole week inside one request — 22 to 36 model calls
back to back on the app's single gunicorn worker, which is killed at 180 seconds.
The work was lost, the tokens were spent, and every other request waited behind it.
These lock down the shape that replaced it, and the save that follows it.

Claude is faked at anthropic.Anthropic: no network, no key, no credits.
"""
import json
import re
from types import SimpleNamespace

import anthropic
import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db


CSRF = "test-csrf-token"


# ── a fake Claude that records every call ────────────────────────────────────

class FakeClaude:
    """Stands in for anthropic.Anthropic. Answers by recognising the prompt."""

    def __init__(self, plan_reply=None, audit_score=9):
        self.calls = []                 # dicts: kind, system, user
        self.plan_reply = plan_reply    # None = a well-formed plan for the asked count
        self.audit_score = audit_score
        fake = self

        class _Messages:
            def create(self, model, max_tokens, messages, system=None, **kw):
                return fake._create(system, messages)

        class _Client:
            def __init__(self, *a, **kw):
                self.messages = _Messages()

        self.client_cls = _Client

    @staticmethod
    def _reply(text):
        content = [] if text is None else [SimpleNamespace(text=text)]
        usage = SimpleNamespace(input_tokens=1, output_tokens=1,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=content, usage=usage)

    def _create(self, system, messages):
        system_text = system[0]['text'] if isinstance(system, list) else (system or '')
        user = messages[0]['content']
        if 'content strategist' in system_text:
            kind = 'plan'
            if self.plan_reply is not None:
                text = self.plan_reply
            else:
                n = int(re.search(r'exactly (\d+) objects', user).group(1))
                text = json.dumps([{"day": i, "topic": "Planned topic %d" % i} for i in range(n)])
        elif 'brand-voice auditor' in system_text:
            kind = 'audit'
            text = json.dumps({"score": self.audit_score, "notes": "ok", "rewritten":
                               None if self.audit_score >= 8 else "A rewritten caption."})
        elif 'hashtags' in user:
            kind = 'hashtags'
            text = '#one #two'
        else:
            kind = 'caption'
            text = 'A caption in her voice.'
        self.calls.append({'kind': kind, 'system': system_text, 'user': user})
        return self._reply(text)

    def kinds(self):
        return [c['kind'] for c in self.calls]


@pytest.fixture()
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key-never-sent')
    monkeypatch.setattr(anthropic, 'Anthropic', fake.client_cls)
    return fake


# ── data and sessions ─────────────────────────────────────────────────────────

@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["performance_metrics", "approval_history", "post_media", "content_posts",
              "trends", "brand_voices", "users", "clients"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    admin = db.create_user("bulk-admin@t.co", generate_password_hash("pw"), role="admin")
    manager = db.create_user("bulk-manager@t.co", generate_password_hash("pw"), role="manager")
    ca = db.create_client({"name": "Alpha Talent", "description": "wellness"})
    cb = db.create_client({"name": "Bravo Talent", "description": "fitness"})
    member = db.create_user("bulk-client@t.co", generate_password_hash("pw"),
                            role="client", client_id=ca)
    return dict(admin=admin, manager=manager, ca=ca, cb=cb, member=member)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


def post_json(c, url, body, csrf=True):
    headers = {"X-CSRF-Token": CSRF} if csrf else {}
    return c.post(url, data=json.dumps(body), content_type="application/json", headers=headers)


ITEM = {"topic": "A topic", "caption": "A caption", "hashtags": "#a",
        "scheduled_date": "2026-10-05T09:00"}


def save_body(cid, **over):
    body = {"client_id": cid, "platform": "instagram", "content_type": "photo",
            "status": "draft", "posts": [dict(ITEM), dict(ITEM)]}
    body.update(over)
    return body


# ── the page and who may use it ───────────────────────────────────────────────

@pytest.mark.parametrize("who", ["admin", "manager"])
def test_the_page_renders_for_the_agency(client, data, who):
    login_as(client, data[who])
    r = client.get("/bulk-generate")
    assert r.status_code == 200, r.data[:400]
    assert b"Alpha Talent" in r.data and b"Bravo Talent" in r.data
    assert b"Bulk Generate" in client.get("/").data          # the nav link is there


def test_a_client_user_cannot_open_the_page_or_see_the_link(client, data):
    login_as(client, data["member"])
    assert client.get("/bulk-generate").status_code == 403
    assert b"Bulk Generate" not in client.get("/").data


@pytest.mark.parametrize("url", ["/api/bulk-generate", "/api/bulk-save"])
def test_a_client_user_is_refused_by_every_bulk_endpoint(client, data, claude, url):
    login_as(client, data["member"])
    r = post_json(client, url, save_body(data["ca"]))
    assert r.status_code == 403
    assert db.get_posts() == []
    assert claude.calls == []
