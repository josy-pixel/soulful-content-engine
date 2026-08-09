"""Stage 2 — the stored per-user interface language.

The point of interest is the allowed-value check: it must read i18n.LOCALES
rather than carry its own list, so adding a locale cannot leave the database
layer refusing a language the rest of the app renders.
"""
import pytest
from werkzeug.security import generate_password_hash

import database as db
import i18n


@pytest.fixture()
def user():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()
    return db.create_user("lang@t.co", generate_password_hash("pw"), role="admin")


# ── the column ───────────────────────────────────────────────────────────────

def test_new_users_default_to_english(user):
    assert db.get_user_language(user) == "en"


def test_the_column_exists_and_is_not_nullable(user):
    conn = db.get_db()
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    conn.close()
    assert "language" in cols
    assert cols["language"][3] == 1                  # notnull
    assert cols["language"][4] == "'en'"             # default


def test_migration_is_idempotent(user):
    """init_db runs on every boot via before_request — a second run must not
    reset a stored preference or fail."""
    db.set_user_language(user, "he")
    db.init_db()
    db.init_db()
    assert db.get_user_language(user) == "he"


# ── set / get ────────────────────────────────────────────────────────────────

def test_set_and_get_round_trip(user):
    assert db.set_user_language(user, "he") == "he"
    assert db.get_user_language(user) == "he"
    db.set_user_language(user, "en")
    assert db.get_user_language(user) == "en"


def test_unsupported_language_is_refused(user):
    """Refused at the door, not stored and coerced later — otherwise the DB
    holds a value no page can render."""
    for bad in ("ru", "klingon", "", None, "EN", "he-IL", 7):
        with pytest.raises(ValueError):
            db.set_user_language(user, bad)
    assert db.get_user_language(user) == "en"        # unchanged throughout


def test_the_allowed_set_comes_from_i18n_not_a_local_copy(user, monkeypatch):
    """Add a locale to i18n.LOCALES and the database layer must accept it with
    no edit of its own. Two lists would disagree on exactly this."""
    monkeypatch.setitem(i18n.LOCALES, "ru", ("Русский", "ltr"))
    assert db.set_user_language(user, "ru") == "ru"
    assert db.get_user_language(user) == "ru"


def test_a_language_removed_from_the_build_reads_as_default(user, monkeypatch):
    """The inverse: a stored value that is no longer renderable must not be
    handed to the renderer."""
    monkeypatch.setitem(i18n.LOCALES, "ru", ("Русский", "ltr"))
    db.set_user_language(user, "ru")
    monkeypatch.delitem(i18n.LOCALES, "ru")
    assert db.get_user_language(user) == "en"


def test_unknown_user_is_distinguishable_from_no_preference(user):
    assert db.get_user_language(999999) is None


# ── end to end: the stored value must actually reach the resolver ────────────

def test_a_real_logged_in_session_resolves_to_the_stored_language(user):
    """Not a stubbed user: the row goes through auth.User and Flask-Login, which
    is the path that would break if the User object stopped carrying the column."""
    import app as flask_app

    db.set_user_language(user, "he")
    flask_app.app.config["TESTING"] = True
    c = flask_app.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user)
        s["_fresh"] = True
        s["language"] = "en"            # session says English...

    with c:
        c.get("/")                       # ...the stored preference must win
        assert i18n.get_locale() == "he"
        ctx = flask_app.inject_locale()
        assert ctx["locale"] == "he"
        assert ctx["text_direction"] == "rtl"


# ── the views ────────────────────────────────────────────────────────────────

def test_no_filtered_view_selects_user_columns(user):
    """Stage 2 asks this explicitly: the v_* views must not carry stale user
    columns. None of them read the users table at all."""
    conn = db.get_db()
    views = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view'").fetchall()
    conn.close()
    assert views, "expected the filtered views to exist"
    for v in views:
        assert "users" not in v["sql"].lower(), v["name"]


def test_views_are_rebuilt_on_boot_so_select_star_cannot_go_stale(user):
    """The ADD COLUMN above lands after the views are first created, which is
    exactly the case DROP-then-CREATE on every boot exists to handle."""
    conn = db.get_db()
    conn.execute("DROP VIEW IF EXISTS v_clients_active")
    conn.commit()
    conn.close()
    db.init_db()
    conn = db.get_db()
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='v_clients_active'"
    ).fetchone()
    conn.close()
    assert row is not None
