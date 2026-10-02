"""The media ingest webhook, and the editing queue behind it.

/webhook/media-ingest is internet-facing and fetches a URL the caller chose, so most
of this file is about what it must refuse: a caller who is not a client's own key,
a link that points anywhere but an allowed public media host (directly, through a
redirect, or through DNS), a file bigger or slower than allowed, a file whose name
and content type disagree, and a "source" link that would run script in the app.

The network and S3 are stubbed for every test: name resolution and the pinned
connection are replaced, so nothing here can reach a real host.
"""
import io
import tempfile
import time
from html.parser import HTMLParser

import pytest
import urllib3
from werkzeug.security import generate_password_hash

import app as flask_app
import database as db
import media_ingest
import s3_media
import webhooks

CSRF = "test-csrf-token"
# Nobody signs in with a password here (the session is set directly), and the real
# hash costs seconds per user on a slow machine.
PW = generate_password_hash("pw", method="pbkdf2:sha256:1")
CDN = "scontent.cdninstagram.com"
PUBLIC = "157.240.0.35"            # any globally routable address; never connected to
GENERIC_FETCH_ERROR = "Could not fetch the media file from media_url."


# ── stubs ────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, headers=None, chunks=(), delay=0.0):
        self.status = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.delay = delay
        self.closed = False

    def read1(self, n):
        if self.delay:
            time.sleep(self.delay)
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


def ok(body=b"\xff\xd8\xff\xe0jpeg", ctype="image/jpeg", **headers):
    return FakeResp(200, dict({"Content-Type": ctype}, **headers), [body])


def redirect(location, status=302):
    return FakeResp(status, {"Location": location})


class Net:
    """A fake DNS and a fake pinned connection. Unknown names do not resolve; an
    unknown path is a test bug, not a 404."""

    def __init__(self):
        self.dns = {CDN: [PUBLIC]}
        self.routes = {}
        self.connects = []
        self.responses = []

    def resolve(self, host):
        if host not in self.dns:
            raise OSError("test DNS has no entry for %s" % host)
        return list(self.dns[host])

    def open(self, address, host, path, timeout):
        self.connects.append((address, host, path))
        r = self.routes[(host, path)]
        r = r() if callable(r) else r
        self.responses.append(r)
        return r


@pytest.fixture(autouse=True)
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(media_ingest, "resolve", n.resolve)
    monkeypatch.setattr(media_ingest, "open_pinned", n.open)
    monkeypatch.delenv("MEDIA_INGEST_ALLOWED_HOSTS", raising=False)
    return n


@pytest.fixture()
def s3(monkeypatch):
    store = {}

    def upload(key, fileobj, content_type):
        store[key] = {"body": fileobj.read(), "content_type": content_type}

    monkeypatch.setattr(s3_media, "enabled", lambda: True)
    monkeypatch.setattr(s3_media, "upload", upload)
    monkeypatch.setattr(s3_media, "delete", lambda key: store.pop(key, None) is not None)
    monkeypatch.setattr(s3_media, "presign_view",
                        lambda key, expires=None: "https://bucket.test/%s?sig=1" % key)
    return store


@pytest.fixture()
def spools(monkeypatch):
    """Every temp file the endpoint opens, so a test can check it was closed."""
    made = []
    real = tempfile.TemporaryFile

    def tracked(*a, **k):
        f = real(*a, **k)
        made.append(f)
        return f

    monkeypatch.setattr(flask_app.tempfile, "TemporaryFile", tracked)
    return made


@pytest.fixture()
def data():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["post_media", "approval_history", "performance_metrics", "content_posts",
              "client_media", "client_api_keys", "client_webhooks", "clients", "users",
              "audit_log"]:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    admin = db.create_user("ing-admin@t.co", PW, role="admin")
    ca = db.create_client({"name": "Ingest Client A"})
    cb = db.create_client({"name": "Ingest Client B"})
    user_a = db.create_user("ing-a@t.co", PW, role="client", client_id=ca)
    _, key_a = db.create_client_api_key(ca, "A scenario")
    _, key_b = db.create_client_api_key(cb, "B scenario")
    return dict(admin=admin, ca=ca, cb=cb, user_a=user_a, key_a=key_a, key_b=key_b)


@pytest.fixture()
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def login_as(c, user_id):
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["_csrf_token"] = CSRF


def ingest(c, key, url=None, headers=None, **fields):
    body = dict(fields)
    if url is not None:
        body["media_url"] = url
    h = {"X-Api-Key": key} if key else {}
    h.update(headers or {})
    return c.post("/webhook/media-ingest", json=body, headers=h)


