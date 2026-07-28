import os
import uuid
import hmac
import hashlib
import logging
import requests
from datetime import datetime

import database as db

log = logging.getLogger('dispatch')


def _build_payload(post):
    """The v1 contract payload for a post. See WEBHOOK_CONTRACT.md. Never remove or
    rename a v1 field; additions only."""
    app_url = os.environ.get('APP_URL', '').rstrip('/')
    return {
        'contract_version': 1,
        'event':            'post_approved',
        'post_id':          post['id'],
        'client_id':        post.get('client_id'),   # stable id; client NAME is ambiguous
        'client':           post.get('client_name'),
        'platform':         post['platform'],
        'idempotency_key':  '%s:%s:%s' % (post['id'], post['platform'], uuid.uuid4().hex),
        'content_type':     post.get('content_type') or 'photo',
        'topic':            post.get('topic'),
        'caption':          post.get('caption'),
        'hashtags':         post.get('hashtags') or '',
        'hook':             post.get('hook') or '',
        'image_url':        post.get('image_url') or '',
        'scheduled_date':   post.get('scheduled_date') or '',
        'approved_at':      datetime.now().isoformat(),
        'callback_url':     f'{app_url}/webhook/publish' if app_url else '',
        'performance_url':  f'{app_url}/api/performance' if app_url else '',
    }


def _http_post(url, payload, secret=None):
    """Low-level POST. Returns (ok, http_status, error). Sends X-Secret when given —
    a NEW per-client mechanism (the legacy global path sends none, unchanged)."""
    headers = {'Content-Type': 'application/json'}
    if secret:
        headers['X-Secret'] = secret
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.ok:
            return True, resp.status_code, None
        return False, resp.status_code, 'HTTP %s' % resp.status_code
    except requests.Timeout:
        return False, None, 'timeout (10s)'
    except requests.RequestException as e:
        return False, None, str(e)


def _audit_dispatch(actor_user_id, actor_role, client_id, post_id, platform, target,
                    http_status, ok, err, request_ip):
    # metadata NEVER contains the secret
    db.add_audit(actor_user_id, actor_role, client_id, 'content', post_id, 'dispatch',
                 metadata={'platform': platform, 'target': str(target),
                           'http_status': http_status, 'ok': ok, 'error': err},
                 request_ip=request_ip)


def dispatch_post(post, actor_user_id=None, actor_role=None, request_ip=None):
    """Dispatch a post to ITS CLIENT'S webhook.

    SECURITY: the target is resolved ONLY from post['client_id'] — a server-side value
    read from the content_posts row, never from a form, query string or JSON body. A
    post belonging to client A is therefore structurally incapable of being sent to
    client B's webhook (there is no client-id parameter a caller could supply).
    Refuses loudly; there is NO default target. Records the outcome on the webhook row
    and in audit_log. Returns (ok, message).
    """
    client_id = post.get('client_id')           # the ONLY routing input, from the row
    platform = post.get('platform')

    # Stage 2 outbound guard: re-read the base row at dispatch time so a post deleted
    # between load and dispatch cannot be published.
    fresh = db.get_post_including_deleted(post['id'])
    if fresh is None or fresh.get('deleted_at'):
        return False, 'Post is deleted; refusing to dispatch.'

    webhook = db.get_client_webhook(client_id)

    if webhook is None:
        # Legacy fallback: time-boxed, env-gated, OFF by default, never silent.
        if os.environ.get('LEGACY_WEBHOOK_FALLBACK') == 'true':
            legacy_url = os.environ.get('MAKE_WEBHOOK_URL', '').strip()
            if not legacy_url:
                return False, 'No webhook for this client and no legacy MAKE_WEBHOOK_URL set.'
            log.warning('LEGACY fallback used for client_id=%s (%s) — no per-client webhook row',
                        client_id, post.get('client_name'))
            ok, status, err = _http_post(legacy_url, _build_payload(post))   # legacy: no X-Secret
            _audit_dispatch(actor_user_id, actor_role, client_id, post['id'], platform,
                            'legacy', status, ok, err, request_ip)
            return (True, 'Dispatched via legacy webhook.') if ok else (False, 'Legacy dispatch failed: %s' % err)
        return False, ('No webhook configured for this client. '
                       'Add one in Settings → Webhooks before publishing.')

    if webhook['status'] == 'disabled':
        return False, "This client's webhook is disabled."

    enabled = [p.strip() for p in (webhook['platforms_enabled'] or '').split(',') if p.strip()]
    if platform not in enabled:
        return False, '%s is not connected for this client.' % (platform or '').title()

    ok, status, err = _http_post(webhook['webhook_url'], _build_payload(post), webhook['webhook_secret'])
    db.record_webhook_result(client_id, ok, error=err)
    _audit_dispatch(actor_user_id, actor_role, client_id, post['id'], platform,
                    webhook['id'], status, ok, err, request_ip)
    return (True, "Dispatched to this client's webhook.") if ok else (False, 'Dispatch failed: %s' % err)


