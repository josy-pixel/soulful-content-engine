"""Guard: no user-facing prose inside JavaScript.

This is the one class of i18n mistake that documentation cannot protect against,
because it fails invisibly. An unwrapped string in template markup at least
shows up as visibly English in a Hebrew UI. A string inside a <script> block is
never seen by pybabel, so it never enters a catalog — no translator is shown a
gap, and nobody downstream can discover it, because downstream never learns it
exists.

The fix is the data-* convention in TRANSLATIONS.md: render the text
server-side where Babel lives, let the script read it back.

    <span id="x" data-sending="{{ _('Sending…') }}"></span>
    el.textContent = el.dataset.sending;

What counts as prose here is narrow on purpose. Class lists, CSS, selectors,
MIME types and HTML fragments all live in scripts legitimately and start
lowercase or with punctuation, so the signal is: a quoted literal containing a
space that begins like a sentence, or one carrying a glyph only ever used in
user-facing text (…, ✓, ✗, →).

Escape hatch: `// i18n-ok` or `# i18n-ok` on the line.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {'.venv', '__pycache__', '.git', 'node_modules', 'translations', 'tests'}
ESCAPE = 'i18n-ok'

SCRIPT_BLOCK = re.compile(r'<script\b[^>]*>(.*?)</script>', re.S | re.I)
# single- or double-quoted literal, and template literals
LITERAL = re.compile(r"""(['"`])((?:(?!\1)[^\\]|\\.)*)\1""")

SENTENCE_LIKE = re.compile(r'^[A-Z][a-z]')
UI_GLYPH = re.compile(r'[…✓✗→←]')
# a Jinja expression inside the literal means it is already server-rendered
JINJA = re.compile(r'\{\{|\{%')


def looks_like_prose(text):
    if JINJA.search(text):
        return False                       # already rendered by the server
    if not text.strip():
        return False
    if UI_GLYPH.search(text):
        return True
    if ' ' not in text:
        return False
    stripped = text.strip()
    if stripped.startswith(('<', '.', '#', '[', '/', '?', '&')):
        return False                       # html, selector, path, query string
    return bool(SENTENCE_LIKE.match(stripped))


def _scan(path, text, is_js_file):
    hits = []
    lines = text.splitlines()
    if is_js_file:
        regions = [(1, text)]
    else:
        regions = []
        for m in SCRIPT_BLOCK.finditer(text):
            start_line = text[:m.start(1)].count('\n') + 1
            regions.append((start_line, m.group(1)))

    for base_line, blob in regions:
        for i, line in enumerate(blob.splitlines()):
            if ESCAPE in line:
                continue
            for m in LITERAL.finditer(line):
                if looks_like_prose(m.group(2)):
                    hits.append('%s:%d  %s'
                                % (os.path.relpath(path, REPO_ROOT),
                                   base_line + i, m.group(0)[:90]))
    return hits


def _offenders():
    hits = []
    for base, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            path = os.path.join(base, n)
            if n.endswith('.html'):
                hits += _scan(path, open(path, encoding='utf-8').read(), False)
            elif n.endswith('.js'):
                hits += _scan(path, open(path, encoding='utf-8').read(), True)
    return hits


def test_no_user_facing_prose_inside_javascript():
    offenders = _offenders()
    assert not offenders, (
        'User-facing text inside JavaScript never reaches a catalog — pybabel '
        'cannot see it, so it fails silently and permanently. Render it '
        'server-side into a data-* attribute and read it back (see '
        'TRANSLATIONS.md). Escape hatch: "// i18n-ok" on the line.\n\n  '
        + '\n  '.join(offenders))


def test_the_guard_catches_prose_and_leaves_code_alone():
    """The guard has to be seen failing on the real shapes, and seen NOT firing
    on the things that legitimately live in a script."""
    prose = [
        "result.textContent = 'Sending…';",
        "el.innerHTML = 'No media in gallery yet.';",
        "alert('Could not load gallery.')",
        "x = '✓ Sent successfully'",
        "y = 'Upload media →'",
    ]
    for line in prose:
        assert _scan('t.js', line, True), line

    code = [
        "headers: {'Content-Type': 'application/json'}",
        "result.className = 'small ms-1 text-success';",
        "div.style = 'width:100px;height:100px;object-fit:cover'",
        "document.querySelector('meta[name=\"csrf-token\"]')",
        "if (e.key === 'Escape') closeSidebar();",
        "fetch('/api/media/client/' + id)",
        "el.classList.toggle('d-none')",
        "var s = '<span class=\"text-muted small\">'",
        "t = el.dataset.sending;",
        "x = '{{ _('Sending…') }}'",          # already server-rendered
    ]
    for line in code:
        assert not _scan('t.js', line, True), line
