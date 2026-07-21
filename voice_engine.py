"""Voice-faithful caption generation — the #1 problem.

Every generation injects the client's ENTIRE voice document (never truncated,
never summarised), all of their real sample captions, brand-voice notes, banned
words, and the weekly direction. A second voice-audit call scores the draft 1-10
against the voice document and rewrites anything below the threshold.

The stable prefix (voice document + samples + brand notes + platform rules) is
marked with cache_control so every post in a batch after the first reads it from
cache at ~10% of input cost. The volatile per-post topic goes in the user turn.
"""
import os
import json
import logging

import anthropic

import config
from claude_api import PLATFORM_GUIDES, LENGTH_GUIDE, EMOJI_GUIDE

log = logging.getLogger('voice')
if not log.handlers:
    logging.basicConfig(level=logging.INFO)


def _client():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None
    return anthropic.Anthropic(api_key=api_key)


def _usage(resp, label):
    """Extract + log token usage so prompt caching is verifiable in the logs.
    cache_read > 0 on the 2nd+ call in a batch means the cached prefix was reused."""
    u = getattr(resp, 'usage', None)
    d = {
        'input':       getattr(u, 'input_tokens', 0) or 0,
        'cache_write': getattr(u, 'cache_creation_input_tokens', 0) or 0,
        'cache_read':  getattr(u, 'cache_read_input_tokens', 0) or 0,
        'output':      getattr(u, 'output_tokens', 0) or 0,
    }
    log.info('[voice] %s usage: input=%d cache_write=%d cache_read=%d output=%d',
             label, d['input'], d['cache_write'], d['cache_read'], d['output'])
    return d


def _json_list(raw):
    try:
        v = json.loads(raw or '[]')
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _voice_system_text(client_name, voice_document, sample_captions, brand_voice, weekly_direction=''):
    """The full, stable voice context. This is the cached prefix — it must be
    identical for every post in a batch so the cache is reused."""
    keywords = _json_list((brand_voice or {}).get('keywords'))
    banned = _json_list((brand_voice or {}).get('avoid_words'))
    platform = (brand_voice or {}).get('platform', 'general')

    parts = [
        f"You write social media captions AS {client_name} — in their own voice, "
        f"not as a social media manager writing about them. The reader should not "
        f"be able to tell a tool wrote it.",
    ]

    if voice_document:
        parts.append(
            "\n=== BRAND VOICE DOCUMENT (authoritative — follow it exactly, "
            "in full) ===\n" + voice_document
        )

    if sample_captions:
        joined = "\n\n".join(f"- {c}" for c in sample_captions)
        parts.append(
            "\n=== REAL CAPTIONS THIS PERSON WROTE (match their phrasing, rhythm, "
            "punctuation, and emoji habits) ===\n" + joined
        )

    # Brand-voice notes (secondary to the document, but useful)
    bv = brand_voice or {}
    notes = []
    if bv.get('tone'):
        notes.append(f"- Tone: {bv['tone']}")
    if bv.get('style'):
        notes.append(f"- Style: {bv['style']}")
    if bv.get('target_audience'):
        notes.append(f"- Audience: {bv['target_audience']}")
    if keywords:
        notes.append(f"- Weave in naturally when they fit: {', '.join(keywords)}")
    if notes:
        parts.append("\n=== VOICE NOTES ===\n" + "\n".join(notes))

    if banned:
        parts.append("\n=== NEVER USE THESE WORDS/PHRASES ===\n" + ", ".join(banned))

    if weekly_direction:
        parts.append("\n=== THIS WEEK'S CREATIVE DIRECTION ===\n" + weekly_direction)

    parts.append("\n=== PLATFORM ===\n" + PLATFORM_GUIDES.get(platform, PLATFORM_GUIDES['general']))
    parts.append("LENGTH: " + LENGTH_GUIDE.get(bv.get('caption_length', 'medium'), LENGTH_GUIDE['medium']))
    parts.append("EMOJI: " + EMOJI_GUIDE.get(bv.get('emoji_usage', 'moderate'), EMOJI_GUIDE['moderate']))
    parts.append("\nReturn ONLY the caption text — no labels, no preamble, no quotes.")

    return "\n".join(parts)


