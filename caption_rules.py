"""What a platform accepts in a caption, checked before a post reaches Make.

Instagram refuses a caption over 2,200 characters, with more than 30 hashtags or
more than 20 @ tags (IG User Media reference, `caption` parameter). The scenario
publishes `caption + ' ' + hashtags` as one caption, so the hashtags field and the
caption count together. A post that breaks a limit came back from Make as a
generic "The caption was too long" and stayed "approved" in the app, never posted:
34 hashtags, 4 in the caption and 30 generated for the field.

One table, used by the publish step and the hashtag generators.
"""
import re

LIMITS = {
    'instagram': {'chars': 2200, 'hashtags': 30, 'mentions': 20},
}

HASHTAG = re.compile(r'(?<![\w#])#(\w+)')
MENTION = re.compile(r'(?<![\w@])@[\w.]+')


def published_caption(caption, hashtags):
    """The caption the scenario sends: `{{caption}} {{hashtags}}`."""
    return '%s %s' % ((caption or '').strip(), (hashtags or '').strip())


def hashtags_in(text):
    return HASHTAG.findall(text or '')


def check(platform, caption, hashtags):
    """(ok, message). The message says what to change, in the numbers a person
    can act on — not the platform's error, which names the wrong limit."""
    limits = LIMITS.get(platform)
    if not limits:
        return True, None
    text = published_caption(caption, hashtags)
    name = platform.title()

    in_caption, in_field = len(hashtags_in(caption)), len(hashtags_in(hashtags))
    if in_caption + in_field > limits['hashtags']:
        return False, ('%s allows %d hashtags in a caption; this post has %d (%d in the caption, '
                       '%d in the hashtags field). Remove at least %d, then send it again.'
                       % (name, limits['hashtags'], in_caption + in_field, in_caption, in_field,
                          in_caption + in_field - limits['hashtags']))
    mentions = len(MENTION.findall(text))
    if mentions > limits['mentions']:
        return False, ('%s allows %d @ tags in a caption; this post has %d. Remove at least %d, '
                       'then send it again.' % (name, limits['mentions'], mentions,
                                                mentions - limits['mentions']))
    if len(text) > limits['chars']:
        return False, ('%s allows %d characters in a caption, hashtags included; this post has %d. '
                       'Shorten it by at least %d, then send it again.'
                       % (name, limits['chars'], len(text), len(text) - limits['chars']))
    return True, None


def fit_hashtags(platform, caption, hashtags):
    """The hashtags field, without the tags the caption already carries, and — where
    the platform has a limit — only as many as still fit beside the caption's own.
    Order is kept: the generator lists the most relevant first."""
    taken = {t.lower() for t in hashtags_in(caption)}
    kept = []
    for tag in hashtags_in(hashtags):
        if tag.lower() not in taken:
            taken.add(tag.lower())
            kept.append('#' + tag)
    limits = LIMITS.get(platform)
    if limits:
        kept = kept[:max(0, limits['hashtags'] - len(hashtags_in(caption)))]
    return ' '.join(kept)
