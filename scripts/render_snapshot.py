"""Capture rendered English HTML for the i18n template pass.

Restructuring a sentence so markup sits outside it CHANGES the rendered output.
"the tests pass" does not cover that — no test asserts rendered copy. So the
English output is diffed, not assumed:

    python scripts/render_snapshot.py before
    ... edit templates ...
    python scripts/render_snapshot.py after
    python scripts/render_snapshot.py diff

Snapshots go to a scratch directory, never the repo.
"""
import os
import sys
import difflib
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(tempfile.gettempdir(), 'soulful-render-snapshots')

_dir = tempfile.mkdtemp(prefix='soulful-render-')
os.environ.setdefault('DB_PATH', os.path.join(_dir, 'render.db'))
os.environ.setdefault('SECRET_KEY', 'render-snapshot')
os.environ.setdefault('MAKE_WEBHOOK_SECRET', 'render-snapshot')

import hashlib                                         # noqa: E402
from datetime import datetime, timedelta               # noqa: E402
from werkzeug.security import generate_password_hash   # noqa: E402
import database as db                                  # noqa: E402
import app as flask_app                                # noqa: E402


def _fixture():
    """A deterministic tenant graph. Fixed values only — a timestamp or a random
    id would show up as a false diff."""
    db.init_db()
    conn = db.get_db()
    conn.execute('PRAGMA foreign_keys=OFF')
    for t in ('post_media', 'performance_metrics', 'approval_history',
              'content_posts', 'client_media', 'brand_voices',
              'client_webhooks', 'clients', 'users', 'audit_log'):
        try:
            conn.execute(f'DELETE FROM {t}')
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user('snap@t.co', generate_password_hash('pw'), role='admin')
    live = db.create_client({'name': 'Soulful Management',
                             'description': 'Talent & lifestyle management agency.',
                             'contact_email': 'josy@soulful.management'})
    # one client with plural counts, one with singular, one with none —
    # so plural forms actually render
    for i in range(3):
        db.create_post({'client_id': live, 'platform': 'facebook',
                        'topic': f'T{i}', 'caption': f'C{i}', 'status': 'posted'})
    db.add_media(live, 'a.jpg', 'a.jpg', 'image')
    db.add_media(live, 'b.jpg', 'b.jpg', 'image')
    db.upsert_client_webhook(live, 'https://hook.eu1.make.com/snapshot', 'sekrit123', 'facebook')

    single = db.create_client({'name': "O'Brien Media", 'description': ''})
    db.create_post({'client_id': single, 'platform': 'facebook',
                    'topic': 'One', 'caption': 'One', 'status': 'draft'})

    empty = db.create_client({'name': 'Empty Co', 'description': ''})
    db.soft_delete_client(empty)
    # Freeze the deletion timestamp. It renders on the page, so leaving it at
    # "now" puts a changing line in every diff — and a harness that always shows
    # a diff is a harness people stop reading.
    conn = db.get_db()
    conn.execute("UPDATE clients SET deleted_at='2026-01-01T00:00:00' WHERE id=?", (empty,))  # raw-query-ok: snapshot fixture pins a rendered timestamp
    # Post timestamps render too (updated_at on the detail page, posted_date in
    # lists), so they are pinned for the same reason.
    conn.execute("UPDATE content_posts SET created_at='2026-01-01 09:00:00', updated_at='2026-01-01 09:00:00', posted_date='2026-01-02 10:00'")  # raw-query-ok: snapshot fixture pins rendered timestamps
    conn.execute("UPDATE approval_history SET changed_at='2026-01-01 09:00:00'")
    conn.commit()
    conn.close()

    # a live invite token so the pre-auth invite page renders
    token = 'snapshot-token-fixed-value'
    thash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    exp = (datetime.now() + timedelta(hours=72)).isoformat()
    db.create_pending_user('invitee@t.co', 'client', live, thash, exp)
    # An ACTIVE client user, for the 403 page. The invited one above is
    # is_active=0 until the invite is consumed, so the login guard bounces it
    # with a 302 and the 403 template never renders.
    portal = db.create_user('portal@t.co', generate_password_hash('pw'),
                            role='client', client_id=live)
    return admin, token, portal


def capture(label):
    admin, token, portal = _fixture()
    flask_app.app.config['TESTING'] = True
    c = flask_app.app.test_client()

    os.makedirs(os.path.join(OUT, label), exist_ok=True)

    # pre-auth pages, no session
    for name, url in {'invite': f'/invite/{token}', 'login': '/login'}.items():
        _write(label, name, c.get(url))

    with c.session_transaction() as s:
        s['_user_id'] = str(admin)
        s['_fresh'] = True
        s['_csrf_token'] = 'snapshot-csrf'

    post_id = db.get_posts(limit=1)[0]['id']
    client_id = db.get_clients()[0]['id']
    for name, url in {'clients': '/clients', 'users': '/users',
                      'content_list': '/content',
                      'dashboard': '/',
                      'content_detail': f'/content/{post_id}',
                      'content_form': f'/content/{post_id}/edit',
                      'client_detail': f'/clients/{client_id}'}.items():
        _write(label, name, c.get(url))

    # 403 renders only for an authenticated user who lacks the role, so it needs
    # a client-user session hitting an admin-only page.

    with c.session_transaction() as s:
        s['_user_id'] = str(portal)
    _write(label, 'forbidden', c.get('/users'))

    print(f'captured "{label}" -> {os.path.join(OUT, label)}')


import re                                              # noqa: E402

# The CSRF token is regenerated per session, so it differs on every capture and
# would appear in every diff. Masked, not ignored — a token going MISSING is a
# real regression the diff should still show.
_CSRF = re.compile(r'(csrf[_-]token"?\s*(?:value|content)=")[^"]+(")', re.I)


def _write(label, name, resp):
    path = os.path.join(OUT, label, f'{name}.html')
    body = _CSRF.sub(r'\1<TOKEN>\2', resp.get_data(as_text=True))
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(body)
    print(f'   {name}: HTTP {resp.status_code}, {len(resp.get_data())} bytes')


def diff():
    a, b = os.path.join(OUT, 'before'), os.path.join(OUT, 'after')
    names = sorted(set(os.listdir(a)) | set(os.listdir(b)))
    total = 0
    for n in names:
        pa, pb = os.path.join(a, n), os.path.join(b, n)
        ta = open(pa, encoding='utf-8').read().splitlines() if os.path.exists(pa) else []
        tb = open(pb, encoding='utf-8').read().splitlines() if os.path.exists(pb) else []
        d = list(difflib.unified_diff(ta, tb, fromfile=f'before/{n}',
                                      tofile=f'after/{n}', lineterm='', n=1))
        if d:
            total += 1
            print('\n'.join(d))
            print()
    print(f'\n{"IDENTICAL — no English output changed" if not total else f"{total} file(s) differ — every change must be justified in the report"}')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'before'
    diff() if cmd == 'diff' else capture(cmd)
