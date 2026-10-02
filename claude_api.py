import anthropic
import json
import os
from datetime import date

import caption_rules

PLATFORM_GUIDES = {
    'instagram': 'Instagram: up to 2200 chars but ideal is 150–300. Use line breaks for readability. Hashtags at end. Strong hook first line.',
    'facebook':  'Facebook: 40–80 chars gets best engagement but up to 500 works well. Conversational, spark discussion. End with a question.',
    'tiktok':    'TikTok: 150 chars max shown before cut-off. Strong hook. Trending sounds awareness. Clear CTA to watch/comment.',
    'linkedin':  'LinkedIn: 1300 chars ideal. Professional insight. 3–5 short paragraphs. End with a thought-provoking question. No fluffy emojis.',
    'youtube':   'YouTube description: 150 chars before fold (crucial!), then full description. Include timestamps if relevant. Keywords early.',
    'general':   'General social media best practices. Clear, engaging, on-brand.',
}

LENGTH_GUIDE = {
    'short':  'Keep it under 150 characters.',
    'medium': 'Aim for 150–300 characters.',
    'long':   'Write 300–600 characters with depth and detail.',
}

EMOJI_GUIDE = {
    'none':     'No emojis at all.',
    'minimal':  '1–2 emojis maximum, used purposefully.',
    'moderate': '3–5 emojis, used to emphasise key points.',
    'heavy':    'Generous emoji use throughout — make it expressive and fun.',
}


def _build_system_prompt(client_name, brand_voice, platform):
    keywords = []
    avoid = []
    try:
        keywords = json.loads(brand_voice.get('keywords', '[]') or '[]')
    except Exception:
        pass
    try:
        avoid = json.loads(brand_voice.get('avoid_words', '[]') or '[]')
    except Exception:
        pass

    parts = [
        f"You are an expert social media copywriter for {client_name}.",
        f"\nBRAND VOICE:",
        f"- Tone: {brand_voice.get('tone', 'authentic and engaging')}",
        f"- Style: {brand_voice.get('style', 'conversational')}",
        f"- Target audience: {brand_voice.get('target_audience', 'general audience')}",
    ]
    if keywords:
        parts.append(f"- Keywords to naturally weave in: {', '.join(keywords)}")
    if avoid:
        parts.append(f"- Words/phrases to AVOID: {', '.join(avoid)}")
    if brand_voice.get('sample_caption'):
        parts.append(f"\nEXAMPLE CAPTION STYLE:\n{brand_voice['sample_caption']}")

    parts.append(f"\nPLATFORM RULES:\n{PLATFORM_GUIDES.get(platform, PLATFORM_GUIDES['general'])}")
    parts.append(f"\nLENGTH: {LENGTH_GUIDE.get(brand_voice.get('caption_length', 'medium'), LENGTH_GUIDE['medium'])}")
    parts.append(f"\nEMOJI USAGE: {EMOJI_GUIDE.get(brand_voice.get('emoji_usage', 'moderate'), EMOJI_GUIDE['moderate'])}")
    parts.append("\nReturn ONLY the caption text — no labels, no preamble, no explanation.")

    return '\n'.join(parts)


def generate_caption(client_name, brand_voice, platform, topic, extra_context=''):
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None, 'ANTHROPIC_API_KEY not set. Add it to your .env file.'

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_system_prompt(client_name, brand_voice, platform)

    user_message = f"Write a {platform} caption about: {topic}"
    if extra_context:
        user_message += f"\n\nAdditional context: {extra_context}"

    try:
        response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1024,
            system=[
                {
                    'type': 'text',
                    'text': system_prompt,
                    'cache_control': {'type': 'ephemeral'},
                }
            ],
            messages=[{'role': 'user', 'content': user_message}],
        )
        caption = response.content[0].text.strip()
        return caption, None
    except anthropic.APIError as e:
        return None, f'Claude API error: {str(e)}'