def generate_caption(client_name, brand_voice, topic, voice_document='',
                     sample_captions=None, weekly_direction='', extra_context=''):
    """Returns (caption, error, usage, system_text). Injects the full voice
    context, cached."""
    client = _client()
    system_text = _voice_system_text(client_name, voice_document, sample_captions or [],
                                     brand_voice, weekly_direction)
    if not client:
        return None, 'ANTHROPIC_API_KEY not set.', None, system_text

    user_message = f"Write a caption for this post: {topic}"
    if extra_context:
        user_message += f"\n\nAdditional context: {extra_context}"

    try:
        resp = client.messages.create(
            model=config.CAPTION_MODEL,
            max_tokens=1024,
            system=[{'type': 'text', 'text': system_text,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': user_message}],
        )
        return resp.content[0].text.strip(), None, _usage(resp, 'caption'), system_text
    except anthropic.APIError as e:
        return None, f'Claude API error: {str(e)}', None, system_text


def audit_caption(client_name, voice_document, sample_captions, caption):
    """Second pass. Scores the draft 1-10 against the voice document and rewrites
    anything below config.VOICE_AUDIT_THRESHOLD. Returns
    (final_caption, score, notes, error)."""
    client = _client()
    if not client:
        return caption, None, '', 'ANTHROPIC_API_KEY not set.', None

    ctx = []
    if voice_document:
        ctx.append("VOICE DOCUMENT:\n" + voice_document)
    if sample_captions:
        ctx.append("REAL CAPTIONS:\n" + "\n".join(f"- {c}" for c in sample_captions))
    context = "\n\n".join(ctx) if ctx else "(no voice document provided)"

    prompt = (
        f"You are a voice-fidelity auditor for {client_name}.\n\n"
        f"{context}\n\n"
        f"DRAFT CAPTION TO AUDIT:\n{caption}\n\n"
        f"Score the draft from 1 to 10 on how faithfully it matches this person's "
        f"voice — signature phrases, punctuation habits, sentence rhythm, banned "
        f"words, and overall tone. If the score is below {config.VOICE_AUDIT_THRESHOLD}, "
        f"rewrite it so it scores at least {config.VOICE_AUDIT_THRESHOLD}, keeping the "
        f"same message.\n\n"
        f"Return ONLY a JSON object, no other text:\n"
        f'{{"score": <1-10>, "notes": "<what matched and what you fixed>", '
        f'"rewritten": "<the improved caption, or null if the draft already scores '
        f'{config.VOICE_AUDIT_THRESHOLD}+>"}}'
    )

    try:
        resp = client.messages.create(
            model=config.AUDIT_MODEL,
            max_tokens=1500,
            system=[{'type': 'text',
                     'text': 'You are a strict brand-voice auditor. Return only valid JSON.',
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': prompt}],
        )
        u = _usage(resp, 'audit')
        raw = resp.content[0].text.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        data = json.loads(raw)
        score = int(data.get('score', 0))
        notes = str(data.get('notes', ''))
        rewritten = data.get('rewritten')
        final = rewritten.strip() if (rewritten and score < config.VOICE_AUDIT_THRESHOLD) else caption
        return final, score, notes, None, u
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        return caption, None, '', f'Audit parse error: {str(e)}', None
    except anthropic.APIError as e:
        return caption, None, '', f'Claude API error: {str(e)}', None


def generate_hashtags(client_name, brand_voice, topic, caption):
    client = _client()
    if not client:
        return ''
    keywords = _json_list((brand_voice or {}).get('keywords'))
    platform = (brand_voice or {}).get('platform', 'general')
    count = 5 if platform == 'linkedin' else (30 if platform == 'instagram' else 10)
    prompt = (
        f"Generate {count} relevant hashtags for a {platform} post by {client_name}.\n"
        f"Topic: {topic}\nBrand keywords: {', '.join(keywords)}\n"
        f"Caption excerpt: {caption[:200]}\n\n"
        f"Return ONLY the hashtags on one line, space-separated, each starting with #."
    )
    try:
        resp = client.messages.create(
            model=config.HASHTAG_MODEL,
            max_tokens=256,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return ''


def generate_post(client_name, brand_voice, topic, voice_document='',
                  sample_captions=None, weekly_direction='', extra_context='', debug=False):
    """Full pipeline: caption -> voice audit -> hashtags.
    Returns a dict with caption, voice_score, voice_audit, hashtags, error.
    When debug, also returns 'usage' (per-call token counts, incl. cache hits)
    and 'system_prompt' (the full injected prompt) for verification."""
    sample_captions = sample_captions or []
    caption, err, cap_usage, system_text = generate_caption(
        client_name, brand_voice, topic, voice_document,
        sample_captions, weekly_direction, extra_context)
    if err:
        return {'error': err}

    final, score, notes, aerr, audit_usage = audit_caption(
        client_name, voice_document, sample_captions, caption)
    hashtags = generate_hashtags(client_name, brand_voice, topic, final)

    result = {
        'caption': final,
        'voice_score': score,
        'voice_audit': notes if not aerr else f'(audit skipped: {aerr})',
        'hashtags': hashtags,
        'error': None,
    }
    if debug:
        result['usage'] = {'caption': cap_usage, 'audit': audit_usage}
        result['system_prompt'] = system_text
        result['system_prompt_chars'] = len(system_text)
        result['voice_document_chars'] = len(voice_document or '')
    return result
