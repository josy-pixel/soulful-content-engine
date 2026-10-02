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