def generate_hashtags(client_name, brand_voice, platform, topic, caption):
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return ''

    client = anthropic.Anthropic(api_key=api_key)
    keywords = []
    try:
        keywords = json.loads(brand_voice.get('keywords', '[]') or '[]')
    except Exception:
        pass

    count = 5 if platform == 'linkedin' else (30 if platform == 'instagram' else 10)

    prompt = (
        f"Generate {count} relevant hashtags for a {platform} post by {client_name}.\n"
        f"Topic: {topic}\n"
        f"Brand keywords: {', '.join(keywords)}\n"
        f"Caption excerpt: {caption[:200]}\n\n"
        f"Return ONLY the hashtags on one line, space-separated, each starting with #."
    )

    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=256,
            messages=[{'role': 'user', 'content': prompt}],
        )
        # Same rule as the voice engine's generator: see caption_rules.fit_hashtags.
        return caption_rules.fit_hashtags(platform, caption, response.content[0].text.strip())
    except Exception:
        return ''


def generate_hook(client_name, brand_voice, platform, topic, caption):
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None, 'ANTHROPIC_API_KEY not set.'

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"Write ONE punchy opening line (hook) for a {platform} video/reel for {client_name}.\n"
        f"Topic: {topic}\n"
        f"Caption excerpt: {caption[:200] if caption else ''}\n\n"
        f"Max 12 words. No hashtags. Return ONLY the hook line, nothing else."
    )
    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=100,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return response.content[0].text.strip(), None
    except anthropic.APIError as e:
        return None, f'Claude API error: {str(e)}'


def generate_trends(clients_summary, platform):
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None, 'ANTHROPIC_API_KEY not set.'

    client = anthropic.Anthropic(api_key=api_key)
    current_date = date.today().isoformat()
    prompt = (
        f"Today is {current_date}. Generate 8 trending content ideas for {platform} "
        f"relevant to: {clients_summary}.\n\n"
        f"Return a JSON array ONLY, no other text, with objects having these fields: "
        f"trend_text, category, platform.\n"
        f"Example: [{{\"trend_text\": \"...\", \"category\": \"wellness\", \"platform\": \"{platform}\"}}]"
    )
    try:
        response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1024,
            system=[{
                'type': 'text',
                'text': 'You are a social media trend analyst. Return only valid JSON arrays, no markdown, no explanation.',
                'cache_control': {'type': 'ephemeral'},
            }],
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        trends = json.loads(raw)
        if not isinstance(trends, list):
            return None, 'Claude did not return a JSON array'
        return trends, None
    except (json.JSONDecodeError, ValueError) as e:
        return None, f'JSON parse error: {str(e)}'
    except anthropic.APIError as e:
        return None, f'Claude API error: {str(e)}'