def upload(c, key, filename="photo.jpg", mimetype="image/jpeg", body=b"\xff\xd8\xff\xe0jpeg",
           **fields):
    form = dict(fields)
    form["file"] = (io.BytesIO(body), filename, mimetype)
    return c.post("/webhook/media-ingest", data=form, content_type="multipart/form-data",
                  headers={"X-Api-Key": key} if key else {})


def media(cid):
    return db.get_client_media(cid)


# ── who may call ─────────────────────────────────────────────────────────────

def test_a_clients_key_files_raw_media_under_that_client(client, data, s3, net):
    net.routes[(CDN, "/v/123_n.jpg")] = ok()
    r = ingest(client, data["key_a"], "https://%s/v/123_n.jpg" % CDN,
               source="instagram", source_url="https://www.instagram.com/p/abc/")
    assert r.status_code == 201
    [m] = media(data["ca"])
    assert r.get_json()["media_id"] == m["id"]
    assert m["edit_status"] == "needs_editing"
    assert m["source"] == "instagram" and m["source_url"] == "https://www.instagram.com/p/abc/"
    assert m["storage"] == "s3" and m["s3_key"].startswith("clients/%d/" % data["ca"])
    assert media(data["cb"]) == []


def test_a_wrong_key_is_refused_before_anything_is_fetched(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok()
    r = ingest(client, "sce_not-a-real-key", "https://%s/x.jpg" % CDN)
    assert r.status_code == 403
    assert net.connects == [] and s3 == {} and media(data["ca"]) == []


def test_a_revoked_key_is_refused(client, data, s3, net):
    key_id, raw = db.create_client_api_key(data["ca"], "temporary")
    db.revoke_client_api_key(key_id)
    net.routes[(CDN, "/x.jpg")] = ok()
    assert ingest(client, raw, "https://%s/x.jpg" % CDN).status_code == 403


def test_the_legacy_shared_secret_is_refused(client, data, s3, net, monkeypatch):
    """The shared secret carries no client: on this endpoint it would have let its
    holder file media under any client named in the body."""
    monkeypatch.setenv("LEGACY_INBOUND_SECRET", "true")          # as in production
    net.routes[(CDN, "/x.jpg")] = ok()
    r = ingest(client, None, "https://%s/x.jpg" % CDN,
               secret="ci-webhook-secret", client_id=data["cb"])
    assert r.status_code == 403
    r = client.post("/webhook/media-ingest", content_type="multipart/form-data",
                    data={"secret": "ci-webhook-secret", "client_id": str(data["cb"]),
                          "file": (io.BytesIO(b"\xff\xd8jpeg"), "x.jpg", "image/jpeg")})
    assert r.status_code == 403
    assert net.connects == [] and s3 == {} and media(data["cb"]) == []


def test_a_credential_in_the_query_string_or_body_is_refused(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok()
    for qs in ("secret=ci-webhook-secret", "api_key=%s" % data["key_a"]):
        r = client.post("/webhook/media-ingest?" + qs,
                        json={"media_url": "https://%s/x.jpg" % CDN, "client_id": data["ca"]})
        assert r.status_code == 403
    r = ingest(client, None, "https://%s/x.jpg" % CDN, api_key=data["key_a"])
    assert r.status_code == 403
    assert net.connects == [] and media(data["ca"]) == []


def test_a_client_id_in_the_body_is_ignored(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok()
    r = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN, client_id=data["cb"])
    assert r.status_code == 201
    assert len(media(data["ca"])) == 1 and media(data["cb"]) == []
    assert all(k.startswith("clients/%d/" % data["ca"]) for k in s3)


def test_a_deleted_clients_key_is_refused(client, data, s3, net):
    db.soft_delete_client(data["ca"], actor_user_id=data["admin"], actor_role="admin")
    net.routes[(CDN, "/x.jpg")] = ok()
    assert ingest(client, data["key_a"], "https://%s/x.jpg" % CDN).status_code == 403
    assert net.connects == []


# ── where a media_url may point ──────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://%s/x.jpg" % CDN,
    "ftp://%s/x.jpg" % CDN,
    "file:///etc/passwd",
    "https://%s:8443/x.jpg" % CDN,
    "https://user:pw@%s/x.jpg" % CDN,
    "not a url",
])
def test_only_plain_https_links_are_fetched(client, data, s3, net, url):
    net.routes[(CDN, "/x.jpg")] = ok()
    r = ingest(client, data["key_a"], url)
    assert r.status_code == 400
    assert net.connects == [] and media(data["ca"]) == []


