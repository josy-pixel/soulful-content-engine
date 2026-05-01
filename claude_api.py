import anthropic
import json
import os

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
        return response.content[0].text.strip()
    except Exception:
        return ''


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
