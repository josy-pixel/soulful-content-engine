"""Interface language. One place that decides which locale a request renders in.

Resolution order, deliberately explicit — Accept-Language is never consulted,
because a browser's guess is not a choice the user made:

    1. the authenticated user's stored preference (users.language)
    2. session['language'], for pages with no user yet (login, invite links)
    3. DEFAULT_LOCALE

Adding a language is a translation-file job: append the code to LOCALES with its
display name and text direction, drop in the catalog, and nothing here changes.
"""
from flask import session
from flask_login import current_user

DEFAULT_LOCALE = 'en'

# code -> (name shown in the switcher, text direction)
LOCALES = {
    'en': ('English', 'ltr'),
    'he': ('עברית', 'rtl'),
}


def supported(code):
    """True when `code` is a locale this build can actually render."""
    return isinstance(code, str) and code in LOCALES


def normalise(code):
    """Any unknown, missing or malformed value degrades to the default rather
    than raising — a bad stored value must never take a page down."""
    return code if supported(code) else DEFAULT_LOCALE


def text_direction(code):
    return LOCALES.get(normalise(code), LOCALES[DEFAULT_LOCALE])[1]


def language_name(code):
    return LOCALES.get(normalise(code), LOCALES[DEFAULT_LOCALE])[0]


def _stored_preference():
    """The logged-in user's saved language, or None.

    Read defensively: the users.language column arrives in Stage 2, and this
    must not break in the window before it exists, nor for a session whose user
    row predates it.
    """
    if not getattr(current_user, 'is_authenticated', False):
        return None
    code = getattr(current_user, 'language', None)
    return code if supported(code) else None


def get_locale():
    """Registered with Babel as the locale selector for every request."""
    stored = _stored_preference()
    if stored:
        return stored
    return normalise(session.get('language'))


def set_session_language(code):
    """Set the pre-authentication language. Returns the code actually applied,
    so a caller can tell that a rejected value was ignored."""
    code = normalise(code)
    session['language'] = code
    return code
