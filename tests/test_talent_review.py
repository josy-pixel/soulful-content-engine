"""The visual review: large media, inline caption edit, and the talent sign-off.

The sign-off is a side flag, not a gate: it never dispatches and never moves the
status. It is only meaningful before the approval gate — approving sends the
caption to Make in the payload, so an edit or a sign-off after that changes the
app's record and nothing that is published. It covers the exact caption,
hashtags and media the talent saw, so any change to them voids it, on every
path. Who gave it is recorded, because staff may tick it on the talent's behalf.
"""
import json
import os
import re
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db
import s3_media
import webhooks


CSRF = "review-csrf"
# Nobody here logs in with a password (sessions are set directly), and hashing
# one costs over a second — so one hash serves every user in the module.
PW = generate_password_hash("pw")
SIGNED = "https://bucket.example/{key}?sig=abc"


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["post_media", "client_media", "approval_history", "audit_log",
              "content_posts", "clients", "users"]:
        conn.execute("DELETE FROM %s" % t)   # raw-query-ok: test teardown
    conn.commit()
    conn.close()
    admin = db.create_user("review-admin@t.co", PW, role="admin")
    a = db.create_client({"name": "Alpha"})
    b = db.create_client({"name": "Bravo"})
    talent_a = db.create_user("talent-a@t.co", PW,
                              role="client", client_id=a)
    talent_b = db.create_user("talent-b@t.co", PW,
                              role="client", client_id=b)
    return dict(admin=admin, a=a, b=b, talent_a=talent_a, talent_b=talent_b)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


@pytest.fixture()
def sent(monkeypatch):
    """Every dispatch that would leave the app. The sign-off must never add one."""
    calls = []

    def fake(post, *a, **kw):
        calls.append(post["id"])
        return True, "Dispatched to this client's webhook."

    monkeypatch.setattr(webhooks, "dispatch_post", fake)
    return calls


@pytest.fixture()
def signed(monkeypatch):
    monkeypatch.setattr(s3_media, "presign_view",
                        lambda key, expires=None: SIGNED.format(key=key))


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


def make_post(cid, status="needs_review", **over):
    d = {"client_id": cid, "platform": "instagram", "content_type": "photo",
         "topic": "Topic %s" % cid, "caption": "Original caption", "hashtags": "#one",
         "status": status}
    d.update(over)
    return db.create_post(d)


def review(c, post_id, body, token=CSRF):
    headers = {"X-CSRF-Token": token} if token else {}
    return c.patch("/api/content/%d/review" % post_id, json=body, headers=headers)


def sign_off(c, post_id):
    r = review(c, post_id, {"talent_approved": True})
    assert r.status_code == 200 and db.get_post(post_id)["talent_approved"] == 1
    return r


