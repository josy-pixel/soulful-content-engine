"""Bringing a media file in from a link, for the ingest webhook.

The server fetching a URL somebody else chose is the dangerous half of ingest: left
alone it will read anything this machine can reach — the cloud metadata address, a
port on localhost, a service on the private network — and file it as "media". So a
link is followed only when every hop is https, on a host the agency has allowed, and
resolves to public addresses only; the connection then goes to the address that was
checked, not to whatever the name resolves to a moment later. The body is streamed to
a temp file under a size cap and an overall deadline, never held in memory — the
instance is small and runs a single worker.

What kind of file it is, is ours to decide rather than the sender's: it is read from
the Content-Type and the extension together, a disagreement is refused, and the type
the file is stored and served as comes from the table below.
"""
import http.client
import ipaddress
import logging
import os
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3

log = logging.getLogger('media_ingest')

# Meta's CDNs — where the Instagram and Facebook Graph APIs serve media from.
# Anything else is added on purpose, through MEDIA_INGEST_ALLOWED_HOSTS.
DEFAULT_ALLOWED_HOSTS = ('cdninstagram.com', 'fbcdn.net')

MAX_REDIRECTS = 3
DEADLINE_SECONDS = 60      # the whole fetch, however slowly the bytes arrive
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 15          # per read; the deadline bounds the total
CHUNK = 64 * 1024
USER_AGENT = 'Mozilla/5.0 (compatible; SoulfulContentEngine/1.0)'
CA_BUNDLE = certifi.where()

# The type a file is stored and served as. The same extensions as the gallery
# upload (app.ALLOWED_EXTENSIONS) and the same video set as media_rules.
MIME_BY_EXT = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp',
    'mp4': 'video/mp4', 'mov': 'video/quicktime', 'webm': 'video/webm',
    'avi': 'video/x-msvideo',
}
EXT_BY_MIME = {
    'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp',
    'video/mp4': 'mp4', 'video/quicktime': 'mov', 'video/webm': 'webm',
    'video/x-msvideo': 'avi',
}
_MIME_ALIASES = {'image/jpg': 'image/jpeg', 'image/pjpeg': 'image/jpeg',
                 'video/avi': 'video/x-msvideo', 'video/msvideo': 'video/x-msvideo'}
# Says nothing about what the file is, so the extension decides.
_GENERIC_MIME = {'', 'application/octet-stream', 'binary/octet-stream'}
_EXT_RE = re.compile(r'^[a-z0-9]{2,5}$')

_URL_RULE = 'media_url must be an https link on an allowed media host.'
_UNSUPPORTED = ('Unsupported file type. Images: JPG, PNG, GIF, WebP. '
                'Video: MP4, MOV, WebM, AVI.')
_TOO_LARGE = 'File exceeds the maximum upload size.'


class Refused(Exception):
    """The request asks for something this endpoint will not do. The message is
    safe to show the caller; `detail` is for the log only."""
    status = 400

    def __init__(self, message, detail=''):
        super().__init__(message)
        self.detail = detail


class Unsupported(Refused):
    status = 415


class Mismatch(Refused):
    status = 422


class TooLarge(Refused):
    status = 413


class FetchFailed(Exception):
    """The far end did not hand over a usable file. The message is for the log only —
    it names hosts and errors the caller has no business seeing."""


# ── type ─────────────────────────────────────────────────────────────────────

def ext_of(name):
    """The extension of a file name or URL path, if it has one that looks like one."""
    last = (name or '').rsplit('/', 1)[-1]
    if '.' not in last:
        return ''
    ext = last.rsplit('.', 1)[1].lower()
    return ext if _EXT_RE.match(ext) else ''


