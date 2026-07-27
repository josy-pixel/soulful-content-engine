#!/usr/bin/env python
"""End-to-end authorization probe against the LIVE app — covers what a browser
cannot: a forged tenant id in a request body, and a cross-tenant POST that a UI
would never let you attempt.

It provisions a fresh client-role user on the ZZ-TEST client (id 6) via the admin
invite flow, consumes the invite programmatically, then acts as that client with a
requests.Session and asserts:

  1. POST /content/12/status  (post 12 belongs to Holly, id 1)  -> 403
     A 302 to /login is a FAIL (means we were not authenticated), not a pass.
  2. POST /api/save-caption with client_id=1 forged in the body -> 200, and the
     created row lands under the client's OWN id (6): proven by the client being
     able to GET the new post (200) — if it had been created under client 1 the
     client would get 403 on it.

Admin credentials are read from a file whose PATH is given by env AUTHZ_CRED_FILE
(two lines: email, password). The path is not a secret; the credentials never
appear on the command line. TARGET_CLIENT defaults to 6 (ZZ-TEST).

Run:  AUTHZ_CRED_FILE=/path/to/admin_creds.txt python scripts/check_authz.py
"""
import os
import re
import sys
import time

import truststore  # use the OS cert store (works behind an SSL-inspecting proxy)
truststore.inject_into_ssl()
import requests

BASE = os.environ.get('AUTHZ_BASE', 'https://soulful-content-engine.onrender.com')
TARGET_CLIENT = int(os.environ.get('AUTHZ_CLIENT', '6'))     # ZZ-TEST
FOREIGN_POST = int(os.environ.get('AUTHZ_FOREIGN_POST', '12'))  # Holly's post
FOREIGN_CLIENT = int(os.environ.get('AUTHZ_FOREIGN_CLIENT', '1'))  # Holly

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print('%-4s %s\n       %s' % ('PASS' if ok else 'FAIL', name, detail))


def read_admin_creds():
    path = os.environ.get('AUTHZ_CRED_FILE')
    if not path or not os.path.exists(path):
        sys.exit('Set AUTHZ_CRED_FILE to a file with admin email + password (two lines).')
    lines = [l.strip() for l in open(path, encoding='utf-8-sig') if l.strip()]
    return lines[0], lines[1]


def csrf(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ''


def login(sess, email, password):
    html = sess.get(BASE + '/login', timeout=30).text
    sess.post(BASE + '/login', timeout=30,
              data={'csrf_token': csrf(html), 'email': email, 'password': password})


def issue_invite(admin, email):
    """Invite the probe email on TARGET_CLIENT; if it already exists, re-issue via
    reset. Returns the one-time invite URL scraped from the flash."""
    r = admin.post(BASE + '/users/invite', timeout=30,
                   data={'email': email, 'client_id': str(TARGET_CLIENT)})
    m = re.search(r'(https?://[^\s"<]+/invite/[A-Za-z0-9_\-]+)', r.text)
    if m:
        return m.group(1)
    # already exists -> find its id on /users and reset to get a fresh link
    users = admin.get(BASE + '/users', timeout=30).text
    for row in users.split('<tr'):
        if email in row:
            uid = re.search(r'/users/(\d+)/reset', row).group(1)
            rr = admin.post(BASE + '/users/%s/reset' % uid, timeout=30)
            return re.search(r'(https?://[^\s"<]+/invite/[A-Za-z0-9_\-]+)', rr.text).group(1)
    sys.exit('could not obtain an invite link for ' + email)


def main():
    admin_email, admin_pw = read_admin_creds()
    probe_email = 'authz-probe@synapseai.co.il'
    probe_pw = 'authz-probe-password-123456'

    admin = requests.Session()
    login(admin, admin_email, admin_pw)
    if admin.get(BASE + '/', timeout=30).url.endswith('/login'):
        sys.exit('admin login failed — check AUTHZ_CRED_FILE credentials')

    invite_url = issue_invite(admin, probe_email)
    print('invite link:', invite_url)

    client = requests.Session()
    form = client.get(invite_url, timeout=30)
    if form.status_code != 200:
        sys.exit('invite link not accepted (status %s)' % form.status_code)
    client.post(invite_url, timeout=30,
                data={'csrf_token': csrf(form.text), 'password': probe_pw, 'confirm': probe_pw})

    # must be authenticated as the client now
    home = client.get(BASE + '/', timeout=30)
    authed = not home.url.endswith('/login') and home.status_code == 200
    record('client session authenticated (not bounced to /login)', authed,
           'GET / -> %s %s' % (home.status_code, home.url))
    if not authed:
        finish()

    # ---- Check 1: cross-tenant POST /content/<foreign>/status must be 403 ----
    r1 = client.post('%s/content/%d/status' % (BASE, FOREIGN_POST), timeout=30,
                     data={'status': 'draft'}, allow_redirects=False)
    ok1 = r1.status_code == 403
    detail = 'POST /content/%d/status -> %d' % (FOREIGN_POST, r1.status_code)
    if r1.status_code in (301, 302):
        detail += '  (redirect to %s — NOT authenticated; FAIL)' % r1.headers.get('Location')
    record('cross-tenant status change is 403 (not 302/login, not 200)', ok1, detail)

    # ---- Check 2: forged client_id in body is overwritten to the client's own ----
    r2 = client.post(BASE + '/api/save-caption', timeout=30, json={
        'client_id': FOREIGN_CLIENT,          # forged: Holly
        'platform': 'facebook', 'topic': 'authz probe', 'caption': 'authz probe',
    })
    try:
        new_id = r2.json().get('post_id')
    except Exception:
        new_id = None
    created = r2.status_code == 200 and new_id
    # the client can GET its OWN post -> 200 proves it landed under client 6, not 1
    own = client.get('%s/content/%s' % (BASE, new_id), timeout=30) if created else None
    ok2 = bool(created) and own is not None and own.status_code == 200
    record('forged client_id overwritten to own tenant (client can view the row)',
           ok2, 'save-caption -> %s post_id=%s ; client GET /content/%s -> %s' % (
               r2.status_code, new_id, new_id, own.status_code if own is not None else 'n/a'))
    # cross-check: the foreign client must NOT be able to see it either way is implied;
    # additionally confirm the row is NOT visible as client 1's by construction (own=200).

    # ---- Check 3: /webhook/test must be admin-only (client -> 403, not a publish) ----
    r3 = client.post('%s/webhook/test/%d' % (BASE, FOREIGN_POST), timeout=30,
                     allow_redirects=False)
    ok3 = r3.status_code == 403
    detail = 'POST /webhook/test/%d -> %d' % (FOREIGN_POST, r3.status_code)
    if r3.status_code in (301, 302):
        detail += '  (redirect to %s — NOT authenticated; FAIL)' % r3.headers.get('Location')
    record('client cannot fire the publish webhook (admin-only, 403)', ok3, detail)

    finish()


def finish():
    print('\n==== SUMMARY ====')
    for name, ok, _ in results:
        print('  %-5s %s' % ('PASS' if ok else 'FAIL', name))
    sys.exit(0 if all(ok for _, ok, _ in results) else 1)


if __name__ == '__main__':
    main()