def audit(post_id):
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM audit_log WHERE entity_type='content' AND entity_id=? "
                        "ORDER BY id", (post_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def s3_media_row(cid, name="hero.jpg", media_type="image"):
    key = "clients/%d/%s" % (cid, name)
    mid = db.add_media(cid, name, name, media_type, 10, storage="s3", s3_key=key)
    return mid, key


def section(html, start, end):
    return html.split(start, 1)[1].split(end, 1)[0]


# ── Access: CSRF, scoping, the approval gate ────────────────────────────────

def test_a_review_without_the_csrf_token_is_refused(client, data):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    assert review(client, p, {"talent_approved": True}, token=None).status_code == 400
    assert db.get_post(p)["talent_approved"] == 0


def test_a_talent_cannot_review_another_clients_post(client, data):
    p = make_post(data["b"])
    login_as(client, data["talent_a"])
    r = review(client, p, {"caption": "hijacked", "talent_approved": True})
    assert r.status_code == 403
    post = db.get_post(p)
    assert post["caption"] == "Original caption" and post["talent_approved"] == 0
    assert audit(p) == []


@pytest.mark.parametrize("status", ["approved", "scheduled", "posted", "error"])
@pytest.mark.parametrize("who", ["talent_a", "admin"])
def test_past_the_gate_nothing_can_be_changed_here_by_anyone(client, data, sent, status, who):
    """Approved means already sent to Make with this caption. The edit form is
    unchanged; this route is stricter than it, never looser."""
    p = make_post(data["a"])
    db.update_post_status(p, status)
    login_as(client, data[who])
    for body in ({"caption": "too late"}, {"hashtags": "#late"}, {"talent_approved": True}):
        assert review(client, p, body).status_code == 409
    post = db.get_post(p)
    assert (post["caption"], post["hashtags"], post["talent_approved"]) == \
           ("Original caption", "#one", 0)
    assert sent == [] and audit(p) == []


@pytest.mark.parametrize("status", ["raw", "branded", "draft", "needs_review"])
def test_before_the_gate_the_talent_can_edit_and_sign_off(client, data, status):
    p = make_post(data["a"], status=status)
    login_as(client, data["talent_a"])
    assert review(client, p, {"caption": "Better caption"}).status_code == 200
    assert review(client, p, {"talent_approved": True}).status_code == 200
    post = db.get_post(p)
    assert post["caption"] == "Better caption" and post["talent_approved"] == 1


def test_signing_off_never_dispatches_or_moves_the_status(client, data, sent):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    sign_off(client, p)
    review(client, p, {"caption": "Edited"})
    review(client, p, {"talent_approved": False})
    assert sent == []
    assert db.get_post(p)["status"] == "needs_review"
    assert [h["to_status"] for h in db.get_approval_history(p)] == ["needs_review"]


# ── Validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    {},
    {"unrelated": 1},
    {"caption": ["a", "list"]},
    {"caption": 123},
    {"caption": None},
    {"caption": ""},
    {"caption": "   \n "},
    {"hashtags": 5},
    {"talent_approved": "false"},       # bool("false") is True — must not count
    {"talent_approved": 1},
    {"talent_approved": None},
])
def test_malformed_review_bodies_are_refused_and_change_nothing(client, data, body):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    r = review(client, p, body)
    assert r.status_code == 400
    assert r.get_json()["error"]
    post = db.get_post(p)
    assert (post["caption"], post["hashtags"], post["talent_approved"]) == \
           ("Original caption", "#one", 0)


