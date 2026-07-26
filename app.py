import os
import json
import uuid
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import database as db
import claude_api as ai
import voice_engine as ve
import config
import webhooks
import auth
import security
from security import (current_scope, enforce_client_id, require_content_access,
                     require_client_access, scoped_posts, scoped_clients, roles_required)
from flask import abort, g
from flask_login import login_user
from werkzeug.security import generate_password_hash
import hashlib


def _hash_token(tok):
    """sha256 of an invite token — only the hash is stored, like the setup token."""
    return hashlib.sha256(tok.encode('utf-8')).hexdigest()

# Pillow is used to serve web-optimized (downscaled) copies of large images so Facebook/Instagram
# can fetch them — social APIs reject oversized files. Optional: falls back to the raw file if absent.
try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None   # trusted uploads; allow large originals to be downscaled
except Exception:
    Image = None

load_dotenv()

app = Flask(__name__)

# No hardcoded secret. Render provides SECRET_KEY (generateValue) and sets RENDER=true.
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if os.environ.get('RENDER'):
        raise RuntimeError('SECRET_KEY must be set in production')
    SECRET_KEY = secrets.token_hex(32)   # ephemeral local-dev key; sessions reset on restart
app.secret_key = SECRET_KEY

# Secure session cookies. Secure requires HTTPS, so enable it in production only.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
)

# Media upload config
UPLOAD_PATH = os.environ.get('UPLOAD_PATH', os.path.join('static', 'uploads'))
ALLOWED_IMAGES = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
ALLOWED_VIDEOS = {'mp4', 'mov', 'avi', 'webm'}
ALLOWED_EXTENSIONS = ALLOWED_IMAGES | ALLOWED_VIDEOS
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '200'))
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _media_type(filename):
    ext = filename.rsplit('.', 1)[1].lower()
    return 'video' if ext in ALLOWED_VIDEOS else 'image'


def _upload_dir(client_id):
    path = os.path.join(UPLOAD_PATH, str(client_id))
    os.makedirs(path, exist_ok=True)
    return path


def _media_url(client_id, filename):
    return url_for('serve_media', client_id=client_id, filename=filename)

PLATFORMS = ['instagram', 'facebook', 'tiktok', 'linkedin', 'youtube']
STATUSES = ['raw', 'branded', 'draft', 'needs_review', 'approved', 'scheduled', 'posted', 'error']
CONTENT_TYPES = {
    'instagram': ['photo', 'video', 'reel', 'story'],
    'facebook':  ['photo', 'video', 'post'],
    'tiktok':    ['video'],
    'linkedin':  ['post'],
    'youtube':   ['video'],
}
STATUS_TRANSITIONS = {
    'raw':          ['branded'],
    'branded':      ['needs_review', 'approved'],
    'draft':        ['needs_review', 'approved'],
    'needs_review': ['draft', 'approved'],
    'approved':     ['scheduled', 'posted', 'needs_review'],
    'scheduled':    ['approved', 'posted'],
    'posted':       [],
    'error':        ['approved', 'draft'],
}
STATUS_COLORS = {
    'raw':          'light',
    'branded':      'info text-dark',
    'draft':        'secondary',
    'needs_review': 'warning',
    'approved':     'info',
    'scheduled':    'primary',
    'posted':       'success',
    'error':        'danger',
}


_db_ready = False

@app.before_request
def setup():
    global _db_ready
    if not _db_ready:
        db.init_db()
        auth.bootstrap_admin()
        _db_ready = True


# ── Health check ────────────────────────────────────────────────────────────
# Public, always 200 — the login guard would otherwise 302 the root path and
# fail Render's health check (which requires a 200-level status).

@app.route('/healthz')
def healthz():
    return 'ok', 200


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    # scope=None for admin/manager (org-wide); the client's own id for a client user.
    stats = db.get_dashboard_stats(scope=current_scope())
    return render_template('dashboard.html', stats=stats, platforms=PLATFORMS, statuses=STATUSES,
                           content_types=CONTENT_TYPES, scope=current_scope())


# ── Clients ────────────────────────────────────────────────────────────────────

@app.route('/clients')
def clients():
    # A client user has no all-clients view — send them to their own client page.
    if current_scope() is not None:
        return redirect(url_for('client_detail', client_id=current_scope()))
    all_clients = db.get_clients()
    return render_template('clients.html', clients=all_clients)


@app.route('/clients/new', methods=['GET', 'POST'])
@roles_required('admin')          # create clients: admin only
def client_new():
    if request.method == 'POST':
        data = {
            'name': request.form['name'].strip(),
            'description': request.form.get('description', '').strip(),
            'contact_email': request.form.get('contact_email', '').strip(),
            'logo_color': request.form.get('logo_color', '#6366f1'),
        }
        if not data['name']:
            flash('Client name is required.', 'error')
            return render_template('client_form.html', client=None)
        new_id = db.create_client(data)
        flash(f"Client '{data['name']}' created successfully.", 'success')
        return redirect(url_for('client_detail', client_id=new_id))
    return render_template('client_form.html', client=None)


