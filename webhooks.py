import os
import hmac
import hashlib
import requests
from datetime import datetime


def send_to_make(post):
    """Fire the Make.com webhook when a post is approved.
    Returns (success: bool, error: str|None).
    """
    webhook_url = os.environ.get('MAKE_WEBHOOK_URL', '').strip()
    if not webhook_url:
        return False, 'MAKE_WEBHOOK_URL is not configured in .env'

    app_url = os.environ.get('APP_URL', '').rstrip('/')

    payload = {
        'event':          'post_approved',
        'post_id':        post['id'],
        'client':         post['client_name'],
        'platform':       post['platform'],
        'topic':          post['topic'],
        'caption':        post['caption'],
        'hashtags':       post.get('hashtags') or '',
        'image_url':      post.get('image_url') or '',
        'scheduled_date': post.get('scheduled_date') or '',
        'approved_at':    datetime.now().isoformat(),
        # Make.com can call this URL to mark the post as published
        'callback_url':   f'{app_url}/webhook/publish' if app_url else '',
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True, None
    except requests.Timeout:
        return False, 'Make.com webhook timed out (10 s)'
    except requests.HTTPError as e:
        return False, f'Make.com returned HTTP {e.response.status_code}'
    except requests.RequestException as e:
        return False, str(e)


def verify_secret(provided: str) -> bool:
    """Constant-time comparison of the inbound webhook secret."""
    expected = os.environ.get('MAKE_WEBHOOK_SECRET', '')
    if not expected:
        return True  # no secret configured — allow all (dev mode)
    return hmac.compare_digest(expected.strip(), provided.strip())
