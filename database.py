import sqlite3
import json
import os
import shutil
import logging
from datetime import datetime, timedelta

# Render mounts a persistent disk at /data — fall back to local file for dev
DB_PATH = os.environ.get('DB_PATH', 'soulful_content.db')


def get_db():
    # timeout + busy_timeout make writers WAIT for a lock instead of failing with
    # "database is locked"; WAL lets readers and a writer work concurrently.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _raise_if_deleted(conn, table, row_id):
    """Write guard (Stage 2): refuse to mutate a soft-deleted row — raise, never
    silently no-op. Reads the BASE table on purpose: it must be able to SEE deleted
    rows in order to reject them. `table` is an internal constant, never user input.
    # raw-query-ok: write guard must read base table to detect deleted rows"""
    row = conn.execute("SELECT deleted_at FROM %s WHERE id=?" % table, (row_id,)).fetchone()
    if row is not None and row['deleted_at'] is not None:
        raise ValueError('%s id=%s is deleted; refusing to mutate it' % (table, row_id))


def restore_if_requested():
    """Boot-time, guarded DB restore. MUST run before any connection is opened.
    Env-controlled and OFF by default:
      RESTORE_FROM     path to a backup file to restore over DB_PATH
      RESTORE_CONFIRM  must equal basename(RESTORE_FROM) — a second, deliberate step
    Five guards:
      1. source validation   — file exists, opens, PRAGMA integrity_check == 'ok'
      2. confirmation token   — RESTORE_CONFIRM must match the backup's filename
      3. marker file          — a completed restore of this source is not repeated on
                                the next boot (an env var left set can't re-overwrite)
      4. pre-overwrite backup — the current live DB is VACUUM INTO'd first; if that
                                fails, the restore ABORTS (never overwrite unbacked)
      5. before/after counts  — logged for users/clients/content_posts
    Returns a short status string. Never raises into boot.
    """
    log = logging.getLogger('restore')
    src = os.environ.get('RESTORE_FROM', '').strip()
    if not src:
        return 'noop'
    try:
        if os.environ.get('RESTORE_CONFIRM', '').strip() != os.path.basename(src):   # guard 2
            log.error('restore: RESTORE_CONFIRM must equal the filename %r — skipping',
                      os.path.basename(src))
            return 'unconfirmed'
        if not os.path.isfile(src):                                                   # guard 1a
            log.error('restore: RESTORE_FROM %s not found — skipping', src)
            return 'missing-source'
        sc = sqlite3.connect(src)
        integrity = sc.execute('PRAGMA integrity_check').fetchone()[0]
        src_counts = {t: sc.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
                      for t in ('users', 'clients', 'content_posts')}
        sc.close()
        if integrity != 'ok':                                                        # guard 1b
            log.error('restore: source integrity_check=%s — skipping', integrity)
            return 'bad-source'
        data_dir = os.path.dirname(DB_PATH) or '.'
        marker = os.path.join(data_dir, '.restored-' + os.path.basename(src))
        if os.path.exists(marker):                                                   # guard 3
            log.warning('restore: %s already restored (marker present) — skipping', src)
            return 'already-restored'
        before = None
        if os.path.exists(DB_PATH):                                                  # guards 4 + 5(before)
            try:
                lc = sqlite3.connect(DB_PATH)
                before = {t: lc.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
                          for t in ('users', 'clients', 'content_posts')}
                bdir = os.path.join(data_dir, 'backups')
                os.makedirs(bdir, exist_ok=True)
                stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
                pre = os.path.join(bdir, 'pre-restore-%s.db' % stamp).replace('\\', '/')
                lc.execute("VACUUM INTO '%s'" % pre)
                lc.close()
                log.warning('restore: current DB backed up to %s (counts=%s)', pre, before)
            except Exception as e:
                log.error('restore: pre-overwrite backup failed (%s) — ABORTING for safety', e)
                return 'pre-backup-failed'
        shutil.copyfile(src, DB_PATH)                                                # perform overwrite
        for sfx in ('-wal', '-shm'):
            stale = DB_PATH + sfx
            if os.path.exists(stale):
                os.remove(stale)
        with open(marker, 'w', encoding='utf-8') as fh:
            fh.write('restored %s from %s\n' % (datetime.now().isoformat(), src))
        log.warning('restore: RESTORED %s over %s — before=%s after=%s',
                    src, DB_PATH, before, src_counts)                                # guard 5(after)
        return 'restored'
    except Exception as e:
        log.error('restore: unexpected error (%s) — DB left as-is', e)
        return 'error'