def test_a_body_that_is_not_json_is_refused(client, data):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    r = client.patch("/api/content/%d/review" % p, data="caption=x",
                     headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 400


def test_caption_and_hashtags_are_trimmed_like_the_edit_form(client, data):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    review(client, p, {"caption": "  New caption \n", "hashtags": " #a #b "})
    post = db.get_post(p)
    assert (post["caption"], post["hashtags"]) == ("New caption", "#a #b")


# ── Who signed off, and the audit trail ─────────────────────────────────────

def test_the_talents_own_sign_off_reads_approved_by_talent(client, data):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    body = sign_off(client, p).get_json()
    assert body["talent_approved"] is True
    assert body["signoff_label"].startswith("Approved by talent")
    assert db.get_post(p)["talent_approved_by"] == data["talent_a"]
    html = client.get("/content/%d" % p).data.decode()
    assert "Approved by talent ·" in html and "(agency)" not in html


def test_staff_marking_it_for_the_talent_says_so(client, data):
    """An OK given on WhatsApp, ticked by the agency, must not read as the
    talent's own click."""
    p = make_post(data["a"])
    login_as(client, data["admin"])
    body = sign_off(client, p).get_json()
    assert body["signoff_label"].startswith("Marked approved by review-admin@t.co (agency)")
    assert db.get_post(p)["talent_approved_by"] == data["admin"]
    html = client.get("/content/%d" % p).data.decode()
    assert "Marked approved by review-admin@t.co (agency)" in html
    assert "Approved by talent ·" not in html


def test_sign_off_withdrawal_and_edits_are_audited_with_who_did_them(client, data):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    sign_off(client, p)
    sign_off(client, p)                                   # a double click: no second row
    review(client, p, {"talent_approved": False})
    login_as(client, data["admin"])
    review(client, p, {"caption": "Admin's caption", "hashtags": "#one"})
    review(client, p, {"caption": "Admin's caption"})     # autosave resend: no row
    rows = audit(p)
    assert [(r["action"], r["actor_user_id"], r["actor_role"]) for r in rows] == [
        ("talent_signoff", data["talent_a"], "client"),
        ("talent_signoff_withdrawn", data["talent_a"], "client"),
        ("caption_edit", data["admin"], "admin"),
    ]
    meta = json.loads(rows[2]["metadata"])
    assert meta["fields"] == ["caption"]                  # hashtags were resent unchanged
    assert meta["before"] == {"caption": "Original caption"}
    assert all(r["tenant_client_id"] == data["a"] for r in rows)
    assert db.get_post(p)["talent_approved_by"] is None   # withdrawn: nobody vouches


# ── A sign-off covers what the talent saw: every change voids it ────────────

def _assert_voided(p, reason):
    post = db.get_post(p)
    assert post["talent_approved"] == 0
    assert (post["talent_approved_at"], post["talent_approved_by"]) == (None, None)
    cleared = [r for r in audit(p) if r["action"] == "talent_signoff_cleared"]
    assert cleared and cleared[-1]["reason"] == reason


@pytest.mark.parametrize("body", [{"caption": "Changed"}, {"hashtags": "#changed"}])
def test_an_inline_edit_voids_the_sign_off(client, data, body):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    sign_off(client, p)
    login_as(client, data["admin"])
    r = review(client, p, body)
    _assert_voided(p, "review")
    assert r.get_json()["talent_approved"] is False       # the page un-ticks at once


def test_resending_the_same_text_keeps_the_sign_off(client, data):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    sign_off(client, p)
    review(client, p, {"caption": "Original caption", "hashtags": "#one"})
    assert db.get_post(p)["talent_approved"] == 1


def _edit_form(c, p, **over):
    post = db.get_post(p)
    form = {"csrf_token": CSRF, "topic": post["topic"], "caption": post["caption"],
            "hashtags": post["hashtags"] or "", "image_url": post["image_url"] or "",
            "content_type": post["content_type"]}
    form.update(over)
    return c.post("/content/%d/edit" % p, data=form)


@pytest.mark.parametrize("field,value", [
    ("caption", "Edited in the form"),
    ("hashtags", "#form"),
    ("image_url", "/uploads/1/other.jpg"),
])
def test_the_edit_form_voids_the_sign_off(client, data, field, value):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    sign_off(client, p)
    assert _edit_form(client, p, **{field: value}).status_code == 302
    _assert_voided(p, "edit form")


def test_an_edit_form_save_that_changes_none_of_it_keeps_the_sign_off(client, data):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    sign_off(client, p)
    _edit_form(client, p, topic="Only the topic changed")
    assert db.get_post(p)["talent_approved"] == 1


def test_attaching_media_voids_the_sign_off(client, data):
    p = make_post(data["a"])
    mid = db.add_media(data["a"], "new.jpg", "new.jpg", "image")
    login_as(client, data["talent_a"])
    sign_off(client, p)
    r = client.post("/api/content/%d/media" % p, json={"media_id": mid},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 200
    _assert_voided(p, "media attached")


def test_detaching_media_voids_the_sign_off(client, data):
    p = make_post(data["a"])
    mid = db.add_media(data["a"], "old.jpg", "old.jpg", "image")
    db.attach_media_to_post(p, mid)
    login_as(client, data["talent_a"])
    sign_off(client, p)
    r = client.delete("/api/content/%d/media/%d" % (p, mid), headers={"X-CSRF-Token": CSRF})
    _assert_voided(p, "media detached")
    assert r.get_json() == {"ok": True, "signoff_cleared": True}


def test_deleting_a_file_voids_the_sign_off_of_every_post_using_it(client, data):
    p1, p2 = make_post(data["a"]), make_post(data["a"])
    mid = db.add_media(data["a"], "shared.jpg", "shared.jpg", "image")
    db.attach_media_to_post(p1, mid)
    db.attach_media_to_post(p2, mid)
    login_as(client, data["talent_a"])
    sign_off(client, p1)
    sign_off(client, p2)
    assert client.delete("/api/media/%d" % mid,
                         headers={"X-CSRF-Token": CSRF}).status_code == 200
    _assert_voided(p1, "media deleted")
    _assert_voided(p2, "media deleted")


def test_a_machine_caption_change_voids_the_sign_off(client, data):
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    sign_off(client, p)
    anon = flask_app.app.test_client()
    r = anon.patch("/api/content/%d" % p, json={"caption": "From Make"},
                   headers={"X-Secret": os.environ.get("MAKE_WEBHOOK_SECRET", "")})
    assert r.status_code == 200
    _assert_voided(p, "api")
    edit = [r for r in audit(p) if r["action"] == "caption_edit"][-1]
    assert (edit["actor_user_id"], edit["actor_role"]) == (None, "machine")


def test_a_generated_caption_voids_the_sign_off(client, data, monkeypatch):
    monkeypatch.setattr(flask_app.ai, "generate_caption", lambda *a, **k: ("AI caption", None))
    monkeypatch.setattr(flask_app.ai, "generate_hashtags", lambda *a, **k: "#ai")
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    sign_off(client, p)
    anon = flask_app.app.test_client()
    r = anon.post("/api/content/%d/generate-caption" % p,
                  headers={"X-Secret": os.environ.get("MAKE_WEBHOOK_SECRET", "")})
    assert r.status_code == 200
    _assert_voided(p, "generated caption")


# ── The pages render — and render S3 media as signed links ──────────────────

@pytest.mark.parametrize("who", ["talent_a", "admin"])
def test_the_post_page_shows_attached_s3_media_as_signed_links(client, data, signed, who):
    p = make_post(data["a"])
    mid, key = s3_media_row(data["a"])
    db.attach_media_to_post(p, mid)
    db.update_post(p, dict(db.get_post(p), image_url=s3_media.ref(key)))
    login_as(client, data[who])
    r = client.get("/content/%d" % p)
    assert r.status_code == 200
    html = r.data.decode()
    hero = section(html, 'class="review-media-frame"', "</div>")
    assert 'src="%s"' % SIGNED.format(key=key) in hero
    strip = section(html, 'id="attachedMediaContainer"', "galleryPickerPane")
    assert SIGNED.format(key=key) in strip
    assert "s3://" not in html
    assert "/uploads/%d/hero.jpg" % data["a"] not in html


@pytest.mark.parametrize("who", ["talent_a", "admin"])
def test_the_dashboard_shows_s3_thumbnails_as_signed_links(client, data, signed, who):
    p = make_post(data["a"], topic="S3 thumb")
    mid, key = s3_media_row(data["a"])
    db.attach_media_to_post(p, mid)
    make_post(data["a"], topic="S3 video", image_url="s3://clients/%d/clip.mp4" % data["a"])
    login_as(client, data[who])
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    grid = section(html, 'id="approvalGrid"', "<!-- KPI row -->")
    assert '<img src="%s"' % SIGNED.format(key=key) in grid
    assert '<video src="%s"' % SIGNED.format(key="clients/%d/clip.mp4" % data["a"]) in grid
    assert "s3://" not in html
    assert "/uploads/%d/hero.jpg" % data["a"] not in html


def test_media_on_disk_still_renders_from_its_local_path(client, data):
    p = make_post(data["a"])
    mid = db.add_media(data["a"], "local.jpg", "local.jpg", "image")
    db.attach_media_to_post(p, mid)
    login_as(client, data["talent_a"])
    hero = section(client.get("/content/%d" % p).data.decode(),
                   'class="review-media-frame"', "</div>")
    assert 'src="/uploads/%d/local.jpg"' % data["a"] in hero
    grid = section(client.get("/").data.decode(), 'id="approvalGrid"', "<!-- KPI row -->")
    assert 'src="/uploads/%d/local.jpg"' % data["a"] in grid


@pytest.mark.parametrize("status,locked", [
    ("draft", False), ("needs_review", False),
    ("approved", True), ("scheduled", True), ("posted", True),
])
@pytest.mark.parametrize("who", ["talent_a", "admin"])
def test_the_review_box_is_open_only_before_the_gate(client, data, status, locked, who):
    p = make_post(data["a"])
    if status != "needs_review":
        db.update_post_status(p, status)
    login_as(client, data[who])
    r = client.get("/content/%d" % p)
    assert r.status_code == 200
    html = r.data.decode()
    caption = re.search(r'<textarea id="captionInput"[^>]*>', html, re.S).group(0)
    check = re.search(r'<input type="checkbox" id="talentApprovedCheck"[^>]*>', html, re.S).group(0)
    assert ("disabled" in caption) is locked
    assert ("disabled" in check) is locked
    assert ("bi-lock-fill" in html) is locked


def test_the_status_form_is_wired_to_save_a_pending_edit_first(client, data):
    """The browser behaviour is not testable here; this keeps the hook it hangs
    on from being renamed out from under it."""
    p = make_post(data["a"])
    login_as(client, data["talent_a"])
    html = client.get("/content/%d" % p).data.decode()
    assert '<form method="POST" action="/content/%d/status" id="statusForm">' % p in html
    assert "getElementById('statusForm')" in html


# ── The dashboard overview ──────────────────────────────────────────────────

def test_the_overview_lists_only_unsigned_posts_waiting_in_review(client, data):
    make_post(data["a"], topic="WAITING")
    make_post(data["a"], topic="A-DRAFT", status="draft")
    for status in ("approved", "scheduled", "posted"):
        p = make_post(data["a"], topic="PAST-" + status)
        db.update_post_status(p, status)
    signed_off = make_post(data["a"], topic="SIGNED-OFF")
    db.set_talent_approval(signed_off, True, data["talent_a"])
    login_as(client, data["talent_a"])
    grid = section(client.get("/").data.decode(), 'id="approvalGrid"', "<!-- KPI row -->")
    assert "WAITING" in grid
    for absent in ("A-DRAFT", "PAST-approved", "PAST-scheduled", "PAST-posted", "SIGNED-OFF"):
        assert absent not in grid


def test_the_overview_is_scoped_to_the_talents_own_client(client, data):
    make_post(data["a"], topic="ALPHA-POST")
    make_post(data["b"], topic="BRAVO-POST")
    login_as(client, data["talent_a"])
    html = client.get("/").data.decode()
    assert "ALPHA-POST" in html and "BRAVO-POST" not in html
    login_as(client, data["admin"])
    grid = section(client.get("/").data.decode(), 'id="approvalGrid"', "<!-- KPI row -->")
    assert "ALPHA-POST" in grid and "BRAVO-POST" in grid


def test_the_overview_hides_deleted_posts_and_deleted_clients(client, data):
    make_post(data["a"], topic="ALPHA-POST")
    gone = make_post(data["a"], topic="DELETED-POST")
    conn = db.get_db()
    conn.execute("UPDATE content_posts SET deleted_at='2026-01-01' WHERE id=?", (gone,))  # raw-query-ok: test setup, must reach the base table
    conn.commit()
    conn.close()
    make_post(data["b"], topic="BRAVO-POST")
    db.soft_delete_client(data["b"])
    login_as(client, data["admin"])
    html = client.get("/").data.decode()
    assert "ALPHA-POST" in html
    assert "DELETED-POST" not in html and "BRAVO-POST" not in html


def test_the_overview_is_capped_and_counts_the_rest(client, data):
    for i in range(25):
        make_post(data["a"], topic="P%02d" % i)
    login_as(client, data["talent_a"])
    html = client.get("/").data.decode()
    grid = section(html, 'id="approvalGrid"', "<!-- KPI row -->")
    assert grid.count('class="col-6 col-md-4 col-xl-3 approval-item"') == 20
    assert 'data-total="25">25 waiting' in html
    assert 'href="/content?status=needs_review"' in html


def test_no_overview_when_nothing_is_waiting(client, data):
    login_as(client, data["talent_a"])
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="approvalOverviewSection"' not in r.data.decode()


# ── The live database gets the columns ──────────────────────────────────────

MASTER_CONTENT_POSTS = """
CREATE TABLE content_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL,
    platform TEXT NOT NULL, topic TEXT NOT NULL, caption TEXT NOT NULL, hashtags TEXT,
    status TEXT DEFAULT 'draft', scheduled_date TIMESTAMP, posted_date TIMESTAMP, notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    image_url TEXT DEFAULT '', content_type TEXT DEFAULT 'photo', posted_url TEXT DEFAULT '',
    hook TEXT DEFAULT '', error_message TEXT DEFAULT '', voice_score INTEGER,
    voice_audit TEXT DEFAULT '', deleted_at TEXT, deleted_by INTEGER, deleted_reason TEXT,
    status_before_delete TEXT, purge_after TEXT
)"""


def test_an_existing_database_gains_the_sign_off_columns(tmp_path, monkeypatch):
    """The Render disk holds a database created before these columns existed.
    init_db must add them in place, keep the rows, and stay safe to rerun."""
    path = str(tmp_path / "live.db")
    raw = sqlite3.connect(path)
    raw.execute(MASTER_CONTENT_POSTS)
    raw.execute("INSERT INTO content_posts (client_id, platform, topic, caption, status) "
                "VALUES (1, 'instagram', 'live post', 'live caption', 'needs_review')")
    raw.commit()
    raw.close()
    monkeypatch.setattr(db, "DB_PATH", path)

    db.init_db()
    db.init_db()                                           # every boot runs it again

    conn = db.get_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(content_posts)")}
    row = conn.execute("SELECT caption, talent_approved, talent_approved_at, talent_approved_by "
                       "FROM v_content_active WHERE topic='live post'").fetchone()
    conn.close()
    assert {"talent_approved", "talent_approved_at", "talent_approved_by"} <= cols
    assert tuple(row) == ("live caption", 0, None, None)
