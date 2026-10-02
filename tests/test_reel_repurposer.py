"""Reel Repurposer: who may use it, what it reads, what it tells Claude, what it saves.

The Anthropic client is stubbed throughout — no network, no API key, no spend.
"""
from types import SimpleNamespace

import pytest
from flask_login import login_required
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db
import voice_engine as ve


CSRF = "test-csrf-token"
H = {"X-CSRF-Token": CSRF}
ENDPOINTS = ("reel_repurposer", "api_repurpose_candidates",
             "api_reel_repurpose_generate", "api_reel_repurpose_save")


# ── a stand-in for Claude ─────────────────────────────────────────────────────

class FakeClaude:
    """Records every call; answers with `text` and `stop_reason`, or raises `error`."""

    def __init__(self):
        self.calls = []
        self.options = []
        self.text = "## 1. DIAGNOSIS\nThe hook is buried."
        self.stop_reason = "end_turn"
        self.error = None
        self.messages = self

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=10, output_tokens=20,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0))

    @property
    def system(self):
        return self.calls[-1]["system"][0]["text"]

    @property
    def user(self):
        return self.calls[-1]["messages"][0]["content"]


@pytest.fixture()
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(ve, "_client", lambda: fake)
    return fake


# ── tenants ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["approval_history", "performance_metrics", "post_media", "content_posts",
              "client_media", "brand_voices", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user("rr-admin@t.co", generate_password_hash("pw"), role="admin")
    manager = db.create_user("rr-manager@t.co", generate_password_hash("pw"), role="manager")
    ca = db.create_client({"name": "Client A"})
    cb = db.create_client({"name": "Client B"})
    member = db.create_user("rr-holly@t.co", generate_password_hash("pw"),
                            role="client", client_id=ca)

    def post(client_id, content_type="reel", status="posted", platform="facebook", topic="t"):
        pid = db.create_post({"client_id": client_id, "platform": platform,
                              "content_type": content_type, "topic": topic, "caption": "c"})
        if status != "draft":
            db.update_post_status(pid, status)
        return pid

    return dict(admin=admin, manager=manager, member=member, ca=ca, cb=cb, post=post,
                reel_a=post(ca, topic="A reel"), reel_b=post(cb, topic="B reel"),
                photo_a=post(ca, "photo", "draft", topic="A photo"))


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
def opened_to_clients(monkeypatch):
    """The routes as they will be once 'client' joins REEL_REPURPOSER_ROLES: the role
    gate gone, everything beneath it (sign-in, the tenant checks) still in place.
    This is what proves the one-line change will not open another client's posts."""
    for ep in ENDPOINTS:
        view = flask_app.app.view_functions[ep]
        monkeypatch.setitem(flask_app.app.view_functions, ep, login_required(view.__wrapped__))
    monkeypatch.setitem(flask_app.app.jinja_env.globals, "REEL_REPURPOSER_ROLES",
                        ("admin", "manager", "client"))


def _generate(client, post_id, material="0:00 talking head"):
    return client.post("/api/reel-repurpose/generate", headers=H,
                       json={"post_id": post_id, "source_material": material})


def _save(client, post_id, package="## 1. DIAGNOSIS\nfine"):
    return client.post("/api/reel-repurpose/save", headers=H,
                       json={"post_id": post_id, "package": package})


# ── who may use it ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("who", ["admin", "manager"])
def test_admin_and_manager_get_the_page(client, data, who):
    login_as(client, data[who])
    r = client.get("/reel-repurposer?client_id=%d&post_id=%d" % (data["ca"], data["reel_a"]))
    assert r.status_code == 200
    assert b"A reel" in r.data
    assert b'href="/reel-repurposer"' in client.get("/").data      # nav link


def test_client_user_is_refused_by_every_route(client, data, claude):
    login_as(client, data["member"])
    assert client.get("/reel-repurposer").status_code == 403
    assert client.get("/api/reel-repurpose/candidates/%d" % data["ca"]).status_code == 403
    assert _generate(client, data["reel_a"]).status_code == 403
    assert _save(client, data["reel_a"]).status_code == 403
    assert claude.calls == []
    assert db.get_post(data["reel_a"])["repurpose_brief"] == ""
    assert b'href="/reel-repurposer"' not in client.get("/").data  # no nav link


