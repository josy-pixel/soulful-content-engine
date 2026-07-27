#!/usr/bin/env python
"""Poll the Render deploy for this service until a target commit is live, and
optionally assert that a path is gone (returns 404).

The Render API key is read from, in order:
  1. env var RENDER_API_KEY
  2. a local, untracked secrets file (default ../soulful-access.json -> render.apiKey)
It is NEVER passed on the command line, so it stays out of shell history and the
process list.

Usage:
  python scripts/check_deploy.py <expected_sha7> [--expect-404 <path>]

Example:
  python scripts/check_deploy.py f8bc2f9 --expect-404 /admin/_maint_dbinfo?token=x
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

SERVICE_URL = 'https://soulful-content-engine.onrender.com'
ACCESS_FILE = os.environ.get(
    'SOULFUL_ACCESS_FILE',
    os.path.join(os.path.dirname(__file__), '..', '..', 'soulful-access.json'),
)


def _load_access():
    try:
        with open(ACCESS_FILE, encoding='utf-8') as fh:
            return json.load(fh).get('render', {})
    except FileNotFoundError:
        return {}


def get_key_and_sid():
    acc = _load_access()
    key = os.environ.get('RENDER_API_KEY') or acc.get('apiKey')
    sid = os.environ.get('RENDER_SERVICE_ID') or acc.get('serviceId')
    if not key or not sid:
        sys.exit('RENDER_API_KEY / service id not found in env or ' + ACCESS_FILE)
    return key, sid


def api(url, key):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def poll(expected_sha, key, sid, tries=45, delay=12):
    for i in range(tries):
        dep = api('https://api.render.com/v1/services/%s/deploys?limit=1' % sid, key)[0]['deploy']
        sha = dep['commit']['id'][:7]
        status = dep['status']
        print('poll %d: %s commit=%s' % (i, status, sha))
        if sha == expected_sha and status == 'live':
            return True
        if status in ('build_failed', 'update_failed', 'canceled', 'pre_deploy_failed'):
            print('DEPLOY FAILED:', status)
            return False
        time.sleep(delay)
    print('timed out waiting for', expected_sha)
    return False


def check_absent(path):
    try:
        urllib.request.urlopen(SERVICE_URL + path, timeout=25)
        print('CHECK %s -> 200  (STILL PRESENT)' % path)
        return False
    except urllib.error.HTTPError as e:
        print('CHECK %s -> %d  (%s)' % (path, e.code, 'gone' if e.code == 404 else 'unexpected'))
        return e.code == 404


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    expected = sys.argv[1]
    key, sid = get_key_and_sid()
    ok = poll(expected, key, sid)
    if '--expect-404' in sys.argv:
        path = sys.argv[sys.argv.index('--expect-404') + 1]
        ok = check_absent(path) and ok
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
