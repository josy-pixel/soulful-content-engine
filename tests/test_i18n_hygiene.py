"""The guard for the string-wrapping pass.

339 strings across 23 files is where mechanical fatigue produces the classic
gettext mistakes, and every one of them is invisible until a translator sees the
catalog. Each is greppable, so each is a test rather than a habit:

  _(f'Deleted {name}')        the extractor stores the interpolated result, so
                              every value produces a different, untranslatable
                              string and the catalog fills with garbage
  _('Deleted ' + name)        same, plus the source string never appears at all
  _('Deleted') + ' ' + name   word order differs by language; in Hebrew so does
                              the direction. One sentence must be one string.
  _('Deleted %s' % name)      interpolates before wrapping — same failure as the
                              f-string, just spelled differently
  _('Deleted {}'.format(n))   likewise

Positional %s is also rejected: a translator reordering a sentence cannot
reorder positional placeholders, so named ones are the only safe form.

The patterns are BUILT from fragments here so this file never matches itself.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {'.venv', '__pycache__', '.git', 'node_modules', 'translations', 'tests'}
ESCAPE = '# i18n-ok'

_G = r'(?:_|gettext|lazy_gettext|_l|ngettext)'


def _quoted(group):
    """A string literal that respects its own delimiter.

    Matching the body as [^"']* is wrong and produced a false positive on real
    code: _("Client '%(name)s' created.", name=x) has an apostrophe INSIDE a
    double-quoted string, so a naive body ends at the apostrophe and the regex
    then sees the following % as pre-formatting. Anchoring to the opening quote
    with a backreference is the only way to read the literal correctly.
    """
    return r'(?P<%s>["\'])(?:(?!(?P=%s)).)*(?P=%s)' % (group, group, group)


def _quoted_body(group):
    """The same, but stopping inside the literal — for looking at its contents."""
    return r'(?P<%s>["\'])(?:(?!(?P=%s)).)*' % (group, group)


# an f-string as the first thing inside the call
F_STRING = re.compile(_G + r'\(\s*[rbRB]*f[rbRB]*["\']')
# string concatenation inside the call, either side
CONCAT_INSIDE = re.compile(
    _G + r'\(\s*(?:' + _quoted('ca') + r'\s*\+|[A-Za-z_][\w.]*\s*\+\s*["\'])')
# two translated fragments glued together
CONCAT_CALLS = re.compile(_G + r'\([^()]*\)\s*\+|\+\s*' + _G + r'\(')
# formatting applied to the literal before it is wrapped
PRE_FORMAT = re.compile(_G + r'\(\s*' + _quoted('pf') + r'\s*(?:%|\.format\s*\()')
# positional placeholders, which a translator cannot reorder
POSITIONAL = re.compile(_G + r'\(\s*' + _quoted_body('po') + r'%[sd](?![\w(])')

CHECKS = [
    ('f-string inside a translation call', F_STRING),
    ('string concatenation inside a translation call', CONCAT_INSIDE),
    ('concatenated translated fragments', CONCAT_CALLS),
    ('formatting applied before wrapping', PRE_FORMAT),
    ('positional placeholder — use %(name)s', POSITIONAL),
]


def _files():
    for base, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if n.endswith('.py') or n.endswith('.html'):
                path = os.path.join(base, n)
                if os.path.abspath(path) == os.path.abspath(__file__):
                    continue
                yield path


def _offenders():
    hits = []
    for path in _files():
        with open(path, encoding='utf-8') as fh:
            for lineno, line in enumerate(fh, 1):
                if ESCAPE in line:
                    continue
                for label, pattern in CHECKS:
                    if pattern.search(line):
                        hits.append('%s:%d  %s\n      %s'
                                    % (os.path.relpath(path, REPO_ROOT), lineno,
                                       label, line.strip()[:140]))
    return hits


def test_no_untranslatable_string_construction():
    offenders = _offenders()
    assert not offenders, (
        'Translation calls that a translator cannot work with. Use one whole '
        'sentence per string with named placeholders — _("Deleted %(name)s", '
        'name=x). Escape hatch: "# i18n-ok" on the line.\n\n  '
        + '\n  '.join(offenders))


def test_the_guard_actually_catches_each_mistake():
    """A guard nobody has seen fail is not a guard."""
    bad = [
        "flash(_(f'Deleted {name}'))",
        "flash(_('Deleted ' + name))",
        "msg = _('Deleted') + ' ' + name",
        "flash(_('Deleted %s' % name))",
        "flash(_('Deleted {}'.format(name)))",
        "flash(_('Deleted %s items'))",
    ]
    for line in bad:
        assert any(p.search(line) for _label, p in CHECKS), line

    good = [
        "flash(_('Deleted %(name)s', name=name))",
        "flash(_('Client deleted.'))",
        "label = lazy_gettext('Email')",
        "flash(_('Deleted %(count)d posts', count=n))",
        # quote inside the opposite quote: the false positive that a naive
        # [^\"']* body produced on real code, and the reason for _quoted()
        "flash(_(\"Client '%(name)s' created.\", name=x))",
        "flash(_('Say \"%(word)s\" again', word=w))",
        "flash(_(\"'%(name)s' moved — %(n)d posts\", name=x, n=3))",
    ]
    for line in good:
        assert not any(p.search(line) for _label, p in CHECKS), line