def test_post_page_shows_a_saved_package_to_the_client_but_no_way_into_the_tool(client, data):
    db.set_repurpose_brief(data["reel_a"], "Re-hook the first 5 seconds.")
    login_as(client, data["member"])
    html = client.get("/content/%d" % data["reel_a"]).data.decode()
    assert "Re-hook the first 5 seconds." in html
    assert "/reel-repurposer?" not in html

    login_as(client, data["admin"])
    html = client.get("/content/%d" % data["reel_a"]).data.decode()
    assert "Re-hook the first 5 seconds." in html
    assert "/reel-repurposer?" in html and "Regenerate" in html


def test_post_page_without_a_package(client, data):
    login_as(client, data["member"])
    r = client.get("/content/%d" % data["reel_a"])
    assert r.status_code == 200 and b"Reel Repurposer" not in r.data
    login_as(client, data["admin"])
    r = client.get("/content/%d" % data["reel_a"])
    assert r.status_code == 200 and b"Repurpose this reel" in r.data


def test_save_without_the_csrf_token_is_refused(client, data):
    login_as(client, data["admin"])
    r = client.post("/api/reel-repurpose/save",
                    json={"post_id": data["reel_a"], "package": "x"})
    assert r.status_code == 400
    assert db.get_post(data["reel_a"])["repurpose_brief"] == ""


# ── another client's posts stay out of reach once client users are let in ──

def test_opened_to_clients_another_clients_post_stays_out_of_reach(
        client, data, claude, opened_to_clients):
    login_as(client, data["member"])
    r = client.get("/reel-repurposer?post_id=%d" % data["reel_b"])
    assert r.status_code == 200 and b"B reel" not in r.data
    assert client.get("/api/reel-repurpose/candidates/%d" % data["cb"]).status_code == 403
    assert _generate(client, data["reel_b"]).status_code == 403
    assert _save(client, data["reel_b"], "pwned").status_code == 403
    assert claude.calls == []
    assert db.get_post(data["reel_b"])["repurpose_brief"] == ""


def test_opened_to_clients_their_own_post_works(client, data, claude, opened_to_clients):
    login_as(client, data["member"])
    assert client.get("/reel-repurposer").status_code == 200
    assert client.get("/api/reel-repurpose/candidates/%d" % data["ca"]).status_code == 200
    assert _generate(client, data["reel_a"]).status_code == 200
    assert _save(client, data["reel_a"]).status_code == 200


# ── the page writes stored values as text ───────────────────────────────────

EVIL = '<img src=x onerror=alert(document.domain)>'


def test_page_writes_stored_values_as_text_not_markup(client, data):
    """A platform or topic holding markup — stored before the create routes
    validated it, or by any path that still does not — must reach the page as
    text. The admin's browser is the one that opens this page."""
    evil = data["post"](data["ca"], platform=EVIL, topic=EVIL)   # repository layer: no validation
    login_as(client, data["admin"])
    r = client.get("/reel-repurposer?client_id=%d&post_id=%d" % (data["ca"], evil))
    assert r.status_code == 200
    html = r.data.decode()
    assert EVIL not in html                       # the preselect is a JSON literal, escaped
    assert "u003cimg" in html                    # ...as a JSON unicode escape
    script = html.split("const platformColors")[-1]   # this page's own script
    for line in script.splitlines():
        if "innerHTML" in line:                   # only fixed strings may become markup
            assert "${" not in line and "+" not in line, line
    assert "textContent" in script


def test_page_reads_each_response_once_and_handles_failures(client, data):
    login_as(client, data["admin"])
    html = client.get("/reel-repurposer").data.decode()
    script = html.split("const platformColors")[-1]
    assert "res.json()" not in script             # one read, then parse: never twice
    assert script.count("await readJson(") == 3  # candidates, generate, save
    assert "catch (e)" in html.split("saveBtn").pop()


# ── the picker's query ───────────────────────────────────────────────────────

def _snapshot(post_id, reach, recorded_at, views=1):
    conn = db.get_db()
    conn.execute("INSERT INTO performance_metrics (post_id, reach, views, recorded_at) "
                 "VALUES (?, ?, ?, ?)", (post_id, reach, views, recorded_at))
    conn.commit()
    conn.close()


