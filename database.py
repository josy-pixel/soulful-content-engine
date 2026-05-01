import sqlite3
import json
import os
from datetime import datetime, timedelta

# Render mounts a persistent disk at /data — fall back to local file for dev
DB_PATH = os.environ.get('DB_PATH', 'soulful_content.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            contact_email TEXT,
            logo_color TEXT DEFAULT '#6366f1',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS brand_voices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            tone TEXT,
            style TEXT,
            target_audience TEXT,
            keywords TEXT,
            avoid_words TEXT,
            sample_caption TEXT,
            emoji_usage TEXT DEFAULT 'moderate',
            caption_length TEXT DEFAULT 'medium',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id),
            UNIQUE(client_id, platform)
        );

        CREATE TABLE IF NOT EXISTS content_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            topic TEXT NOT NULL,
            caption TEXT NOT NULL,
            hashtags TEXT,
            status TEXT DEFAULT 'draft',
            scheduled_date TIMESTAMP,
            posted_date TIMESTAMP,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id)
        );

        CREATE TABLE IF NOT EXISTS approval_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            notes TEXT,
            changed_by TEXT DEFAULT 'user',
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (post_id) REFERENCES content_posts(id)
        );

        CREATE TABLE IF NOT EXISTS performance_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            saves INTEGER DEFAULT 0,
            reach INTEGER DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            FOREIGN KEY (post_id) REFERENCES content_posts(id)
        );
    ''')
    conn.commit()

    existing = c.execute('SELECT COUNT(*) FROM clients').fetchone()[0]
    if existing == 0:
        _seed_data(c)
        conn.commit()

    conn.close()


def _seed_data(c):
    now = datetime.now()

    c.execute(
        'INSERT INTO clients (name, description, contact_email, logo_color) VALUES (?,?,?,?)',
        ('Holly', 'Personal brand focused on wellness, mindfulness, and authentic living.',
         'holly@example.com', '#8b5cf6')
    )
    holly_id = c.lastrowid

    c.execute(
        'INSERT INTO clients (name, description, contact_email, logo_color) VALUES (?,?,?,?)',
        ('Soulful Management', 'Talent & lifestyle management agency championing conscious creators.',
         'hello@soulfulmanagement.com', '#06b6d4')
    )
    soulful_id = c.lastrowid

    holly_voices = [
        ('general',    'Warm, authentic, empowering',           'Conversational storytelling with personal anecdotes',  'Women 25–45 into wellness & growth',       '["authentic","mindful","growth","healing","community"]', '["hustle","grind","toxic"]',       "Today I'm reminded that healing isn't linear — and that's perfectly okay. 🌿",                        'moderate', 'medium'),
        ('instagram',  'Visually poetic, deeply personal',      'Short punchy lines with intentional line breaks',       'Wellness-conscious women 25–40',            '["soulful","grounded","intentional"]',                  '["algorithm","hack","viral"]',     'Some seasons are for growing. Some are for resting. Both are sacred. ✨',                              'moderate', 'short'),
        ('facebook',   'Warm community storytelling',           'Longer form, inviting conversation',                    'Women 30–50 in wellness community',         '["community","support","journey"]',                     '["hustle"]',                       'This week taught me something important about showing up for yourself...',                              'minimal',  'long'),
        ('linkedin',   'Professional yet heart-led',            'Thought leadership with personal insight',              'Professionals into conscious leadership',   '["leadership","authentic","purpose","impact"]',         '["crushing it","killing it"]',     "The most transformative leadership lesson I've learned has nothing to do with strategy.",               'none',     'medium'),
        ('tiktok',     'Playful, real, and relatable',          'Hook-first, trend-aware, strong CTA',                  'Young women 18–35',                         '["real","relatable","raw","honest"]',                   '["filtered","perfect"]',           "POV: You finally stopped apologizing for taking up space 🌸",                                          'heavy',    'short'),
        ('youtube',    'Educational and inspiring',             'Story arc with clear takeaways',                        'Women seeking growth content 25–45',        '["journey","lessons","practical"]',                     '["quick fix","hack"]',             'I spent 30 days doing this every morning — here\'s what happened to my mental health.',               'minimal',  'long'),
    ]

    soulful_voices = [
        ('general',    'Elevated, professional, community-centred', 'Agency voice that celebrates its talent',           'Creators, brands, industry pros',           '["talent","authentic","collaborative","visionary"]',    '["fake","manufactured"]',          "We don't just manage talent — we nurture vision. 🌟",                                                  'minimal',  'medium'),
        ('instagram',  'Aspirational and celebratory',             'Showcase-focused with community pride',              'Aspiring creators & insiders 20–35',        '["talent","celebrating","community","creative"]',       '["ordinary"]',                     'Proud to champion creators who lead with heart. ✨',                                                    'moderate', 'short'),
        ('linkedin',   'Industry authority voice',                 'Thought leadership and agency expertise',            'Brands, professionals, creators',           '["industry","partnership","talent management"]',        '["viral","hack"]',                 'The creator economy is evolving. Here\'s how conscious talent management is leading the change.',      'none',     'long'),
        ('facebook',   'Community and behind-the-scenes',          'Warm industry insider perspective',                  'Creative community 25–45',                  '["behind the scenes","team","grateful"]',               '[]',                               "Another incredible week of creative work wrapping up. Here's what our talent has been up to...",       'minimal',  'medium'),
        ('tiktok',     'Fun industry insider content',             'Trend-aware, educational about the industry',        'Aspiring creators 18–28',                   '["industry","creator tips","agency life"]',             '[]',                               "What talent managers actually look for (it's not what you think) 👀",                                  'heavy',    'short'),
        ('youtube',    'Educational agency content',              'Industry insights and creator success stories',       'Aspiring creators & professionals 20–35',   '["success stories","industry insights","how to"]',      '[]',                               'How we helped this creator go from 10K to 1M followers — the real story.',                             'minimal',  'long'),
    ]

    for v in holly_voices:
        c.execute(
            'INSERT OR IGNORE INTO brand_voices (client_id,platform,tone,style,target_audience,keywords,avoid_words,sample_caption,emoji_usage,caption_length) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (holly_id,) + v
        )

    for v in soulful_voices:
        c.execute(
            'INSERT OR IGNORE INTO brand_voices (client_id,platform,tone,style,target_audience,keywords,avoid_words,sample_caption,emoji_usage,caption_length) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (soulful_id,) + v
        )

    posts = [
        (holly_id,   'instagram', 'Morning mindfulness routine',
         "The morning doesn't have to be perfect to be beautiful. 🌅\n\nI used to think a 'successful' morning meant waking at 5am, journaling, meditating, exercising, and preparing a gourmet breakfast.\n\nThen life happened. And I learned that the perfect morning is simply the one where you show up for yourself — even if that's just 5 minutes of stillness before the chaos begins.\n\nWhat does your morning ritual look like? Share below 👇",
         '#mindfulness #morningroutine #selfcare #wellness #intentionalliving',
         'approved', (now + timedelta(days=2)).strftime('%Y-%m-%d %H:%M'), None, None),

        (holly_id,   'linkedin',  'Authentic leadership in 2024',
         "The most powerful leadership move I've made this year?\n\nAdmitting I didn't have all the answers.\n\nIn a culture that glorifies certainty, choosing vulnerability feels radical. But here's what I've discovered: teams don't need leaders who pretend to be infallible. They need leaders who model what it looks like to learn, grow, and adapt.\n\nThis shift didn't weaken my leadership. It transformed it.\n\nWhat's the most important leadership lesson you've learned recently?",
         '#leadership #authenticity #growth #mindfulness #purposedriven',
         'needs_review', None, None, None),

        (soulful_id, 'instagram', 'Creator spotlight — monthly feature',
         "Every month, we celebrate a creator in our family who is quietly changing the game. ✨\n\nThis month, we're shining a light on the ones who show up consistently — not for the virality, but for the connection.\n\nBecause that's where real influence lives.\n\nTag a creator below who deserves more flowers 🌸",
         '#creatoreconomy #talent #soulfulmanagement #creatorspotlight #authentic',
         'posted', None, (now - timedelta(days=3)).strftime('%Y-%m-%d %H:%M'), None),

        (holly_id,   'tiktok',   'Setting boundaries without guilt',
         "POV: You just said no to something that doesn't align with your values — without apologising 🌸\n\nBoundaries aren't walls. They're doors with locks. You decide who gets the key.\n\nThis took me YEARS to understand. Save this for when you need the reminder 💜",
         '#boundaries #selfworth #mentalhealth #healing #fyp',
         'scheduled', (now + timedelta(days=1)).strftime('%Y-%m-%d %H:%M'), None, None),

        (soulful_id, 'linkedin',  'Creator economy trends 2024',
         "The creator economy isn't just growing — it's maturing.\n\nHere's what we're seeing at Soulful Management:\n\n→ Brands are shifting budget from macro to micro-creators\n→ Authenticity metrics now outweigh vanity metrics\n→ Long-form content is making a comeback\n→ Community > audience\n\nThe creators thriving right now are those who built relationships before they built reach.\n\nWhat trends are you seeing in your corner of the creator world?",
         '#creatoreconomy #influencermarketing #contentcreation #digitalmarketing',
         'draft', None, None, None),

        (holly_id,   'facebook',  'Weekly community check-in',
         "It's been one of those weeks where everything felt a little heavier than usual.\n\nAnd instead of pushing through and pretending otherwise, I want to be honest with this community — because that's what we're here for.\n\nSome weeks are for growing. Some are for surviving. And this one? This one was for learning to ask for help.\n\nHow are you all doing this week? Drop a number 1–10 and let's support each other 💜",
         '#community #mentalhealth #authenticity #wellness #connection',
         'posted', None, (now - timedelta(days=7)).strftime('%Y-%m-%d %H:%M'), None),

        (soulful_id, 'tiktok',   'Behind the scenes at the agency',
         "Day in the life at a soulful talent agency ✨\n\nSpoiler: it's mostly emails, strategy calls, and celebrating small wins with our creators.\n\nBut honestly? We wouldn't trade it for anything. 🙌",
         '#agencylife #talentmanagement #creatoreconomy #behindthescenes #fyp',
         'approved', (now + timedelta(days=3)).strftime('%Y-%m-%d %H:%M'), None, None),

        (holly_id,   'youtube',   'My self-care non-negotiables',
         "I spent years putting everyone else first. Here's what finally changed — and the 5 self-care practices I now protect at all costs.\n\nThis isn't about bubble baths and face masks (though I love both). It's about the deep work of deciding your own needs matter.\n\nTimestamps:\n0:00 Intro\n2:30 Why I hit rock bottom\n8:00 The 5 practices\n18:00 How to start",
         '#selfcare #wellness #mentalhealth #boundaries #intentionalliving',
         'needs_review', None, None, 'Check video thumbnail before approving'),
    ]

    for p in posts:
        c.execute(
            'INSERT INTO content_posts (client_id,platform,topic,caption,hashtags,status,scheduled_date,posted_date,notes) VALUES (?,?,?,?,?,?,?,?,?)',
            p
        )
        post_id = c.lastrowid
        c.execute('INSERT INTO approval_history (post_id,from_status,to_status,notes) VALUES (?,?,?,?)',
                  (post_id, None, 'draft', 'Post created'))
        if p[5] != 'draft':
            c.execute('INSERT INTO approval_history (post_id,from_status,to_status,notes) VALUES (?,?,?,?)',
                      (post_id, 'draft', p[5], 'Status updated'))

    # Performance for posted posts
    c.execute("SELECT id FROM content_posts WHERE status='posted'")
    posted = c.fetchall()
    sample_metrics = [
        (847, 43, 128, 67, 3420, 5891, 89),
        (234, 67, 45, 23, 1876, 3201, 34),
    ]
    for i, row in enumerate(posted):
        if i < len(sample_metrics):
            m = sample_metrics[i]
            c.execute(
                'INSERT INTO performance_metrics (post_id,likes,comments,shares,saves,reach,impressions,clicks) VALUES (?,?,?,?,?,?,?,?)',
                (row[0],) + m
            )


# ── Query helpers ──────────────────────────────────────────────────────────────

def get_clients():
    conn = get_db()
    rows = conn.execute('SELECT * FROM clients ORDER BY name').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_client(client_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM clients WHERE id=?', (client_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_client(data):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        'INSERT INTO clients (name,description,contact_email,logo_color) VALUES (?,?,?,?)',
        (data['name'], data.get('description', ''), data.get('contact_email', ''), data.get('logo_color', '#6366f1'))
    )
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return new_id


def update_client(client_id, data):
    conn = get_db()
    conn.execute(
        'UPDATE clients SET name=?,description=?,contact_email=?,logo_color=? WHERE id=?',
        (data['name'], data.get('description', ''), data.get('contact_email', ''), data.get('logo_color', '#6366f1'), client_id)
    )
    conn.commit()
    conn.close()


def get_brand_voice(client_id, platform):
    conn = get_db()
    row = conn.execute('SELECT * FROM brand_voices WHERE client_id=? AND platform=?', (client_id, platform)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_brand_voices(client_id):
    conn = get_db()
    rows = conn.execute('SELECT * FROM brand_voices WHERE client_id=?', (client_id,)).fetchall()
    conn.close()
    return {r['platform']: dict(r) for r in rows}


def upsert_brand_voice(client_id, platform, data):
    conn = get_db()
    conn.execute('''
        INSERT INTO brand_voices (client_id,platform,tone,style,target_audience,keywords,avoid_words,sample_caption,emoji_usage,caption_length,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(client_id,platform) DO UPDATE SET
            tone=excluded.tone, style=excluded.style, target_audience=excluded.target_audience,
            keywords=excluded.keywords, avoid_words=excluded.avoid_words, sample_caption=excluded.sample_caption,
            emoji_usage=excluded.emoji_usage, caption_length=excluded.caption_length, updated_at=CURRENT_TIMESTAMP
    ''', (
        client_id, platform,
        data.get('tone', ''), data.get('style', ''), data.get('target_audience', ''),
        data.get('keywords', '[]'), data.get('avoid_words', '[]'),
        data.get('sample_caption', ''), data.get('emoji_usage', 'moderate'), data.get('caption_length', 'medium')
    ))
    conn.commit()
    conn.close()


def get_posts(client_id=None, platform=None, status=None, limit=100, offset=0):
    conn = get_db()
    query = '''
        SELECT p.*, c.name AS client_name, c.logo_color
        FROM content_posts p
        JOIN clients c ON c.id = p.client_id
        WHERE 1=1
    '''
    params = []
    if client_id:
        query += ' AND p.client_id=?'; params.append(client_id)
    if platform:
        query += ' AND p.platform=?'; params.append(platform)
    if status:
        query += ' AND p.status=?'; params.append(status)
    query += ' ORDER BY p.updated_at DESC LIMIT ? OFFSET ?'
    params += [limit, offset]
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_post(post_id):
    conn = get_db()
    row = conn.execute('''
        SELECT p.*, c.name AS client_name, c.logo_color
        FROM content_posts p JOIN clients c ON c.id=p.client_id
        WHERE p.id=?
    ''', (post_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_post(data):
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO content_posts (client_id,platform,topic,caption,hashtags,status,scheduled_date,notes)
        VALUES (?,?,?,?,?,?,?,?)
    ''', (
        data['client_id'], data['platform'], data['topic'], data['caption'],
        data.get('hashtags', ''), data.get('status', 'draft'),
        data.get('scheduled_date') or None, data.get('notes', '')
    ))
    post_id = c.lastrowid
    conn.execute('INSERT INTO approval_history (post_id,from_status,to_status,notes) VALUES (?,?,?,?)',
                 (post_id, None, data.get('status', 'draft'), 'Post created'))
    conn.commit()
    conn.close()
    return post_id


