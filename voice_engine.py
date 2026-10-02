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
import re
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


def planning_constraints(voice_document, brand_voice):
    """The part of the rulebook a topic planner must respect: the voice document
    and the banned words, each only if the client set it. Nothing else — an
    unset setting adds nothing, so the planner is never handed a default that
    the document contradicts."""
    parts = []
    if voice_document:
        parts.append("=== BRAND VOICE DOCUMENT (authoritative) ===\n" + voice_document)
    banned = _json_list((brand_voice or {}).get('avoid_words'))
    if banned:
        parts.append("=== NEVER USE THESE WORDS/PHRASES ===\n" + ", ".join(str(w) for w in banned))
    return "\n\n".join(parts)


def banned_in(text, brand_voice):
    """The client's banned words/phrases that appear in `text` as whole words,
    ignoring case. Empty when none are set or none appear."""
    found = []
    for word in _json_list((brand_voice or {}).get('avoid_words')):
        word = str(word).strip()
        if word and re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', text or '', re.IGNORECASE):
            found.append(word)
    return found


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
# note: its working guidance (the diagnostic checklist, the retention
# structure, the checks) is fixed here; the talent's voice and this
# specific post's own measured performance are swapped in per call. This is the
# complement to a from-scratch builder — it salvages and re-hooks something that
# already has an edit (and ideally performance data) behind it, rather than
# scripting something new. Claude cannot watch footage or hear audio, so this
# always works from text.
#
# Three rules hold the prompt honest:
# 1. The talent's voice outranks every piece of guidance (the PRECEDENCE block) — the
#    lesson of the caption engine, where an injected default that contradicted
#    the voice document was scored 3/10 by the critic.
# 2. The fixed numbers and checks are labelled as guidance, not platform rules.
#    Where they came from is unverified, so they are attributed to no one and
#    Claude is told never to pass them off as Meta policy. The Facebook Content
#    Monetization part (qualified views, earnings, the length bands, licensed
#    audio) is added only for a Facebook post; it says nothing true about an
#    Instagram reel.
# 3. The app measures likes, reach and the rest — not length, audio, retention
#    or earnings. The prompt says which is which, so a guess about an unmeasured
#    number is offered as a hypothesis to check, never as a finding.

REEL_REPURPOSE_PRECEDENCE = """
=== PRECEDENCE ===
Three kinds of instruction follow. Where they disagree, this order decides:
1. The TALENT VOICE sections, when present — the brand voice document, banned words, voice notes and real captions. They are the top authority; within them the voice document wins over the voice notes. Where any guidance below — the diagnostic checklist, the retention structure, a length band, a check — conflicts with the talent's voice, follow the talent's voice, and say in NOTES FOR THE EDITOR which guidance you set aside and why.
2. This post's MEASURED DATA and the facts the user states in ADDITIONAL CONTEXT. They describe this account; where the guidance's general expectation disagrees with them, they win.
3. The GUIDANCE below (Steps 1 to 6). It is general working guidance, not platform rules — not an official Meta, Facebook or Instagram policy, and not verified against this account. Never present any of it, numbers included, to the talent as an official rule or a platform fact; where the package relies on it, call it guidance.
"""

REEL_REPURPOSE_DATA = """
=== WHAT IS MEASURED AND WHAT IS NOT ===
The app records, per post: topic, platform, content type, posted date and — when a snapshot has been recorded — likes, comments, shares, saves, views, reach, impressions and clicks. That is everything under THIS POST'S MEASURED PERFORMANCE in the user message.
The app does NOT record {unmeasured}. Anything about those comes only from ADDITIONAL CONTEXT. Where it is not stated there, it is UNKNOWN:
- say "unknown" and never estimate a figure for it;
- a diagnosis that depends on it is a hypothesis to check, not a finding — name it as one, and say exactly what to look up, and where, to confirm or rule it out;
- never write as if a number was measured when it was not.
If no metrics snapshot has been recorded, say so plainly — a missing snapshot is not evidence that the post flopped. A single zero beside healthy numbers (views 0 with reach in the thousands, say) usually means the metric was not collected for this post: treat it as missing, not as a result.
"""

