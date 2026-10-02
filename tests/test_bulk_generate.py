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

    EMPTY = object()   # a reply with no content blocks at all

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
            if self.plan_reply is FakeClaude.EMPTY:
                text = None
            elif self.plan_reply is not None:
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


def plan_body(cid, **over):
    body = {"client_id": cid, "platform": "instagram", "content_type": "photo",
            "theme": "Autumn reset", "days": 7, "posts_per_day": 1,
            "start_date": "2026-10-05"}
    body.update(over)
    return body


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


@pytest.mark.parametrize("url", ["/api/bulk-plan", "/api/bulk-save"])
def test_a_client_user_is_refused_by_every_bulk_endpoint(client, data, claude, url):
    login_as(client, data["member"])
    r = post_json(client, url, save_body(data["ca"]))
    assert r.status_code == 403
    assert db.get_posts() == []
    assert claude.calls == []


# ── one request, one small piece of work ──────────────────────────────────────

def test_the_whole_week_in_one_request_is_gone(client, data, claude):
    """The route that wrote every post inside one request — killed by the worker
    timeout with the tokens already spent — no longer exists."""
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-generate", plan_body(data["ca"]))
    assert r.status_code == 404
    assert claude.calls == []


def test_the_plan_is_one_model_call_and_dates_each_topic(client, data, claude):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", plan_body(data["ca"], days=7, posts_per_day=2))
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert claude.kinds() == ["plan"]
    assert body["client_id"] == data["ca"] and body["theme"] == "Autumn reset"
    assert len(body["posts"]) == 14 and body["requested"] == 14
    slots = [p["scheduled_date"] for p in body["posts"]]
    assert slots[:3] == ["2026-10-05T09:00", "2026-10-05T13:00", "2026-10-06T09:00"]
    assert slots[-1] == "2026-10-11T13:00"
    assert db.get_posts() == []                       # planning stores nothing


def test_each_post_is_its_own_request_of_a_few_calls(client, data, monkeypatch):
    """Worst case for one post: draft, three audits, hashtags — five calls, well
    inside the worker's 180 seconds, however long the week is."""
    fake = FakeClaude(audit_score=5)                  # every audit asks for a rewrite
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key-never-sent')
    monkeypatch.setattr(anthropic, 'Anthropic', fake.client_cls)
    login_as(client, data["admin"])
    r = post_json(client, "/api/generate-caption",
                  {"client_id": data["ca"], "platform": "instagram",
                   "topic": "Planned topic 0", "weekly_direction": "Autumn reset"})
    assert r.status_code == 200, r.data
    assert len(fake.calls) <= 5


def test_the_weekly_direction_is_the_same_cached_rulebook_for_every_post(client, data, claude):
    login_as(client, data["admin"])
    for topic in ("First topic", "Second topic"):
        r = post_json(client, "/api/generate-caption",
                      {"client_id": data["ca"], "platform": "instagram",
                       "topic": topic, "weekly_direction": "Autumn reset"})
        assert r.status_code == 200, r.data
    writer = [c["system"] for c in claude.calls if c["kind"] == "caption"]
    auditor = [c["system"] for c in claude.calls if c["kind"] == "audit"]
    assert len(set(writer)) == 1 and len(set(auditor)) == 1   # byte-identical prefix
    assert "THIS WEEK'S CREATIVE DIRECTION" in writer[0] and "Autumn reset" in writer[0]
    assert "Autumn reset" in auditor[0]                       # the auditor sees it too


def test_the_single_generator_is_unchanged_without_a_direction(client, data, claude):
    login_as(client, data["admin"])
    r = post_json(client, "/api/generate-caption",
                  {"client_id": data["ca"], "platform": "instagram", "topic": "A topic"})
    assert r.status_code == 200
    assert all("THIS WEEK'S CREATIVE DIRECTION" not in c["system"] for c in claude.calls)


@pytest.mark.parametrize("days,per_day", [(15, 1), (5, 3), (8, 2), (0, 1), (7, 4), ("x", 1)])
def test_a_plan_over_the_cap_is_refused_before_any_call(client, data, claude, days, per_day):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", plan_body(data["ca"], days=days, posts_per_day=per_day))
    assert r.status_code == 400
    assert r.get_json()["error"]
    assert claude.calls == []


def test_the_cap_itself_is_allowed(client, data, claude):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", plan_body(data["ca"], days=14, posts_per_day=1))
    assert r.status_code == 200 and len(r.get_json()["posts"]) == 14


@pytest.mark.parametrize("reply", ["Sure! Here are some ideas.", FakeClaude.EMPTY, "{}"])
def test_a_failed_plan_answers_in_json(client, data, monkeypatch, reply):
    """The page reads one JSON body; a broken model reply must not become an HTML 500."""
    fake = FakeClaude(plan_reply=reply)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key-never-sent')
    monkeypatch.setattr(anthropic, 'Anthropic', fake.client_cls)
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", plan_body(data["ca"]))
    assert r.status_code == 500
    assert r.is_json and r.get_json()["error"]


def test_planning_for_a_deleted_client_is_404(client, data, claude):
    db.soft_delete_client(data["cb"])
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", plan_body(data["cb"]))
    assert r.status_code == 404 and r.is_json
    assert claude.calls == []


def test_a_plan_request_that_is_not_an_object_is_400(client, data, claude):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", ["not", "an", "object"])
    assert r.status_code == 400 and r.is_json


def test_the_page_writes_post_by_post_through_the_shared_pool(client, data):
    login_as(client, data["admin"])
    page = client.get("/bulk-generate").data.decode()
    assert "js/run_pool.js" in page
    assert "/api/bulk-plan" in page and "/api/generate-caption" in page
    assert "/api/bulk-generate" not in page
    js = client.get("/static/js/run_pool.js")
    assert js.status_code == 200 and b"async function runPool" in js.data
    js.close()
    gallery = client.get("/clients/%d/gallery" % data["ca"]).data.decode()
    assert "js/run_pool.js" in gallery                        # extracted, not copied
    assert "async function runPool" not in gallery
