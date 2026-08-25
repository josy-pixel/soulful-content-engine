"""Templates hold references, not URLs.

A post stores s3://key so it does not rot when a signed link expires — which means
every template that renders it has to resolve it first. Miss one and the page shows
a broken image, which is exactly what happened the first time.
"""
import pytest

import app as flask_app
import s3_media


@pytest.fixture()
def signed(monkeypatch):
    monkeypatch.setattr(s3_media, "presign_view",
                        lambda key, expires=None: f"https://bucket.example/{key}?sig=abc")


def test_a_reference_is_resolved_into_a_usable_link(signed):
    out = flask_app.media_src("s3://clients/7/abc.mp4")
    assert out == "https://bucket.example/clients/7/abc.mp4?sig=abc"
    assert not out.startswith("s3://")          # never handed to the browser raw


def test_on_disk_media_passes_through_unchanged(signed):
    assert flask_app.media_src("/uploads/7/old.jpg") == "/uploads/7/old.jpg"


def test_an_external_link_passes_through_unchanged(signed):
    assert flask_app.media_src("https://youtu.be/xyz") == "https://youtu.be/xyz"


def test_empty_stays_empty(signed):
    assert flask_app.media_src("") == ""
    assert flask_app.media_src(None) == ""


@pytest.mark.parametrize("ref,expected", [
    ("s3://clients/7/clip.mp4", True),
    ("s3://clients/7/clip.MOV", True),
    ("/uploads/7/clip.webm", True),
    ("/uploads/7/photo.jpg", False),
    ("https://youtu.be/xyz", False),
    ("", False),
])
def test_video_is_recognised_wherever_it_is_stored(ref, expected):
    """A video in an <img> tag is a permanently broken image, so the template
    has to know before it renders."""
    assert flask_app.is_video(ref) is expected


def test_the_post_page_renders_a_reference_without_leaking_it(signed):
    """The whole point: nothing beginning with s3:// reaches the HTML."""
    tmpl = flask_app.app.jinja_env.from_string(
        '{% if post.image_url | is_video %}'
        '<video src="{{ post.image_url | media_src }}"></video>'
        '{% else %}<img src="{{ post.image_url | media_src }}">{% endif %}'
    )
    html = tmpl.render(post={"image_url": "s3://clients/7/abc.mp4"})
    assert "s3://" not in html
    assert "<video" in html                      # a video, not a broken <img>
    assert "https://bucket.example/clients/7/abc.mp4?sig=abc" in html
