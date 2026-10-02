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
PW = generate_password_hash("pw")      # hashed once: the hash is slow by design


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
    admin = db.create_user("bulk-admin@t.co", PW, role="admin")
    manager = db.create_user("bulk-manager@t.co", PW, role="manager")
    ca = db.create_client({"name": "Alpha Talent", "description": "wellness"})
    cb = db.create_client({"name": "Bravo Talent", "description": "fitness"})
    member = db.create_user("bulk-client@t.co", PW,
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


# ── only what something downstream can publish ────────────────────────────────

def test_the_page_offers_only_publishable_platforms_and_types(client, data):
    login_as(client, data["admin"])
    page = client.get("/bulk-generate").data.decode()
    offered = set(re.findall(r'data-platform="(\w+)"', page))
    assert offered == {"instagram", "facebook"}
    types = json.loads(re.search(r"const CONTENT_TYPES = (\{.*?\});", page).group(1))
    assert types == {"instagram": ["photo", "video", "reel"],
                     "facebook": ["photo", "video", "post"]}


UNPUBLISHABLE = [("instagram", "story"), ("tiktok", "video"), ("linkedin", "post"),
                 ("youtube", "video"), ("facebook", "reel"), ("instagram", None),
                 ("instagram", "carousel"), (["instagram"], "photo")]


@pytest.mark.parametrize("platform,content_type", UNPUBLISHABLE)
def test_a_plan_for_something_nothing_publishes_is_refused(client, data, claude,
                                                          platform, content_type):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan",
                  plan_body(data["ca"], platform=platform, content_type=content_type))
    assert r.status_code == 400 and r.get_json()["error"]
    assert claude.calls == []


@pytest.mark.parametrize("platform,content_type", UNPUBLISHABLE)
def test_a_save_of_something_nothing_publishes_is_refused(client, data,
                                                         platform, content_type):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-save",
                  save_body(data["ca"], platform=platform, content_type=content_type))
    assert r.status_code == 400
    assert db.get_posts() == []


@pytest.mark.parametrize("platform,content_type", [("instagram", "reel"), ("facebook", "post")])
def test_publishable_combinations_are_planned_and_saved(client, data, claude,
                                                        platform, content_type):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan",
                  plan_body(data["ca"], platform=platform, content_type=content_type))
    assert r.status_code == 200 and r.get_json()["content_type"] == content_type
    r = post_json(client, "/api/bulk-save",
                  save_body(data["ca"], platform=platform, content_type=content_type))
    assert r.status_code == 200
    assert {(p["platform"], p["content_type"]) for p in db.get_posts()} == {(platform, content_type)}


# ── the save: the gate, the batch's own fields, and all or nothing ────────────

NOT_CREATABLE = [st for st in flask_app.STATUSES if st not in flask_app.CREATE_STATUSES]


@pytest.mark.parametrize("status", NOT_CREATABLE + ["", None, ["draft"]])
def test_no_post_of_a_batch_is_born_past_the_approval_gate(client, data, status):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-save", save_body(data["ca"], status=status))
    assert r.status_code == 400
    assert db.get_posts() == []


@pytest.mark.parametrize("status", flask_app.CREATE_STATUSES)
def test_a_batch_is_saved_in_a_creatable_status(client, data, status):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-save", save_body(data["ca"], status=status))
    assert r.status_code == 200 and r.get_json()["count"] == 2
    posts = db.get_posts()
    assert {p["status"] for p in posts} == {status}
    assert {p["scheduled_date"] for p in posts} == {"2026-10-05T09:00"}
    assert {p["notes"] for p in posts} == {"Created via bulk weekly build."}


def test_what_one_post_says_about_status_client_or_platform_is_ignored(client, data):
    login_as(client, data["admin"])
    forged = dict(ITEM, status="approved", client_id=data["cb"], platform="facebook",
                  content_type="post")
    r = post_json(client, "/api/bulk-save", save_body(data["ca"], posts=[forged, forged]))
    assert r.status_code == 200
    posts = db.get_posts()
    assert len(posts) == 2
    assert {(p["client_id"], p["status"], p["platform"], p["content_type"]) for p in posts} \
        == {(data["ca"], "draft", "instagram", "photo")}


