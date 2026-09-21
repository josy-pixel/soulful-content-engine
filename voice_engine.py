"""Voice-faithful caption generation — the #1 problem.

Every generation injects the client's ENTIRE voice document (never truncated,
never summarised), all of their real sample captions, brand-voice notes, banned
words, and the weekly direction. A second voice-audit call scores the draft 1-10
against that same context and rewrites anything below the threshold.

Three rules this module exists to hold:

1. The voice document is AUTHORITATIVE. Where the per-client settings rows
   (emoji usage, caption length, platform conventions) disagree with it, the
   document wins. Previously an unset emoji_usage silently defaulted to
   "3-5 emojis", so a document that banned emoji still produced emoji — and the
   auditor then marked the caption down for it. Regenerating could never fix
   that, because the contradiction was baked into the instructions.

2. The auditor judges against the SAME rulebook the writer was given. It used
   to receive only the document and samples, so it penalised banned words and
   emoji policy it had to infer.

3. The score always describes the caption actually returned. The old code
   returned the rewrite paired with the ORIGINAL draft's score, so the badge
   could only ever read 8+ when no rewrite had happened at all.

The rulebook is the cached prefix for both the writer and the auditor, so every
post after the first in a batch reads it back at ~10% of input cost.
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


def build_rulebook(client_name, voice_document, sample_captions, brand_voice,
                   weekly_direction=''):
    """The single source of voice truth, shared by the writer and the auditor.

    Returns (rulebook_text, deferred) where `deferred` names the settings that
    were withheld because the voice document governs them instead — surfaced so
    a silent override is never invisible.
    """
    bv = brand_voice or {}
    keywords = _json_list(bv.get('keywords'))
    banned = _json_list(bv.get('avoid_words'))
    platform = bv.get('platform', 'general')
    has_doc = bool(voice_document)
    deferred = []

    parts = [
        f"You write social media captions AS {client_name} — in their own voice, "
        f"not as a social media manager writing about them. The reader should not "
        f"be able to tell a tool wrote it.",
    ]

    if has_doc:
        parts.append(
            "\n=== PRECEDENCE ===\n"
            "The BRAND VOICE DOCUMENT below is authoritative. Where anything else "
            "in this prompt — voice notes, platform conventions, length guidance, "
            "emoji guidance — disagrees with the document, follow the DOCUMENT and "
            "ignore the conflicting instruction."
        )
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

    # Length and emoji: only stated when the client actually set them. An unset
    # value used to fall through to a hardcoded default that could contradict the
    # document outright — the emoji case is exactly how this bug was found.
    length = (bv.get('caption_length') or '').strip()
    emoji = (bv.get('emoji_usage') or '').strip()

    if length in LENGTH_GUIDE:
        parts.append("LENGTH: " + LENGTH_GUIDE[length]
                     + (" (the voice document overrides this)" if has_doc else ""))
    elif has_doc:
        deferred.append('caption_length')
    else:
        parts.append("LENGTH: " + LENGTH_GUIDE['medium'])

    if emoji in EMOJI_GUIDE:
        parts.append("EMOJI: " + EMOJI_GUIDE[emoji]
                     + (" (the voice document overrides this)" if has_doc else ""))
    elif has_doc:
        deferred.append('emoji_usage')
    else:
        parts.append("EMOJI: " + EMOJI_GUIDE['moderate'])

    return "\n".join(parts), deferred


def generate_caption(client_name, brand_voice, topic, voice_document='',
                     sample_captions=None, weekly_direction='', extra_context=''):
    """Returns (caption, error, usage, system_text, deferred)."""
    client = _client()
    rulebook, deferred = build_rulebook(client_name, voice_document,
                                        sample_captions or [], brand_voice,
                                        weekly_direction)
    system_text = rulebook + "\n\nReturn ONLY the caption text — no labels, no preamble, no quotes."
    if not client:
        return None, 'ANTHROPIC_API_KEY not set.', None, system_text, deferred

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
        return resp.content[0].text.strip(), None, _usage(resp, 'caption'), system_text, deferred
    except anthropic.APIError as e:
        return None, f'Claude API error: {str(e)}', None, system_text, deferred


def audit_caption(client_name, rulebook, caption):
    """Score one caption against the rulebook. Judgement only — the caller
    decides what to keep, so a score is never paired with different text.

    Returns (score, notes, rewritten, error, usage).
    """
    client = _client()
    if not client:
        return None, '', None, 'ANTHROPIC_API_KEY not set.', None

    # The rulebook goes in the cached system block, not the user turn: it is the
    # same on every post in a batch, and it is far too big to pay for each time.
    system_text = (
        "You are a strict brand-voice auditor. You judge captions against the "
        "rulebook below — the SAME rulebook the writer was given, so you must "
        "not invent rules it does not contain, and must not penalise anything it "
        "permits.\n\n" + rulebook + "\n\nReturn only valid JSON."
    )

    prompt = (
        f"DRAFT CAPTION TO AUDIT (for {client_name}):\n{caption}\n\n"
        f"Score it from 1 to 10 on how faithfully it matches this person's voice "
        f"— signature phrases, punctuation habits, sentence rhythm, banned words, "
        f"and overall tone — judged against the rulebook above. If the score is "
        f"below {config.VOICE_AUDIT_THRESHOLD}, rewrite it so it scores at least "
        f"{config.VOICE_AUDIT_THRESHOLD}, keeping the same message.\n\n"
        f"Return ONLY a JSON object, no other text:\n"
        f'{{"score": <1-10>, "notes": "<what matched and what you fixed>", '
        f'"rewritten": "<the improved caption, or null if it already scores '
        f'{config.VOICE_AUDIT_THRESHOLD}+>"}}'
    )

    try:
        resp = client.messages.create(
            model=config.AUDIT_MODEL,
            max_tokens=1500,
            system=[{'type': 'text', 'text': system_text,
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
        rewritten = rewritten.strip() if isinstance(rewritten, str) and rewritten.strip() else None
        return score, notes, rewritten, None, u
    except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
        return None, '', None, f'Audit parse error: {str(e)}', None
    except anthropic.APIError as e:
        return None, '', None, f'Claude API error: {str(e)}', None


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


def audit_to_threshold(client_name, rulebook, caption):
    """Audit, rewrite, and RE-audit until the text clears the threshold or the
    attempt budget runs out.

    The invariant: the score returned always describes the caption returned. A
    rewrite is only adopted when there is still an audit left to score it with —
    otherwise the last rewrite would go out unscored, which is the bug this
    replaces. Returns (caption, score, notes, error, usages, attempts).
    """
    current = caption
    score = None
    notes = ''
    usages = []
    attempts = []
    budget = max(1, config.VOICE_MAX_AUDITS)

    for i in range(budget):
        s, n, rewritten, err, u = audit_caption(client_name, rulebook, current)
        if err:
            return current, score, notes, err, usages, attempts
        if u:
            usages.append(u)
        score, notes = s, n                      # always describes `current`
        attempts.append({'attempt': i + 1, 'score': s, 'rewritten': bool(rewritten)})
        if s is not None and s >= config.VOICE_AUDIT_THRESHOLD:
            break
        if not rewritten:
            break                                # auditor offered no improvement
        if i == budget - 1:
            break                                # no audit left to score a rewrite
        current = rewritten

    return current, score, notes, None, usages, attempts


def generate_post(client_name, brand_voice, topic, voice_document='',
                  sample_captions=None, weekly_direction='', extra_context='', debug=False):
    """Full pipeline: caption -> voice audit (with re-scoring) -> hashtags.

    Returns a dict with caption, voice_score, voice_audit, voice_attempts,
    voice_deferred_settings, hashtags, error. When debug, also returns per-call
    token usage and the full system prompt.
    """
    sample_captions = sample_captions or []
    caption, err, cap_usage, system_text, deferred = generate_caption(
        client_name, brand_voice, topic, voice_document,
        sample_captions, weekly_direction, extra_context)
    if err:
        return {'error': err}

    rulebook, _ = build_rulebook(client_name, voice_document, sample_captions,
                                 brand_voice, weekly_direction)
    final, score, notes, aerr, audit_usages, attempts = audit_to_threshold(
        client_name, rulebook, caption)
    hashtags = generate_hashtags(client_name, brand_voice, topic, final)

    result = {
        'caption': final,
        'voice_score': score,
        'voice_audit': notes if not aerr else f'(audit skipped: {aerr})',
        'voice_attempts': attempts,
        'voice_deferred_settings': deferred,
        'hashtags': hashtags,
        'error': None,
    }
    if debug:
        result['usage'] = {'caption': cap_usage, 'audit': audit_usages}
        result['system_prompt'] = system_text
        result['system_prompt_chars'] = len(system_text)
        result['voice_document_chars'] = len(voice_document or '')
    return result


# ── Reel repurposer ───────────────────────────────────────────────────────────
# The reel-repurposer skill, generalised per its own "per-talent guardrails"
# note: platform mechanics (the failure-mode diagnosis, the retention
# structure, the compliance gate) are fixed and never change; only the
# talent's voice/guardrails and this specific post's own measured performance
# are swapped in per call. This is the complement to a from-scratch builder —
# it salvages and re-hooks something that already has an edit (and ideally
# performance data) behind it, rather than scripting something new. Claude
# cannot watch footage or hear audio, so this always works from text.

REEL_REPURPOSE_RULES = """
You are diagnosing and re-cutting an EXISTING piece of video — a published underperformer, an old reel being mined for a remake, or footage that was cut once and didn't land. You do not watch or hear the footage; work only from the transcript/shot list and the performance data provided. This is a diagnosis-and-rebuild task, not a from-scratch script — ground every recommendation in the actual source material and actual numbers given, never invent footage, dialogue or metrics that weren't provided.