REEL_REPURPOSE_RULES = """
You are diagnosing and re-cutting an EXISTING piece of video — a published underperformer, an old reel being mined for a remake, or footage that was cut once and didn't land. You do not watch or hear the footage; work only from the transcript/shot list and the data provided. This is a diagnosis-and-rebuild task, not a from-scratch script — ground every recommendation in the actual source material and actual numbers given, never invent footage, dialogue or metrics that weren't provided.

Steps 1 to 6 are GUIDANCE, not platform rules: they rank below the talent's voice and the measured data.

=== STEP 1 — DIAGNOSE BEFORE TOUCHING THE EDIT ===
Run the source against this diagnostic checklist before assuming the fix is "better editing":
- Weak or buried hook — the first thing to check is whether the reveal or emotional peak is front-loaded before roughly the 5-8 second mark, where many viewers decide whether to stay. A flat, generic opener ("can you relate?") with no promise of a payoff is a hook failure, not an editing-craft failure.
- Low-stakes topic — sometimes the edit and hook are both fine and the topic itself just isn't interesting enough to sustain a rebuild. Flag these for retirement rather than a remake.
- Wasted short clip — a strong sub-30s moment that never got built out is an extension candidate, not a flop.
{platform_checks}State the diagnosis explicitly before proposing a fix, and for each cause say whether the measured data or the stated context supports it, or whether it is a hypothesis to check.

=== STEP 2 — CLASSIFY THE REPURPOSING ACTION ===
Based on the diagnosis, assign exactly one action:
- Re-hook only — the body of the edit works, the opening doesn't; rebuild the first 5-8 seconds and keep the rest.
- Full re-cut — retrim to the target length, restructure pacing, rebuild the hook.
- Extend — grow a wasted sub-30s moment into a full build.
{platform_actions}- Retire — low-stakes topic; not worth remaking. Say so plainly rather than forcing a rebuild.
- Multiply — the source is strong enough to also yield a 48-hour pull-clip and/or a quote static.
Every remake must be a genuinely new edit (new first frame, new on-screen text) — never a straight re-upload of the same cut.

=== STEP 3 — REWRITE THE HOOK (3-5 RANKED OPTIONS) ===
For any action other than Retire, write 3-5 ranked cold-open options. Each must:
- Front-load the reveal, confession, or emotional peak — no scene-setting before it
- Create a specific curiosity gap rather than a generic tease ("the real reason X happened.." beats "you won't believe what happened")
- Be deliverable within the first 3 seconds of screen time
- Match the talent's voice exactly (pull specific banned words, punctuation rules and tone markers from the talent voice sections below)
The underlying hook model: the first 3 seconds must promise a specific, resolvable payoff — a "3-second world" the viewer wants closed — never a vague tease.

=== STEP 4 — REBUILD THE RETENTION STRUCTURE ===
Map the rebuilt edit to this beat structure, scaled to {target_length}:
- 0-8s: the hook lands, no dead air, no throat-clearing
- ~1/3 mark: a re-engagement beat — a new piece of information, a twist, or a payoff tease that gives a reason to keep watching past the point most viewers would drop
- Escalation, not deceleration: each beat should raise stakes or interest versus the one before it; no dull or filler beats anywhere in the cut
- Ending: cut cleanly on the peak or payoff rather than winding down — protect retention through the final second rather than signalling "this is ending"
Produce this as a shot-by-shot timeline (timestamp, visual, on-screen text, voiceover/caption line) so an editor can cut directly from it. Skip this and Step 3 if the action is Retire.

=== STEP 5 — CHECKS ("REEL FILTER") ===
Before the package ships, check it against these checks and state pass, fail or unknown on each. They are guidance, not platform rules:
- Passes the talent's brand/safety guardrails (banned topics, no exes, required tagging, etc. — see the talent voice sections)
- Audio: state what audio the rebuild uses; if neither the source material nor the context says what the original used, mark it unknown rather than assuming
{platform_checks_step5}
=== STEP 6 — MULTIPLICATION PLAN ===
If the source is strong, specify what else it should generate: a 48-hour pull-clip (a short highlight posted after the main reel{pull_clip_note}) and/or a quote static pulled from the strongest line. State explicitly if a source is NOT strong enough to multiply — not every remake needs to produce three assets.
"""

