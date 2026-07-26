"""Authorization / multi-tenant scoping for the Soulful Content Engine.

ONE rule underpins everything: a 'client' user may only ever see and act on
their own client_id. 'admin' and 'manager' are unscoped (they see all clients).

HARD RULES — do not violate:
 1. NEVER trust a client_id coming from a form, query string, or JSON body for a
    client-role user. Always overwrite it server-side with current_scope().
 2. Every read/write that touches clients, content_posts or analytics must be
    scoped: pass current_scope() to the db.* query, or check the loaded object.
 3. Object-level checks happen on the OBJECT (its client_id), not on the URL.
    /content/17 must load row 17 and compare its client_id — hiding a link is
    not security.
 4. Client dropdowns: a client user gets a single fixed value = their own client;
    any submitted value is ignored.
"""
from functools import wraps

from flask import abort, g
from flask_login import current_user, login_required

import database as db
from auth import roles_required  # noqa: F401  (re-exported so callers can import from security)


def current_scope():
    """Return None for admin/manager (unscoped), or the bound client_id for a
    client user. This is the single source of truth for tenant scoping."""
    if getattr(current_user, 'role', None) == 'client':
        return getattr(current_user, 'client_id', None)
    return None


def is_client():
    return getattr(current_user, 'role', None) == 'client'


def enforce_client_id(submitted=None):
    """For any write: a client user's client_id ALWAYS comes from their session,
    never from user input. admin/manager keep the submitted value."""
    scope = current_scope()
    return scope if scope is not None else submitted


def can_see_client(client_id):
    """True if the current user is allowed to see this client record."""
    scope = current_scope()
    if scope is None:
        return True
    try:
        return int(client_id) == int(scope)
    except (TypeError, ValueError):
        return False


def scoped_posts(**kwargs):
    """db.get_posts() forced into the caller's tenant scope. A client user can
    never widen it: their client_id is imposed regardless of any passed value."""
    scope = current_scope()
    if scope is not None:
        kwargs['client_id'] = scope
    return db.get_posts(**kwargs)


def scoped_clients():
    """All clients for admin/manager; only the own client for a client user."""
    scope = current_scope()
    if scope is None:
        return db.get_clients()
    c = db.get_client(scope)
    return [c] if c else []


def require_content_access(param='post_id'):
    """Load the content row named by <param>; 404 if missing, 403 if out of scope.
    The row is stashed on flask.g.content_row so the handler can reuse it."""
    def wrapper(fn):
        @wraps(fn)
        @login_required
        def inner(*args, **kwargs):
            post = db.get_post(kwargs.get(param))
            if not post:
                abort(404)
            scope = current_scope()
            if scope is not None and int(post['client_id']) != int(scope):
                abort(403)
            g.content_row = post
            return fn(*args, **kwargs)
        return inner
    return wrapper


def require_client_access(param='client_id'):
    """403 if a client user targets a client record other than their own."""
    def wrapper(fn):
        @wraps(fn)
        @login_required
        def inner(*args, **kwargs):
            scope = current_scope()
            if scope is not None and int(kwargs.get(param)) != int(scope):
                abort(403)
            return fn(*args, **kwargs)
        return inner
    return wrapper