def resolve_type(declared, ext):
    """(mime, ext) this file is stored as, from its Content-Type and extension together.

    Either may be missing or generic, and then the other decides. When both name a
    type they must agree: video bytes under a .jpg name would otherwise be filed as
    an image and attached to a photo post — the failure media_rules exists to stop.
    """
    mime = (declared or '').split(';', 1)[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    if mime in _GENERIC_MIME:
        mime = ''
    if (mime and mime not in EXT_BY_MIME) or (ext and ext not in MIME_BY_EXT):
        raise Unsupported(_UNSUPPORTED, 'content type %r, extension %r' % (mime, ext))
    by_ext = MIME_BY_EXT.get(ext, '')
    if mime and by_ext and mime != by_ext:
        raise Mismatch('File type mismatch: the name says .%s but the content type is %s.'
                       % (ext, mime))
    final = mime or by_ext
    if not final:
        raise Unsupported('Could not tell what kind of file this is — give it a media '
                          'file extension or a matching Content-Type.')
    return final, EXT_BY_MIME[final]


# ── fetching ─────────────────────────────────────────────────────────────────

def allowed_hosts():
    """Domain suffixes a media_url may point at."""
    raw = os.environ.get('MEDIA_INGEST_ALLOWED_HOSTS', '')
    hosts = tuple(h.strip().strip('.').lower() for h in raw.split(',') if h.strip().strip('.'))
    return hosts or DEFAULT_ALLOWED_HOSTS


def host_allowed(host, suffixes):
    return any(host == s or host.endswith('.' + s) for s in suffixes)


def is_public(address):
    """True only for a globally routable unicast address. An IPv6 address that wraps
    an IPv4 one (::ffff:127.0.0.1, 2002:7f00:1::) is judged by the address it wraps."""
    try:
        ip = ipaddress.ip_address(address.split('%', 1)[0])
    except ValueError:
        return False
    candidates = [ip]
    if ip.version == 6:
        candidates += [a for a in (ip.ipv4_mapped, ip.sixtofour) if a is not None]
    for a in candidates:
        if (not a.is_global or a.is_private or a.is_loopback or a.is_link_local
                or a.is_reserved or a.is_multicast or a.is_unspecified):
            return False
    return True


def resolve(host):
    """Every address the name resolves to. Replaced in tests."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def check_url(url, suffixes):
    """(host, request_path, address) for a link that may be fetched. Raises Refused
    for one that may not, FetchFailed when the name does not resolve."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise Refused(_URL_RULE, 'unparseable url')
    host = (parts.hostname or '').rstrip('.').lower()
    if parts.scheme.lower() != 'https' or not host:
        raise Refused(_URL_RULE, 'not an https url: %r' % url)
    if parts.username is not None or parts.password is not None or port not in (None, 443):
        raise Refused(_URL_RULE, 'credentials or a non-default port in %r' % url)
    if not host_allowed(host, suffixes):
        raise Refused(_URL_RULE, 'host %s is not on the allow-list' % host)
    try:
        addresses = resolve(host)
    except (OSError, UnicodeError) as e:
        raise FetchFailed('could not resolve %s: %s' % (host, e))
    if not addresses:
        raise FetchFailed('%s resolved to nothing' % host)
    private = [a for a in addresses if not is_public(a)]
    if private:
        raise Refused(_URL_RULE, '%s resolves to non-public %s' % (host, private))
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    return host, path, addresses[0]                 # the fragment never leaves


def open_pinned(address, host, path, timeout):
    """GET `path` from `host`, connected to the `address` that was checked. Replaced
    in tests. TLS still verifies the certificate against the host name (SNI and the
    hostname check both use `host`); pinning only stops the name being re-resolved
    somewhere else between the check and the connect."""
    pool = urllib3.HTTPSConnectionPool(
        address, port=443, timeout=timeout, maxsize=1, retries=False,
        cert_reqs='CERT_REQUIRED', ca_certs=CA_BUNDLE,
        assert_hostname=host, server_hostname=host)
    return pool.urlopen(
        'GET', path,
        headers={'Host': host, 'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'},
        redirect=False, retries=False, preload_content=False, assert_same_host=False)


def fetch_to_file(url, out, max_bytes):
    """Stream the file at `url` into the open binary file `out`.

    Returns (content_type, final_url, size). Raises Refused when the link itself may
    not be fetched, TooLarge past `max_bytes`, and FetchFailed for anything the far
    end does wrong — including a redirect to somewhere the first link could not have
    pointed. Every hop is checked again; nothing is followed automatically.
    """
    suffixes = allowed_hosts()
    deadline = time.monotonic() + DEADLINE_SECONDS
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        try:
            host, path, address = check_url(current, suffixes)
        except Refused as e:
            if hop == 0:
                raise
            raise FetchFailed('redirect %d to a refused target %r: %s' % (hop, current, e.detail))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchFailed('deadline passed before hop %d' % hop)
        timeout = urllib3.Timeout(connect=min(CONNECT_TIMEOUT, remaining),
                                  read=min(READ_TIMEOUT, remaining))
        resp = None
        try:
            resp = open_pinned(address, host, path, timeout)
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get('Location')
                if not location:
                    raise FetchFailed('%s redirected without a Location' % host)
                current = urljoin(current, location)
                continue
            if resp.status != 200:
                raise FetchFailed('%s answered %s' % (host, resp.status))
            encoding = (resp.headers.get('Content-Encoding') or 'identity').strip().lower()
            if encoding != 'identity':
                raise FetchFailed('%s sent Content-Encoding %s' % (host, encoding))
            declared = (resp.headers.get('Content-Length') or '').strip()
            if declared.isdigit() and int(declared) > max_bytes:
                raise TooLarge(_TOO_LARGE, 'Content-Length %s' % declared)
            size = 0
            while True:
                if time.monotonic() > deadline:
                    raise FetchFailed('deadline passed after %d bytes from %s' % (size, host))
                chunk = resp.read1(CHUNK)          # one read — a slow drip cannot hide in it
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise TooLarge(_TOO_LARGE, 'more than %d bytes' % max_bytes)
                out.write(chunk)
            return resp.headers.get('Content-Type', ''), current, size
        except (Refused, FetchFailed):
            raise
        except (urllib3.exceptions.HTTPError, http.client.HTTPException, OSError, ValueError) as e:
            raise FetchFailed('%s from %s: %s' % (type(e).__name__, host, e)) from e
        finally:
            if resp is not None:
                resp.close()
    raise FetchFailed('more than %d redirects, last %r' % (MAX_REDIRECTS, current))
