"""Authentication for the Soulful Content Engine.

Session-based login (Flask-Login) with hashed passwords. Human routes are
protected by a global before_request guard; machine routes (Make.com webhooks)
keep their own X-Secret gate and are whitelisted here so they never require a
browser login. Structured so founder/assistant/editor/client roles can be added
later without a rewrite.
"""
import os
import hmac
import time
import secrets
from functools import wraps

from flask import (redirect, url_for, request, render_template,
                   flash, session, abort)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash

import database as db

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.login_message = 'Please sign in to continue.'

# Routes reachable without a browser login.
_PUBLIC_ENDPOINTS = {'login', 'static'}
# Machine-to-machine routes: authenticated by their own X-Secret gate
# (see webhooks.verify_secret / _check_secret), not by a session login —
# Make.com cannot sign in. Keep this list in sync with the X-Secret routes.
_MACHINE_ENDPOINTS = {
    'webhook_publish',
    'api_performance_inbound',
    'api_content_get',
    'api_content_patch',
    'api_generate_caption_for_post',
    'api_generate_hook',
    'api_content_create',
    'api_trends_generate',
}

# ── Brute-force lockout (in-memory; the app runs a single gunicorn worker) ──
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 15 * 60
_attempts = {}   # email -> {'count': int, 'until': float}


def _is_locked(email):
    rec = _attempts.get(email)
    if not rec:
        return False
    if rec['until'] and rec['until'] > time.time():
        return True
    if rec['until'] and rec['until'] <= time.time():
        _attempts.pop(email, None)   # lockout expired — reset
    return False


def _record_failure(email):
    rec = _attempts.setdefault(email, {'count': 0, 'until': 0})
    rec['count'] += 1
    if rec['count'] >= _MAX_ATTEMPTS:
        rec['until'] = time.time() + _LOCKOUT_SECONDS


def _clear_attempts(email):
    _attempts.pop(email, None)


def _lock_minutes_left(email):
    rec = _attempts.get(email)
    if rec and rec['until'] > time.time():
        return int((rec['until'] - time.time()) // 60) + 1
    return 0


# ── CSRF for the login form (lightweight, no extra dependency) ──
def _csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def _csrf_ok(submitted):
    expected = session.get('_csrf_token', '')
    return bool(expected) and bool(submitted) and hmac.compare_digest(expected, submitted)


# ── User model ──
class User(UserMixin):
    def __init__(self, row):
        self.id = row['id']
        self.email = row['email']
        self.role = row['role']

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id):
    try:
        row = db.get_user_by_id(int(user_id))
    except (TypeError, ValueError):
        return None
    return User(row) if row else None


def roles_required(*roles):
    """Scaffolding for future roles. Only 'admin' exists today."""
    def wrapper(fn):
        @wraps(fn)
        @login_required
        def inner(*args, **kwargs):
            if getattr(current_user, 'role', None) not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return inner
    return wrapper


def bootstrap_admin():
    """Seed the first admin from env vars. Values are read only from
    os.environ — never stored in code, the repo, or example files."""
    email = os.environ.get('ADMIN_EMAIL', '').strip().lower()
    password = os.environ.get('ADMIN_PASSWORD', '')
    if not email or not password:
        return
    if db.get_user_by_email(email) or db.any_users():
        return
    db.create_user(email, generate_password_hash(password), role='admin')


def init_auth(app):
    login_manager.init_app(app)

    @app.before_request
    def _require_login():
        ep = request.endpoint
        if ep is None:
            return
        if ep in _PUBLIC_ENDPOINTS or ep in _MACHINE_ENDPOINTS:
            return
        if not current_user.is_authenticated:
            return redirect(url_for('login', next=request.path))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')

            if not _csrf_ok(request.form.get('csrf_token', '')):
                flash('Your session expired. Please try again.', 'danger')
                return redirect(url_for('login'))

            if _is_locked(email):
                flash(f'Too many failed attempts. Try again in '
                      f'{_lock_minutes_left(email)} minute(s).', 'danger')
                return render_template('login.html', csrf_token=_csrf_token())

            row = db.get_user_by_email(email)
            if row and check_password_hash(row['password_hash'], password):
                _clear_attempts(email)
                session.pop('_csrf_token', None)
                login_user(User(row))
                nxt = request.args.get('next', '')
                if nxt.startswith('/') and not nxt.startswith('//'):
                    return redirect(nxt)
                return redirect(url_for('dashboard'))

            _record_failure(email)
            flash('Incorrect email or password.', 'danger')

        return render_template('login.html', csrf_token=_csrf_token())

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        flash('You have been signed out.', 'success')
        return redirect(url_for('login'))