def plan_week(client_name, description, theme, platform, count,
              trends_list=None, performance_rows=None, content_type=None,
              voice_constraints=''):
    """Break a week's direction into `count` distinct daily topics, informed by
    recent performance and current trends — the planning step ahead of writing
    each post in the client's voice. `voice_constraints` is the part of the
    client's rulebook a topic must respect (voice_engine.planning_constraints);
    empty adds nothing. Returns (topics, error) where topics is a list of
    {"day": int, "topic": str}."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None, 'ANTHROPIC_API_KEY not set.'

    client = anthropic.Anthropic(api_key=api_key)

    if performance_rows:
        # Rows arrive best-performing first (db.get_recent_performance).
        perf_lines = []
        for r in performance_rows[:10]:
            if r.get('likes') is None and r.get('reach') is None:
                stats = 'no metrics yet'
            else:
                stats = ', '.join(
                    f'{k}={r[k]}' for k in ('likes', 'comments', 'shares', 'saves', 'reach', 'views')
                    if r.get(k) is not None
                )
                if r.get('engagement_rate') is not None:
                    stats += f", engagement rate={r['engagement_rate']}%"
            perf_lines.append(f"- [{(r.get('posted_date') or '')[:10]}] {r.get('topic', '')} ({stats})")
        perf_text = '\n'.join(perf_lines)
    else:
        perf_text = 'No recent performance data available.'

    trends_text = '\n'.join(f'- {t}' for t in (trends_list or [])) or 'None supplied.'

    prompt = (
        f"You are planning a week of {platform} content for {client_name}"
        f"{(' — ' + description) if description else ''}"
        f"{(' — every post is a ' + content_type) if content_type else ''}.\n\n"
        f"THIS WEEK'S DIRECTION:\n{theme}\n\n"
        f"WHAT WORKED RECENTLY (last 7 days, best-performing first):\n{perf_text}\n\n"
        f"CURRENT TRENDING THEMES TO CONSIDER (use only what genuinely fits — never force one):\n{trends_text}\n\n"
        f"Plan {count} distinct posts across the week — one clear, specific topic per post, "
        f"each different enough that the week doesn't repeat itself, together forming one "
        f"coherent through-line rather than {count} disconnected ideas.\n\n"
        f"Return a JSON array ONLY, no other text, exactly {count} objects, each with fields "
        f'"day" (integer, 0-indexed position in the week) and "topic" (one specific sentence '
        f"a copywriter could write a caption from directly — not a vague theme).\n"
        f'Example: [{{"day": 0, "topic": "..."}}, ...]'
    )
    system_text = ('You are a social media content strategist. '
                   'Return only a valid JSON array, no markdown, no explanation.')
    if voice_constraints:
        system_text += (
            "\n\nEvery post will be written in the client's own voice under the rules "
            "below. Plan only topics those rules allow, and never put a banned word or "
            "phrase into a topic.\n\n" + voice_constraints)
    # No cache_control: this runs once per batch, so a cache write would cost more
    # and never be read back — and without a voice document the prompt is far
    # below the model's minimum cacheable length anyway.
    try:
        response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1536,
            system=system_text,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        topics = json.loads(raw)
        if not isinstance(topics, list):
            return None, 'Claude did not return a JSON array'
        return topics, None
    except IndexError:
        return None, 'Claude returned an empty reply — try again.'
    except (json.JSONDecodeError, ValueError) as e:
        return None, f'JSON parse error: {str(e)}'
    except anthropic.APIError as e:
        return None, f'Claude API error: {str(e)}'


def generate_report(report_data):
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None, 'ANTHROPIC_API_KEY not set.'

    client = anthropic.Anthropic(api_key=api_key)

    posts = report_data.get('posts', [])
    posted = report_data.get('posted', [])
    perf = report_data.get('performance', {})
    platform_breakdown = report_data.get('platform_breakdown', [])

    platform_text = ', '.join(f"{p['platform']}: {p['cnt']} posts" for p in platform_breakdown) or 'None'
    perf_text = (
        f"Likes: {perf.get('likes') or 0}, Comments: {perf.get('comments') or 0}, "
        f"Shares: {perf.get('shares') or 0}, Reach: {perf.get('reach') or 0}, "
        f"Impressions: {perf.get('impressions') or 0}"
    )

    captions_sample = '\n'.join(
        f"- [{p['client_name']} / {p['platform']}] {p['topic']}: {p['caption'][:120]}..."
        for p in posted[:5]
    ) or 'No posts published this period.'

    prompt = f"""You are a social media strategist writing a weekly performance report.

PERIOD: {report_data.get('start_date', '')} to {report_data.get('end_date', '')}

DATA SUMMARY:
- Total content created: {len(posts)} posts
- Content published: {len(posted)} posts
- Platform breakdown: {platform_text}
- Performance metrics: {perf_text}

SAMPLE PUBLISHED CONTENT:
{captions_sample}

Write a professional but warm weekly report with these sections:
1. **Weekly Overview** — 2–3 sentence summary
2. **Content Performance** — highlight what worked and metrics
3. **Platform Insights** — per-platform notes
4. **Top Performing Content** — call out standouts
5. **Recommendations** — 3 actionable suggestions for next week
6. **Next Steps** — brief action items

Use markdown formatting. Be specific, data-driven, and encouraging. Keep it under 600 words."""

    try:
        response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=2048,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return response.content[0].text.strip(), None
    except anthropic.APIError as e:
        return None, f'Claude API error: {str(e)}'