# Facebook Content Monetization guidance. Added only for a Facebook
# post: qualified views, earnings and licensed-music rules belong to that
# programme, and "never use trending audio" is wrong advice for Instagram reach.
REEL_REPURPOSE_FACEBOOK = {
    'platform_checks': (
        "- Audio ineligibility (Facebook Content Monetization guidance) — licensed or commercial music can stop a reel earning outright, regardless of performance (0 qualified views, $0.00, even on a well-watched reel). The app does not record the audio, so this is a hypothesis unless ADDITIONAL CONTEXT states it; if the audio is confirmed licensed, the fix is a Sound Collection swap and repost, not a re-edit.\n"
        "- Length bucket (Facebook Content Monetization guidance) — sub-60s reels can retain well and still earn almost nothing; the earning sweet spot is put at 70-90s (the 60-99s band is said to drive the large majority of reel earnings); 3-5 min only works for genuine \"come with me\" journey content. These figures are guidance, unverified for this account, and the app records neither length nor earnings — so this is a hypothesis unless ADDITIONAL CONTEXT gives them.\n"),
    'platform_actions': "- Audio swap — re-export with monetisation-safe audio only; no creative changes needed.\n",
    'target_length': "the 70-90s target this guidance sets for a main Facebook reel",
    'platform_checks_step5': (
        "- Facebook Content Monetization audio (guidance): Sound Collection or the talent's own voice; never licensed, trending or explicit tracks\n"
        "- Length in the 70-90s band for a main Facebook reel (guidance: never sub-60s as a main reel — only as a 48-hour pull-clip)\n"
        "- Note qualified views as the metric to re-check 48 hours after posting, not raw views\n"),
    'pull_clip_note': ", never posted as a main reel itself",
    'unmeasured': ("video length, the audio used or its licensing, retention or drop-off, "
                   "watch time, qualified views or earnings"),
}

# Every other platform: the same craft, no monetisation claims and no audio rule.
REEL_REPURPOSE_GENERAL = {
    'platform_checks': '',
    'platform_actions': '',
    'target_length': ("the rebuilt edit's target length — the source's own length unless the "
                      "talent's voice or the stated context sets one"),
    'platform_checks_step5': '',
    'pull_clip_note': '',
    'unmeasured': "video length, the audio used or its licensing, retention or drop-off, or watch time",
}

REEL_REPURPOSE_OUTPUT = """

=== OUTPUT PACKAGE ===
Deliver, in this order:
1. DIAGNOSIS — which cause(s) apply, stated explicitly; for each, whether it rests on the measured data or stated context, or is a hypothesis to check (and how to check it).
2. ASSIGNED ACTION — exactly one of the actions in Step 2. Justify the choice.
3. RANKED HOOK OPTIONS — 3-5 (omit if the action is Retire), each an exact first-3-second line.
4. SHOT-BY-SHOT RE-CUT TIMELINE — timestamp, visual, on-screen text, voiceover/caption line, for each beat (omit if the action is Retire).
5. ON-SCREEN TEXT — all burned-in text for sound-off viewing.
6. CHECKS — pass, fail or unknown against each check in Step 5, with a one-line reason for anything but a pass.
7. MULTIPLICATION PLAN — the 48-hour pull-clip and/or quote static, or an explicit "not strong enough to multiply" if that's the honest call.
8. NOTES FOR THE EDITOR — anything else they need, stated plainly, including any guidance set aside for the talent's voice.

Write in clear markdown with numbered headers matching the structure above. Be specific and concrete — this is a finished package a human editor builds from with no further questions.
"""


# One long generation, in-request. gunicorn kills the worker at 180 seconds, so the
# call gives up first and the person gets a message rather than a dead request.
# No retries: a retry starts the whole generation again inside the same budget.
REEL_REPURPOSE_TIMEOUT = 150.0
REEL_REPURPOSE_MAX_TOKENS = 6144


def build_repurpose_voice(voice_document, sample_captions, brand_voice):
    """The talent's voice for the repurposer: the same material build_rulebook
    gives the caption writer — the document, real captions, voice notes and
    keywords, banned words — without its caption-only parts (caption length,
    emoji counts, platform caption conventions) and without any default. A
    setting that is not set adds nothing; with nothing set this returns ''."""
    bv = brand_voice or {}
    parts = []
    if voice_document:
        parts.append(
            "\n=== TALENT VOICE: BRAND VOICE DOCUMENT (authoritative — follow it exactly, "
            "in full; banned words, pronoun/voice rules, cast-tagging and sign-off style "
            "live here, and every hook and line of on-screen text must match it) ===\n"
            + voice_document)
    if sample_captions:
        parts.append(
            "\n=== TALENT VOICE: REAL CAPTIONS THIS PERSON WROTE (match their phrasing, "
            "rhythm, punctuation, emoji habits) ===\n"
            + "\n\n".join("- %s" % c for c in sample_captions))
    notes = []
    if bv.get('tone'):
        notes.append("- Tone: %s" % bv['tone'])
    if bv.get('style'):
        notes.append("- Style: %s" % bv['style'])
    if bv.get('target_audience'):
        notes.append("- Audience: %s" % bv['target_audience'])
    keywords = _json_list(bv.get('keywords'))
    if keywords:
        notes.append("- Weave in naturally when they fit: %s" % ', '.join(keywords))
    if notes:
        parts.append("\n=== TALENT VOICE: VOICE NOTES ===\n" + "\n".join(notes))
    banned = _json_list(bv.get('avoid_words'))
    if banned:
        parts.append("\n=== TALENT VOICE: NEVER USE THESE WORDS/PHRASES (in hooks, "
                     "on-screen text, voiceover and captions) ===\n" + ", ".join(banned))
    return "\n".join(parts)