@app.route('/clients/<int:client_id>')
@require_client_access('client_id')     # a client user may only open their own record
def client_detail(client_id):
    client = db.get_client(client_id)
    if not client:
        flash('Client not found.', 'error')
        return redirect(url_for('clients'))
    voices = db.get_all_brand_voices(client_id)
    posts = db.get_posts(client_id=client_id, limit=10)
    return render_template('client_detail.html', client=client, voices=voices,
                           posts=posts, platforms=PLATFORMS)


@app.route('/clients/<int:client_id>/edit', methods=['GET', 'POST'])
@roles_required('admin')            # edit clients: admin only
def client_edit(client_id):
    client = db.get_client(client_id)
    if not client:
        flash('Client not found.', 'error')
        return redirect(url_for('clients'))
    if request.method == 'POST':
        data = {
            'name': request.form['name'].strip(),
            'description': request.form.get('description', '').strip(),
            'contact_email': request.form.get('contact_email', '').strip(),
            'logo_color': request.form.get('logo_color', '#6366f1'),
        }
        db.update_client(client_id, data)
        # Voice engine: full voice document + real sample captions (blank-line separated)
        voice_document = request.form.get('voice_document', '').strip()
        raw_samples = request.form.get('sample_captions', '')
        sample_captions = [c.strip() for c in raw_samples.split('\n\n') if c.strip()]
        db.update_client_voice(client_id, voice_document, sample_captions)
        flash('Client updated.', 'success')
        return redirect(url_for('client_detail', client_id=client_id))
    voice_document, sample_captions = db.get_client_voice(client_id)
    return render_template('client_form.html', client=client,
                           voice_document=voice_document,
                           sample_captions_text='\n\n'.join(sample_captions))


# ── Media Gallery ─────────────────────────────────────────────────────────────

IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
WEB_MAX = 1600   # cap the longest edge so Facebook/Instagram accept the fetched image

@app.route('/uploads/<int:client_id>/<path:filename>')
def serve_media(client_id, filename):
    directory = os.path.join(UPLOAD_PATH, str(client_id))
    original = os.path.join(directory, filename)
    ext = os.path.splitext(filename)[1].lower()
    # Serve a downscaled, re-compressed copy of large images (cached next to the original on the
    # persistent disk). Social APIs reject oversized files. Any failure falls back to the raw file.
    if Image is not None and ext in IMG_EXTS and not filename.endswith('_web.jpg') and os.path.isfile(original):
        web = os.path.join(directory, os.path.splitext(filename)[0] + '_web.jpg')
        try:
            if not os.path.isfile(web) or os.path.getmtime(web) < os.path.getmtime(original):
                im = Image.open(original)
                im.draft('RGB', (WEB_MAX, WEB_MAX))   # cheap JPEG downscale-on-decode (low memory)
                if im.mode != 'RGB':
                    im = im.convert('RGB')
                im.thumbnail((WEB_MAX, WEB_MAX))
                im.save(web, 'JPEG', quality=82, optimize=True)
            return send_file(web, mimetype='image/jpeg')
        except Exception:
            pass
    return send_from_directory(directory, filename)


@app.route('/clients/<int:client_id>/gallery')
@require_client_access('client_id')
def client_gallery(client_id):
    client = db.get_client(client_id)
    if not client:
        flash('Client not found.', 'error')
        return redirect(url_for('clients'))
    media = db.get_client_media(client_id)
    for m in media:
        m['url'] = _media_url(client_id, m['filename'])
    return render_template('client_gallery.html', client=client, media=media)


@app.route('/clients/<int:client_id>/media/upload', methods=['POST'])
@require_client_access('client_id')
def media_upload(client_id):
    client = db.get_client(client_id)
    if not client:
        return jsonify({'error': 'Client not found'}), 404

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files provided'}), 400

    uploaded = []
    errors = []
    for f in files:
        if not f or not f.filename:
            continue
        if not _allowed_file(f.filename):
            errors.append(f'{f.filename}: file type not allowed')
            continue
        ext = f.filename.rsplit('.', 1)[1].lower()
        unique_name = f'{uuid.uuid4().hex}.{ext}'
        save_dir = _upload_dir(client_id)
        f.save(os.path.join(save_dir, unique_name))
        size = os.path.getsize(os.path.join(save_dir, unique_name))
        mtype = _media_type(f.filename)
        caption_hint = request.form.get('caption_hint', '')
        tags = request.form.get('tags', '[]')
        media_id = db.add_media(client_id, unique_name, secure_filename(f.filename),
                                mtype, size, caption_hint, tags)
        uploaded.append({
            'id': media_id,
            'filename': unique_name,
            'original_name': secure_filename(f.filename),
            'media_type': mtype,
            'url': _media_url(client_id, unique_name),
        })

    return jsonify({'ok': True, 'uploaded': uploaded, 'errors': errors})