def test_opening_the_gate_to_talents_keeps_their_client_id_their_own(data, claude):
    """BULK_GENERATE_ROLES may one day include 'client'. The routes themselves still
    overwrite a forged client_id with the talent's own — called here past the
    role decorator, as they would run once the gate opens."""
    import auth
    from flask_login import login_user
    member = auth.User(db.get_user_by_id(data["member"]))
    forged_save = save_body(data["cb"], posts=[dict(ITEM, client_id=data["cb"])])
    with flask_app.app.test_request_context("/api/bulk-save", method="POST", json=forged_save):
        login_user(member)
        r = flask_app.api_bulk_save.__wrapped__()
    assert r.status_code == 200
    assert {p["client_id"] for p in db.get_posts()} == {data["ca"]}
    with flask_app.app.test_request_context("/api/bulk-plan", method="POST",
                                            json=plan_body(data["cb"])):
        login_user(member)
        r = flask_app.api_bulk_plan.__wrapped__()
    assert r.get_json()["client_id"] == data["ca"]
    plan_prompt = [c for c in claude.calls if c["kind"] == "plan"][0]["user"]
    assert "Alpha Talent" in plan_prompt and "Bravo Talent" not in plan_prompt


def test_a_save_without_the_csrf_token_is_refused(client, data):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-save", save_body(data["ca"]), csrf=False)
    assert r.status_code == 400
    assert db.get_posts() == []


MALFORMED = [
    {"posts": "a string"}, {"posts": {"topic": "t", "caption": "c"}}, {"posts": None},
    {"posts": []}, {"posts": ["just text"]}, {"posts": [dict(ITEM), 7]},
    {"posts": [dict(ITEM), dict(ITEM, caption="  ")]}, {"posts": [dict(ITEM, topic=None)]},
    {"posts": [dict(ITEM, caption=["a list"])]}, {"posts": [dict(ITEM, hashtags=5)]},
    {"posts": [dict(ITEM, scheduled_date="next tuesday")]},
    {"posts": [dict(ITEM, scheduled_date=20261005)]},
]


@pytest.mark.parametrize("over", MALFORMED)
def test_a_malformed_batch_is_400_and_saves_nothing(client, data, over):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-save", save_body(data["ca"], **over))
    assert r.status_code == 400 and r.is_json and r.get_json()["error"]
    assert db.get_posts() == []


@pytest.mark.parametrize("body", [["a", "list"], "text", 5])
def test_a_save_body_that_is_not_an_object_is_400(client, data, body):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-save", body)
    assert r.status_code == 400 and r.is_json


def test_a_batch_over_the_cap_is_refused_and_the_cap_is_allowed(client, data):
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-save", save_body(data["ca"], posts=[dict(ITEM)] * 15))
    assert r.status_code == 400
    assert db.get_posts() == []
    r = post_json(client, "/api/bulk-save", save_body(data["ca"], posts=[dict(ITEM)] * 14))
    assert r.status_code == 200 and r.get_json()["count"] == 14


def test_saving_to_a_deleted_client_is_404(client, data):
    db.soft_delete_client(data["cb"])
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-save", save_body(data["cb"]))
    assert r.status_code == 404 and r.is_json
    conn = db.get_db()
    n = conn.execute("SELECT COUNT(*) FROM content_posts").fetchone()[0]   # raw-query-ok: counts deleted too
    conn.close()
    assert n == 0


@pytest.fixture()
def boom_on_third_post():
    """A real database failure partway through a batch: the third insert aborts."""
    conn = db.get_db()
    conn.execute("""CREATE TRIGGER IF NOT EXISTS bulk_boom BEFORE INSERT ON content_posts
                    WHEN NEW.topic = 'boom' BEGIN SELECT RAISE(ABORT, 'boom'); END""")
    conn.commit()
    conn.close()
    yield
    conn = db.get_db()
    conn.execute("DROP TRIGGER IF EXISTS bulk_boom")
    conn.commit()
    conn.close()


