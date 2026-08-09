"""Guard: a sentence must not be split into separately-translated fragments.

This mistake reached the repo twice — once in the mechanical pass that was
reverted mid-Stage-3, and again in 3e after two clean chunks. Both times the
rule was written down and known. That is the argument for a test: a convention
only remembered is a convention that comes back.

The shape is a sentence broken around inline markup:

    {{ _('Click') }} <strong>{{ _('Generate') }}</strong> {{ _('to continue.') }}

Each piece is separately translatable and none can be reordered, so no language
whose word order differs from English can be served — and in Hebrew the reading
direction differs too. The fix is one {% trans %} block with the markup inside:

    {% trans %}Click <strong>Generate</strong> to continue.{% endtrans %}

Two shapes are flagged:

  1. two translation calls on one line separated only by inline formatting
  2. a translation call followed by inline markup and then bare prose — the
     half-wrapped sentence, which is worse because the tail never reaches a
     catalog at all

Only INLINE tags count as separators. Two labels in separate table cells,
buttons or options are not a sentence, and must not be flagged.

Escape hatch: `i18n-ok` on the line.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO_ROOT, 'templates')
ESCAPE = 'i18n-ok'

# Tags that keep text in the same sentence. Deliberately excludes a, button, td,
# th, li, option, label, div, p — those separate distinct pieces of UI, not
# clauses of one sentence.
INLINE = r'(?:strong|b|em|i|u|code|small|span|bdi|br|sup|sub)'
INLINE_TAG = r'</?%s(?:\s[^<>]*)?/?>' % INLINE

CALL = r'\{\{\s*_\(.*?\)\s*\}\}'
GAP = r'(?:\s|%s)*' % INLINE_TAG

# 1. call ... inline markup ... call
SPLIT = re.compile(CALL + GAP + CALL)
# 2. call ... inline markup ... bare prose (two+ letters, not a Jinja tag)
HALF = re.compile(CALL + r'\s*' + INLINE_TAG + GAP + r'[A-Za-z]{2,}')


def _offenders():
    hits = []
    for base, dirs, names in os.walk(TEMPLATES):
        for n in names:
            if not n.endswith('.html'):
                continue
            path = os.path.join(base, n)
            with open(path, encoding='utf-8') as fh:
                for lineno, line in enumerate(fh, 1):
                    if ESCAPE in line:
                        continue
                    for label, pattern in (('split sentence', SPLIT),
                                           ('half-wrapped sentence', HALF)):
                        m = pattern.search(line)
                        if m:
                            hits.append('%s:%d  %s\n      %s'
                                        % (os.path.relpath(path, REPO_ROOT), lineno,
                                           label, m.group(0)[:110]))
                            break
    return hits


def test_no_sentence_is_split_into_fragments():
    offenders = _offenders()
    assert not offenders, (
        'A sentence is split into separately-translated fragments. No translator '
        'can reorder these, and Hebrew needs to. Use one {% trans %} block with '
        'the markup inside. Escape hatch: "i18n-ok" on the line.\n\n  '
        + '\n  '.join(offenders))


def test_the_guard_catches_the_real_regressions():
    """Both shapes that actually reached the repo, plus the patterns it must
    leave alone. A guard nobody has seen fail is not a guard."""
    bad = [
        # the 3e regression
        """<p>{{ _('Click') }} <strong>{{ _("Generate This Week's Trends") }}</strong> {{ _('to pull ideas.') }}</p>""",
        # the mid-Stage-3 revert
        "<strong>{{ _('Shown once.') }}</strong> {{ _('The app does') }} <strong>{{ _('not') }}</strong> email it",
        # half-wrapped: tail never reaches a catalog
        "<strong>{{ _('send a test ping') }}</strong> before publishing real content.",
        "{{ _('Secret (sent as') }} <code>{{ _('X-Secret') }}</code>",
    ]
    for line in bad:
        assert _fires(line), line

    good = [
        # separate cells / controls / options are not one sentence
        "<tr><th>{{ _('Email') }}</th><th>{{ _('Status') }}</th></tr>",
        "<td data-label=\"{{ _('Client') }}\">{{ c.name }}</td>",
        "<button>{{ _('Filter') }}</button>\n<a>{{ _('Clear') }}</a>",
        "<option value=\"client\">{{ _('Client — their own only') }}</option>",
        # icon then label is one label
        "<i class=\"bi bi-people-fill\"></i> {{ _('Clients') }}",
        # whole sentence with the markup inside — the correct form
        "{% trans %}Click <strong>Generate</strong> to continue.{% endtrans %}",
        # a call next to a value, not next to another call
        "<bdi>{{ u.email }}</bdi> <span class=\"small\">{{ _('(you)') }}</span>",
    ]
    for line in good:
        assert not _fires(line), line


def _fires(line):
    return bool(SPLIT.search(line) or HALF.search(line))