@app.route('/api/media/<int:media_id>', methods=['PATCH'])
def api_media_update(media_id):
    media = db.get_media(media_id)
    if not media:
        return jsonify({'error': 'Not found'}), 404
    if not security.can_see_client(media['client_id']):   # object-level tenant check
        abort(403)
    data = request.get_json(silent=True) or {}
    db.update_media(media_id,
                    caption_hint=data.get('caption_hint', media.get('caption_hint', '')),
                    tags=data.get('tags', media.get('tags', '[]')))
    return jsonify({'ok': True})


@app.route('/api/media/<int:media_id>', methods=['DELETE'])
def api_media_delete(media_id):
    media = db.get_media(media_id)
    if not media:
        return jsonify({'error': 'Not found'}), 404
    if not security.can_see_client(media['client_id']):   # object-level tenant check
        abort(403)
    file_path = os.path.join(UPLOAD_PATH, str(media['client_id']), media['filename'])
    if os.path.exists(file_path):
        os.remove(file_path)
    db.delete_media(media_id)
    return jsonify({'ok': True})


@app.route('/api/media/client/<int:client_id>')
@require_client_access('client_id')
def api_client_media(client_id):
    media_type = request.args.get('type')
    media = db.get_client_media(client_id, media_type or None)
    for m in media:
        m['url'] = _media_url(client_id, m['filename'])
    return jsonify(media)


@app.route('/api/content/<int:post_id>/media', methods=['POST'])
@require_content_access('post_id')
def api_attach_media(post_id):
    data = request.get_json(silent=True) or {}
    media_id = data.get('media_id')
    if not media_id:
        return jsonify({'error': 'media_id required'}), 400
    _m = db.get_media(media_id)
    if _m and not security.can_see_client(_m['client_id']):   # no cross-client media
        abort(403)
    db.attach_media_to_post(post_id, media_id, data.get('sort_order', 0))
    media = db.get_media(media_id)
    if media:
        merged = db.get_post(post_id)
        if merged and not merged.get('image_url'):
            db.update_post(post_id, {
                'topic': merged['topic'], 'caption': merged['caption'],
                'hashtags': merged.get('hashtags', ''), 'image_url': _media_url(media['client_id'], media['filename']),
                'hook': merged.get('hook', ''), 'content_type': merged.get('content_type', 'photo'),
                'scheduled_date': merged.get('scheduled_date'), 'notes': merged.get('notes', ''),
            })
    return jsonify({'ok': True})


@app.route('/api/content/<int:post_id>/media/<int:media_id>', methods=['DELETE'])
@require_content_access('post_id')
def api_detach_media(post_id, media_id):
    db.detach_media_from_post(post_id, media_id)
    return jsonify({'ok': True})


# ── Brand Voice ────────────────────────────────────────────────────────────────

@app.route('/api/brand-voice/<int:client_id>/<platform>', methods=['POST'])
@require_client_access('client_id')
def save_brand_voice(client_id, platform):
    if platform not in PLATFORMS + ['general']:
        return jsonify({'error': 'Invalid platform'}), 400
    data = request.get_json()
    db.upsert_brand_voice(client_id, platform, data)
    return jsonify({'ok': True})


# ── Caption Generator ──────────────────────────────────────────────────────────

@app.route('/caption-generator')
def caption_generator():
    all_clients = scoped_clients()   # a client user sees only their own client
    preselect_client = current_scope() or request.args.get('client_id', type=int)
    preselect_platform = request.args.get('platform', '')
    return render_template('caption_generator.html', clients=all_clients,
                           platforms=PLATFORMS, preselect_client=preselect_client,
                           preselect_platform=preselect_platform)


