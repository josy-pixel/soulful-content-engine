"""A caption Instagram will refuse is stopped before it reaches Make.

Post 64 went to Make with 4 hashtags in its caption and 30 more in the hashtags
field. The scenario publishes the two as one caption; Instagram refused it as
"The caption was too long" and the post stayed "approved" and unposted, because
the scenario only reports back on success. Limits: IG User Media reference,
`caption` — 2,200 characters, 30 hashtags, 20 @ tags.
"""
import pytest

import caption_rules
import claude_api
import database as db
import voice_engine as ve
import webhooks

CAPTION_TAGS = "#TalentManagement #CreatorEconomy #FacebookMonetisation #SoulfulManagement"
THIRTY = " ".join("#Tag%02d" % i for i in range(30))


def tags(n, prefix="Tag"):
    return " ".join("#%s%02d" % (prefix, i) for i in range(n))


# ── the limits ───────────────────────────────────────────────────────────────

def test_post_64_is_refused_and_the_message_counts_both_places():
    ok, why = caption_rules.check("instagram", "Twenty years in this industry.\n\n" + CAPTION_TAGS, THIRTY)
    assert ok is False
    assert "34" in why and "4 in the caption" in why and "30 in the hashtags field" in why
    assert "at least 4" in why


def test_thirty_hashtags_in_total_is_allowed():
    ok, _ = caption_rules.check("instagram", "Caption " + tags(4, "Cap"), tags(26))
    assert ok is True


def test_the_character_limit_counts_the_caption_the_hashtags_and_the_space_between():
    ok, _ = caption_rules.check("instagram", "x" * 2190, "#abcdefghi")   # 2190 + 1 + 10 = 2201
    assert ok is False
    ok, _ = caption_rules.check("instagram", "x" * 2189, "#abcdefghi")   # 2200
    assert ok is True


def test_more_than_twenty_mentions_is_refused():
    caption = " ".join("@talent%d" % i for i in range(21))
    ok, why = caption_rules.check("instagram", caption, "")
    assert ok is False and "@ tags" in why


def test_an_email_address_is_not_a_mention_and_a_url_fragment_is_not_a_hashtag():
    caption = "Write to hello@soulful.co.uk or see https://soulful.co.uk/page#section"
    assert caption_rules.MENTION.findall(caption) == []
    assert caption_rules.hashtags_in(caption) == []


def test_facebook_has_no_caption_limit_here():
    ok, _ = caption_rules.check("facebook", "x" * 5000, tags(60))
    assert ok is True


# ── the publish step ─────────────────────────────────────────────────────────

@pytest.fixture()
def cid():
    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in ["post_media", "content_posts", "client_webhooks", "clients"]:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    c = db.create_client({"name": "Caption Client"})
    db.upsert_client_webhook(c, "https://hook.eu1.make.com/caption-test", "s3cret",
                             "facebook,instagram")
    return c


@pytest.fixture()
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(webhooks, "_http_post",
                        lambda url, payload, secret=None: (calls.append(payload) or (True, 200, None)))
    return calls


def _post(cid, platform, caption, hashtags):
    return db.get_post(db.create_post({"client_id": cid, "platform": platform,
                                       "content_type": "photo", "topic": "t",
                                       "caption": caption, "hashtags": hashtags}))


def test_an_instagram_caption_over_the_limit_never_reaches_make(cid, sent):
    ok, why = webhooks.dispatch_post(_post(cid, "instagram", "Caption " + CAPTION_TAGS, THIRTY))
    assert ok is False and "Remove at least 4" in why
    assert sent == []


def test_the_same_caption_still_goes_to_facebook(cid, sent):
    ok, _ = webhooks.dispatch_post(_post(cid, "facebook", "Caption " + CAPTION_TAGS, THIRTY))
    assert ok is True and len(sent) == 1


def test_a_caption_within_the_limits_goes_to_instagram(cid, sent):
    ok, _ = webhooks.dispatch_post(_post(cid, "instagram", "Caption " + CAPTION_TAGS, tags(26)))
    assert ok is True and len(sent) == 1


# ── the generators ───────────────────────────────────────────────────────────

def test_fit_drops_the_captions_own_tags_and_keeps_what_still_fits():
    field = "#talentmanagement #New01 #CreatorEconomy " + tags(30, "More")
    fitted = caption_rules.fit_hashtags("instagram", "Caption " + CAPTION_TAGS, field)
    kept = caption_rules.hashtags_in(fitted)
    assert len(kept) == 26                                   # 30 minus the caption's 4
    assert kept[0] == "New01"                                # most relevant first, order kept
    assert "talentmanagement" not in [k.lower() for k in kept]


def test_fit_leaves_nothing_when_the_caption_already_uses_every_hashtag():
    assert caption_rules.fit_hashtags("instagram", "c " + tags(30, "Cap"), tags(5)) == ""


def test_fit_only_dedupes_where_the_platform_has_no_limit():
    fitted = caption_rules.fit_hashtags("facebook", "c #Same", "#same " + tags(40))
    assert len(caption_rules.hashtags_in(fitted)) == 40


class _Resp:
    def __init__(self, text):
        self.content = [type("B", (), {"text": text})()]


def _fake_messages(text):
    class Messages:
        def create(self, **kw):
            return _Resp(text)
    return Messages()


def test_the_voice_engine_generator_returns_hashtags_that_fit(monkeypatch):
    client = type("C", (), {"messages": _fake_messages("#TalentManagement " + THIRTY)})()
    monkeypatch.setattr(ve, "_client", lambda: client)
    out = ve.generate_hashtags("Soulful", {"platform": "instagram"}, "topic",
                               "Caption " + CAPTION_TAGS)
    kept = caption_rules.hashtags_in(out)
    assert len(kept) == 26 and "TalentManagement" not in kept
    ok, _ = caption_rules.check("instagram", "Caption " + CAPTION_TAGS, out)
    assert ok is True


def test_the_pipeline_generator_returns_hashtags_that_fit(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-sent")
    fake = type("C", (), {"messages": _fake_messages("#CreatorEconomy " + THIRTY)})()
    monkeypatch.setattr(claude_api.anthropic, "Anthropic", lambda **kw: fake)
    out = claude_api.generate_hashtags("Soulful", {}, "instagram", "topic",
                                       "Caption " + CAPTION_TAGS)
    assert len(caption_rules.hashtags_in(out)) == 26
