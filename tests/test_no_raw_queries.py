"""Part 1 guard — the real deliverable.

Fails if any file OTHER than the repository (database.py) reaches the content_posts
or clients tables directly. All reads must go through database.py (and its
v_*_active views); trash/purge use the explicit get_*_including_deleted helpers.
This is what stops a future session from quietly reintroducing a query that forgets
the `deleted_at IS NULL` filter.

Escape hatch: put `# raw-query-ok: <reason>` on the same line to allow a deliberate
exception — visible in review, not silent.

The forbidden substrings are BUILT here rather than written literally, so this test
never matches itself.
"""
import os

# built from fragments so the literal patterns never appear in this file
_VERBS = ('FROM', 'UPDATE', 'DELETE FROM')
_TABLES = ('content_posts', 'clients')
NEEDLES = tuple('%s %s' % (v, t) for v in _VERBS for t in _TABLES)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# database.py IS the repository (the single allowed reader/writer of these tables)
ALLOWED = {'database.py'}
SKIP_DIRS = {'.venv', '__pycache__', '.git', 'node_modules', 'static', 'templates'}
ESCAPE = '# raw-query-ok'


def _python_files():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith('.py'):
                yield os.path.join(root, f), f


def _offenders():
    hits = []
    for path, name in _python_files():
        if name in ALLOWED or os.path.abspath(path) == os.path.abspath(__file__):
            continue
        with open(path, encoding='utf-8') as fh:
            for lineno, line in enumerate(fh, 1):
                if ESCAPE in line:
                    continue
                for needle in NEEDLES:
                    if needle in line:
                        hits.append('%s:%d  %s  |  %s'
                                    % (os.path.relpath(path, REPO_ROOT), lineno, needle, line.strip()))
    return hits


def test_no_raw_table_access_outside_repository():
    offenders = _offenders()
    assert not offenders, (
        'Raw content_posts/clients access outside database.py. Route it through a '
        'database.py function (views hide deleted rows), or add "# raw-query-ok: '
        '<reason>" if truly intentional:\n  ' + '\n  '.join(offenders))