@app.route('/api/generate-caption', methods=['POST'])
def api_generate_caption():
    data = request.get_json()
    # HARD RULE 1: never trust client_id from the body for a client user.
    client_id = enforce_client_id(data.get('client_id'))
    platform = data.get('platform')
    topic = data.get('topic', '').strip()
    extra = data.get('extra_context', '').strip()

    if not all([client_id, platform, topic]):
        return jsonify({'error': 'client_id, platform, and topic are required.'}), 400

    client = db.get_client(client_id)
    if not client:
        return jsonify({'error': 'Client not found.'}), 404

    brand_voice = dict(db.get_brand_voice(client_id, platform) or db.get_brand_voice(client_id, 'general') or {})
    brand_voice['platform'] = platform   # ensure platform rules match the selection

    # Full-fidelity voice: inject the entire voice document + real sample captions.
    voice_document, sample_captions = db.get_client_voice(client_id)

    result = ve.generate_post(client['name'], brand_voice, topic,
                              voice_document=voice_document,
                              sample_captions=sample_captions,
                              extra_context=extra,
                              debug=config.DEBUG_ENGINE)
    if result.get('error'):
        return jsonify({'error': result['error']}), 500

    payload = {
        'caption': result['caption'],
        'hashtags': result['hashtags'],
        'voice_score': result.get('voice_score'),
        'voice_audit': result.get('voice_audit', ''),
    }
    if config.DEBUG_ENGINE:   # hidden unless DEBUG_ENGINE=1 (admin-only page anyway)
        payload['debug'] = {
            'usage': result.get('usage'),
            'system_prompt': result.get('system_prompt', ''),
            'system_prompt_chars': result.get('system_prompt_chars'),
            'voice_document_chars': result.get('voice_document_chars'),
        }
    return jsonify(payload)


@app.route('/api/save-caption', methods=['POST'])
def api_save_caption():
    data = request.get_json()
    # HARD RULE 1: a client user's post is always created under THEIR client_id,
    # never a forged one from the request body.
    data['client_id'] = enforce_client_id(data.get('client_id'))
    required = ['client_id', 'platform', 'topic', 'caption']
    if not all(data.get(k) for k in required):
        return jsonify({'error': 'Missing required fields.'}), 400
    post_id = db.create_post(data)
    return jsonify({'ok': True, 'post_id': post_id})


# ── Content Library ────────────────────────────────────────────────────────────

@app.route('/content')
def content_list():
    client_id = request.args.get('client_id', type=int)
    platform = request.args.get('platform', '')
    status = request.args.get('status', '')
    # scoped_posts imposes the client user's own client_id regardless of the filter.
    posts = scoped_posts(
        client_id=client_id or None,
        platform=platform or None,
        status=status or None,
        limit=50
    )
    all_clients = scoped_clients()
    return render_template('content_list.html', posts=posts, clients=all_clients,
                           platforms=PLATFORMS, statuses=STATUSES,
                           filter_client=client_id, filter_platform=platform,
                           filter_status=status)


@app.route('/content/new', methods=['GET', 'POST'])
def content_new():
    all_clients = scoped_clients()
    if request.method == 'POST':
        # HARD RULE 1: client user's client_id comes from the session, not the form.
        submitted_cid = request.form.get('client_id', type=int)
        data = {
            'client_id': enforce_client_id(submitted_cid),
            'platform': request.form['platform'],
            'content_type': request.form.get('content_type', 'photo'),
            'topic': request.form['topic'].strip(),
            'caption': request.form['caption'].strip(),
            'hashtags': request.form.get('hashtags', '').strip(),
            'image_url': request.form.get('image_url', '').strip(),
            'status': request.form.get('status', 'draft'),
            'scheduled_date': request.form.get('scheduled_date') or None,
            'notes': request.form.get('notes', '').strip(),
        }
        if not data['topic'] or not data['caption']:
            flash('Topic and caption are required.', 'error')
            return render_template('content_form.html', post=None, clients=all_clients,
                                   platforms=PLATFORMS, statuses=STATUSES,
                                   content_types=CONTENT_TYPES, preselect={})
        post_id = db.create_post(data)
        flash('Post created successfully.', 'success')
        return redirect(url_for('content_detail', post_id=post_id))
    preselect = {
        'client_id': request.args.get('client_id', ''),
        'platform': request.args.get('platform', ''),
    }
    return render_template('content_form.html', post=None, clients=all_clients,
                           platforms=PLATFORMS, statuses=STATUSES,
                           content_types=CONTENT_TYPES, preselect=preselect)


@app.route('/content/<int:post_id>')
@require_content_access('post_id')
def content_detail(post_id):
    post = db.get_post(post_id)
    if not post:
        flash('Post not found.', 'error')
        return redirect(url_for('content_list'))
    history = db.get_approval_history(post_id)
    metrics = db.get_performance(post_id)
    allowed_transitions = STATUS_TRANSITIONS.get(post['status'], [])
    post_media = db.get_post_media(post_id)
    for m in post_media:
        m['url'] = _media_url(m['client_id'], m['filename'])
    client_media = db.get_client_media(post['client_id'])
    for m in client_media:
        m['url'] = _media_url(m['client_id'], m['filename'])
    return render_template('content_detail.html', post=post, history=history,
                           metrics=metrics, allowed_transitions=allowed_transitions,
                           statuses=STATUSES, post_media=post_media,
                           client_media=client_media)