@pytest.mark.parametrize("host", [
    "evil.example", "cdninstagram.com.evil.example", "evilcdninstagram.com",
    "127.0.0.1", "[::1]", "169.254.169.254", "localhost",
])
def test_a_host_off_the_allow_list_is_refused(client, data, s3, net, host):
    net.dns[host.strip("[]")] = [PUBLIC]
    r = ingest(client, data["key_a"], "https://%s/x.jpg" % host)
    assert r.status_code == 400
    assert r.get_json()["error"] == "media_url must be an https link on an allowed media host."
    assert net.connects == []


def test_the_allow_list_comes_from_the_environment(client, data, s3, net, monkeypatch):
    monkeypatch.setenv("MEDIA_INGEST_ALLOWED_HOSTS", " media.example.org ,")
    net.dns["cdn.media.example.org"] = [PUBLIC]
    net.routes[("cdn.media.example.org", "/a.png")] = ok(b"\x89PNG", "image/png")
    net.routes[(CDN, "/x.jpg")] = ok()
    assert ingest(client, data["key_a"], "https://%s/x.jpg" % CDN).status_code == 400
    assert ingest(client, data["key_a"], "https://cdn.media.example.org/a.png").status_code == 201


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0",
    "100.64.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
    "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
    "2002:a9fe:a9fe::1",
])
def test_an_allowed_name_that_resolves_to_a_non_public_address_is_refused(
        client, data, s3, net, address):
    net.dns[CDN] = [address]
    net.routes[(CDN, "/x.jpg")] = ok()
    r = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN)
    assert r.status_code == 400
    assert address not in r.get_data(as_text=True)
    assert net.connects == [] and media(data["ca"]) == []


def test_one_non_public_address_among_several_is_enough_to_refuse(client, data, s3, net):
    net.dns[CDN] = [PUBLIC, "10.0.0.7"]
    net.routes[(CDN, "/x.jpg")] = ok()
    assert ingest(client, data["key_a"], "https://%s/x.jpg" % CDN).status_code == 400
    assert net.connects == []