def test_a_batch_is_saved_whole_or_not_at_all(client, data, boom_on_third_post):
    login_as(client, data["admin"])
    posts = [dict(ITEM, topic="one"), dict(ITEM, topic="two"), dict(ITEM, topic="boom"),
             dict(ITEM, topic="four")]
    with pytest.raises(Exception):
        post_json(client, "/api/bulk-save", save_body(data["ca"], posts=posts))
    conn = db.get_db()
    posts_left = conn.execute("SELECT COUNT(*) FROM content_posts").fetchone()[0]   # raw-query-ok: counts every row
    history_left = conn.execute("SELECT COUNT(*) FROM approval_history").fetchone()[0]
    conn.close()
    assert (posts_left, history_left) == (0, 0)
    # and the connection was not left holding the write lock
    assert db.create_post({"client_id": data["ca"], "platform": "instagram",
                           "topic": "after", "caption": "c"})


@pytest.mark.parametrize("cid", [None, "", "abc", [1], {"id": 1}])
def test_a_batch_without_a_usable_client_id_is_400(client, data, claude, cid):
    login_as(client, data["admin"])
    assert post_json(client, "/api/bulk-plan", plan_body(cid)).status_code == 400
    assert post_json(client, "/api/bulk-save", save_body(cid)).status_code == 400
    assert claude.calls == [] and db.get_posts() == []


# ── the plan hears this client's voice and trends, and nobody else's ──────────

def _plan_call(claude):
    return [c for c in claude.calls if c["kind"] == "plan"][0]


def test_the_plan_uses_this_clients_trends_and_the_org_wide_ones_only(client, data, claude):
    db.add_trends([
        {"platform": "instagram", "trend_text": "ORG-WIDE quiet luxury"},
        {"platform": "instagram", "trend_text": "ALPHA morning pages", "client_id": data["ca"]},
        {"platform": "instagram", "trend_text": "BRAVO gym tour", "client_id": data["cb"]},
        {"platform": "facebook", "trend_text": "ALPHA facebook-only", "client_id": data["ca"]},
    ])
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", plan_body(data["ca"]))
    used = r.get_json()["trends_used"]
    assert sorted(used) == ["ALPHA morning pages", "ORG-WIDE quiet luxury"]
    prompt = _plan_call(claude)["user"]
    assert "BRAVO gym tour" not in prompt and "ALPHA facebook-only" not in prompt


def test_the_plan_is_given_the_voice_document_and_banned_words(client, data, claude):
    db.update_client_voice(data["ca"], "VOICE DOC: short, steady sentences.", ["A sample."])
    db.upsert_brand_voice(data["ca"], "instagram", {"avoid_words": json.dumps(["journey", "unlock"])})
    login_as(client, data["admin"])
    post_json(client, "/api/bulk-plan", plan_body(data["ca"]))
    system = _plan_call(claude)["system"]
    assert "VOICE DOC: short, steady sentences." in system
    assert "NEVER USE" in system and "journey, unlock" in system
    assert "EMOJI:" not in system and "LENGTH:" not in system   # unset settings add nothing


def test_a_client_with_no_voice_settings_gets_no_injected_rules(client, data, claude):
    login_as(client, data["admin"])
    post_json(client, "/api/bulk-plan", plan_body(data["cb"]))
    system = _plan_call(claude)["system"]
    for absent in ("BRAND VOICE DOCUMENT", "NEVER USE", "EMOJI", "LENGTH", "emoji"):
        assert absent not in system


def test_a_topic_that_carries_a_banned_word_is_flagged_for_an_edit(client, data, monkeypatch):
    fake = FakeClaude(plan_reply=json.dumps([
        {"day": 0, "topic": "Your Journey back to calm"},
        {"day": 1, "topic": "The journeyman's quiet morning"},     # a different word
        {"day": 2, "topic": "Unlock: three slow habits"},
    ]))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key-never-sent')
    monkeypatch.setattr(anthropic, 'Anthropic', fake.client_cls)
    db.upsert_brand_voice(data["ca"], "instagram", {"avoid_words": json.dumps(["journey", "unlock"])})
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", plan_body(data["ca"], days=3))
    assert [p["banned"] for p in r.get_json()["posts"]] == [["journey"], [], ["unlock"]]