def send_test_ping(client_id, actor_user_id=None, actor_role=None, request_ip=None):
    """Verify a newly-cloned scenario before real content flows. Sends TWO pings:
       1. with the CORRECT secret  -> must be ACCEPTED (2xx)
       2. with a deliberately WRONG secret -> must be REJECTED (non-2xx)
    A scenario that accepts the wrong secret is NOT verifying X-Secret — that is
    'insecure', never 'verified'. Records the verdict, audits it, returns a UI dict.
    The payload carries "test": true so a correctly-built scenario skips publishing.
    """
    webhook = db.get_client_webhook(client_id)
    if webhook is None:
        return {'ok': False, 'status': None, 'detail': 'No webhook configured for this client.'}
    url, secret = webhook['webhook_url'], webhook['webhook_secret']
    base = {'contract_version': 1, 'event': 'test_ping', 'test': True,
            'client_id': client_id, 'platform': 'facebook',
            'idempotency_key': 'test:%s:%s' % (client_id, uuid.uuid4().hex)}

    good_ok, good_http, good_err = _http_post(url, base, secret)
    wrong_ok, wrong_http, _ = _http_post(url, dict(base, note='wrong-secret-probe'), secret + '_WRONG')

    if not good_ok:
        status = 'failing'
        detail = 'Correct-secret ping failed (%s) — the scenario did not accept a valid request.' % good_err
    elif wrong_ok:
        status = 'insecure'
        detail = ('The scenario ACCEPTED a deliberately WRONG secret (HTTP %s) — it is not verifying '
                  'X-Secret. Add the secret check (see ONBOARDING.md) before sending real content.' % wrong_http)
    else:
        status = 'verified'
        detail = 'Correct secret accepted (HTTP %s); wrong secret rejected (HTTP %s).' % (good_http, wrong_http)

    db.set_client_webhook_test_status(client_id, status, None if status == 'verified' else detail)
    db.add_audit(actor_user_id, actor_role, client_id, 'webhook', webhook['id'], 'test_ping',
                 metadata={'result': status, 'good_http': good_http, 'wrong_http': wrong_http},
                 request_ip=request_ip)
    return {'ok': status == 'verified', 'status': status, 'detail': detail,
            'good_http': good_http, 'wrong_http': wrong_http}


def mask_secret(secret):
    """Show only the last 4 characters of a secret, for UI and logs. Never reveal the
    rest, and don't leak the length."""
    if not secret:
        return ''
    return '…' + secret[-4:] if len(secret) >= 4 else '…'


def verify_secret(provided: str) -> bool:
    """Constant-time comparison of the inbound webhook secret."""
    expected = os.environ.get('MAKE_WEBHOOK_SECRET', '')
    if not expected:
        return True  # no secret configured — allow all (dev mode)
    return hmac.compare_digest(expected.strip(), provided.strip())