@app.route('/content/<int:post_id>/edit', methods=['GET', 'POST'])
@require_content_access('post_id')
def content_edit(post_id):
    post = g.content_row   # loaded + scope-checked by the decorator
    # Matrix: a client user may edit their own content only while NOT yet posted.
    if current_scope() is not None and post.get('status') == 'posted':
        abort(403)
    all_clients = scoped_clients()
    if request.method == 'POST':
        data = {
            'topic': request.form['topic'].strip(),
            'caption': request.form['caption'].strip(),
            'hashtags': request.form.get('hashtags', '').strip(),
            'image_url': request.form.get('image_url', '').strip(),
            'content_type': request.form.get('content_type', post.get('content_type', 'photo')),
            'scheduled_date': request.form.get('scheduled_date') or None,
            'notes': request.form.get('notes', '').strip(),
        }
        db.update_post(post_id, data)
        flash('Post updated.', 'success')
        return redirect(url_for('content_detail', post_id=post_id))
    return render_template('content_form.html', post=post, clients=all_clients,
                           platforms=PLATFORMS, statuses=STATUSES,
                           content_types=CONTENT_TYPES, preselect={})


@app.route('/content/<int:post_id>/status', methods=['POST'])
@require_content_access('post_id')
def content_status(post_id):
    new_status = request.form.get('status')
    notes = request.form.get('notes', '')
    if new_status not in STATUSES:
        flash('Invalid status.', 'error')
        return redirect(url_for('content_detail', post_id=post_id))
    db.update_post_status(post_id, new_status, notes)
    flash(f'Status updated to "{new_status.replace("_", " ").title()}".', 'success')

    if new_status == 'approved':
        post = db.get_post(post_id)
        ok, err = webhooks.send_to_make(post)
        if ok:
            flash('Post sent to Make.com webhook.', 'success')
        elif err and 'not configured' not in err:
            flash(f'Make.com webhook failed: {err}', 'warning')

    return redirect(url_for('content_detail', post_id=post_id))


# ── Webhooks ───────────────────────────────────────────────────────────────────

@app.route('/webhook/publish', methods=['POST'])
def webhook_publish():
    """Inbound endpoint — Make.com calls this after publishing a post.

    Expected JSON body:
        { "post_id": 123, "secret": "...", "posted_url": "https://..." }
    """
    data = request.get_json(silent=True) or {}

    secret = data.get('secret', request.args.get('secret', ''))
    if not webhooks.verify_secret(secret):
        return jsonify({'error': 'Forbidden'}), 403

    post_id = data.get('post_id')
    if not post_id:
        return jsonify({'error': 'post_id is required'}), 400

    post = db.get_post(int(post_id))
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    posted_url = data.get('posted_url', '') or ''
    notes = posted_url or 'Marked posted by Make.com'
    db.update_post_status(int(post_id), 'posted', notes, changed_by='make.com',
                          posted_url=posted_url or None)
    return jsonify({'ok': True, 'post_id': post_id, 'status': 'posted'})


@app.route('/webhook/test', methods=['POST'])
def webhook_test():
    """Fire the Make.com webhook for a post manually (for testing)."""
    data = request.get_json(silent=True) or {}
    post_id = data.get('post_id')
    if not post_id:
        return jsonify({'error': 'post_id is required'}), 400
    post = db.get_post(int(post_id))
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    ok, err = webhooks.send_to_make(post)
    if ok:
        return jsonify({'ok': True, 'message': 'Webhook fired successfully'})
    return jsonify({'ok': False, 'error': err}), 500


@app.route('/content/<int:post_id>/delete', methods=['POST'])
@require_content_access('post_id')
def content_delete(post_id):
    post = g.content_row
    # Matrix: a client user may delete their own content only in draft/needs_review.
    # (Stage 2 will replace this hard delete with soft-delete + trash + status rules.)
    if current_scope() is not None and post.get('status') not in ('draft', 'needs_review'):
        abort(403)
    db.delete_post(post_id)
    flash('Post deleted.', 'success')
    return redirect(url_for('content_list'))


# ── Scheduling ─────────────────────────────────────────────────────────────────

@app.route('/scheduling')
def scheduling():
    scheduled = db.get_scheduled_posts(scope=current_scope())
    now = datetime.now()
    for p in scheduled:
        if p.get('scheduled_date'):
            try:
                dt = datetime.strptime(p['scheduled_date'], '%Y-%m-%d %H:%M')
                p['is_overdue'] = dt < now and p['status'] != 'posted'
                p['days_until'] = (dt - now).days
            except Exception:
                p['is_overdue'] = False
                p['days_until'] = None
    return render_template('scheduling.html', scheduled=scheduled, platforms=PLATFORMS)


# ── Performance ────────────────────────────────────────────────────────────────