=== STEP 1 — DIAGNOSE BEFORE TOUCHING THE EDIT ===
Run the source against these known Facebook-specific failure modes before assuming the fix is "better editing":
1. Audio ineligibility — licensed/commercial music kills earnings outright regardless of performance (0 qualified views, $0.00, even on a well-watched reel). Check the audio flag first; if it's licensed, this is a Sound Collection swap and repost, not a re-edit.
2. Length bucket — sub-60s reels are a monetisation trap: they can retain well and still earn almost nothing. The earning sweet spot is 70-90s (the 60-99s band typically drives the large majority of reel earnings); 3-5 min only works for genuine "come with me" journey content.
3. Weak or buried hook — the single biggest predictor of a flop is not front-loading the reveal or emotional peak before roughly the 5-8 second mark, where the steepest audience drop-off happens. A flat, generic opener ("can you relate?") with no promise of a payoff is a hook failure, not an editing-craft failure.
4. Low-stakes topic — sometimes the edit and hook are both fine and the topic itself just isn't interesting enough to sustain a rebuild. Flag these for retirement rather than a remake.
5. Wasted short clip — a strong sub-30s moment that never got built out is an extension candidate, not a flop.
State the diagnosis explicitly before proposing a fix.

=== STEP 2 — CLASSIFY THE REPURPOSING ACTION ===
Based on the diagnosis, assign exactly one action:
- Re-hook only — the body of the edit works, the opening doesn't; rebuild the first 5-8 seconds and keep the rest.
- Full re-cut — retrim to the target length bucket, restructure pacing, rebuild the hook.
- Extend — grow a wasted sub-30s moment into a full 70-90s build.
- Audio swap — re-export with monetisation-safe audio only; no creative changes needed.
- Retire — low-stakes topic; not worth remaking. Say so plainly rather than forcing a rebuild.
- Multiply — the source is strong enough to also yield a 48-hour pull-clip and/or a quote static.
Every remake must be a genuinely new edit (new first frame, new on-screen text) — never a straight re-upload of the same cut.

