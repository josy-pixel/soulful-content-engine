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
import hashlib
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
# 'serve_media' is public so social platforms (Facebook/Instagram) can fetch
# post images by URL when publishing — filenames are unguessable hashes.
_PUBLIC_ENDPOINTS = {'login', 'setup_admin', 'healthz', 'static', 'serve_media',
                     'accept_invite'}
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


# ── First-run setup token ──
# Only the SHA-256 hash of the token is committed (irreversible, safe to be
# public). The plaintext lives outside the repo and was shared out of band.
def _expected_setup_hash():
    path = os.path.join(os.path.dirname(__file__), 'setup_token.hash')
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _setup_token_ok(submitted):
    expected = _expected_setup_hash()
    if not expected:
        return False   # no hash file -> setup is closed, fail safe
    if not submitted:
        return False
    digest = hashlib.sha256(submitted.strip().encode('utf-8')).hexdigest()
    return hmac.compare_digest(expected, digest)


# ── User model ──
class User(UserMixin):
    def __init__(self, row):
        keys = row.keys()
        self.id = row['id']
        self.email = row['email']
        self.role = row['role']
        # NULL for admin/manager; the bound client id for a 'client' user.
        self.client_id = row['client_id'] if 'client_id' in keys else None
        raw_active = row['is_active'] if 'is_active' in keys else 1
        self._active = raw_active in (1, '1', True)

    @property
    def is_active(self):
        # Deactivated users cannot log in (login_user honours this) and are
        # bounced by the before_request guard if a live session goes inactive.
        return self._active

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
    os.environ — never stored in code, the repo, or example files.
    If ADMIN_RESET=1, also (re)set the given admin's password even when the
    account already exists — a controlled, env-gated password recovery."""
    email = os.environ.get('ADMIN_EMAIL', '').strip().lower()
    password = os.environ.get('ADMIN_PASSWORD', '')
    if not email or not password:
        return
    reset = os.environ.get('ADMIN_RESET') == '1'
    existing = db.get_user_by_email(email)
    if existing:
        if reset:
            db.set_password(email, generate_password_hash(password))
        return
    if reset or not db.any_users():
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
        # A session that went inactive mid-life (deactivated by an admin) is bounced.
        if not getattr(current_user, 'is_active', True):
            logout_user()
            flash('Your account has been deactivated.', 'danger')
            return redirect(url_for('login'))

    @app.route('/setup', methods=['GET', 'POST'])
    def setup_admin():
        # First-run only: available until the first admin account exists,
        # then closes permanently. No secret in the repo, no dashboard needed.
        if db.any_users():
            return redirect(url_for('login'))

        if request.method == 'POST':
            if not _csrf_ok(request.form.get('csrf_token', '')):
                flash('Your session expired. Please try again.', 'danger')
                return redirect(url_for('setup_admin'))
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            confirm = request.form.get('confirm', '')
            if not _setup_token_ok(request.form.get('setup_token', '')):
                flash('Invalid setup code.', 'danger')
            elif not email or not password:
                flash('Email and password are required.', 'danger')
            elif len(password) < 8:
                flash('Password must be at least 8 characters.', 'danger')
            elif password != confirm:
                flash('Passwords do not match.', 'danger')
            elif db.any_users():
                return redirect(url_for('login'))
            else:
                db.create_user(email, generate_password_hash(password), role='admin')
                session.pop('_csrf_token', None)
                flash('Admin account created. Please sign in.', 'success')
                return redirect(url_for('login'))

        return render_template('setup.html', csrf_token=_csrf_token())

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
        if not db.any_users():
            return redirect(url_for('setup_admin'))

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
            # row['password_hash'] is '' for an invited-but-not-yet-activated user
            # -> falsy -> they cannot log in until they consume their invite.
            if row and row['password_hash'] and check_password_hash(row['password_hash'], password):
                user = User(row)
                if not user.is_active:
                    _record_failure(email)
                    flash('This account is deactivated. Contact an administrator.', 'danger')
                    return render_template('login.html', csrf_token=_csrf_token())
                _clear_attempts(email)
                session.pop('_csrf_token', None)
                login_user(user)
                db.update_last_login(user.id)
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