def test_candidates_latest_snapshot_one_row_each_weakest_first(data):
    post, ca = data["post"], data["ca"]
    tie = post(ca, topic="tie")
    two = post(ca, "video", topic="two snapshots")
    weak = post(ca, topic="weak")
    _snapshot(two, 50, "2026-08-01 10:00:00")
    _snapshot(two, 5000, "2026-09-02 10:00:00")        # the later one counts
    _snapshot(tie, 100, "2026-09-01 10:00:00")
    _snapshot(tie, 200, "2026-09-01 10:00:00")         # same second: the later row wins
    _snapshot(weak, 10, "2026-09-03 10:00:00")
    rows = db.get_repurpose_candidates(ca)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "a post listed twice"
    by_id = {r["id"]: r for r in rows}
    assert by_id[two]["reach"] == 5000
    assert by_id[tie]["reach"] == 200
    assert ids[:3] == [weak, tie, two]                 # weakest reach first
    assert ids[-1] == data["reel_a"]                   # no snapshot: listed, last
    assert by_id[data["reel_a"]]["reach"] is None      # ...as no data, not as zero


def test_candidates_only_posted_video_and_reel_of_live_posts_and_clients(data):
    post, ca = data["post"], data["ca"]
    story = post(ca, "story", topic="story")
    draft_reel = post(ca, "reel", "draft", topic="draft reel")
    approved = post(ca, "video", "approved", topic="approved video")
    gone = post(ca, topic="soft-deleted")
    conn = db.get_db()
    conn.execute("UPDATE content_posts SET deleted_at='2026-09-30' WHERE id=?", (gone,))  # raw-query-ok: test setup, must reach the base table
    conn.commit()
    conn.close()
    ids = [r["id"] for r in db.get_repurpose_candidates(ca)]
    assert ids == [data["reel_a"]]
    assert not {story, draft_reel, approved, gone, data["photo_a"]} & set(ids)
    assert "repurpose_brief" not in db.get_repurpose_candidates(ca)[0]   # picker gets no bodies

    db.soft_delete_client(data["cb"])
    assert db.get_repurpose_candidates(data["cb"]) == []


def test_candidates_route_returns_the_same_rows(client, data):
    login_as(client, data["admin"])
    r = client.get("/api/reel-repurpose/candidates/%d" % data["ca"])
    assert r.status_code == 200
    assert [c["id"] for c in r.get_json()] == [data["reel_a"]]


def test_saving_a_package_does_not_reorder_the_content_library(client, data):
    conn = db.get_db()
    conn.execute("UPDATE content_posts SET updated_at='2026-01-01 00:00:00' WHERE id=?",  # raw-query-ok: test setup
                 (data["reel_a"],))
    conn.commit()
    conn.close()
    before = [p["id"] for p in db.get_posts(client_id=data["ca"])]
    login_as(client, data["admin"])
    assert _save(client, data["reel_a"]).status_code == 200
    post = db.get_post(data["reel_a"])
    assert post["updated_at"] == "2026-01-01 00:00:00"
    assert post["repurpose_brief_generated_at"]
    assert [p["id"] for p in db.get_posts(client_id=data["ca"])] == before


