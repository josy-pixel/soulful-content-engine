"""What kind of media each kind of post can carry.

A photo post published with a video attached fails at the network with a generic
error hours later — it happened to Josy before, and again today. The app already
knows both facts at the moment they are put together; this is where it says so.

One table, used by the attach step, the publish step and the UI, so the rule
cannot drift into three slightly different versions.
"""

IMAGE = 'image'
VIDEO = 'video'

VIDEO_EXTENSIONS = ('.mp4', '.mov', '.avi', '.webm')

# content_type -> the media kinds it may carry
ACCEPTS = {
    'photo':    {IMAGE},
    'video':    {VIDEO},
    'reel':     {VIDEO},
    'story':    {VIDEO},
    'carousel': {IMAGE, VIDEO},
    # A plain feed post is text-or-link first; either kind of media may ride along.
    'post':     {IMAGE, VIDEO},
}

# Shown to a person, so it names the thing rather than the enum.
_WORD = {IMAGE: 'an image', VIDEO: 'a video'}


# What the publishing scenarios can actually carry out today. A content type the
# app offers but no module handles must be refused here — sending it produces one
# operation, nothing published, and a green "success", which is the failure mode
# that takes longest to notice. Instagram stories have no module anywhere yet.
PUBLISHABLE = {
    'facebook':  {'photo', 'video', 'post', 'reel'},
    'instagram': {'photo', 'video', 'reel', 'carousel'},
}


def can_publish(platform, content_type):
    """(ok, message). Platforms absent from the table are not judged here — the
    webhook's own platform list already refuses those."""
    allowed = PUBLISHABLE.get((platform or '').lower())
    # An empty content type is not a mismatch — the payload defaults it downstream,
    # the same way the rest of the app does.
    if allowed is None or not content_type or content_type.lower() in allowed:
        return True, None
    return False, ('%s %s posts are not published by this system yet — nothing is '
                   'set up to send them. Change the content type, or post it by hand.'
                   % ((platform or '').title(), content_type))


def accepts(content_type):
    """Which media kinds this content type may carry. Unknown types allow both
    rather than blocking work on a type this table has not caught up with."""
    return ACCEPTS.get((content_type or '').lower(), {IMAGE, VIDEO})


def kind_of_filename(filename):
    """Whether a file is a video, judged by extension — the same test the rest of
    the app uses, applied to a stored reference or a plain name."""
    name = (filename or '').lower().split('?')[0]
    return VIDEO if name.endswith(VIDEO_EXTENSIONS) else IMAGE


def check(content_type, media_kind):
    """(ok, message). The message is for a person to read and act on."""
    allowed = accepts(content_type)
    if media_kind in allowed:
        return True, None
    wanted = ' or '.join(sorted(_WORD[k] for k in allowed))
    return False, ('A %s post takes %s. This file is %s.'
                   % (content_type or 'post', wanted, _WORD[media_kind]))