def test_the_connection_goes_to_the_address_that_was_checked(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok()
    assert ingest(client, data["key_a"], "https://%s/x.jpg" % CDN).status_code == 201
    assert net.connects == [(PUBLIC, CDN, "/x.jpg")]


def test_an_ipv4_address_is_preferred_when_ipv6_is_listed_first(client, data, s3, net):
    net.dns[CDN] = ["2a03:2880:f12f:83:face:b00c:0:25de", PUBLIC]
    net.routes[(CDN, "/x.jpg")] = ok()
    assert ingest(client, data["key_a"], "https://%s/x.jpg" % CDN).status_code == 201
    assert net.connects == [(PUBLIC, CDN, "/x.jpg")]


def test_a_response_without_read1_is_read_through_its_http_client_body(client, data, s3, net):
    """urllib3 1.26 responses have no read1; the stream underneath them does."""
    class Legacy:
        status, headers, closed = 200, {"Content-Type": "image/jpeg"}, False

        def __init__(self):
            self._fp = FakeResp(chunks=[b"\xff\xd8", b"\xff\xe0jpeg"])

        def close(self):
            self.closed = True

    net.routes[(CDN, "/legacy.jpg")] = Legacy()
    assert ingest(client, data["key_a"], "https://%s/legacy.jpg" % CDN).status_code == 201
    [stored] = s3.values()
    assert stored["body"] == b"\xff\xd8\xff\xe0jpeg"


def test_the_fragment_trick_neither_picks_the_target_nor_the_type(client, data, s3, net):
    """'#.jpg' once made any path look like an image. The fragment is never sent and
    never read as an extension; an internal address is refused before connecting."""
    net.dns["meta.cdninstagram.com"] = ["169.254.169.254"]
    r = ingest(client, data["key_a"], "https://meta.cdninstagram.com/latest/meta-data/#.jpg")
    assert r.status_code == 400 and net.connects == []

    net.routes[(CDN, "/internal")] = ok(b"secret text", "text/plain")
    r = ingest(client, data["key_a"], "https://%s/internal#.jpg" % CDN)
    assert r.status_code == 415
    assert net.connects == [(PUBLIC, CDN, "/internal")]
    assert media(data["ca"]) == [] and s3 == {}


@pytest.mark.parametrize("location,dns", [
    ("https://meta.cdninstagram.com/latest/", {"meta.cdninstagram.com": ["169.254.169.254"]}),
    ("http://169.254.169.254/latest/meta-data/", {}),
    ("https://127.0.0.1/admin", {}),
    ("https://evil.example/x.jpg", {"evil.example": [PUBLIC]}),
    ("http://%s/x.jpg" % CDN, {}),
])
def test_a_redirect_is_checked_like_the_first_link(client, data, s3, net, location, dns):
    net.dns.update(dns)
    net.routes[(CDN, "/r.jpg")] = redirect(location)
    r = ingest(client, data["key_a"], "https://%s/r.jpg" % CDN)
    assert r.status_code == 502
    assert r.get_json()["error"] == GENERIC_FETCH_ERROR
    assert net.connects == [(PUBLIC, CDN, "/r.jpg")]         # never the second hop
    assert all(resp.closed for resp in net.responses)
    assert media(data["ca"]) == [] and s3 == {}


def test_a_redirect_between_allowed_hosts_is_followed(client, data, s3, net):
    net.dns["video.fbcdn.net"] = ["31.13.64.7"]
    net.routes[(CDN, "/r")] = redirect("https://video.fbcdn.net/v/clip.mp4?oh=1")
    net.routes[("video.fbcdn.net", "/v/clip.mp4?oh=1")] = redirect("/v/final.mp4", 307)
    net.routes[("video.fbcdn.net", "/v/final.mp4")] = ok(b"\x00\x00\x00\x18ftypmp42", "video/mp4")
    r = ingest(client, data["key_a"], "https://%s/r" % CDN)
    assert r.status_code == 201
    assert [c[0] for c in net.connects] == [PUBLIC, "31.13.64.7", "31.13.64.7"]
    [m] = media(data["ca"])
    assert m["media_type"] == "video" and m["filename"].endswith(".mp4")


def test_at_most_three_redirects_are_followed(client, data, s3, net):
    for i in range(4):
        net.routes[(CDN, "/r%d" % i)] = redirect("/r%d" % (i + 1))
    net.routes[(CDN, "/r3")] = ok()                   # three hops: allowed
    assert ingest(client, data["key_a"], "https://%s/r0" % CDN).status_code == 201

    net.connects.clear()
    net.routes[(CDN, "/r3")] = redirect("/r4")        # a fourth: refused
    net.routes[(CDN, "/r4")] = ok()
    r = ingest(client, data["key_a"], "https://%s/r0" % CDN, source_url="https://x.test/2")
    assert r.status_code == 502
    assert [c[2] for c in net.connects] == ["/r0", "/r1", "/r2", "/r3"]


def test_a_file_over_the_cap_is_refused_as_soon_as_it_is_known(client, data, s3, net, spools,
                                                               monkeypatch):
    monkeypatch.setitem(flask_app.app.config, "MAX_CONTENT_LENGTH", 1000)
    declared = FakeResp(200, {"Content-Type": "image/jpeg", "Content-Length": "5000"},
                        [b"x" * 600] * 9)
    net.routes[(CDN, "/declared.jpg")] = declared
    r = ingest(client, data["key_a"], "https://%s/declared.jpg" % CDN)
    assert r.status_code == 413
    assert len(declared._chunks) == 9                  # not a byte read

    undeclared = FakeResp(200, {"Content-Type": "image/jpeg"}, [b"x" * 600] * 9)
    net.routes[(CDN, "/undeclared.jpg")] = undeclared
    r = ingest(client, data["key_a"], "https://%s/undeclared.jpg" % CDN)
    assert r.status_code == 413
    assert len(undeclared._chunks) == 7                # stopped at the second chunk
    assert s3 == {} and media(data["ca"]) == []
    assert spools and all(f.closed for f in spools)
    assert declared.closed and undeclared.closed


def test_a_slow_file_is_cut_off_at_the_deadline(client, data, s3, net, monkeypatch):
    monkeypatch.setattr(media_ingest, "DEADLINE_SECONDS", 0.3)
    net.routes[(CDN, "/drip.jpg")] = FakeResp(200, {"Content-Type": "image/jpeg"},
                                              [b"x"] * 100, delay=0.05)
    t0 = time.monotonic()
    r = ingest(client, data["key_a"], "https://%s/drip.jpg" % CDN)
    assert r.status_code == 502 and time.monotonic() - t0 < 2
    assert s3 == {} and media(data["ca"]) == []


@pytest.mark.parametrize("failure", ["dns", "connect", "read", "status", "encoding"])
def test_network_failures_are_a_generic_502(client, data, s3, net, spools, failure):
    url = "https://%s/x.jpg" % CDN
    if failure == "dns":
        net.dns.pop(CDN)
    elif failure == "connect":
        def refuse():
            raise urllib3.exceptions.NewConnectionError(None, "refused by 10.9.8.7:443")
        net.routes[(CDN, "/x.jpg")] = refuse
    elif failure == "read":
        class Broken(FakeResp):
            def read1(self, n):
                raise urllib3.exceptions.ReadTimeoutError(None, "/x.jpg", "read timed out")
        net.routes[(CDN, "/x.jpg")] = Broken(200, {"Content-Type": "image/jpeg"})
    elif failure == "status":
        net.routes[(CDN, "/x.jpg")] = FakeResp(404, {"Content-Type": "text/html"}, [b"nope"])
    else:
        net.routes[(CDN, "/x.jpg")] = ok(**{"Content-Encoding": "gzip"})
    r = ingest(client, data["key_a"], url)
    assert r.status_code == 502
    assert r.get_json() == {"error": GENERIC_FETCH_ERROR}     # no host, port or exception text
    assert media(data["ca"]) == [] and s3 == {}
    assert all(f.closed for f in spools)


# ── what kind of file it is ──────────────────────────────────────────────────

def test_a_name_and_content_type_that_disagree_are_refused(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok(b"\x00\x00\x00\x18ftypmp42", "video/mp4")
    r = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN)
    assert r.status_code == 422 and "mismatch" in r.get_json()["error"]
    r = upload(client, data["key_a"], "clip.jpg", "video/mp4")
    assert r.status_code == 422
    assert media(data["ca"]) == [] and s3 == {}


@pytest.mark.parametrize("path,ctype", [
    ("/page.jpg", "text/html"), ("/x.svg", "image/svg+xml"), ("/x", "text/plain"),
    ("/x.html", "image/jpeg"), ("/x", "application/octet-stream"),
])
def test_anything_but_a_supported_image_or_video_is_refused(client, data, s3, net, path, ctype):
    net.routes[(CDN, path)] = ok(b"<svg onload=alert(1)>", ctype)
    r = ingest(client, data["key_a"], "https://%s%s" % (CDN, path))
    assert r.status_code == 415
    assert media(data["ca"]) == [] and s3 == {}


def test_either_the_extension_or_the_content_type_can_decide(client, data, s3, net):
    net.routes[(CDN, "/v/AQN3x")] = ok(b"\x00\x00\x00\x18ftypmp42", "video/mp4")
    net.routes[(CDN, "/v/clip.mp4")] = ok(b"\x00\x00\x00\x18ftypmp42", "application/octet-stream")
    net.routes[(CDN, "/v/t51.2885-15")] = ok(b"\xff\xd8", "image/jpg")      # a common alias
    for path in ("/v/AQN3x", "/v/clip.mp4", "/v/t51.2885-15"):
        assert ingest(client, data["key_a"], "https://%s%s" % (CDN, path)).status_code == 201
    kinds = sorted((m["media_type"], m["filename"].rsplit(".", 1)[1]) for m in media(data["ca"]))
    assert kinds == [("image", "jpg"), ("video", "mp4"), ("video", "mp4")]


def test_s3_serves_the_file_as_our_type_never_the_senders(client, data, s3, net):
    net.routes[(CDN, "/a.JPG")] = ok(b"\xff\xd8", "IMAGE/JPEG; charset=binary")
    assert ingest(client, data["key_a"], "https://%s/a.JPG" % CDN).status_code == 201
    r = upload(client, data["key_a"], "clip.mov", "application/octet-stream", b"\x00\x00moov")
    assert r.status_code == 201
    assert sorted(v["content_type"] for v in s3.values()) == ["image/jpeg", "video/quicktime"]


# ── where the bytes go ───────────────────────────────────────────────────────

def test_the_s3_key_is_built_from_the_keys_client(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok(b"\xff\xd8 body")
    ingest(client, data["key_a"], "https://%s/x.jpg" % CDN, client_id=data["cb"])
    [m] = media(data["ca"])
    [key] = s3
    assert key == m["s3_key"] and key.startswith("clients/%d/" % data["ca"])
    assert m["filename"] == key.rsplit("/", 1)[1] and m["file_size"] == len(b"\xff\xd8 body")
    assert s3[key]["body"] == b"\xff\xd8 body"


def test_without_s3_nothing_is_fetched_or_written_to_disk(client, data, net, monkeypatch,
                                                          tmp_path):
    monkeypatch.setattr(s3_media, "enabled", lambda: False)
    monkeypatch.setattr(flask_app, "UPLOAD_PATH", str(tmp_path))
    net.routes[(CDN, "/x.jpg")] = ok()
    r = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN)
    assert r.status_code == 503
    assert upload(client, data["key_a"]).status_code == 503
    assert net.connects == [] and list(tmp_path.iterdir()) == []
    assert media(data["ca"]) == []


def test_a_storage_failure_is_a_502_and_leaves_no_row(client, data, s3, net, monkeypatch):
    def broken(key, fileobj, content_type):
        raise RuntimeError("S3 said no to arn:aws:s3:::secret-bucket")
    monkeypatch.setattr(s3_media, "upload", broken)
    net.routes[(CDN, "/x.jpg")] = ok()
    r = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN)
    assert r.status_code == 502 and "arn:" not in r.get_data(as_text=True)
    assert media(data["ca"]) == []


