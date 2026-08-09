"""Stage 1 — i18n infrastructure.

Nothing user-visible changes yet, so these test the decision layer: that the
locale resolution order is exactly stored-preference → session → default, that
a bad value can never take a page down, and that English is untouched.
"""
import pytest
from werkzeug.security import generate_password_hash

import database as db
import app as flask_app
import i18n


CSRF = "test-csrf-token"


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["content_posts", "clients", "users"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    admin = db.create_user("i18n-admin@t.co", generate_password_hash("pw"), role="admin")
    return dict(admin=admin)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


# ── the locale table ─────────────────────────────────────────────────────────

def test_english_and_hebrew_are_declared():
    assert i18n.DEFAULT_LOCALE == "en"
    assert set(i18n.LOCALES) == {"en", "he"}
    assert i18n.text_direction("en") == "ltr"
    assert i18n.text_direction("he") == "rtl"


def test_unknown_values_degrade_to_the_default():
    """A bad stored value must never raise — it renders English instead."""
    for bad in ("ru", "", None, "EN", 7, "he-IL"):
        assert i18n.normalise(bad) == "en"
        assert i18n.text_direction(bad) == "ltr"
    assert i18n.supported("he") and not i18n.supported("ru")


# ── resolution order ─────────────────────────────────────────────────────────

def test_anonymous_request_defaults_to_english(client):
    with flask_app.app.test_request_context("/"):
        assert i18n.get_locale() == "en"


def test_session_language_is_honoured_before_login(client):
    with flask_app.app.test_request_context("/"):
        i18n.set_session_language("he")
        assert i18n.get_locale() == "he"


def test_session_rejects_an_unsupported_language(client):
    with flask_app.app.test_request_context("/"):
        applied = i18n.set_session_language("ru")
        assert applied == "en"
        assert i18n.get_locale() == "en"


def test_stored_preference_beats_the_session(client, data, monkeypatch):
    """Once logged in the saved preference wins, whatever the login-page toggle
    had been set to."""
    class FakeUser:
        is_authenticated = True
        language = "he"

    monkeypatch.setattr(i18n, "current_user", FakeUser())
    with flask_app.app.test_request_context("/"):
        i18n.set_session_language("en")
        assert i18n.get_locale() == "he"


def test_a_user_with_no_language_column_falls_through(client, monkeypatch):
    """The users.language column lands in Stage 2. Until then — and for any row
    that predates it — resolution must fall through to the session cleanly."""
    class LegacyUser:
        is_authenticated = True          # no .language attribute at all

    monkeypatch.setattr(i18n, "current_user", LegacyUser())
    with flask_app.app.test_request_context("/"):
        i18n.set_session_language("he")
        assert i18n.get_locale() == "he"


def test_a_corrupt_stored_preference_falls_through(client, monkeypatch):
    class BrokenUser:
        is_authenticated = True
        language = "klingon"

    monkeypatch.setattr(i18n, "current_user", BrokenUser())
    with flask_app.app.test_request_context("/"):
        assert i18n.get_locale() == "en"


# ── wiring ───────────────────────────────────────────────────────────────────

def test_babel_is_registered_with_our_selector():
    assert "babel" in flask_app.app.extensions
    assert flask_app.app.config["BABEL_DEFAULT_LOCALE"] == "en"


def test_templates_receive_locale_and_direction(client, data):
    """The context processor runs on a real render, not just in isolation."""
    login_as(client, data["admin"])
    with flask_app.app.test_request_context("/"):
        ctx = flask_app.inject_locale()
    assert ctx["locale"] == "en"
    assert ctx["text_direction"] == "ltr"
    assert "he" in ctx["available_locales"]


def test_stage_1_changes_nothing_on_screen(client, data):
    """English regression: pages still render and still read English."""
    login_as(client, data["admin"])
    for path, needle in (("/", "Dashboard"), ("/clients", "Clients"), ("/users", "Users")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert needle in r.get_data(as_text=True), path