def test_the_plan_prompt_carries_no_dead_cache_marker(client, data, claude, monkeypatch):
    seen = {}
    real = claude._create

    def spy(system, messages):
        seen["system"] = system
        return real(system, messages)

    monkeypatch.setattr(claude, "_create", spy)
    login_as(client, data["admin"])
    post_json(client, "/api/bulk-plan", plan_body(data["ca"]))
    assert isinstance(seen["system"], str)       # a plain prompt, no cache_control block


# ── recent performance: one row per post, latest snapshot, best first ─────────

def _posted(cid, topic):
    pid = db.create_post({"client_id": cid, "platform": "instagram", "topic": topic, "caption": "c"})
    db.update_post_status(pid, "posted")
    return pid


def _snapshot(pid, recorded_at, **metrics):
    db.add_performance(pid, metrics)
    conn = db.get_db()
    conn.execute("UPDATE performance_metrics SET recorded_at = ? WHERE id = "
                 "(SELECT MAX(id) FROM performance_metrics WHERE post_id = ?)", (recorded_at, pid))
    conn.commit()
    conn.close()


def test_each_post_appears_once_with_its_latest_snapshot(data):
    pid = _posted(data["ca"], "steady")
    _snapshot(pid, "2026-01-01 10:00:00", likes=1, reach=10)
    _snapshot(pid, "2026-01-02 10:00:00", likes=7, reach=10)
    _snapshot(pid, "2026-01-02 10:00:00", likes=9, reach=10)     # same second: the later row wins
    rows = db.get_recent_performance(data["ca"])
    assert len(rows) == 1
    assert rows[0]["likes"] == 9


def test_deleted_unposted_old_and_other_clients_posts_are_left_out(data):
    keep = _posted(data["ca"], "keep")
    gone = _posted(data["ca"], "deleted")
    db.delete_post(gone)
    db.create_post({"client_id": data["ca"], "platform": "instagram", "topic": "draft", "caption": "c"})
    old = _posted(data["ca"], "old")
    conn = db.get_db()
    conn.execute("UPDATE content_posts SET posted_date = datetime('now', '-30 days') WHERE id = ?", (old,))  # raw-query-ok: backdating a test row
    conn.commit()
    conn.close()
    _posted(data["cb"], "someone else's")
    assert [r["topic"] for r in db.get_recent_performance(data["ca"])] == ["keep"]
    assert keep


def test_what_worked_is_ranked_by_engagement_rate_with_unmeasured_posts_last(data):
    unmeasured = _posted(data["ca"], "unmeasured")
    big = _posted(data["ca"], "big reach, 5%")
    _snapshot(big, "2026-01-01 10:00:00", likes=40, comments=10, reach=1000)
    best = _posted(data["ca"], "small reach, 12%")
    _snapshot(best, "2026-01-01 10:00:00", likes=10, comments=2, reach=100)
    rows = db.get_recent_performance(data["ca"])
    assert [r["topic"] for r in rows] == ["small reach, 12%", "big reach, 5%", "unmeasured"]
    assert rows[0]["engagement_rate"] == 12.0 and rows[2]["engagement_rate"] is None
    assert unmeasured


def test_the_plan_reads_performance_best_first_and_names_unmeasured_posts(client, data, claude):
    _posted(data["ca"], "unmeasured post")
    best = _posted(data["ca"], "best post")
    _snapshot(best, "2026-01-01 10:00:00", likes=10, reach=100)
    login_as(client, data["admin"])
    r = post_json(client, "/api/bulk-plan", plan_body(data["ca"]))
    assert r.get_json()["performance_used"] == 2
    prompt = _plan_call(claude)["user"]
    assert prompt.index("best post") < prompt.index("unmeasured post")
    assert "unmeasured post (no metrics yet)" in prompt
    assert "engagement rate=10.0%" in prompt