@app.route('/performance')
def performance():
    platform = request.args.get('platform', '')
    client_id = request.args.get('client_id', type=int)
    all_clients = scoped_clients()

    # Only show posted posts; scoped_posts imposes the client user's own client_id.
    posted_posts = scoped_posts(platform=platform or None, client_id=client_id or None,
                                status='posted', limit=50)
    for p in posted_posts:
        metrics = db.get_performance(p['id'])
        if metrics:
            m = metrics[0]
            p['metrics'] = m
            total_eng = (m['likes'] or 0) + (m['comments'] or 0) + (m['shares'] or 0)
            reach = m['reach'] or 1
            p['engagement_rate'] = round((total_eng / reach) * 100, 2)
        else:
            p['metrics'] = None
            p['engagement_rate'] = None

    return render_template('performance.html', posts=posted_posts, clients=all_clients,
                           platforms=PLATFORMS, filter_platform=platform,
                           filter_client=client_id)


@app.route('/api/performance/<int:post_id>', methods=['POST'])
@require_content_access('post_id')
def api_add_performance(post_id):
    data = request.get_json()
    db.add_performance(post_id, data)
    return jsonify({'ok': True})


@app.route('/api/performance', methods=['POST'])
def api_performance_inbound():
    """Called by Make.com ~24h after publishing with auto-fetched platform stats.

    Expected JSON body:
        {
          "post_id": 123, "secret": "...",
          "likes": 0, "comments": 0, "shares": 0, "saves": 0,
          "reach": 0, "impressions": 0, "clicks": 0
        }
    """
    data = request.get_json(silent=True) or {}

    secret = data.get('secret', request.args.get('secret', ''))
    if not webhooks.verify_secret(secret):
        return jsonify({'error': 'Forbidden'}), 403

    post_id = data.get('post_id')
    if not post_id:
        return jsonify({'error': 'post_id required'}), 400
    if not db.get_post(int(post_id)):
        return jsonify({'error': 'Post not found'}), 404

    metrics = {
        'likes':       int(data.get('likes', 0) or 0),
        'comments':    int(data.get('comments', 0) or 0),
        'shares':      int(data.get('shares', 0) or 0),
        'saves':       int(data.get('saves', 0) or 0),
        'views':       int(data.get('views', 0) or 0),
        'reach':       int(data.get('reach', 0) or 0),
        'impressions': int(data.get('impressions', 0) or 0),
        'clicks':      int(data.get('clicks', 0) or 0),
        'notes':       data.get('notes', 'Auto-fetched by Make.com 24h post-publish'),
    }
    db.add_performance(int(post_id), metrics)
    return jsonify({'ok': True, 'post_id': post_id})


# ── Pipeline REST API ──────────────────────────────────────────────────────────

def _check_secret(request):
    secret = request.headers.get('X-Secret', '') or request.args.get('secret', '')
    return webhooks.verify_secret(secret)


@app.route('/api/content/<int:post_id>', methods=['GET'])
def api_content_get(post_id):
    if not _check_secret(request):
        return jsonify({'error': 'Forbidden'}), 403
    post = db.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    return jsonify(dict(post))


@app.route('/api/content/<int:post_id>', methods=['PATCH'])
def api_content_patch(post_id):
    if not _check_secret(request):
        return jsonify({'error': 'Forbidden'}), 403
    post = db.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    data = request.get_json(silent=True) or {}
    updatable = ['caption', 'hashtags', 'hook', 'image_url', 'posted_url', 'error_message', 'notes']
    patch = {k: data[k] for k in updatable if k in data}

    new_status = data.get('status')
    if new_status and new_status != post['status']:
        posted_url = data.get('posted_url') or patch.get('posted_url')
        db.update_post_status(post_id, new_status,
                              notes=data.get('notes', ''),
                              changed_by='make.com',
                              posted_url=posted_url or None)

    if patch:
        # Merge patch onto existing post fields for update_post()
        merged = {
            'topic':        post['topic'],
            'caption':      patch.get('caption', post['caption']),
            'hashtags':     patch.get('hashtags', post.get('hashtags', '')),
            'image_url':    patch.get('image_url', post.get('image_url', '')),
            'hook':         patch.get('hook', post.get('hook', '')),
            'content_type': post.get('content_type', 'photo'),
            'scheduled_date': post.get('scheduled_date'),
            'notes':        patch.get('notes', post.get('notes', '')),
        }
        db.update_post(post_id, merged)

    if 'error_message' in data:
        db.set_post_error(post_id, data['error_message'])

    return jsonify({'ok': True, 'post_id': post_id})