def test_existing_database_gains_the_package_columns_on_boot(monkeypatch, tmp_path):
    """The live database predates these columns; CREATE TABLE never runs on it.
    Build one the way it is today — every column but these two — and boot."""
    import sqlite3
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("needs ALTER TABLE DROP COLUMN to build the old schema")
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "live_like.db"))
    db.init_db()
    conn = db.get_db()
    conn.executescript("""
        DROP VIEW v_content_active;
        ALTER TABLE content_posts DROP COLUMN repurpose_brief_generated_at;
        ALTER TABLE content_posts DROP COLUMN repurpose_brief;
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(content_posts)")]
    conn.close()
    assert "repurpose_brief" not in cols

    db.init_db()                                       # the deploy
    conn = db.get_db()
    view_cols = [r[1] for r in conn.execute("PRAGMA table_info(v_content_active)")]
    conn.close()
    assert {"repurpose_brief", "repurpose_brief_generated_at"} <= set(view_cols)
    cid = db.create_client({"name": "Old DB client"})
    pid = db.create_post({"client_id": cid, "platform": "facebook", "content_type": "reel",
                          "topic": "t", "caption": "c"})
    db.set_repurpose_brief(pid, "package")
    assert db.get_post(pid)["repurpose_brief"] == "package"


# ── only a posted video can be repurposed ────────────────────────────────────

@pytest.mark.parametrize("content_type,status", [
    ("photo", "posted"), ("story", "posted"), ("carousel", "posted"),
    ("reel", "draft"), ("video", "approved"), ("reel", "error"),
])
def test_generate_and_save_refuse_anything_but_a_posted_video(
        client, data, claude, content_type, status):
    pid = data["post"](data["ca"], content_type, status)
    login_as(client, data["admin"])
    r = _generate(client, pid)
    assert r.status_code == 409 and "Only a posted reel or video" in r.get_json()["error"]
    r = _save(client, pid)
    assert r.status_code == 409
    assert claude.calls == []                          # no tokens spent on it
    assert db.get_post(pid)["repurpose_brief"] == ""


@pytest.mark.parametrize("raw", ["abc", "12abc", 1.5, True, [1], {"id": 1}, "", None])
def test_a_malformed_post_id_is_a_400_not_a_500(client, data, claude, raw):
    login_as(client, data["admin"])
    assert _generate(client, raw).status_code == 400
    assert _save(client, raw).status_code == 400
    assert claude.calls == []


def test_unknown_post_is_404(client, data):
    login_as(client, data["admin"])
    assert _generate(client, 999999).status_code == 404
    assert _save(client, "999999").status_code == 404


def test_page_does_not_preselect_a_post_that_cannot_be_repurposed(client, data):
    login_as(client, data["admin"])
    html = client.get("/reel-repurposer?post_id=%d" % data["photo_a"]).data.decode()
    assert "A photo" not in html


def test_source_material_and_package_are_required(client, data, claude):
    login_as(client, data["admin"])
    assert _generate(client, data["reel_a"], material="   ").status_code == 400
    assert _save(client, data["reel_a"], package="  ").status_code == 400
    assert claude.calls == []


# ── what Claude is told ──────────────────────────────────────────────────────

import json  # noqa: E402

DOC = "Never say 'journey'. Reels stay under 45 seconds. Short, steady sentences."
VOICE = {"tone": "TONE_MARK", "style": "STYLE_MARK", "target_audience": "AUDIENCE_MARK",
         "keywords": json.dumps(["KEYWORD_MARK"]),
         "avoid_words": json.dumps(["BANNED_ONE", "BANNED_TWO"])}
FACEBOOK_ONLY = ("Content Monetization", "qualified views", "70-90s", "60-99s",
                 "Sound Collection", "$0.00", "sub-60s", "trending", "earn")


def _prompt(claude, voice=None, doc="", samples=(), platform="instagram"):
    ve.build_reel_repurpose("Holly", voice or {}, doc, list(samples), "0:00 talking head",
                            "Metrics: none", platform=platform)
    return claude.system


@pytest.mark.parametrize("doc,samples", [(DOC, []), ("", ["a real caption"]), ("", [])])
def test_every_voice_setting_reaches_the_prompt(claude, doc, samples):
    """Banned words used to be dropped whenever a document or captions existed,
    and tone, style and audience with them."""
    s = _prompt(claude, VOICE, doc, samples)
    for mark in ("BANNED_ONE", "BANNED_TWO", "KEYWORD_MARK", "TONE_MARK", "STYLE_MARK",
                 "AUDIENCE_MARK"):
        assert mark in s, mark
    if doc:
        assert DOC in s                                # whole, never summarised
    if samples:
        assert "a real caption" in s


def test_an_unset_voice_injects_nothing(claude):
    s = _prompt(claude)
    assert "=== TALENT VOICE" not in s
    assert "warm, direct, honest" not in s
    assert "Tone:" not in s and "NEVER USE" not in s


def test_the_talents_voice_outranks_every_piece_of_guidance(claude):
    s = _prompt(claude, VOICE, DOC, platform="facebook")
    assert "=== PRECEDENCE ===" in s
    assert s.index("=== PRECEDENCE ===") < s.index("=== STEP 1")
    precedence = s.split("=== PRECEDENCE ===")[1].split("===")[0]
    assert "top authority" in precedence
    assert precedence.index("TALENT VOICE") < precedence.index("MEASURED DATA") \
        < precedence.index("GUIDANCE below")
    assert "not platform rules" in precedence
    assert "not an official Meta, Facebook or Instagram policy" in precedence
    assert "Never present any of it" in precedence     # ...to the talent as an official rule
    assert "the post's own data wins" not in s         # data no longer outranks the voice


def test_monetisation_guidance_is_facebook_only(claude):
    fb = _prompt(claude, platform="facebook")
    for text in FACEBOOK_ONLY:
        assert text in fb, text
    for platform in ("instagram", "tiktok", "youtube", "", None):
        other = _prompt(claude, platform=platform)
        for text in FACEBOOK_ONLY:
            assert text not in other, (platform, text)
        assert "STEP 4" in other and "STEP 5" in other   # the craft guidance stays


def test_numbers_are_framed_as_guidance_and_unmeasured_data_as_unknown(claude):
    """The fixed numbers and checks are unverified: they are offered as guidance,
    attributed to no one, and never as Meta's rules."""
    s = _prompt(claude, VOICE, DOC, platform="facebook")
    assert "does NOT record video length" in s
    assert "hypothesis to check" in s
    assert "four compliance hard-stops" not in s and "hard stops" not in s
    assert "known Facebook-specific failure modes" not in s
    assert "agency" not in s.lower()                   # no one's name on unverified numbers
    checks = s.split("=== STEP 5")[1].split("=== STEP 6")[0]
    assert "guidance, not platform rules" in checks
    assert "These figures are guidance, unverified for this account" in s


def test_route_says_no_data_rather_than_zeros_and_passes_the_platform(client, data, claude):
    insta = data["post"](data["ca"], "reel", platform="instagram", topic="insta reel")
    login_as(client, data["admin"])
    assert _generate(client, insta).status_code == 200
    assert "no performance data recorded" in claude.user
    assert "Reach: 0" not in claude.user and "Likes: 0" not in claude.user
    assert "Content Monetization" not in claude.system

    _snapshot(data["reel_a"], 452000, "2026-09-02 10:00:00", views=0)
    assert _generate(client, data["reel_a"]).status_code == 200    # facebook
    assert "Reach: 452000" in claude.user and "recorded 2026-09-02 10:00:00" in claude.user
    assert "Content Monetization" in claude.system


def test_route_injects_the_clients_banned_words(client, data, claude):
    db.upsert_brand_voice(data["ca"], "facebook", {"avoid_words": json.dumps(["BANNED_ONE"])})
    db.update_client_voice(data["ca"], DOC, ["a real caption"])
    login_as(client, data["admin"])
    assert _generate(client, data["reel_a"]).status_code == 200
    assert "BANNED_ONE" in claude.system and DOC in claude.system


# ── a package that did not finish is never offered as finished ──────────────

import anthropic  # noqa: E402
import httpx  # noqa: E402


def test_one_call_bounded_under_the_worker_timeout_with_no_retries(client, data, claude):
    login_as(client, data["admin"])
    assert _generate(client, data["reel_a"]).status_code == 200
    assert len(claude.calls) == 1
    opts = claude.options[-1]
    assert opts["max_retries"] == 0
    assert 0 < opts["timeout"] < 180                   # gunicorn --timeout 180
    assert claude.calls[0]["max_tokens"] == ve.REEL_REPURPOSE_MAX_TOKENS


def test_a_truncated_package_is_refused_not_returned(client, data, claude):
    claude.stop_reason = "max_tokens"
    res = ve.build_reel_repurpose("Holly", {}, "", [], "0:00 x", "none", platform="facebook")
    assert res.get("incomplete") and "package" not in res
    login_as(client, data["admin"])
    r = _generate(client, data["reel_a"])
    assert r.status_code == 502
    body = r.get_json()
    assert "length limit" in body["error"] and "package" not in body


def test_a_timeout_is_a_clean_json_504(client, data, claude):
    claude.error = anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    login_as(client, data["admin"])
    r = _generate(client, data["reel_a"])
    assert r.status_code == 504
    assert "didn't finish within 150 seconds" in r.get_json()["error"]


def test_any_other_api_error_is_a_json_500(client, data, claude):
    claude.error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    login_as(client, data["admin"])
    r = _generate(client, data["reel_a"])
    assert r.status_code == 500 and "Claude API error" in r.get_json()["error"]


def test_an_empty_answer_is_an_error(client, data, claude):
    claude.text = "   "
    login_as(client, data["admin"])
    assert _generate(client, data["reel_a"]).status_code == 500