def test_a_multipart_upload_goes_straight_to_s3(client, data, s3, net):
    body = b"\xff\xd8" + b"\x01" * (600 * 1024)        # past werkzeug's in-memory limit
    r = upload(client, data["key_a"], "Shoot 01.JPG", "image/jpeg", body,
               source="instagram", caption_hint="  behind the scenes  ")
    assert r.status_code == 201
    [m] = media(data["ca"])
    assert m["original_name"] == "Shoot_01.JPG" and m["caption_hint"] == "behind the scenes"
    assert m["media_type"] == "image" and m["file_size"] == len(body)
    assert s3[m["s3_key"]] == {"body": body, "content_type": "image/jpeg"}
    assert net.connects == []


def test_a_multipart_request_without_a_file_is_refused(client, data, s3):
    r = client.post("/webhook/media-ingest", data={"source": "web"},
                    content_type="multipart/form-data", headers={"X-Api-Key": data["key_a"]})
    assert r.status_code == 400
    assert upload(client, data["key_a"], body=b"").status_code == 400
    assert s3 == {}


# ── the same post twice ──────────────────────────────────────────────────────

def test_the_same_original_post_is_ingested_once(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok()
    first = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN,
                   source_url="https://www.instagram.com/p/abc/")
    again = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN,
                   source_url="https://www.instagram.com/p/abc/")
    assert first.status_code == 201 and again.status_code == 200
    assert again.get_json()["media_id"] == first.get_json()["media_id"]
    assert len(media(data["ca"])) == 1 and len(s3) == 1
    assert len(net.connects) == 1                      # not even downloaded again