@app.route('/api/content/<int:post_id>/generate-caption', methods=['POST'])
def api_generate_caption_for_post(post_id):
    if not _check_secret(request):
        return jsonify({'error': 'Forbidden'}), 403
    post = db.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    brand_voice = (db.get_brand_voice(post['client_id'], post['platform'])
                   or db.get_brand_voice(post['client_id'], 'general') or {})
    caption, err = ai.generate_caption(post['client_name'], brand_voice,
                                       post['platform'], post['topic'])
    if err:
        return jsonify({'error': err}), 500
    hashtags = ai.generate_hashtags(post['client_name'], brand_voice,
                                    post['platform'], post['topic'], caption)
    merged = {
        'topic': post['topic'], 'caption': caption, 'hashtags': hashtags,
        'image_url': post.get('image_url', ''), 'hook': post.get('hook', ''),
        'content_type': post.get('content_type', 'photo'),
        'scheduled_date': post.get('scheduled_date'), 'notes': post.get('notes', ''),
    }
    db.update_post(post_id, merged)
    return jsonify({'ok': True, 'caption': caption, 'hashtags': hashtags})


@app.route('/api/content/<int:post_id>/generate-hook', methods=['POST'])
def api_generate_hook(post_id):
    if not _check_secret(request):
        return jsonify({'error': 'Forbidden'}), 403
    post = db.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    brand_voice = (db.get_brand_voice(post['client_id'], post['platform'])
                   or db.get_brand_voice(post['client_id'], 'general') or {})
    hook, err = ai.generate_hook(post['client_name'], brand_voice,
                                 post['platform'], post['topic'], post.get('caption', ''))
    if err:
        return jsonify({'error': err}), 500
    merged = {
        'topic': post['topic'], 'caption': post.get('caption', ''),
        'hashtags': post.get('hashtags', ''), 'image_url': post.get('image_url', ''),
        'hook': hook, 'content_type': post.get('content_type', 'photo'),
        'scheduled_date': post.get('scheduled_date'), 'notes': post.get('notes', ''),
    }
    db.update_post(post_id, merged)
    return jsonify({'ok': True, 'hook': hook})


@app.route('/api/content', methods=['POST'])
def api_content_create():
    """Create a post from Make.com (Scenario A ingestion)."""
    if not _check_secret(request):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    required = ['client_id', 'platform', 'topic']
    if not all(data.get(k) for k in required):
        return jsonify({'error': 'client_id, platform, topic are required'}), 400
    if not db.get_client(int(data['client_id'])):
        return jsonify({'error': 'Client not found'}), 404
    post_data = {
        'client_id': int(data['client_id']),
        'platform': data['platform'],
        'content_type': data.get('content_type', 'photo'),
        'topic': data['topic'],
        'caption': data.get('caption', ''),
        'hashtags': data.get('hashtags', ''),
        'image_url': data.get('image_url', ''),
        'hook': data.get('hook', ''),
        'status': data.get('status', 'raw'),
        'scheduled_date': data.get('scheduled_date') or None,
        'notes': data.get('notes', ''),
    }
    post_id = db.create_post(post_data)
    return jsonify({'ok': True, 'post_id': post_id}), 201


@app.route('/api/clients', methods=['GET'])
def api_clients_list():
    clients = scoped_clients()
    return jsonify([{'id': c['id'], 'name': c['name'],
                     'description': c.get('description', ''),
                     'logo_color': c.get('logo_color', '')} for c in clients])


@app.route('/api/client-config/<int:client_id>', methods=['GET'])
@require_client_access('client_id')
def api_client_config(client_id):
    client = db.get_client(client_id)
    if not client:
        return jsonify({'error': 'Client not found'}), 404
    voices = db.get_all_brand_voices(client_id)
    return jsonify({'client': dict(client), 'voices': voices})


@app.route('/api/trends/generate', methods=['POST'])
def api_trends_generate():
    if not _check_secret(request):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    platform = data.get('platform', 'instagram')
    client_id = data.get('client_id')

    if client_id:
        client = db.get_client(int(client_id))
        clients_summary = client['name'] + ': ' + (client.get('description') or '') if client else platform
    else:
        all_clients = db.get_clients()
        clients_summary = ', '.join(c['name'] + ' (' + (c.get('description') or '')[:60] + ')'
                                    for c in all_clients)

    trends, err = ai.generate_trends(clients_summary, platform)
    if err:
        return jsonify({'error': err}), 500

    rows = [{**t, 'client_id': client_id or None} for t in trends]
    db.add_trends(rows)
    return jsonify({'ok': True, 'count': len(trends), 'trends': trends})


# ── Trends page ────────────────────────────────────────────────────────────────

@app.route('/trends')
def trends():
    platform = request.args.get('platform', '')
    all_clients = db.get_clients()
    trend_rows = db.get_trends(platform=platform or None, limit=100)
    webhook_secret = os.environ.get('MAKE_WEBHOOK_SECRET', '')
    return render_template('trends.html', trends=trend_rows, platforms=PLATFORMS,
                           clients=all_clients, filter_platform=platform,
                           webhook_secret=webhook_secret)


# ── Report ─────────────────────────────────────────────────────────────────────

