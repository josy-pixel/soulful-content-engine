"""S3 storage for client media.

The browser uploads straight to S3 and the file never travels through this
server, so neither MAX_CONTENT_LENGTH nor the size of the Render disk limits
how big a video a client can add. This module only hands out short-lived
permits and reads back what actually landed.

Falls back to being disabled when the environment is not configured, in which
case callers keep using the original on-disk path.
"""
import os
import uuid

try:  # the workstation sits behind a TLS-intercepting proxy; Render does not
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - absence is normal in production
    pass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = os.environ.get('S3_BUCKET', '')
REGION = os.environ.get('S3_REGION', 'eu-north-1')

# A presigned POST tops out at 5 GB; that is far above anything the agency posts.
MAX_UPLOAD_BYTES = int(os.environ.get('S3_MAX_UPLOAD_MB', '5120')) * 1024 * 1024
UPLOAD_TTL = int(os.environ.get('S3_UPLOAD_TTL', '900'))      # 15 min to finish uploading
VIEW_TTL = int(os.environ.get('S3_VIEW_TTL', '3600'))         # 1 h for a view/fetch link

_client = None


def enabled():
    """True when the app is configured to store media in S3."""
    return bool(BUCKET and os.environ.get('AWS_ACCESS_KEY_ID'))


def client():
    """A client that signs for the bucket's own region.

    Without the explicit s3v4 + virtual addressing the SDK signs against the
    global endpoint while the request lands on the regional one, and every
    presigned link comes back 403 SignatureDoesNotMatch - which reads like a
    permissions problem and is not one.
    """
    global _client
    if _client is None:
        _client = boto3.client(
            's3',
            region_name=REGION,
            config=Config(
                signature_version='s3v4',
                s3={'addressing_style': 'virtual'},
                retries={'max_attempts': 3, 'mode': 'standard'},
            ),
        )
    return _client


def build_key(client_id, filename):
    """clients/<client_id>/<random>.<ext> - the original name is never trusted."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
    ext = ''.join(ch for ch in ext if ch.isalnum())[:8] or 'bin'
    return 'clients/{}/{}.{}'.format(int(client_id), uuid.uuid4().hex, ext)


def presign_upload(key, content_type=None):
    """A permit the browser posts the file to. S3 itself rejects oversized files."""
    fields = {}
    conditions = [['content-length-range', 1, MAX_UPLOAD_BYTES]]
    if content_type:
        fields['Content-Type'] = content_type
        conditions.append({'Content-Type': content_type})
    return client().generate_presigned_post(
        Bucket=BUCKET,
        Key=key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=UPLOAD_TTL,
    )


def put(key, data, content_type=None):
    """Write bytes straight to the bucket — for a trusted server-to-server
    sender (a Make.com scenario) rather than a browser, where the presigned
    POST flow above is what keeps bytes off this server."""
    kwargs = {'Bucket': BUCKET, 'Key': key, 'Body': data}
    if content_type:
        kwargs['ContentType'] = content_type
    client().put_object(**kwargs)


def presign_view(key, expires=None):
    """A link that works for a while and then stops working."""
    return client().generate_presigned_url(
        'get_object',
        Params={'Bucket': BUCKET, 'Key': key},
        ExpiresIn=expires or VIEW_TTL,
    )


SCHEME = 's3://'


def ref(key):
    """The stable reference stored in the DB. Never store a signed URL - it expires."""
    return SCHEME + key


def resolve(stored, app_url=None, expires=None):
    """Turn a stored reference into a URL that anything can fetch right now.

    Always returns an absolute URL, so Make and Facebook can use it as-is
    instead of gluing the app's domain onto a relative path.
    """
    if not stored:
        return ''
    if stored.startswith(SCHEME):
        return presign_view(stored[len(SCHEME):], expires)
    if stored.startswith('http://') or stored.startswith('https://'):
        return stored                      # an external link the user pasted
    base = (app_url if app_url is not None else os.environ.get('APP_URL', '')).rstrip('/')
    return base + stored if base else stored


def head(key):
    """What actually landed in the bucket, or None. Never trust the browser's word."""
    try:
        r = client().head_object(Bucket=BUCKET, Key=key)
    except ClientError:
        return None
    return {'size': r['ContentLength'], 'content_type': r.get('ContentType', '')}


def delete(key):
    """Remove an object. Versioning keeps a recoverable copy behind a delete marker."""
    try:
        client().delete_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError:
        return False