def test_the_same_post_for_two_clients_is_two_files(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok
    for key in (data["key_a"], data["key_b"]):
        assert ingest(client, key, "https://%s/x.jpg" % CDN,
                      source_url="https://www.instagram.com/p/abc/").status_code == 201
    assert len(media(data["ca"])) == 1 and len(media(data["cb"])) == 1


def test_without_a_source_url_nothing_is_treated_as_a_duplicate(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok
    for _ in range(2):
        assert ingest(client, data["key_a"], "https://%s/x.jpg" % CDN).status_code == 201
    assert len(media(data["ca"])) == 2


def test_a_duplicate_that_slips_past_the_check_is_caught_by_the_index(client, data, s3, net,
                                                                      monkeypatch):
    net.routes[(CDN, "/x.jpg")] = ok
    url, src = "https://%s/x.jpg" % CDN, "https://www.instagram.com/p/race/"
    first = ingest(client, data["key_a"], url, source_url=src)
    real = db.get_media_by_source
    calls = []

    def blind_once(cid, source_url):                   # the second delivery checks too early
        calls.append(1)
        return None if len(calls) == 1 else real(cid, source_url)
    monkeypatch.setattr(db, "get_media_by_source", blind_once)
    again = ingest(client, data["key_a"], url, source_url=src)
    assert again.status_code == 200
    assert again.get_json()["media_id"] == first.get_json()["media_id"]
    assert len(media(data["ca"])) == 1 and len(s3) == 1   # the second copy was removed


# ── the source link ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("source_url", [
    "javascript:alert(document.domain)", " JaVaScRiPt:alert(1)", "data:text/html,<script>",
    "https:alert(1)", "//evil.example/x", "vbscript:msgbox", "https://x.test/\nline",
])
def test_a_source_url_that_is_not_a_web_link_is_refused(client, data, s3, net, source_url):
    net.routes[(CDN, "/x.jpg")] = ok()
    r = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN, source_url=source_url)
    assert r.status_code == 400
    assert upload(client, data["key_a"], source_url=source_url).status_code == 400
    assert net.connects == [] and media(data["ca"]) == []


def test_an_unknown_source_is_refused(client, data, s3, net):
    net.routes[(CDN, "/x.jpg")] = ok()
    for source in ("manual", "facebook", "<b>"):
        r = ingest(client, data["key_a"], "https://%s/x.jpg" % CDN, source=source)
        assert r.status_code == 400
    assert ingest(client, data["key_a"], "https://%s/x.jpg" % CDN,
                  source="TikTok").status_code == 201
    assert media(data["ca"])[0]["source"] == "tiktok"