=== STEP 3 — REWRITE THE HOOK (3-5 RANKED OPTIONS) ===
For any action other than Retire, write 3-5 ranked cold-open options. Each must:
- Front-load the reveal, confession, or emotional peak — no scene-setting before it
- Create a specific curiosity gap rather than a generic tease ("the real reason X happened.." beats "you won't believe what happened")
- Be deliverable within the first 3 seconds of screen time
- Match the talent's locked brand voice exactly (pull specific banned words, punctuation rules and tone markers from the talent voice section below)
The underlying hook model: the first 3 seconds must promise a specific, resolvable payoff — a "3-second world" the viewer wants closed — never a vague tease.

=== STEP 4 — REBUILD THE RETENTION STRUCTURE ===
Map the rebuilt edit to this beat structure, scaled to a 70-90s reel:
- 0-8s: the hook lands, no dead air, no throat-clearing
- ~1/3 mark: a re-engagement beat — a new piece of information, a twist, or a payoff tease that gives a reason to keep watching past the point most viewers would drop
- Escalation, not deceleration: each beat should raise stakes or interest versus the one before it; no dull or filler beats anywhere in the cut
- Ending: cut cleanly on the peak or payoff rather than winding down — protect retention through the final second rather than signalling "this is ending"
Produce this as a shot-by-shot timeline (timestamp, visual, on-screen text, voiceover/caption line) so an editor can cut directly from it. Skip this and Step 3 if the action is Retire.

