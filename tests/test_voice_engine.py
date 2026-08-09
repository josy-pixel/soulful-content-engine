"""Voice engine: the rulebook, and the score/caption pairing.

The Anthropic client is stubbed — these test the engine's own logic, which is
where all three bugs lived. No network, no API key needed.
"""
import json

import pytest

import config
import voice_engine as ve


DOC = "Never use emoji. Never use the word 'journey'. Short, steady sentences."
SAMPLES = ["The work speaks. That's the whole strategy.",
           "Ten years in, still the same answer."]


# ── the rulebook ──────────────────────────────────────────────────────────────

def test_unset_emoji_does_not_inject_a_contradicting_default():
    """The bug: an unset emoji_usage fell through to 'moderate' = 3-5 emojis,
    contradicting a document that bans them. The writer obeyed, the auditor
    then marked it down, and regenerating could never escape it."""
    text, deferred = ve.build_rulebook("Soulful", DOC, SAMPLES, {})
    assert "3–5 emojis" not in text
    assert "emoji_usage" in deferred
    assert "caption_length" in deferred


def test_voice_document_is_declared_authoritative():
    text, _ = ve.build_rulebook("Soulful", DOC, SAMPLES, {})
    assert "PRECEDENCE" in text
    assert "follow the DOCUMENT" in text
    assert DOC in text                      # injected whole, never summarised


def test_explicit_settings_are_kept_but_marked_subordinate():
    text, deferred = ve.build_rulebook(
        "Soulful", DOC, SAMPLES, {"emoji_usage": "minimal", "caption_length": "short"})
    assert "1–2 emojis maximum" in text
    assert "the voice document overrides this" in text
    assert deferred == []


def test_without_a_document_the_defaults_still_apply():
    """No document means nothing to defer to — behaviour must not regress for
    clients who have not written one yet."""
    text, deferred = ve.build_rulebook("Soulful", "", [], {})
    assert "3–5 emojis" in text
    assert "150–300 characters" in text
    assert deferred == []


def test_banned_words_reach_the_rulebook():
    text, _ = ve.build_rulebook("Soulful", DOC, [],
                                {"avoid_words": json.dumps(["journey", "unlock"])})
    assert "NEVER USE" in text
    assert "journey" in text


def test_auditor_and_writer_get_the_same_rulebook(monkeypatch):
    """The auditor used to receive only the document and samples, so it policed
    banned words and emoji rules it had to guess at."""
    seen = {}

    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"text": text})()]
            self.usage = None

    class Messages:
        def create(self, **kw):
            seen[kw["model"]] = kw["system"][0]["text"]
            return Resp('{"score": 9, "notes": "ok", "rewritten": null}')

    class Client:
        messages = Messages()

    monkeypatch.setattr(ve, "_client", lambda: Client())
    bv = {"avoid_words": json.dumps(["journey"])}
    rulebook, _ = ve.build_rulebook("Soulful", DOC, SAMPLES, bv)
    ve.audit_caption("Soulful", rulebook, "some caption")

    auditor_prompt = seen[config.AUDIT_MODEL]
    assert DOC in auditor_prompt
    assert "journey" in auditor_prompt          # banned list reaches the auditor
    assert SAMPLES[0] in auditor_prompt


# ── the score must describe the caption returned ─────────────────────────────

class FakeAuditor:
    """Returns a scripted (score, rewritten) per call."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def __call__(self, client_name, rulebook, caption):
        self.seen.append(caption)
        score, rewritten = self.script.pop(0)
        return score, f"notes for {score}", rewritten, None, {"input": 1}


def test_a_rewrite_is_rescored_not_paired_with_the_old_score(monkeypatch):
    """THE bug. Old code returned the rewrite with the draft's score, so the
    badge could only read 8+ when no rewrite had happened."""
    fake = FakeAuditor([(3, "rewritten text"), (9, None)])
    monkeypatch.setattr(ve, "audit_caption", fake)

    final, score, notes, err, usages, attempts = ve.audit_to_threshold(
        "Soulful", "RULEBOOK", "draft text")

    assert final == "rewritten text"
    assert score == 9                       # the rewrite's own score, not the 3
    assert fake.seen == ["draft text", "rewritten text"]
    assert len(attempts) == 2
    assert err is None


def test_a_good_first_draft_is_not_rewritten(monkeypatch):
    fake = FakeAuditor([(9, None)])
    monkeypatch.setattr(ve, "audit_caption", fake)
    final, score, _, _, _, attempts = ve.audit_to_threshold(
        "Soulful", "RULEBOOK", "draft text")
    assert final == "draft text" and score == 9
    assert len(attempts) == 1


def test_never_returns_an_unscored_rewrite(monkeypatch):
    """With the budget exhausted, keep the text that WAS scored rather than
    adopt a rewrite nobody judged — that would recreate the original bug."""
    monkeypatch.setattr(config, "VOICE_MAX_AUDITS", 2)
    fake = FakeAuditor([(3, "rewrite one"), (4, "rewrite two")])
    monkeypatch.setattr(ve, "audit_caption", fake)

    final, score, _, _, _, attempts = ve.audit_to_threshold(
        "Soulful", "RULEBOOK", "draft text")

    assert final == "rewrite one"           # the last text that was scored
    assert score == 4                       # and that is its score
    assert "rewrite two" not in fake.seen
    assert len(attempts) == 2


def test_stops_when_the_auditor_offers_no_rewrite(monkeypatch):
    """A low score with no rewrite means the auditor had nothing better —
    looping again would just burn calls."""
    fake = FakeAuditor([(4, None)])
    monkeypatch.setattr(ve, "audit_caption", fake)
    final, score, _, _, _, attempts = ve.audit_to_threshold(
        "Soulful", "RULEBOOK", "draft text")
    assert final == "draft text" and score == 4
    assert len(attempts) == 1


def test_audit_error_returns_the_caption_unharmed(monkeypatch):
    def boom(client_name, rulebook, caption):
        return None, "", None, "Claude API error: nope", None

    monkeypatch.setattr(ve, "audit_caption", boom)
    final, score, _, err, _, _ = ve.audit_to_threshold(
        "Soulful", "RULEBOOK", "draft text")
    assert final == "draft text"
    assert err and score is None