@app.route('/users')
@roles_required('admin')     # user management: admin only
def users():
    return render_template('users.html', users=db.get_users(), clients=db.get_clients())


@app.route('/users/invite', methods=['POST'])
@roles_required('admin')
def users_invite():
    email = request.form.get('email', '').strip().lower()
    client_id = request.form.get('client_id', type=int)
    if not email or not client_id:
        flash('Email and client are both required.', 'error')
        return redirect(url_for('users'))
    if db.get_user_by_email(email):
        flash('A user with that email already exists.', 'error')
        return redirect(url_for('users'))
    if not db.get_client(client_id):
        flash('Client not found.', 'error')
        return redirect(url_for('users'))
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=72)).isoformat()
    db.create_pending_client_user(email, client_id, _hash_token(token), expires)
    invite_url = url_for('accept_invite', token=token, _external=True)
    # Shown once, on screen only — never emailed from the app.
    flash(f'Invite link for {email} (valid 72h, copy it now): {invite_url}', 'invite')
    return redirect(url_for('users'))


@app.route('/users/<int:user_id>/deactivate', methods=['POST'])
@roles_required('admin')
def users_deactivate(user_id):
    db.set_user_active(user_id, False)
    flash('User deactivated. Their login is blocked; audit history is kept.', 'success')
    return redirect(url_for('users'))


@app.route('/users/<int:user_id>/activate', methods=['POST'])
@roles_required('admin')
def users_activate(user_id):
    db.set_user_active(user_id, True)
    flash('User reactivated.', 'success')
    return redirect(url_for('users'))


@app.route('/users/<int:user_id>/reset', methods=['POST'])
@roles_required('admin')
def users_reset(user_id):
    u = db.get_user_by_id(user_id)
    if not u:
        flash('User not found.', 'error')
        return redirect(url_for('users'))
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=72)).isoformat()
    db.set_user_invite(user_id, _hash_token(token), expires)
    invite_url = url_for('accept_invite', token=token, _external=True)
    flash(f'Password-reset link for {u["email"]} (valid 72h, copy it now): {invite_url}', 'invite')
    return redirect(url_for('users'))


@app.route('/invite/<token>', methods=['GET', 'POST'])
def accept_invite(token):
    """Public: consume a single-use invite, set a password (min 12), log in.
    Expired / used / unknown tokens all get the same generic rejection."""
    generic = 'This invite link is invalid or has expired.'
    row = db.get_user_by_invite_hash(_hash_token(token))
    if not row:
        flash(generic, 'danger')
        return redirect(url_for('login'))
    try:
        if not row['invite_expires_at'] or \
           datetime.fromisoformat(row['invite_expires_at']) < datetime.now():
            flash(generic, 'danger')
            return redirect(url_for('login'))
    except (ValueError, TypeError):
        flash(generic, 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        pw = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if len(pw) < 12:
            flash('Password must be at least 12 characters.', 'danger')
        elif pw != confirm:
            flash('Passwords do not match.', 'danger')
        else:
            db.consume_invite(row['id'], generate_password_hash(pw))
            fresh = db.get_user_by_id(row['id'])
            login_user(auth.User(fresh))
            db.update_last_login(fresh['id'])
            flash('Welcome! Your account is ready.', 'success')
            return redirect(url_for('dashboard'))

    return render_template('invite.html', token=token, email=row['email'])


@app.route('/report')
@roles_required('admin', 'manager')   # org-wide reports: not exposed to client users
def report():
    end = datetime.now()
    start = end - timedelta(days=7)
    return render_template('report.html',
                           default_start=start.strftime('%Y-%m-%d'),
                           default_end=end.strftime('%Y-%m-%d'))


@app.route('/api/generate-report', methods=['POST'])
@roles_required('admin', 'manager')
def api_generate_report():
    data = request.get_json()
    start_date = data.get('start_date', '')
    end_date = data.get('end_date', '')

    if not start_date or not end_date:
        return jsonify({'error': 'start_date and end_date required.'}), 400

    # Extend end_date to end of day
    end_full = end_date + ' 23:59:59'
    start_full = start_date + ' 00:00:00'

    report_data = db.get_report_data(start_full, end_full)
    report_md, error = ai.generate_report(report_data)

    if error:
        return jsonify({'error': error}), 500

    return jsonify({
        'report': report_md,
        'stats': {
            'total_created': len(report_data['posts']),
            'total_posted': len(report_data['posted']),
            'performance': report_data['performance'],
            'platform_breakdown': report_data['platform_breakdown'],
        }
    })


# Register auth after all page routes are defined so the login guard's
# before_request runs after setup() (DB init) on the first request.
auth.init_auth(app)


if __name__ == '__main__':
    db.init_db()
    auth.bootstrap_admin()
    app.run(debug=True, port=5000)