=== STEP 5 — COMPLIANCE GATE ("REEL FILTER") ===
Before the package ships, check it against these hard stops and state pass/fail on each:
- Monetisation-safe audio only (Sound Collection or the talent's own voice; never licensed/trending/explicit)
- Length in the 70-90s band for a main reel (never sub-60s as a main reel — only as a 48-hour pull-clip)
- Passes the talent's brand/safety guardrails (banned topics, no exes, required tagging, etc. — see talent voice below)
- Note qualified-views as the metric to re-check 48 hours after posting, not raw views

=== STEP 6 — MULTIPLICATION PLAN ===
If the source is strong, specify what else it should generate: a 48-hour pull-clip (a sub-30s highlight posted after the main reel, never as a main reel itself) and/or a quote static pulled from the strongest line. State explicitly if a source is NOT strong enough to multiply — not every remake needs to produce three assets.

Where a rule above conflicts with this specific post's own measured performance data, the post's own data wins — it reflects this account's actual payout behaviour, not general advice.
"""

REEL_REPURPOSE_OUTPUT = """

=== OUTPUT PACKAGE ===
Deliver, in this order:
1. DIAGNOSIS — which failure mode(s) apply, stated explicitly, grounded in the performance data and source material provided.
2. ASSIGNED ACTION — one of: Re-hook only / Full re-cut / Extend / Audio swap / Retire / Multiply. Justify the choice.
3. RANKED HOOK OPTIONS — 3-5 (omit if the action is Retire), each an exact first-3-second line.
4. SHOT-BY-SHOT RE-CUT TIMELINE — timestamp, visual, on-screen text, voiceover/caption line, for each beat (omit if the action is Retire).
5. ON-SCREEN TEXT — all burned-in text for sound-off viewing.
6. COMPLIANCE CHECK — pass/fail against each of the four hard stops in Step 5, with a one-line reason for any fail.
7. MULTIPLICATION PLAN — the 48-hour pull-clip and/or quote static, or an explicit "not strong enough to multiply" if that's the honest call.
8. NOTES FOR THE EDITOR — anything else they need, stated plainly.

Write in clear markdown with numbered headers matching the structure above. Be specific and concrete — this is a finished package a human editor builds from with no further questions.
"""


def build_reel_repurpose(client_name, brand_voice, voice_document, sample_captions,
                         source_material, performance_summary, extra_context='',
                         debug=False):
    """Diagnose and re-cut an existing/underperforming reel into a re-hooked,
    retention-structured, compliance-checked repurposing package.

    source_material: the video's transcript or shot list — required, Claude
    cannot watch footage. performance_summary: this post's actual metrics as
    text. extra_context: anything the app doesn't track structurally yet
    (audio eligibility, known length, a retention/drop-off note from Facebook
    Insights, why this is being revisited).

    Returns {'package': ..., 'error': None} or {'error': ...}.
    """
    client = _client()
    if not client:
        return {'error': 'ANTHROPIC_API_KEY not set.'}
    if not (source_material or '').strip():
        return {'error': "Source material is required — Claude can't watch video, "
                         "so describe or transcribe the existing footage first."}

    voice_block = ''
    if voice_document:
        voice_block = ("\n=== TALENT VOICE & GUARDRAILS (authoritative — banned words, "
                       "pronoun/voice rules, cast-tagging, sign-off style all live here; "
                       "match this talent's locked voice exactly in every hook and line "
                       "of on-screen text) ===\n" + voice_document)
    if sample_captions:
        voice_block += ("\n\n=== REAL CAPTIONS THIS PERSON WROTE (match their phrasing, "
                        "rhythm, punctuation, emoji habits) ===\n" +
                        "\n\n".join("- %s" % c for c in sample_captions))
    if not voice_block:
        bv = brand_voice or {}
        notes = []
        if bv.get('tone'):
            notes.append("Tone: %s" % bv['tone'])
        if bv.get('style'):
            notes.append("Style: %s" % bv['style'])
        voice_block = "\n=== TALENT VOICE ===\n" + ("\n".join(notes) or
                      "No voice document on file yet — write in a warm, direct, honest register.")

    system_text = (
        ("You are diagnosing and repurposing a reel for %s.\n" % client_name) +
        REEL_REPURPOSE_RULES + voice_block + REEL_REPURPOSE_OUTPUT
    )

    user_message = ("SOURCE MATERIAL (transcript / shot list of the existing video):\n\n"
                    + source_material)
    user_message += ("\n\nTHIS POST'S ACTUAL PERFORMANCE:\n"
                     + (performance_summary or 'No performance data available.'))
    if extra_context:
        user_message += ("\n\nADDITIONAL CONTEXT (audio eligibility, known length, "
                         "retention notes, why this is being revisited):\n" + extra_context)

    try:
        resp = client.messages.create(
            model=config.CAPTION_MODEL,
            max_tokens=6144,
            system=[{'type': 'text', 'text': system_text,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': user_message}],
        )
        result = {'package': resp.content[0].text.strip(), 'error': None}
        if debug:
            result['usage'] = _usage(resp, 'reel_repurpose')
            result['system_prompt'] = system_text
        return result
    except anthropic.APIError as e:
        return {'error': 'Claude API error: %s' % str(e)}
