"""Model tiering — configurable per task type, never hardcoded at the call site.

Defaults per the approved plan; overridable via env so we can switch (e.g. to
Opus for captions after the blind A/B test) without touching code.
"""
import os

# Caption generation and the voice-audit pass run on the same tier by default.
CAPTION_MODEL = os.environ.get('CAPTION_MODEL', 'claude-sonnet-4-6')
AUDIT_MODEL   = os.environ.get('AUDIT_MODEL',   'claude-sonnet-4-6')
# Hashtags are cheap and mechanical.
HASHTAG_MODEL = os.environ.get('HASHTAG_MODEL', 'claude-haiku-4-5')

# Voice-audit: rewrite any draft scoring below this (out of 10).
VOICE_AUDIT_THRESHOLD = int(os.environ.get('VOICE_AUDIT_THRESHOLD', '8'))

# How many audit calls one caption may spend. A rewrite is only adopted when an
# audit remains to score it, so this is also the cap on rewrites + 1: at 3, the
# draft is scored, rewritten, rescored, rewritten, rescored. The returned score
# always belongs to the returned text.
VOICE_MAX_AUDITS = int(os.environ.get('VOICE_MAX_AUDITS', '3'))

# Engine debug: when '1', generation returns the full system prompt and per-call
# token usage so we can verify full voice injection and prompt caching. Hidden
# in normal production (env unset). Token usage is logged to stdout regardless.
DEBUG_ENGINE = os.environ.get('DEBUG_ENGINE', '') == '1'