def update_post(post_id, data):
    conn = get_db()
    conn.execute('''
        UPDATE content_posts SET topic=?,caption=?,hashtags=?,scheduled_date=?,notes=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    ''', (data['topic'], data['caption'], data.get('hashtags', ''),
          data.get('scheduled_date') or None, data.get('notes', ''), post_id))
    conn.commit()
    conn.close()


def update_post_status(post_id, new_status, notes='', changed_by='user'):
    conn = get_db()
    post = conn.execute('SELECT status FROM content_posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        conn.close()
        return False
    old_status = post['status']
    extra = {}
    if new_status == 'posted':
        extra['posted_date'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    update_clause = 'status=?, updated_at=CURRENT_TIMESTAMP'
    params = [new_status]
    if extra.get('posted_date'):
        update_clause += ', posted_date=?'
        params.append(extra['posted_date'])
    params.append(post_id)
    conn.execute(f'UPDATE content_posts SET {update_clause} WHERE id=?', params)
    conn.execute('INSERT INTO approval_history (post_id,from_status,to_status,notes,changed_by) VALUES (?,?,?,?,?)',
                 (post_id, old_status, new_status, notes, changed_by))
    conn.commit()
    conn.close()
    return True


def delete_post(post_id):
    conn = get_db()
    conn.execute('DELETE FROM approval_history WHERE post_id=?', (post_id,))
    conn.execute('DELETE FROM performance_metrics WHERE post_id=?', (post_id,))
    conn.execute('DELETE FROM content_posts WHERE id=?', (post_id,))
    conn.commit()
    conn.close()


def get_approval_history(post_id):
    conn = get_db()
    rows = conn.execute('SELECT * FROM approval_history WHERE post_id=? ORDER BY changed_at DESC', (post_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_performance(post_id):
    conn = get_db()
    rows = conn.execute('SELECT * FROM performance_metrics WHERE post_id=? ORDER BY recorded_at DESC', (post_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_performance(post_id, data):
    conn = get_db()
    conn.execute('''
        INSERT INTO performance_metrics (post_id,likes,comments,shares,saves,reach,impressions,clicks,notes)
        VALUES (?,?,?,?,?,?,?,?,?)
    ''', (
        post_id,
        int(data.get('likes', 0)), int(data.get('comments', 0)),
        int(data.get('shares', 0)), int(data.get('saves', 0)),
        int(data.get('reach', 0)), int(data.get('impressions', 0)),
        int(data.get('clicks', 0)), data.get('notes', '')
    ))
    conn.commit()
    conn.close()


def get_dashboard_stats():
    conn = get_db()

    status_counts = {r['status']: r['cnt'] for r in conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM content_posts GROUP BY status"
    ).fetchall()}

    platform_counts = [dict(r) for r in conn.execute(
        "SELECT platform, COUNT(*) AS cnt FROM content_posts GROUP BY platform ORDER BY cnt DESC"
    ).fetchall()]

    client_counts = [dict(r) for r in conn.execute(
        "SELECT c.name, c.logo_color, COUNT(p.id) AS cnt FROM clients c LEFT JOIN content_posts p ON p.client_id=c.id GROUP BY c.id"
    ).fetchall()]

    upcoming = [dict(r) for r in conn.execute('''
        SELECT p.*, c.name AS client_name, c.logo_color
        FROM content_posts p JOIN clients c ON c.id=p.client_id
        WHERE p.status IN ('approved','scheduled') AND p.scheduled_date IS NOT NULL
        ORDER BY p.scheduled_date ASC LIMIT 5
    ''').fetchall()]

    recent = [dict(r) for r in conn.execute('''
        SELECT p.*, c.name AS client_name, c.logo_color
        FROM content_posts p JOIN clients c ON c.id=p.client_id
        ORDER BY p.updated_at DESC LIMIT 6
    ''').fetchall()]

    total_posts = conn.execute('SELECT COUNT(*) FROM content_posts').fetchone()[0]

    perf = conn.execute('''
        SELECT SUM(likes) AS likes, SUM(comments) AS comments, SUM(shares) AS shares,
               SUM(reach) AS reach, SUM(impressions) AS impressions
        FROM performance_metrics
    ''').fetchone()

    conn.close()
    return {
        'status_counts': status_counts,
        'platform_counts': platform_counts,
        'client_counts': client_counts,
        'upcoming': upcoming,
        'recent': recent,
        'total_posts': total_posts,
        'total_likes': perf['likes'] or 0,
        'total_reach': perf['reach'] or 0,
        'total_impressions': perf['impressions'] or 0,
    }


def get_scheduled_posts():
    conn = get_db()
    rows = conn.execute('''
        SELECT p.*, c.name AS client_name, c.logo_color
        FROM content_posts p JOIN clients c ON c.id=p.client_id
        WHERE p.scheduled_date IS NOT NULL
        ORDER BY p.scheduled_date ASC
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_report_data(start_date, end_date):
    conn = get_db()

    posts = [dict(r) for r in conn.execute('''
        SELECT p.*, c.name AS client_name
        FROM content_posts p JOIN clients c ON c.id=p.client_id
        WHERE p.created_at BETWEEN ? AND ?
        ORDER BY p.created_at DESC
    ''', (start_date, end_date)).fetchall()]

    posted = [dict(r) for r in conn.execute('''
        SELECT p.*, c.name AS client_name
        FROM content_posts p JOIN clients c ON c.id=p.client_id
        WHERE p.posted_date BETWEEN ? AND ?
    ''', (start_date, end_date)).fetchall()]

    perf = conn.execute('''
        SELECT SUM(m.likes) AS likes, SUM(m.comments) AS comments,
               SUM(m.shares) AS shares, SUM(m.saves) AS saves,
               SUM(m.reach) AS reach, SUM(m.impressions) AS impressions
        FROM performance_metrics m
        JOIN content_posts p ON p.id=m.post_id
        WHERE m.recorded_at BETWEEN ? AND ?
    ''', (start_date, end_date)).fetchone()

    platform_breakdown = [dict(r) for r in conn.execute('''
        SELECT platform, COUNT(*) AS cnt
        FROM content_posts WHERE created_at BETWEEN ? AND ?
        GROUP BY platform
    ''', (start_date, end_date)).fetchall()]

    conn.close()
    return {
        'posts': posts,
        'posted': posted,
        'performance': dict(perf) if perf else {},
        'platform_breakdown': platform_breakdown,
        'start_date': start_date,
        'end_date': end_date,
    }