def test_what_a_request_can_write_to_the_database_is_bounded(client, data, s3, net):
    """The caption is stored in SQLite, on the same small disk as everything else."""
    net.routes[(CDN, "/x.jpg")] = ok
    url = "https://%s/x.jpg" % CDN
    assert ingest(client, data["key_a"], url, caption_hint="x" * 5001).status_code == 400
    assert upload(client, data["key_a"], caption_hint="x" * 5001).status_code == 400
    assert ingest(client, data["key_a"], url, caption_hint="x" * 70000).status_code == 413
    assert net.connects == [] and media(data["ca"]) == []
    assert ingest(client, data["key_a"], url, caption_hint="x" * 5000).status_code == 201


# ── raw media is not publishable ─────────────────────────────────────────────

def _raw_and_ready(data):
    raw = db.add_media(data["ca"], "aaaa1111.jpg", "raw.jpg", "image", 10,
                       storage="s3", s3_key="clients/%d/aaaa1111.jpg" % data["ca"],
                       edit_status="needs_editing", source="instagram",
                       source_url="https://www.instagram.com/p/raw/")
    ready = db.add_media(data["ca"], "bbbb2222.jpg", "ready.jpg", "image", 10,
                         storage="s3", s3_key="clients/%d/bbbb2222.jpg" % data["ca"])
    return raw, ready


def _patch(c, media_id, status):
    return c.patch("/api/media/%d" % media_id, json={"edit_status": status},
                   headers={"X-CSRF-Token": CSRF})


def test_the_picker_offers_only_finished_media(client, data, s3):
    raw, ready = _raw_and_ready(data)
    login_as(client, data["admin"])
    ids = [m["id"] for m in client.get("/api/media/client/%d" % data["ca"]).get_json()]
    assert ids == [ready]
    assert _patch(client, raw, "ready").status_code == 200
    ids = {m["id"] for m in client.get("/api/media/client/%d" % data["ca"]).get_json()}
    assert ids == {raw, ready}


def test_attaching_raw_media_is_refused_until_it_is_marked_ready(client, data, s3):
    raw, _ = _raw_and_ready(data)
    pid = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "t",
                          "caption": "c", "content_type": "photo"})
    login_as(client, data["admin"])
    r = client.post("/api/content/%d/media" % pid, json={"media_id": raw},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 409 and "needs editing" in r.get_json()["error"]
    assert db.get_post_media(pid) == [] and not db.get_post(pid)["image_url"]

    assert _patch(client, raw, "ready").status_code == 200
    r = client.post("/api/content/%d/media" % pid, json={"media_id": raw},
                    headers={"X-CSRF-Token": CSRF})
    assert r.status_code == 200


def test_dispatch_refuses_a_post_carrying_raw_media(client, data, s3, monkeypatch):
    """The picker and attach stop it going on; this stops a file that was put back
    into editing after it was attached, or a raw file's link pasted by hand."""
    raw, ready = _raw_and_ready(data)
    db.upsert_client_webhook(data["ca"], "https://hook.test/a", "secretAAAA", "facebook")
    sent = []
    monkeypatch.setattr(webhooks, "_http_post",
                        lambda url, payload, secret=None: (sent.append(payload), (True, 200, None))[1])
    login_as(client, data["admin"])

    attached = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "t",
                               "caption": "c", "content_type": "photo"})
    assert client.post("/api/content/%d/media" % attached, json={"media_id": ready},
                       headers={"X-CSRF-Token": CSRF}).status_code == 200
    assert _patch(client, ready, "needs_editing").status_code == 200
    ok_, msg = webhooks.dispatch_post(db.get_post(attached))
    assert not ok_ and "needs editing" in msg

    pasted = db.create_post({"client_id": data["ca"], "platform": "facebook", "topic": "t",
                             "caption": "c", "content_type": "photo",
                             "image_url": "https://bucket.test/clients/%d/aaaa1111.jpg?sig=1"
                                          % data["ca"]})
    ok_, msg = webhooks.dispatch_post(db.get_post(pasted))
    assert not ok_ and "needs editing" in msg
    assert sent == []

    assert _patch(client, ready, "ready").status_code == 200
    ok_, _ = webhooks.dispatch_post(db.get_post(attached))
    assert ok_ and len(sent) == 1


# ── the queue ────────────────────────────────────────────────────────────────