def init_db():
    restore_if_requested()   # guarded, env-gated, no-op unless RESTORE_FROM is set
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
            views INTEGER DEFAULT 0,
            reach INTEGER DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            FOREIGN KEY (post_id) REFERENCES content_posts(id)
        );

        CREATE TABLE IF NOT EXISTS trends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            trend_text TEXT NOT NULL,
            category TEXT,
            client_id INTEGER,
            week_of DATE,
            source TEXT DEFAULT 'ai',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS client_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT,
            media_type TEXT NOT NULL DEFAULT 'image',
            file_size INTEGER DEFAULT 0,
            caption_hint TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id)
        );

        CREATE TABLE IF NOT EXISTS post_media (
            post_id INTEGER NOT NULL,
            media_id INTEGER NOT NULL,
            sort_order INTEGER DEFAULT 0,
            PRIMARY KEY (post_id, media_id),
            FOREIGN KEY (post_id) REFERENCES content_posts(id),
            FOREIGN KEY (media_id) REFERENCES client_media(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Per-client outbound webhook (one active per client; platform routed inside
        -- the client's Make scenario). webhook_secret is plaintext in SQLite — masked
        -- in all UI/logs; see MIGRATION_NOTES.md for the limitation.
        CREATE TABLE IF NOT EXISTS client_webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            webhook_url TEXT NOT NULL,
            webhook_secret TEXT NOT NULL,
            platforms_enabled TEXT NOT NULL DEFAULT 'facebook',   -- csv: facebook,instagram
            status TEXT NOT NULL DEFAULT 'untested',              -- untested | verified | failing | disabled
            last_test_at TEXT,
            last_success_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_client_webhook_unique
            ON client_webhooks(client_id) WHERE deleted_at IS NULL;
        -- one webhook URL belongs to exactly one client: two clients on one scenario
        -- would publish to the wrong page and the routing test would not catch it.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_client_webhook_url_unique
            ON client_webhooks(webhook_url) WHERE deleted_at IS NULL;
    ''')
    conn.commit()

    # Non-destructive migrations for existing DBs
    for migration in [
        "ALTER TABLE content_posts ADD COLUMN image_url TEXT DEFAULT ''",
        "ALTER TABLE content_posts ADD COLUMN content_type TEXT DEFAULT 'photo'",
        "ALTER TABLE content_posts ADD COLUMN posted_url TEXT DEFAULT ''",
        "ALTER TABLE content_posts ADD COLUMN hook TEXT DEFAULT ''",
        "ALTER TABLE content_posts ADD COLUMN error_message TEXT DEFAULT ''",
        "ALTER TABLE performance_metrics ADD COLUMN views INTEGER DEFAULT 0",
        # Voice engine — full-fidelity voice per client
        "ALTER TABLE clients ADD COLUMN voice_document TEXT DEFAULT ''",
        "ALTER TABLE clients ADD COLUMN sample_captions TEXT DEFAULT '[]'",
        # Voice-audit results per post
        "ALTER TABLE content_posts ADD COLUMN voice_score INTEGER",
        "ALTER TABLE content_posts ADD COLUMN voice_audit TEXT DEFAULT ''",
        # ── Roles & client portal (Stage 1) ──
        # SQLite can't ADD a CHECK/FK to an existing table, so the
        # "client role must be bound to exactly one client" rule is enforced in
        # app code (security.py / create_client_user), not by a DB constraint.
        "ALTER TABLE users ADD COLUMN client_id INTEGER",          # NULL for admin/manager
        "ALTER TABLE users ADD COLUMN invite_token_hash TEXT",     # sha256 of single-use invite
        "ALTER TABLE users ADD COLUMN invite_expires_at TEXT",     # ISO8601
        "ALTER TABLE users ADD COLUMN last_login_at TEXT",         # ISO8601
        "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
        # ── Deletion, trash & lifecycle (Stage 2) — additive only ──
        "ALTER TABLE content_posts ADD COLUMN deleted_at TEXT",
        "ALTER TABLE content_posts ADD COLUMN deleted_by INTEGER",
        "ALTER TABLE content_posts ADD COLUMN deleted_reason TEXT",
        "ALTER TABLE content_posts ADD COLUMN status_before_delete TEXT",
        "ALTER TABLE content_posts ADD COLUMN purge_after TEXT",
        "ALTER TABLE clients ADD COLUMN deleted_at TEXT",
        "ALTER TABLE clients ADD COLUMN deleted_by INTEGER",
        "ALTER TABLE clients ADD COLUMN deleted_reason TEXT",
        "ALTER TABLE clients ADD COLUMN purge_after TEXT",
        "ALTER TABLE clients ADD COLUMN erased_at TEXT",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass

    # Stage 2: append-only audit log + delete indexes (idempotent).
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER,
            actor_role TEXT,
            tenant_client_id INTEGER,
            entity_type TEXT NOT NULL,   -- client | content | user | social_account
            entity_id INTEGER NOT NULL,
            action TEXT NOT NULL,        -- delete | restore | purge | erase | republish
            reason TEXT,
            metadata TEXT,               -- json.dumps(); SQLite has no jsonb
            request_ip TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
        CREATE INDEX IF NOT EXISTS idx_content_deleted ON content_posts(deleted_at);
        CREATE INDEX IF NOT EXISTS idx_clients_deleted ON clients(deleted_at);
    ''')
    conn.commit()

    # Filtered read views — EVERY application read goes through these, so a new
    # query can't forget the delete filter. Recreated on every boot (never
    # CREATE VIEW IF NOT EXISTS) so `SELECT *` cannot go stale against a later
    # ADD COLUMN. Writes and the trash/purge readers use the base tables directly.
    conn.executescript('''
        DROP VIEW IF EXISTS v_content_active;
        CREATE VIEW v_content_active AS SELECT * FROM content_posts WHERE deleted_at IS NULL;
        DROP VIEW IF EXISTS v_clients_active;
        CREATE VIEW v_clients_active AS SELECT * FROM clients WHERE deleted_at IS NULL;
        DROP VIEW IF EXISTS v_client_webhooks_active;
        CREATE VIEW v_client_webhooks_active AS SELECT * FROM client_webhooks WHERE deleted_at IS NULL;
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
    rows = conn.execute('SELECT * FROM v_clients_active ORDER BY name').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_client(client_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM v_clients_active WHERE id=?', (client_id,)).fetchone()
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
    _raise_if_deleted(conn, 'clients', client_id)   # write guard
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
        FROM v_content_active p
        JOIN v_clients_active c ON c.id = p.client_id
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
        FROM v_content_active p JOIN v_clients_active c ON c.id=p.client_id
        WHERE p.id=?
    ''', (post_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Trash / restore / purge readers — the ONLY readers of the base tables ──
# These deliberately bypass the v_*_active views so soft-deleted rows are visible.
# Never use them for normal reads. Callers must be trash/restore/purge paths.

def get_post_including_deleted(post_id):
    """Base-table read (sees soft-deleted). For trash/restore/purge ONLY.
    # raw-query-ok: trash/restore/purge must see deleted rows"""
    conn = get_db()
    row = conn.execute(
        'SELECT p.*, c.name AS client_name, c.logo_color '
        'FROM content_posts p JOIN clients c ON c.id=p.client_id WHERE p.id=?',
        (post_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_clients_including_deleted():
    """Base-table read (sees soft-deleted). For trash/restore/purge ONLY.
    # raw-query-ok: trash/restore/purge must see deleted rows"""
    conn = get_db()
    rows = conn.execute('SELECT * FROM clients ORDER BY name').fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Per-client outbound webhooks ──────────────────────────────────────────────
# Reads go through v_client_webhooks_active. The base table is touched only by the
# writes below and by *_including_deleted (re-onboarding a removed webhook).

def get_client_webhook(client_id):
    """The active webhook row for a client, or None. Read via the view."""
    conn = get_db()
    row = conn.execute('SELECT * FROM v_client_webhooks_active WHERE client_id=?',
                       (client_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_client_webhooks():
    """Every active webhook joined to its client name — for the settings table."""
    conn = get_db()
    rows = conn.execute(
        'SELECT w.*, c.name AS client_name FROM v_client_webhooks_active w '
        'JOIN v_clients_active c ON c.id = w.client_id ORDER BY c.name').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_client_webhook_including_deleted(client_id):
    """Base-table read (sees soft-deleted). For re-onboarding a removed webhook.
    # raw-query-ok: must see deleted rows to re-activate"""
    conn = get_db()
    row = conn.execute('SELECT * FROM client_webhooks WHERE client_id=? ORDER BY id DESC LIMIT 1',
                       (client_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_client_webhook(client_id, webhook_url, webhook_secret, platforms_enabled):
    """Create the client's webhook or update the existing active one. A new URL/secret
    resets status to 'untested' — it must be re-tested before real content flows.
    Raises ValueError if the URL already belongs to a DIFFERENT active client (two
    clients on one scenario would publish to the wrong page)."""
    now = datetime.now().isoformat()
    conn = get_db()
    clash = conn.execute(
        'SELECT client_id FROM client_webhooks WHERE webhook_url=? AND deleted_at IS NULL '
        'AND client_id<>?', (webhook_url, client_id)).fetchone()
    if clash:
        conn.close()
        raise ValueError('That webhook URL is already assigned to another client. '
                         'Each client needs its own Make scenario / webhook URL.')
    existing = conn.execute(
        'SELECT id FROM client_webhooks WHERE client_id=? AND deleted_at IS NULL',
        (client_id,)).fetchone()
    if existing:
        conn.execute(
            'UPDATE client_webhooks SET webhook_url=?, webhook_secret=?, platforms_enabled=?, '
            "status='untested', updated_at=? WHERE id=?",
            (webhook_url, webhook_secret, platforms_enabled, now, existing['id']))
        wid = existing['id']
    else:
        cur = conn.execute(
            'INSERT INTO client_webhooks (client_id, webhook_url, webhook_secret, '
            'platforms_enabled, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
            (client_id, webhook_url, webhook_secret, platforms_enabled, 'untested', now, now))
        wid = cur.lastrowid
    conn.commit()
    conn.close()
    return wid


def record_webhook_result(client_id, success, error=None, is_test=False):
    """Persist a dispatch or test-ping outcome onto the client's active webhook row.
    Never stores the secret."""
    now = datetime.now().isoformat()
    test_set = ', last_test_at=?' if is_test else ''
    conn = get_db()
    if success:
        conn.execute(
            "UPDATE client_webhooks SET status='verified', last_success_at=?, last_error=NULL, "
            "updated_at=?" + test_set + " WHERE client_id=? AND deleted_at IS NULL",
            [now, now] + ([now] if is_test else []) + [client_id])
    else:
        conn.execute(
            "UPDATE client_webhooks SET status='failing', last_error=?, updated_at=?" + test_set +
            " WHERE client_id=? AND deleted_at IS NULL",
            [error, now] + ([now] if is_test else []) + [client_id])
    conn.commit()
    conn.close()


def set_client_webhook_test_status(client_id, status, error=None):
    """Record a test-ping verdict: 'verified' | 'failing' | 'insecure'. 'insecure'
    means the scenario accepted a deliberately wrong secret — it is NOT verifying
    X-Secret and must not be treated as safe."""
    now = datetime.now().isoformat()
    conn = get_db()
    if status == 'verified':
        conn.execute("UPDATE client_webhooks SET status='verified', last_test_at=?, "
                     "last_success_at=?, last_error=NULL, updated_at=? "
                     "WHERE client_id=? AND deleted_at IS NULL", (now, now, now, client_id))
    else:
        conn.execute("UPDATE client_webhooks SET status=?, last_test_at=?, last_error=?, "
                     "updated_at=? WHERE client_id=? AND deleted_at IS NULL",
                     (status, now, error, now, client_id))
    conn.commit()
    conn.close()


def set_client_webhook_enabled(client_id, enabled):
    """Disable (status='disabled', dispatch refuses) or re-enable (status='untested',
    must be re-tested) a client's webhook without deleting the row."""
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE client_webhooks SET status=?, updated_at=? WHERE client_id=? AND deleted_at IS NULL",
        ('untested' if enabled else 'disabled', now, client_id))
    conn.commit()
    conn.close()


def delete_client_webhook(client_id):
    """Soft-delete the client's active webhook (frees the unique index for re-onboarding)."""
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE client_webhooks SET deleted_at=?, updated_at=? WHERE client_id=? AND deleted_at IS NULL",
        (now, now, client_id))
    conn.commit()
    conn.close()


def add_audit(actor_user_id, actor_role, tenant_client_id, entity_type, entity_id,
              action, reason=None, metadata=None, request_ip=None):
    """Append-only audit row (Stage 2 audit_log). metadata is json.dumps'd.
    NEVER pass a secret in reason or metadata."""
    conn = get_db()
    conn.execute(
        'INSERT INTO audit_log (actor_user_id, actor_role, tenant_client_id, entity_type, '
        'entity_id, action, reason, metadata, request_ip, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (actor_user_id, actor_role, tenant_client_id, entity_type, entity_id, action, reason,
         json.dumps(metadata) if metadata is not None else None, request_ip,
         datetime.now().isoformat()))
    conn.commit()
    conn.close()


def create_post(data):
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO content_posts (client_id,platform,content_type,topic,caption,hashtags,image_url,hook,status,scheduled_date,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        data['client_id'], data['platform'], data.get('content_type', 'photo'),
        data['topic'], data['caption'],
        data.get('hashtags', ''), data.get('image_url', ''), data.get('hook', ''),
        data.get('status', 'draft'),
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
    _raise_if_deleted(conn, 'content_posts', post_id)   # write guard
    conn.execute('''
        UPDATE content_posts SET topic=?,caption=?,hashtags=?,image_url=?,content_type=?,hook=?,scheduled_date=?,notes=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    ''', (data['topic'], data['caption'], data.get('hashtags', ''), data.get('image_url', ''),
          data.get('content_type', 'photo'), data.get('hook', ''),
          data.get('scheduled_date') or None, data.get('notes', ''), post_id))
    conn.commit()
    conn.close()


def update_post_status(post_id, new_status, notes='', changed_by='user', posted_url=None):
    conn = get_db()
    post = conn.execute('SELECT status, deleted_at FROM content_posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        conn.close()
        return False
    if post['deleted_at'] is not None:      # write guard: a deleted post cannot reach the publish queue
        conn.close()
        raise ValueError('content_posts id=%s is deleted; refusing to change status' % post_id)
    old_status = post['status']
    update_clause = 'status=?, updated_at=CURRENT_TIMESTAMP'
    params = [new_status]
    if new_status == 'posted':
        update_clause += ', posted_date=?'
        params.append(datetime.now().strftime('%Y-%m-%d %H:%M'))
    if posted_url:
        update_clause += ', posted_url=?'
        params.append(posted_url)
    params.append(post_id)
    conn.execute(f'UPDATE content_posts SET {update_clause} WHERE id=?', params)
    conn.execute('INSERT INTO approval_history (post_id,from_status,to_status,notes,changed_by) VALUES (?,?,?,?,?)',
                 (post_id, old_status, new_status, notes, changed_by))
    conn.commit()
    conn.close()
    return True


def set_post_error(post_id, error_message):
    conn = get_db()
    _raise_if_deleted(conn, 'content_posts', post_id)   # write guard
    conn.execute(
        'UPDATE content_posts SET error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
        (error_message, post_id)
    )
    conn.commit()
    conn.close()


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
        INSERT INTO performance_metrics (post_id,likes,comments,shares,saves,views,reach,impressions,clicks,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    ''', (
        post_id,
        int(data.get('likes', 0) or 0), int(data.get('comments', 0) or 0),
        int(data.get('shares', 0) or 0), int(data.get('saves', 0) or 0),
        int(data.get('views', 0) or 0),
        int(data.get('reach', 0) or 0), int(data.get('impressions', 0) or 0),
        int(data.get('clicks', 0) or 0), data.get('notes', '')
    ))
    conn.commit()
    conn.close()


def get_dashboard_stats(scope=None):
    """scope = a client_id to restrict every aggregate to that tenant (client
    portal), or None for the org-wide admin/manager view."""
    conn = get_db()
    cw = ' WHERE client_id = ?' if scope is not None else ''      # content_posts (no alias)
    pw = ' AND p.client_id = ?' if scope is not None else ''      # aliased p.*
    one = (scope,) if scope is not None else ()

    status_counts = {r['status']: r['cnt'] for r in conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM v_content_active" + cw + " GROUP BY status", one
    ).fetchall()}

    platform_counts = [dict(r) for r in conn.execute(
        "SELECT platform, COUNT(*) AS cnt FROM v_content_active" + cw + " GROUP BY platform ORDER BY cnt DESC", one
    ).fetchall()]

    if scope is not None:
        client_counts = [dict(r) for r in conn.execute(
            "SELECT c.name, c.logo_color, COUNT(p.id) AS cnt FROM v_clients_active c "
            "LEFT JOIN v_content_active p ON p.client_id=c.id WHERE c.id=? GROUP BY c.id", (scope,)
        ).fetchall()]
    else:
        client_counts = [dict(r) for r in conn.execute(
            "SELECT c.name, c.logo_color, COUNT(p.id) AS cnt FROM v_clients_active c "
            "LEFT JOIN v_content_active p ON p.client_id=c.id GROUP BY c.id"
        ).fetchall()]

    upcoming = [dict(r) for r in conn.execute(
        "SELECT p.*, c.name AS client_name, c.logo_color "
        "FROM v_content_active p JOIN v_clients_active c ON c.id=p.client_id "
        "WHERE p.status IN ('approved','scheduled') AND p.scheduled_date IS NOT NULL" + pw +
        " ORDER BY p.scheduled_date ASC LIMIT 5", one
    ).fetchall()]

    recent = [dict(r) for r in conn.execute(
        "SELECT p.*, c.name AS client_name, c.logo_color "
        "FROM v_content_active p JOIN v_clients_active c ON c.id=p.client_id" +
        (" WHERE p.client_id = ?" if scope is not None else "") +
        " ORDER BY p.updated_at DESC LIMIT 6", one
    ).fetchall()]

    total_posts = conn.execute("SELECT COUNT(*) FROM v_content_active" + cw, one).fetchone()[0]

    if scope is not None:
        perf = conn.execute(
            "SELECT SUM(m.likes) AS likes, SUM(m.comments) AS comments, SUM(m.shares) AS shares, "
            "SUM(m.views) AS views, SUM(m.reach) AS reach, SUM(m.impressions) AS impressions "
            "FROM performance_metrics m JOIN v_content_active p ON p.id=m.post_id WHERE p.client_id = ?", (scope,)
        ).fetchone()
    else:
        perf = conn.execute(
            "SELECT SUM(likes) AS likes, SUM(comments) AS comments, SUM(shares) AS shares, "
            "SUM(views) AS views, SUM(reach) AS reach, SUM(impressions) AS impressions "
            "FROM performance_metrics"
        ).fetchone()

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


def get_scheduled_posts(scope=None):
    conn = get_db()
    rows = conn.execute(
        "SELECT p.*, c.name AS client_name, c.logo_color "
        "FROM v_content_active p JOIN v_clients_active c ON c.id=p.client_id "
        "WHERE p.scheduled_date IS NOT NULL" +
        (" AND p.client_id = ?" if scope is not None else "") +
        " ORDER BY p.scheduled_date ASC",
        (scope,) if scope is not None else ()
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_report_data(start_date, end_date):
    conn = get_db()

    posts = [dict(r) for r in conn.execute('''
        SELECT p.*, c.name AS client_name
        FROM v_content_active p JOIN v_clients_active c ON c.id=p.client_id
        WHERE p.created_at BETWEEN ? AND ?
        ORDER BY p.created_at DESC
    ''', (start_date, end_date)).fetchall()]

    posted = [dict(r) for r in conn.execute('''
        SELECT p.*, c.name AS client_name
        FROM v_content_active p JOIN v_clients_active c ON c.id=p.client_id
        WHERE p.posted_date BETWEEN ? AND ?
    ''', (start_date, end_date)).fetchall()]

    perf = conn.execute('''
        SELECT SUM(m.likes) AS likes, SUM(m.comments) AS comments,
               SUM(m.shares) AS shares, SUM(m.saves) AS saves,
               SUM(m.views) AS views, SUM(m.reach) AS reach, SUM(m.impressions) AS impressions
        FROM performance_metrics m
        JOIN v_content_active p ON p.id=m.post_id
        WHERE m.recorded_at BETWEEN ? AND ?
    ''', (start_date, end_date)).fetchone()

    platform_breakdown = [dict(r) for r in conn.execute('''
        SELECT platform, COUNT(*) AS cnt
        FROM v_content_active WHERE created_at BETWEEN ? AND ?
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


def get_trends(platform=None, limit=50):
    conn = get_db()
    query = 'SELECT * FROM trends'
    params = []
    if platform:
        query += ' WHERE platform=?'
        params.append(platform)
    query += ' ORDER BY created_at DESC LIMIT ?'
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_trends(rows):
    if not rows:
        return
    from datetime import date
    week_of = date.today().strftime('%Y-%m-%d')
    conn = get_db()
    for row in rows:
        conn.execute(
            'INSERT INTO trends (platform,trend_text,category,client_id,week_of,source) VALUES (?,?,?,?,?,?)',
            (row.get('platform', ''), row.get('trend_text', ''),
             row.get('category', ''), row.get('client_id') or None,
             row.get('week_of', week_of), row.get('source', 'ai'))
        )
    conn.commit()
    conn.close()


# ── Media Gallery ──────────────────────────────────────────────────────────────

def add_media(client_id, filename, original_name, media_type, file_size=0, caption_hint='', tags='[]'):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        'INSERT INTO client_media (client_id,filename,original_name,media_type,file_size,caption_hint,tags) VALUES (?,?,?,?,?,?,?)',
        (client_id, filename, original_name, media_type, file_size, caption_hint, tags)
    )
    media_id = c.lastrowid
    conn.commit()
    conn.close()
    return media_id


def get_client_media(client_id, media_type=None):
    conn = get_db()
    query = 'SELECT * FROM client_media WHERE client_id=?'
    params = [client_id]
    if media_type:
        query += ' AND media_type=?'
        params.append(media_type)
    query += ' ORDER BY created_at DESC'
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_media(media_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM client_media WHERE id=?', (media_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_media(media_id, caption_hint='', tags='[]'):
    conn = get_db()
    conn.execute(
        'UPDATE client_media SET caption_hint=?, tags=? WHERE id=?',
        (caption_hint, tags, media_id)
    )
    conn.commit()
    conn.close()


def delete_media(media_id):
    conn = get_db()
    conn.execute('DELETE FROM post_media WHERE media_id=?', (media_id,))
    conn.execute('DELETE FROM client_media WHERE id=?', (media_id,))
    conn.commit()
    conn.close()


def attach_media_to_post(post_id, media_id, sort_order=0):
    conn = get_db()
    conn.execute(
        'INSERT OR IGNORE INTO post_media (post_id,media_id,sort_order) VALUES (?,?,?)',
        (post_id, media_id, sort_order)
    )
    conn.commit()
    conn.close()


def detach_media_from_post(post_id, media_id):
    conn = get_db()
    conn.execute('DELETE FROM post_media WHERE post_id=? AND media_id=?', (post_id, media_id))
    conn.commit()
    conn.close()


def get_post_media(post_id):
    conn = get_db()
    rows = conn.execute('''
        SELECT m.* FROM client_media m
        JOIN post_media pm ON pm.media_id = m.id
        WHERE pm.post_id=?
        ORDER BY pm.sort_order ASC, m.created_at ASC
    ''', (post_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Users / auth ─────────────────────────────────────────────────────────────

def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return row


def get_user_by_email(email):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    conn.close()
    return row


def any_users():
    conn = get_db()
    n = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    return n > 0


def create_user(email, password_hash, role='admin', client_id=None):
    # Code-level enforcement of the tenancy invariant that SQLite can't express
    # as a CHECK on an existing table: a client user MUST be bound to a client,
    # and admin/manager must NOT be. (chk_client_user_scope in the spec.)
    if role == 'client' and client_id is None:
        raise ValueError("a 'client' user must be bound to a client_id")
    if role != 'client' and client_id is not None:
        raise ValueError("only a 'client' user may have a client_id")
    conn = get_db()
    c = conn.cursor()
    c.execute(
        'INSERT INTO users (email, password_hash, role, client_id) VALUES (?, ?, ?, ?)',
        (email, password_hash, role, client_id),
    )
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return new_id


def set_password(email, password_hash):
    conn = get_db()
    conn.execute('UPDATE users SET password_hash = ? WHERE email = ?', (password_hash, email))
    conn.commit()
    conn.close()


# ── User management (Stage 1: roles & client portal) ──

def get_users():
    """All users with their linked client name — for the /users admin page."""
    conn = get_db()
    rows = conn.execute('''
        SELECT u.id, u.email, u.role, u.client_id, u.is_active,
               u.last_login_at, u.created_at,
               u.invite_token_hash, u.invite_expires_at,
               c.name AS client_name
        FROM users u LEFT JOIN v_clients_active c ON c.id = u.client_id
        ORDER BY u.created_at DESC
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_pending_user(email, role, client_id, token_hash, expires_at):
    """Invite a user: created inactive with no usable password until the invite
    is consumed. Same tenancy invariant as create_user() — a 'client' user MUST
    be bound to a client, anything else MUST NOT be."""
    if role == 'client' and client_id is None:
        raise ValueError("a 'client' user must be bound to a client_id")
    if role != 'client' and client_id is not None:
        raise ValueError("only a 'client' user may have a client_id")
    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO users (email, password_hash, role, client_id,
                              invite_token_hash, invite_expires_at, is_active)
           VALUES (?, '', ?, ?, ?, ?, 0)''',
        (email, role, client_id, token_hash, expires_at),
    )
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return new_id


def create_pending_client_user(email, client_id, token_hash, expires_at):
    """Back-compat shim for the client-only invite path."""
    return create_pending_user(email, 'client', client_id, token_hash, expires_at)


def count_active_admins(exclude_user_id=None):
    """How many admins could still log in. Guards the last-admin lockout: an
    invited-but-not-yet-accepted admin has is_active=0 and does NOT count."""
    conn = get_db()
    sql = "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1"
    params = []
    if exclude_user_id is not None:
        sql += ' AND id != ?'
        params.append(exclude_user_id)
    n = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return n


def set_user_invite(user_id, token_hash, expires_at):
    """(Re)issue an invite token for an existing user — also used for password reset."""
    conn = get_db()
    conn.execute(
        'UPDATE users SET invite_token_hash=?, invite_expires_at=? WHERE id=?',
        (token_hash, expires_at, user_id),
    )
    conn.commit()
    conn.close()


def get_user_by_invite_hash(token_hash):
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM users WHERE invite_token_hash = ?', (token_hash,)
    ).fetchone()
    conn.close()
    return row


def consume_invite(user_id, password_hash):
    """Set the password, clear the invite, activate the account."""
    conn = get_db()
    conn.execute(
        '''UPDATE users SET password_hash=?, invite_token_hash=NULL,
               invite_expires_at=NULL, is_active=1 WHERE id=?''',
        (password_hash, user_id),
    )
    conn.commit()
    conn.close()


def set_user_active(user_id, active):
    conn = get_db()
    conn.execute('UPDATE users SET is_active=? WHERE id=?', (1 if active else 0, user_id))
    conn.commit()
    conn.close()


def deactivate_users_for_client(client_id):
    """Used when a client is (soft-)deleted — block their logins."""
    conn = get_db()
    conn.execute('UPDATE users SET is_active=0 WHERE client_id=?', (client_id,))
    conn.commit()
    conn.close()


def update_last_login(user_id):
    conn = get_db()
    conn.execute('UPDATE users SET last_login_at=? WHERE id=?',
                 (datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()


# ── Voice engine ─────────────────────────────────────────────────────────────

def get_client_voice(client_id):
    """Full-fidelity voice context for a client: the voice document and the
    list of real sample captions. Returns (voice_document, [sample_captions])."""
    conn = get_db()
    row = conn.execute(
        'SELECT voice_document, sample_captions FROM v_clients_active WHERE id = ?',
        (client_id,),
    ).fetchone()
    conn.close()
    if not row:
        return '', []
    doc = row['voice_document'] or ''
    try:
        samples = json.loads(row['sample_captions'] or '[]')
    except (ValueError, TypeError):
        samples = []
    return doc, samples


def update_client_voice(client_id, voice_document, sample_captions):
    """sample_captions: a list of strings (10–15 real captions)."""
    conn = get_db()
    _raise_if_deleted(conn, 'clients', client_id)   # write guard
    conn.execute(
        'UPDATE clients SET voice_document = ?, sample_captions = ? WHERE id = ?',
        (voice_document or '', json.dumps(sample_captions or []), client_id),
    )
    conn.commit()
    conn.close()


def set_post_voice_audit(post_id, score, audit_notes):
    conn = get_db()
    _raise_if_deleted(conn, 'content_posts', post_id)   # write guard
    conn.execute(
        'UPDATE content_posts SET voice_score = ?, voice_audit = ? WHERE id = ?',
        (score, audit_notes or '', post_id),
    )
    conn.commit()
    conn.close()