def build_reel_repurpose_prompt(client_name, brand_voice, voice_document, sample_captions,
                                platform=''):
    """The system prompt alone — what build_reel_repurpose sends. Separate so it
    can be read and tested without a model call."""
    platform = (platform or '').strip().lower()
    segments = REEL_REPURPOSE_FACEBOOK if platform == 'facebook' else REEL_REPURPOSE_GENERAL
    return (
        ("You are diagnosing and repurposing a reel for %s. It was posted on %s.\n"
         % (client_name, platform or 'an unrecorded platform'))
        + REEL_REPURPOSE_PRECEDENCE + REEL_REPURPOSE_DATA.format(**segments)
        + REEL_REPURPOSE_RULES.format(**segments)
        + build_repurpose_voice(voice_document, sample_captions, brand_voice)
        + REEL_REPURPOSE_OUTPUT
    )


def build_reel_repurpose(client_name, brand_voice, voice_document, sample_captions,
                         source_material, performance_summary, extra_context='',
                         platform='', debug=False):
    """Diagnose and re-cut an existing/underperforming reel into a re-hooked,
    retention-structured, checked repurposing package.

    source_material: the video's transcript or shot list — required, Claude
    cannot watch footage. performance_summary: what the app measured for this
    post, as text. extra_context: what the app doesn't track (audio, length, a
    retention note from Insights, why this is being revisited) — stated, not
    measured. platform: the post's platform; Facebook adds the Content
    Monetization guidance, every other platform gets none of it.

    Returns {'package': ..., 'error': None} or {'error': ...}. A failure that
    spent no usable output says which: 'timeout' (gave up waiting) or
    'incomplete' (the output hit the token limit and was cut off — it is never
    returned as a package, so it cannot be saved as a finished one).
    """
    client = _client()
    if not client:
        return {'error': 'ANTHROPIC_API_KEY not set.'}
    if not (source_material or '').strip():
        return {'error': "Source material is required — Claude can't watch video, "
                         "so describe or transcribe the existing footage first."}

    system_text = build_reel_repurpose_prompt(client_name, brand_voice, voice_document,
                                              sample_captions, platform)

    user_message = ("SOURCE MATERIAL (transcript / shot list of the existing video):\n\n"
                    + source_material)
    user_message += ("\n\nTHIS POST'S MEASURED PERFORMANCE (recorded in the app):\n"
                     + (performance_summary or 'No performance data recorded.'))
    if extra_context:
        user_message += ("\n\nADDITIONAL CONTEXT (stated by the user, not measured by the "
                         "app — audio, length, retention notes, why this is being "
                         "revisited):\n" + extra_context)

    try:
        resp = client.with_options(timeout=REEL_REPURPOSE_TIMEOUT, max_retries=0).messages.create(
            model=config.CAPTION_MODEL,
            max_tokens=REEL_REPURPOSE_MAX_TOKENS,
            system=[{'type': 'text', 'text': system_text,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': user_message}],
        )
    except anthropic.APITimeoutError:
        return {'error': "Claude didn't finish within %d seconds, so the request was "
                         "stopped. Nothing was saved — try again, or shorten the source "
                         "material." % REEL_REPURPOSE_TIMEOUT, 'timeout': True}
    except anthropic.APIError as e:
        return {'error': 'Claude API error: %s' % str(e)}

    usage = _usage(resp, 'reel_repurpose')
    if getattr(resp, 'stop_reason', None) == 'max_tokens':
        log.warning('[voice] reel_repurpose hit max_tokens=%d; package refused as incomplete',
                    REEL_REPURPOSE_MAX_TOKENS)
        return {'error': "Claude stopped before the package was finished — it reached the "
                         "length limit, so the end is missing. An incomplete package can't be "
                         "saved. Try again, or shorten the source material.",
                'incomplete': True}
    package = ''.join(getattr(block, 'text', '') or '' for block in (resp.content or [])).strip()
    if not package:
        return {'error': 'Claude returned an empty package. Try again.'}

    result = {'package': package, 'error': None}
    if debug:
        result['usage'] = usage
        result['system_prompt'] = system_text
    return result