def test_the_queue_is_scoped_to_the_viewer(client, data, s3):
    _raw_and_ready(data)
    db.add_media(data["cb"], "cccc3333.jpg", "b-raw.jpg", "image", 10, storage="s3",
                 s3_key="clients/%d/cccc3333.jpg" % data["cb"], edit_status="needs_editing")
    login_as(client, data["user_a"])
    page = client.get("/editing-queue").get_data(as_text=True)
    assert "Ingest Client A" in page and "Ingest Client B" not in page
    assert page.count('class="col-6 col-md-4 col-xl-3 queue-item"') == 1

    login_as(client, data["admin"])
    page = client.get("/editing-queue").get_data(as_text=True)
    assert page.count('queue-item"') == 2

    db.soft_delete_client(data["cb"], actor_user_id=data["admin"], actor_role="admin")
    page = client.get("/editing-queue").get_data(as_text=True)
    assert "Ingest Client B" not in page and page.count('queue-item"') == 1


def test_the_ready_toggle_round_trips_through_the_queue(client, data, s3):
    raw, _ = _raw_and_ready(data)
    login_as(client, data["user_a"])
    assert "raw.jpg" in client.get("/editing-queue").get_data(as_text=True)
    assert _patch(client, raw, "ready").status_code == 200
    assert db.get_media(raw)["edit_status"] == "ready"
    assert 'data-id="%d"' % raw not in client.get("/editing-queue").get_data(as_text=True)
    assert _patch(client, raw, "needs_editing").status_code == 200
    assert 'data-id="%d"' % raw in client.get("/editing-queue").get_data(as_text=True)
    assert _patch(client, raw, "published").status_code == 400

    b_raw = db.add_media(data["cb"], "dddd4444.jpg", "b.jpg", "image", 10,
                         edit_status="needs_editing")
    assert _patch(client, b_raw, "ready").status_code == 403
    assert db.get_media(b_raw)["edit_status"] == "needs_editing"


# ── what the pages actually show ─────────────────────────────────────────────

class _Controls(HTMLParser):
    """Records each element carrying `cls`, with whether any ancestor hides it until
    hover (opacity-0) — a control that only appears on hover does not exist on touch."""
    VOID = {"input", "img", "br", "hr", "meta", "link", "source"}

    def __init__(self, cls):
        super().__init__()
        self.cls, self.stack, self.found = cls, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if self.cls in classes:
            self.found.append((a, any("opacity-0" in cs for _, cs in self.stack)))
        if tag not in self.VOID:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):        # tolerate an unclosed child
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _controls(html, cls):
    p = _Controls(cls)
    p.feed(html)
    return p.found


def test_the_gallery_shows_the_badge_source_and_toggle(client, data, s3):
    raw, ready = _raw_and_ready(data)
    login_as(client, data["user_a"])
    html = client.get("/clients/%d/gallery" % data["ca"]).get_data(as_text=True)
    assert html.count("Needs editing") == 1
    assert "from Instagram" in html
    assert 'href="https://www.instagram.com/p/raw/"' in html
    toggles = _controls(html, "edit-status-toggle")
    assert {a["data-id"]: a["data-status"] for a, _ in toggles} == \
        {str(raw): "needs_editing", str(ready): "ready"}
    assert not any(hidden for _, hidden in toggles)
    assert "Mark as Ready" in html and "Mark as Needs Editing" in html


def test_the_editing_queue_page_shows_each_raw_file_with_its_controls(client, data, s3):
    raw, ready = _raw_and_ready(data)
    login_as(client, data["user_a"])
    html = client.get("/editing-queue").get_data(as_text=True)
    assert "Editing Queue" in html                                  # the nav link and title
    buttons = _controls(html, "mark-ready-btn")
    assert [a["data-id"] for a, _ in buttons] == [str(raw)]
    assert not any(hidden for _, hidden in buttons)
    assert 'href="https://www.instagram.com/p/raw/"' in html
    assert "bi-instagram" in html


def test_a_stored_non_web_source_link_is_never_rendered_as_a_link(client, data, s3):
    """Rows written before the endpoint checked source_url, or by anything else."""
    db.add_media(data["ca"], "eeee5555.jpg", "old.jpg", "image", 10, storage="s3",
                 s3_key="clients/%d/eeee5555.jpg" % data["ca"], edit_status="needs_editing",
                 source="web", source_url="javascript:alert(document.domain)")
    login_as(client, data["admin"])
    for url in ("/clients/%d/gallery" % data["ca"], "/editing-queue"):
        html = client.get(url).get_data(as_text=True)
        assert "old.jpg" in html and "javascript:" not in html
