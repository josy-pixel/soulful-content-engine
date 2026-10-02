import os
import json
import uuid
import secrets
import sqlite3
import tempfile
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import database as db
import claude_api as ai
import voice_engine as ve
import config
import webhooks
import s3_media
import media_rules
import media_ingest
import auth
import security
from security import (current_scope, enforce_client_id, require_content_access,
                     require_client_access, scoped_posts, scoped_clients, roles_required)
from flask import abort, g, session
from flask_login import login_user, current_user
from werkzeug.security import generate_password_hash
import hashlib
import logging


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

log = logging.getLogger('app')

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


def _media_ref(media):
    """What gets stored on a post: a stable reference, never a signed URL.

    Signed URLs expire, so persisting one would leave the post pointing at a
    dead link a few hours later. The reference is resolved to a fetchable URL
    at the moment it is needed instead.
    """
    if media.get('storage') == 's3' and media.get('s3_key'):
        return s3_media.ref(media['s3_key'])
    return _media_url(media['client_id'], media['filename'])


VIDEO_SUFFIXES = tuple('.' + e for e in ALLOWED_VIDEOS)


@app.template_filter('media_src')
def media_src(stored):
    """Resolve a stored media reference into something the browser can load.

    Templates hold references, not URLs — a post outlives a signed link. On-disk
    media is already a usable path and passes through unchanged.
    """
    if not stored:
        return ''
    if stored.startswith(s3_media.SCHEME):
        return s3_media.presign_view(stored[len(s3_media.SCHEME):])
    return stored


@app.template_filter('is_video')
def is_video(stored):
    """A video in an <img> tag is a permanently broken image."""
    return bool(stored) and stored.lower().split('?')[0].endswith(VIDEO_SUFFIXES)


def _media_display_url(media):
    """A URL the browser can show right now, wherever the bytes actually live."""
    if media.get('storage') == 's3' and media.get('s3_key'):
        return s3_media.presign_view(media['s3_key'])
    return _media_url(media['client_id'], media['filename'])

PLATFORMS = ['instagram', 'facebook', 'tiktok', 'linkedin', 'youtube']
STATUSES = ['raw', 'branded', 'draft', 'needs_review', 'approved', 'scheduled', 'posted', 'error']
# Statuses a post may be BORN in. Creating a post never publishes it — the move to
# 'approved' is the only thing that does (see content_status). A post created already
# approved would sit there looking ready while nothing was ever sent, and its author
# would then reach for the 'Posted' button to finish the job by hand. That is how a
# post marked "posted" comes to exist that no network has ever seen.
PRE_APPROVAL_STATUSES = ['raw', 'branded', 'draft', 'needs_review']
# What the create form offers a person. 'raw' and 'branded' belong to the machine
# ingestion pipeline, not to someone sitting down to write a post.
CREATE_STATUSES = ['draft', 'needs_review']
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

@app.errorhandler(403)
def _forbidden(e):
    # 403 fires only for authenticated-but-unauthorized users (login_required
    # redirects anonymous ones first), so the base layout renders fine.
    return render_template('403.html'), 403


@app.context_processor
def inject_user_scope():
    """Expose the client user's own client record to every template so the
    sidebar/topbar can render the portal identity. None for admin/manager."""
    cc = None
    if getattr(current_user, 'is_authenticated', False) and \
       getattr(current_user, 'role', None) == 'client':
        cc = db.get_client(current_user.client_id)
    return {'current_client': cc}


@app.route('/')
def dashboard():
    # scope=None for admin/manager (org-wide); the client's own id for a client user.
    scope = current_scope()
    stats = db.get_dashboard_stats(scope=scope)
    pending_approval, pending_total = db.get_posts_pending_approval(scope=scope)
    for p in pending_approval:
        if p.get('hero_filename'):
            p['hero_url'] = _media_display_url({
                'storage': p['hero_storage'], 's3_key': p['hero_s3_key'],
                'client_id': p['hero_client_id'], 'filename': p['hero_filename']})
        elif p.get('image_url'):
            p['hero_url'] = media_src(p['image_url'])
            p['hero_type'] = 'video' if is_video(p['image_url']) else 'image'
        else:
            p['hero_url'] = None
    return render_template('dashboard.html', stats=stats, platforms=PLATFORMS, statuses=STATUSES,
                           content_types=CONTENT_TYPES, scope=scope, pending_approval=pending_approval,
                           pending_total=pending_total)


# ── Clients ────────────────────────────────────────────────────────────────────

@app.route('/clients')
def clients():
    # A client user has no all-clients view — send them to their own client page.
    if current_scope() is not None:
        return redirect(url_for('client_detail', client_id=current_scope()))
    all_clients = db.get_clients()
    # Deletion impact per client, so the confirm can say what it costs. Only an
    # admin sees the trash — a client user never reaches this branch at all.
    is_admin = getattr(current_user, 'role', None) == 'admin'
    impacts = {c['id']: db.get_client_deletion_impact(c['id']) for c in all_clients} if is_admin else {}
    return render_template('clients.html', clients=all_clients, impacts=impacts,
                           deleted_clients=db.get_deleted_clients() if is_admin else [])


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
    webhook = db.get_client_webhook(client_id)   # for the onboarding-status banner
    return render_template('client_detail.html', client=client, voices=voices,
                           posts=posts, platforms=PLATFORMS, webhook=webhook)


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


@app.route('/clients/<int:client_id>/delete', methods=['POST'])
@roles_required('admin')            # delete clients: admin only, never a client user
def client_delete(client_id):
    """Soft delete. The client leaves the app, their login stops working and
    their webhook is removed — but nothing is destroyed, so it is reversible."""
    client = db.get_client(client_id)
    if not client:
        flash('Client not found.', 'error')
        return redirect(url_for('clients'))
    try:
        impact = db.soft_delete_client(client_id, current_user.id, current_user.role,
                                       request_ip=request.remote_addr)
    except ValueError:
        flash('That client is already deleted.', 'error')
        return redirect(url_for('clients'))
    if impact is None:
        flash('Client not found.', 'error')
        return redirect(url_for('clients'))
    flash(f"'{client['name']}' moved to deleted clients — {impact['posts']} posts and "
          f"{impact['users']} logins came with it. Nothing was destroyed; you can restore it.",
          'success')
    return redirect(url_for('clients'))


@app.route('/clients/<int:client_id>/restore', methods=['POST'])
@roles_required('admin')
def client_restore(client_id):
    try:
        ok = db.restore_client(client_id, current_user.id, current_user.role,
                               request_ip=request.remote_addr)
    except ValueError:
        flash('That client is not deleted.', 'error')
        return redirect(url_for('clients'))
    if not ok:
        flash('Client not found.', 'error')
        return redirect(url_for('clients'))
    client = db.get_client(client_id)
    flash(f"'{client['name']}' restored. Their logins and their webhook are still "
          f"off — re-enable them deliberately when you are ready to publish again.",
          'success')
    return redirect(url_for('clients'))


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
    media = db.get_client_media_with_usage(client_id)
    for m in media:
        m['url'] = _media_display_url(m)
    return render_template('client_gallery.html', client=client, media=media,
                           unused=sum(1 for m in media if not m['uses']))


@app.route('/editing-queue')
def editing_queue():
    """Raw media pulled in from Instagram/TikTok/the web, across every client
    (or just one, for a client-portal user) — still needs Canva or the video
    editor before it can go on a post. Oldest first, so nothing gets lost."""
    pending = db.get_pending_edits(scope=current_scope())
    for m in pending:
        m['url'] = _media_display_url(m)
    return render_template('editing_queue.html', pending=pending)


@app.route('/media')
def media_library():
    """The way in. A client lands in their own library; an admin picks whose to open.

    The gallery route itself has always allowed a client user through — there was
    simply no link to it anywhere, so for them it did not exist.
    """
    scope = current_scope()
    if scope is not None:
        return redirect(url_for('client_gallery', client_id=scope))
    wanted = request.args.get('client', type=int)
    if wanted:
        return redirect(url_for('client_gallery', client_id=wanted))
    clients_ = db.get_clients()
    if len(clients_) == 1:
        return redirect(url_for('client_gallery', client_id=clients_[0]['id']))
    return render_template('media_picker.html', clients=clients_)


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


@app.route('/clients/<int:client_id>/media/presign', methods=['POST'])
@require_client_access('client_id')
def media_presign(client_id):
    """Hand the browser a short-lived permit to upload one file straight to S3.

    The bytes never reach this server, so neither MAX_CONTENT_LENGTH nor the size
    of the Render disk applies. The permit is scoped to a single key we choose —
    the caller cannot pick where the file lands, or overwrite another client's.
    """
    if not s3_media.enabled():
        return jsonify({'error': 'Direct upload is not configured on this server.'}), 503
    if not db.get_client(client_id):
        return jsonify({'error': 'Client not found'}), 404

    data = request.get_json(silent=True) or {}
    filename = (data.get('filename') or '').strip()
    if not filename or not _allowed_file(filename):
        return jsonify({'error': 'File type not allowed'}), 400

    key = s3_media.build_key(client_id, filename)
    permit = s3_media.presign_upload(key, data.get('content_type') or None)
    return jsonify({
        'ok': True,
        'key': key,
        'url': permit['url'],
        'fields': permit['fields'],
        'max_bytes': s3_media.MAX_UPLOAD_BYTES,
    })


@app.route('/clients/<int:client_id>/media/complete', methods=['POST'])
@require_client_access('client_id')
def media_complete(client_id):
    """Register a file the browser says it uploaded — after checking that it did.

    The key is re-derived from client_id rather than trusted, and the object is
    read back from S3, so a caller cannot register someone else's file or a row
    for bytes that were never stored.
    """
    if not s3_media.enabled():
        return jsonify({'error': 'Direct upload is not configured on this server.'}), 503

    data = request.get_json(silent=True) or {}
    key = (data.get('key') or '').strip()
    original = secure_filename((data.get('filename') or '').strip())
    if not key.startswith('clients/%d/' % client_id):
        abort(403)                                   # not this client's prefix
    if not original or not _allowed_file(original):
        return jsonify({'error': 'File type not allowed'}), 400

    stored = s3_media.head(key)
    if not stored:
        return jsonify({'error': 'Upload did not arrive — nothing to register.'}), 400

    mtype = _media_type(original)
    media_id = db.add_media(client_id, key.rsplit('/', 1)[-1], original, mtype,
                            stored['size'], data.get('caption_hint', ''),
                            data.get('tags', '[]'), storage='s3', s3_key=key)
    media = db.get_media(media_id)
    return jsonify({'ok': True, 'media': {
        'id': media_id,
        'filename': media['filename'],
        'original_name': original,
        'media_type': mtype,
        'file_size': stored['size'],
        'url': _media_display_url(media),
    }})


@app.route('/api/media/<int:media_id>', methods=['PATCH'])
def api_media_update(media_id):
    media = db.get_media(media_id)
    if not media:
        return jsonify({'error': 'Not found'}), 404
    if not security.can_see_client(media['client_id']):   # object-level tenant check
        abort(403)
    data = request.get_json(silent=True) or {}
    edit_status = data.get('edit_status')
    if edit_status and edit_status not in ('ready', 'needs_editing'):
        return jsonify({'error': 'Invalid edit_status.'}), 400
    db.update_media(media_id,
                    caption_hint=data.get('caption_hint', media.get('caption_hint', '')),
                    tags=data.get('tags', media.get('tags', '[]')),
                    edit_status=edit_status)
    return jsonify({'ok': True})


@app.route('/api/media/<int:media_id>/usage')
def api_media_usage(media_id):
    """What removing this file would cost — asked before the confirm, not after."""
    media = db.get_media(media_id)
    if not media:
        return jsonify({'error': 'Not found'}), 404
    if not security.can_see_client(media['client_id']):
        abort(403)
    usage = db.get_media_usage(media_id)
    usage['client_may_delete'] = usage['posted_uses'] == 0
    return jsonify(usage)


@app.route('/api/media/<int:media_id>', methods=['DELETE'])
def api_media_delete(media_id):
    media = db.get_media(media_id)
    if not media:
        return jsonify({'error': 'Not found'}), 404
    if not security.can_see_client(media['client_id']):   # object-level tenant check
        abort(403)

    # Deleting a media row silently detaches it from every post that uses it. For a
    # post that is already published that quietly breaks the record of what was
    # posted, while changing nothing on the network — so a client user cannot do it.
    # An admin still can, deliberately.
    usage = db.get_media_usage(media_id)
    if usage['posted_uses'] and current_scope() is not None:
        return jsonify({
            'error': 'This file is used by %d published post(s). Ask an admin to remove it.'
                     % usage['posted_uses'],
            'uses': usage['uses'], 'posted_uses': usage['posted_uses'],
        }), 409

    if media.get('storage') == 's3' and media.get('s3_key'):
        # Versioning keeps a recoverable copy behind a delete marker, so this is
        # not the irreversible loss that removing the local file is.
        s3_media.delete(media['s3_key'])
    else:
        file_path = os.path.join(UPLOAD_PATH, str(media['client_id']), media['filename'])
        if os.path.exists(file_path):
            os.remove(file_path)
    for post_id in db.delete_media(media_id):
        _audit_post({'id': post_id, 'client_id': media['client_id']},
                    'talent_signoff_cleared', reason='media deleted')
    return jsonify({'ok': True})


@app.route('/api/media/client/<int:client_id>')
@require_client_access('client_id')
def api_client_media(client_id):
    media_type = request.args.get('type')
    # Feeds the post pickers, so finished files only: raw media waits in the editing queue.
    media = db.get_client_media(client_id, media_type or None, edit_status='ready')
    for m in media:
        m['url'] = _media_display_url(m)
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
    if not _m:
        return jsonify({'error': 'Media not found'}), 404
    if _m.get('edit_status') == 'needs_editing':
        return jsonify({'error': 'This file still needs editing. Finish it in Canva or the '
                                 'video editor, then mark it Ready in the gallery.'}), 409

    # Refuse the mismatch here rather than letting the network refuse it hours
    # later with a generic message. The app knows both facts at this moment.
    post = g.content_row
    ok, why = media_rules.check(post.get('content_type'),
                                media_rules.kind_of_filename(_m.get('filename')))
    if not ok:
        return jsonify({'error': why}), 409

    if db.attach_media_to_post(post_id, media_id, data.get('sort_order', 0)):
        _audit_post(post, 'talent_signoff_cleared', reason='media attached')
    media = db.get_media(media_id)
    if media:
        merged = db.get_post(post_id)
        if merged and not merged.get('image_url'):
            db.update_post(post_id, {
                'topic': merged['topic'], 'caption': merged['caption'],
                'hashtags': merged.get('hashtags', ''), 'image_url': _media_ref(media),
                'hook': merged.get('hook', ''), 'content_type': merged.get('content_type', 'photo'),
                'scheduled_date': merged.get('scheduled_date'), 'notes': merged.get('notes', ''),
            })
    return jsonify({'ok': True})


@app.route('/api/content/<int:post_id>/media/<int:media_id>', methods=['DELETE'])
@require_content_access('post_id')
def api_detach_media(post_id, media_id):
    cleared = db.detach_media_from_post(post_id, media_id)
    if cleared:
        _audit_post(g.content_row, 'talent_signoff_cleared', reason='media detached')
    return jsonify({'ok': True, 'signoff_cleared': cleared})


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
    # A bulk week sends its direction with every post. It belongs in the rulebook,
    # not the user turn, so the auditor judges against it too — and being the same
    # for the whole batch, it keeps the cached prefix identical post to post.
    direction = (data.get('weekly_direction') or '').strip()

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
                              weekly_direction=direction,
                              extra_context=extra,
                              debug=config.DEBUG_ENGINE)
    if result.get('error'):
        return jsonify({'error': result['error']}), 500

    payload = {
        'caption': result['caption'],
        'hashtags': result['hashtags'],
        'voice_score': result.get('voice_score'),
        'voice_audit': result.get('voice_audit', ''),
        # The score belongs to the caption below it. These two say how it got
        # there, so a low score is never a mystery.
        'voice_attempts': result.get('voice_attempts', []),
        'voice_deferred_settings': result.get('voice_deferred_settings', []),
    }
    if config.DEBUG_ENGINE:   # hidden unless DEBUG_ENGINE=1 (admin-only page anyway)
        payload['debug'] = {
            'usage': result.get('usage'),
            'system_prompt': result.get('system_prompt', ''),
            'system_prompt_chars': result.get('system_prompt_chars'),
            'voice_document_chars': result.get('voice_document_chars'),
        }
    return jsonify(payload)


def _refuse_unknown_platform(data):
    """Normalise platform and content_type on a post about to be created, and
    refuse a value the app does not know. Both are written into pages and into
    the payload sent to the publishing scenario, so an arbitrary string here is a
    post nothing can publish and, worse, markup running in an admin's browser.
    content_type stays optional: absent, it defaults downstream as it always has.
    Returns an error message, or None (and leaves `data` normalised)."""
    platform = str(data.get('platform') or '').strip().lower()
    if platform not in PLATFORMS:
        return 'Unknown platform. Use one of: %s.' % ', '.join(PLATFORMS)
    data['platform'] = platform
    if data.get('content_type'):
        content_type = str(data['content_type']).strip().lower()
        if content_type not in media_rules.ACCEPTS:
            return ('Unknown content type. Use one of: %s.'
                    % ', '.join(sorted(media_rules.ACCEPTS)))
        data['content_type'] = content_type
    return None


@app.route('/api/save-caption', methods=['POST'])
def api_save_caption():
    data = request.get_json()
    # HARD RULE 1: a client user's post is always created under THEIR client_id,
    # never a forged one from the request body.
    data['client_id'] = enforce_client_id(data.get('client_id'))
    required = ['client_id', 'platform', 'topic', 'caption']
    if not all(data.get(k) for k in required):
        return jsonify({'error': 'Missing required fields.'}), 400
    problem = _refuse_unknown_platform(data)
    if problem:
        return jsonify({'error': problem}), 400
    if data.get('status', 'draft') not in PRE_APPROVAL_STATUSES:
        return jsonify({'error': 'A post cannot be created past the approval gate.'}), 400
    post_id = db.create_post(data)
    return jsonify({'ok': True, 'post_id': post_id})


# ── Bulk Content Generator ───────────────────────────────────────────────────
# A week is planned in one request and written one post per request:
#
#   /api/bulk-plan          one model call: the week's topics, each with its slot
#   /api/generate-caption   the single generator, once per topic, called by the
#                           page two at a time — the same voice-faithful pipeline
#   /api/bulk-save          the batch a person reviewed
#
# Writing the whole week inside one request meant 22 to 36 model calls in a row
# on the app's single gunicorn worker, which is killed at 180 seconds. The
# captions died with the worker, the tokens were still spent, and every other
# request — Make's publish callbacks included — waited behind it. One post is a
# handful of calls, well inside the limit, and a post that fails is retried on
# its own instead of sinking the week.
#
# Every post in a batch carries the same weekly direction, so the rulebook is
# identical across the batch and is the cached prefix after the first post.

# Who may run a batch. A week is dozens of model calls, so it starts with the
# agency; adding 'client' here opens every bulk route and the nav link to talents.
BULK_GENERATE_ROLES = ('admin', 'manager')
# Posts in one batch, enforced on plan and on save: two a day for a week.
BULK_MAX_POSTS = 14
BULK_HOUR_SLOTS = [9, 13, 17]   # posting times on a day with more than one post


@app.context_processor
def inject_bulk_access():
    return {'can_bulk_generate': getattr(current_user, 'role', None) in BULK_GENERATE_ROLES}


@app.route('/bulk-generate')
@roles_required(*BULK_GENERATE_ROLES)
def bulk_generate():
    all_clients = scoped_clients()
    preselect_client = current_scope() or request.args.get('client_id', type=int)
    return render_template('bulk_generate.html', clients=all_clients,
                           bulk_content_types=_bulk_content_types(),
                           create_statuses=CREATE_STATUSES, max_posts=BULK_MAX_POSTS,
                           preselect_client=preselect_client)


def _bulk_content_types():
    """Platform -> the content types a batch may use: what the app offers that
    the publishing scenarios can actually send (media_rules). A week of posts
    nothing downstream can publish would look ready and never go out."""
    return {p: [t for t in CONTENT_TYPES.get(p, []) if media_rules.can_publish(p, t)[0]]
            for p in PLATFORMS if p in media_rules.PUBLISHABLE}


def _bulk_combo_error(platform, content_type):
    """The plan and the save refuse the same combinations the page never offers."""
    allowed = _bulk_content_types()
    if not isinstance(platform, str) or platform not in allowed:
        return ('Bulk generation covers %s only — nothing publishes %s posts from here yet.'
                % (' and '.join(p.title() for p in allowed), str(platform or 'those').title()))
    if content_type not in allowed[platform]:
        return ('%s %s posts are not published by this system. Pick one of: %s.'
                % (platform.title(), content_type or 'untyped', ', '.join(allowed[platform])))
    return None


def _bulk_client_id(data):
    """The batch's client as an int, or None. HARD RULE 1: a client user's id
    always comes from their session (enforce_client_id), never from the body."""
    try:
        return int(enforce_client_id(data.get('client_id')))
    except (TypeError, ValueError):
        return None


def _bulk_batch_size(data):
    """(days, per_day, error). A batch bigger than the cap is refused, not
    quietly trimmed — the person asked for a number and should hear why not."""
    try:
        days = int(data.get('days', 7))
        per_day = int(data.get('posts_per_day', 1))
    except (TypeError, ValueError):
        return None, None, 'Days and posts per day must be whole numbers.'
    if days < 1 or not 1 <= per_day <= len(BULK_HOUR_SLOTS):
        return None, None, ('Pick at least one day, and 1 to %d posts a day.'
                            % len(BULK_HOUR_SLOTS))
    if days * per_day > BULK_MAX_POSTS:
        return None, None, ('A batch is at most %d posts — %d days at %d a day is %d.'
                            % (BULK_MAX_POSTS, days, per_day, days * per_day))
    return days, per_day, None


@app.route('/api/bulk-plan', methods=['POST'])
@roles_required(*BULK_GENERATE_ROLES)
def api_bulk_plan():
    """One model call: the week's topics, each with its posting slot. Nothing is
    stored — the page writes each post next, and a person reviews before saving."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Send the plan request as a JSON object.'}), 400
    # HARD RULE 1: never trust client_id from the body for a client user.
    client_id = _bulk_client_id(data)
    platform = data.get('platform')
    content_type = data.get('content_type')
    theme = str(data.get('theme') or '').strip()

    if not all([client_id, platform, theme]):
        return jsonify({'error': 'client_id, a platform, and a theme are required.'}), 400
    err = _bulk_combo_error(platform, content_type)
    if err:
        return jsonify({'error': err}), 400
    days, per_day, err = _bulk_batch_size(data)
    if err:
        return jsonify({'error': err}), 400

    client = db.get_client(client_id)
    if not client:
        return jsonify({'error': 'Client not found.'}), 404

    try:
        base_date = datetime.strptime(data.get('start_date') or '', '%Y-%m-%d')
    except (TypeError, ValueError):
        base_date = datetime.now()

    count = days * per_day
    # The same voice the writer will use: the platform's settings, else general.
    brand_voice = dict(db.get_brand_voice(client_id, platform) or db.get_brand_voice(client_id, 'general') or {})
    voice_document, _ = db.get_client_voice(client_id)
    performance_rows = db.get_recent_performance(client_id, days=7) if data.get('include_performance', True) else []
    # Org-wide trends and this client's own — never one generated for another client.
    trend_rows = db.get_client_trends(client_id, platform, limit=8) if data.get('include_trends', True) else []
    trend_texts = [t['trend_text'] for t in trend_rows]

    topics, err = ai.plan_week(client['name'], client.get('description') or '', theme,
                               platform, count, trends_list=trend_texts,
                               performance_rows=performance_rows, content_type=content_type,
                               voice_constraints=ve.planning_constraints(voice_document, brand_voice))
    if err:
        return jsonify({'error': err}), 500

    texts = []
    for t in topics or []:
        text = str((t.get('topic') if isinstance(t, dict) else t) or '').strip()
        if text:
            texts.append(text)
    texts = texts[:count]   # a long list is trimmed; a short one is shown as it came
    if not texts:
        return jsonify({'error': 'Claude returned no topics — try again.'}), 500

    posts = []
    for i, topic in enumerate(texts):
        day_offset, slot = divmod(i, per_day)
        when = (base_date + timedelta(days=day_offset)).replace(
            hour=BULK_HOUR_SLOTS[slot], minute=0, second=0, microsecond=0)
        # The planner is told the banned words; this is the check that it listened.
        # A topic that slipped one in is held on the page for a person to edit.
        posts.append({'topic': topic, 'scheduled_date': when.strftime('%Y-%m-%dT%H:%M'),
                      'banned': ve.banned_in(topic, brand_voice)})

    return jsonify({
        'ok': True,
        'client_id': client_id,
        'platform': platform,
        'content_type': content_type,
        'theme': theme,
        'posts': posts,
        'requested': count,
        'performance_used': len(performance_rows),
        'trends_used': trend_texts,
    })


def _bulk_post_row(p):
    """(row, problem) for one post of a batch. Only its own words and slot are
    read from it: status, client, platform and type belong to the batch."""
    if not isinstance(p, dict):
        return None, 'is not a post'
    topic, caption, hashtags = p.get('topic'), p.get('caption'), p.get('hashtags') or ''
    if not (isinstance(topic, str) and topic.strip() and isinstance(caption, str) and caption.strip()):
        return None, 'needs a topic and a caption'
    if not isinstance(hashtags, str):
        return None, 'has hashtags that are not text'
    when = p.get('scheduled_date') or None
    if when is not None:
        try:
            when = datetime.strptime(str(when), '%Y-%m-%dT%H:%M').strftime('%Y-%m-%dT%H:%M')
        except ValueError:
            return None, 'has a scheduled date that is not a date and time'
    return {'topic': topic.strip(), 'caption': caption.strip(),
            'hashtags': hashtags.strip(), 'scheduled_date': when}, None


@app.route('/api/bulk-save', methods=['POST'])
@roles_required(*BULK_GENERATE_ROLES)
def api_bulk_save():
    """Create the reviewed batch: every post, or — if any of them is wrong —
    none, with the reason. A half-saved week is easy to miss and is doubled by
    the save that retries it."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Send the batch as a JSON object.'}), 400
    # HARD RULE 1: a client user's posts are always created under THEIR client_id.
    client_id = _bulk_client_id(data)
    platform = data.get('platform')
    content_type = data.get('content_type')
    status = data.get('status', 'draft')
    posts = data.get('posts')

    if not client_id or not platform:
        return jsonify({'error': 'client_id and a platform are required.'}), 400
    err = _bulk_combo_error(platform, content_type)
    if err:
        return jsonify({'error': err}), 400
    if status not in CREATE_STATUSES:
        return jsonify({'error': 'A post cannot be created past the approval gate.'}), 400
    if not isinstance(posts, list) or not posts:
        return jsonify({'error': 'No posts to save — send them as a list.'}), 400
    if len(posts) > BULK_MAX_POSTS:
        return jsonify({'error': 'A batch is at most %d posts; this one has %d.'
                                 % (BULK_MAX_POSTS, len(posts))}), 400

    rows = []
    for n, p in enumerate(posts, 1):
        row, problem = _bulk_post_row(p)
        if problem:
            return jsonify({'error': 'Post %d %s. Nothing was saved.' % (n, problem)}), 400
        rows.append(dict(row, client_id=client_id, platform=platform,
                         content_type=content_type, status=status,
                         notes='Created via bulk weekly build.'))

    if not db.get_client(client_id):          # v_clients_active: a deleted client is gone
        return jsonify({'error': 'Client not found.'}), 404

    created = db.create_posts(rows)
    return jsonify({'ok': True, 'created': created, 'count': len(created)})


# ── Reel Repurposer ───────────────────────────────────────────────────────────
# The reel-repurposer skill: diagnose and re-cut an EXISTING/underperforming
# video using its own measured performance, rather than scripting a new one.
#
# Admin and manager only for now. Opening it to client users is adding 'client'
# here: every route below still checks the post's own client, and the nav link
# and the post-page button read this same tuple. A saved package stays visible on
# the post page to anyone who can already see the post.
REEL_REPURPOSER_ROLES = ('admin', 'manager')
app.jinja_env.globals['REEL_REPURPOSER_ROLES'] = REEL_REPURPOSER_ROLES
# What can be repurposed: something already published, that is a video.
REPURPOSABLE_CONTENT_TYPES = ('video', 'reel')


def _repurposable_post(raw_id):
    """Resolve the post a Reel Repurposer request names, telling the caller what
    is wrong in this order: no usable id (400), no such post (404), not theirs
    (403), not a posted video (409). Returns (post, error_response)."""
    if raw_id is None or raw_id == '':
        return None, (jsonify({'error': 'post_id is required.'}), 400)
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        post_id = raw_id
    elif isinstance(raw_id, str) and raw_id.strip().isdigit():
        post_id = int(raw_id)
    else:
        return None, (jsonify({'error': 'post_id must be a whole number.'}), 400)
    post = db.get_post(post_id)
    if not post:
        return None, (jsonify({'error': 'Post not found.'}), 404)
    if not security.can_see_client(post['client_id']):
        abort(403)
    if post.get('status') != 'posted' or post.get('content_type') not in REPURPOSABLE_CONTENT_TYPES:
        return None, (jsonify({'error': 'Only a posted reel or video can be repurposed. This '
                                        'post is a %s %s.' % ((post.get('status') or '').replace('_', ' '),
                                                              post.get('content_type') or 'post')}), 409)
    return post, None


def _repurpose_performance_summary(post, snapshot):
    """What the app measured for this post, as the prompt reads it. No snapshot
    is said in words: printed as zeros it read to the model as a total flop."""
    lines = ['Topic: %s' % post['topic'],
             'Platform: %s' % post['platform'],
             'Content type: %s' % post.get('content_type'),
             'Posted: %s' % (post.get('posted_date') or 'unknown')]
    if not snapshot:
        lines.append('Metrics: no performance data recorded in the app for this post.')
    else:
        lines.append('Latest metrics snapshot (recorded %s): Likes: %s, Comments: %s, '
                     'Shares: %s, Saves: %s, Views: %s, Reach: %s, Impressions: %s, Clicks: %s'
                     % tuple([snapshot.get('recorded_at')] +
                             [snapshot.get(k) for k in ('likes', 'comments', 'shares', 'saves',
                                                        'views', 'reach', 'impressions', 'clicks')]))
    return '\n'.join(lines)


@app.route('/reel-repurposer')
@roles_required(*REEL_REPURPOSER_ROLES)
def reel_repurposer():
    all_clients = scoped_clients()
    preselect_client = current_scope() or request.args.get('client_id', type=int)
    post_id = request.args.get('post_id', type=int)
    post = None
    if post_id:
        post = db.get_post(post_id)
        if post and not security.can_see_client(post['client_id']):
            post = None   # not this user's post — behave as if none was picked
        elif post and (post.get('status') != 'posted'
                       or post.get('content_type') not in REPURPOSABLE_CONTENT_TYPES):
            post = None   # nothing to repurpose — don't preselect what generate refuses
    return render_template('reel_repurposer.html', clients=all_clients,
                           preselect_client=preselect_client, post=post)


@app.route('/api/reel-repurpose/candidates/<int:client_id>')
@roles_required(*REEL_REPURPOSER_ROLES)
@require_client_access('client_id')
def api_repurpose_candidates(client_id):
    candidates = db.get_repurpose_candidates(client_id)
    return jsonify(candidates)


@app.route('/api/reel-repurpose/generate', methods=['POST'])
@roles_required(*REEL_REPURPOSER_ROLES)
def api_reel_repurpose_generate():
    data = request.get_json(silent=True) or {}
    source_material = (data.get('source_material') or '').strip()
    extra_context = (data.get('extra_context') or '').strip()

    post, err = _repurposable_post(data.get('post_id'))
    if err:
        return err
    if not source_material:
        return jsonify({'error': "Source material is required — Claude can't watch video, "
                                 'so paste a transcript or shot list first.'}), 400

    brand_voice = dict(db.get_brand_voice(post['client_id'], post['platform'])
                       or db.get_brand_voice(post['client_id'], 'general') or {})
    voice_document, sample_captions = db.get_client_voice(post['client_id'])

    performance_summary = _repurpose_performance_summary(
        post, db.get_latest_performance(post['id']))

    result = ve.build_reel_repurpose(post['client_name'], brand_voice, voice_document,
                                     sample_captions, source_material, performance_summary,
                                     extra_context=extra_context, platform=post['platform'],
                                     debug=config.DEBUG_ENGINE)
    if result.get('error'):
        # 504: gave up waiting on Claude. 502: Claude's answer was cut off. Either
        # way the body is JSON the page can show, never a worker killed mid-request.
        status = 504 if result.get('timeout') else 502 if result.get('incomplete') else 500
        return jsonify({'error': result['error']}), status
    return jsonify({'ok': True, 'package': result['package'],
                    'performance_summary': performance_summary})


@app.route('/api/reel-repurpose/save', methods=['POST'])
@roles_required(*REEL_REPURPOSER_ROLES)
def api_reel_repurpose_save():
    data = request.get_json(silent=True) or {}
    package = (data.get('package') or '').strip()
    post, err = _repurposable_post(data.get('post_id'))
    if err:
        return err
    if not package:
        return jsonify({'error': 'There is no package to save.'}), 400
    db.set_repurpose_brief(post['id'], package)
    return jsonify({'ok': True})


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

        # One post per platform rather than one post carrying several. Content type,
        # approval, the link it ends up at and its metrics are all per-platform, and
        # a single row could hold only one of each.
        chosen = [p for p in request.form.getlist('platforms') if p in PLATFORMS]
        if not chosen and request.form.get('platform') in PLATFORMS:
            chosen = [request.form['platform']]              # older form, still accepted

        base = {
            'client_id': enforce_client_id(submitted_cid),
            'topic': request.form['topic'].strip(),
            'caption': request.form['caption'].strip(),
            'hashtags': request.form.get('hashtags', '').strip(),
            'image_url': request.form.get('image_url', '').strip(),
            'status': request.form.get('status', 'draft'),
            'scheduled_date': request.form.get('scheduled_date') or None,
            'notes': request.form.get('notes', '').strip(),
        }

        problem = None
        if not chosen:
            problem = 'Pick at least one platform.'
        elif not base['topic'] or not base['caption']:
            problem = 'Topic and caption are required.'
        elif base['status'] not in PRE_APPROVAL_STATUSES:
            problem = ('A post cannot be created as "%s". Create it, attach its media, '
                       'then approve it — approving is what sends it to be published.'
                       % base['status'].replace('_', ' '))
        if problem:
            flash(problem, 'error')
            return render_template('content_form.html', post=None, clients=all_clients,
                                   platforms=PLATFORMS, statuses=STATUSES,
                                   create_statuses=CREATE_STATUSES,
                                   content_types=CONTENT_TYPES, preselect={})

        created = []
        for platform in chosen:
            allowed = CONTENT_TYPES.get(platform, ['photo'])
            # Several types may be ticked for one platform — a reel and a story are
            # two different posts on Instagram, not one post in two shapes. Anything
            # that platform does not offer is dropped rather than substituted.
            wanted = [t for t in request.form.getlist('content_type_%s' % platform)
                      if t in allowed]
            if not wanted:
                single = request.form.get('content_type')
                wanted = [single if single in allowed else allowed[0]]
            for content_type in dict.fromkeys(wanted):        # de-duplicated, order kept
                created.append(db.create_post(
                    dict(base, platform=platform, content_type=content_type)))

        if len(created) == 1:
            flash('Post created successfully.', 'success')
            return redirect(url_for('content_detail', post_id=created[0]))
        flash('Created %d posts — one per platform and content type. Each is approved '
              'and published separately.' % len(created), 'success')
        return redirect(url_for('content_list'))
    preselect = {
        'client_id': request.args.get('client_id', ''),
        'platform': request.args.get('platform', ''),
    }
    return render_template('content_form.html', post=None, clients=all_clients,
                           platforms=PLATFORMS, statuses=STATUSES,
                           create_statuses=CREATE_STATUSES,
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
        m['url'] = _media_display_url(m)     # S3 media has no file under /uploads
    client_media = db.get_client_media(post['client_id'])
    for m in client_media:
        m['url'] = _media_url(m['client_id'], m['filename'])
    return render_template('content_detail.html', post=post, history=history,
                           metrics=metrics, allowed_transitions=allowed_transitions,
                           statuses=STATUSES, post_media=post_media,
                           client_media=client_media,
                           review_open=post['status'] in PRE_APPROVAL_STATUSES,
                           signoff_label=_signoff_label(post))


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
        _audit_change(post, db.update_post(post_id, data), 'edit form')
        flash('Post updated.', 'success')
        return redirect(url_for('content_detail', post_id=post_id))
    return render_template('content_form.html', post=post, clients=all_clients,
                           platforms=PLATFORMS, statuses=STATUSES,
                           create_statuses=CREATE_STATUSES,
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
        ok, msg = webhooks.dispatch_post(post, current_user.id, current_user.role, request.remote_addr)
        if ok:
            flash(msg, 'success')
        else:
            db.set_post_error(post_id, msg)   # persist the failure — never leave it silently "sent"
            flash(f'Dispatch failed: {msg}', 'warning')

    return redirect(url_for('content_detail', post_id=post_id))


def _audit_post(post, action, reason=None, **metadata):
    """Append a change to a post's caption or talent sign-off to audit_log, with
    who made it. Machine routes have no signed-in user and are recorded as such."""
    if getattr(current_user, 'is_authenticated', False):
        actor, role = current_user.id, current_user.role
    else:
        actor, role = None, 'machine'
    db.add_audit(actor, role, post['client_id'], 'content', post['id'], action,
                 reason=reason, metadata=metadata or None, request_ip=request.remote_addr)


def _audit_change(post, change, via):
    """Audit what db.update_post / update_post_review reported: a caption or
    hashtag edit, and the talent sign-off it voided."""
    if change.get('before'):
        _audit_post(post, 'caption_edit', fields=sorted(change['before']),
                    before=change['before'], via=via)
    if change.get('signoff_cleared'):
        _audit_post(post, 'talent_signoff_cleared', reason=via)


def _signoff_label(post):
    """The line under the sign-off checkbox. The talent is the client user; staff
    may tick it on the talent's behalf (an OK given on WhatsApp), and the line
    then says so — the two must never read the same."""
    if not post.get('talent_approved'):
        return 'Not yet confirmed'
    when = ' · %s' % post['talent_approved_at'] if post.get('talent_approved_at') else ''
    who = db.get_user_by_id(post['talent_approved_by']) if post.get('talent_approved_by') else None
    if who is None:
        return 'Approved' + when
    if who['role'] == 'client':
        return 'Approved by talent' + when
    return 'Marked approved by %s (agency)%s' % (who['email'], when)


@app.route('/api/content/<int:post_id>/review', methods=['PATCH'])
@require_content_access('post_id')
def api_content_review(post_id):
    """Inline caption/hashtags edit + talent sign-off, for the visual review UI
    on the post detail page and dashboard. Separate from the admin/manager
    status workflow — ticking this never triggers publish dispatch."""
    post = g.content_row   # loaded + scope-checked by the decorator
    # Only before the approval gate, for every role. Approving sends the caption
    # to Make in the payload; an edit or a sign-off after that would change the
    # app's record and nothing that is published.
    if post.get('status') not in PRE_APPROVAL_STATUSES:
        return jsonify({'error': 'This post is past the approval gate — its caption and '
                                 'sign-off can no longer be changed here.'}), 409

    # Refuse anything malformed rather than store it: bool("false") is True, and a
    # non-text caption would be written as-is into what gets published.
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data.keys() & {'caption', 'hashtags', 'talent_approved'}:
        return jsonify({'error': 'Send caption, hashtags or talent_approved.'}), 400
    for field in ('caption', 'hashtags'):
        if field in data and not isinstance(data[field], str):
            return jsonify({'error': '%s must be text.' % field.capitalize()}), 400
    if 'caption' in data and not data['caption'].strip():
        return jsonify({'error': 'The caption cannot be empty.'}), 400
    if 'talent_approved' in data and not isinstance(data['talent_approved'], bool):
        return jsonify({'error': 'talent_approved must be true or false.'}), 400

    if 'caption' in data or 'hashtags' in data:
        change = db.update_post_review(post_id,
                                       caption=data['caption'].strip() if 'caption' in data else None,
                                       hashtags=data['hashtags'].strip() if 'hashtags' in data else None)
        _audit_change(post, change, 'review')
    if 'talent_approved' in data:
        if db.set_talent_approval(post_id, data['talent_approved'], current_user.id):
            _audit_post(post, 'talent_signoff' if data['talent_approved'] else 'talent_signoff_withdrawn')

    fresh = db.get_post(post_id)
    return jsonify({'ok': True, 'talent_approved': bool(fresh['talent_approved']),
                    'signoff_label': _signoff_label(fresh)})


# ── Webhooks ───────────────────────────────────────────────────────────────────

def _inbound_caller(data):
    """Who is calling a machine endpoint, and how much are they allowed to touch?

    Returns (authorised, client_id, how). A per-client key resolves to the client
    it belongs to — the caller never states which client they are, so a key cannot
    be aimed at someone else's content. The old shared secret is honoured while
    scenarios migrate, but it carries no client, so it stays unscoped.
    """
    key = (request.headers.get('X-Api-Key') or data.get('api_key') or '').strip()
    if key:
        cid = db.client_id_for_api_key(key)
        if cid is not None:
            return True, cid, 'key'
        return False, None, None            # a key was offered and it was not valid

    # Legacy shared secret. Body only — a secret in the query string ends up in
    # access logs, proxies and browser history.
    if os.environ.get('LEGACY_INBOUND_SECRET', 'true') == 'true':
        if webhooks.verify_secret(data.get('secret', '')):
            return True, None, 'legacy'
    return False, None, None


def _inbound_post(data):
    """Resolve the post a machine call refers to, refusing anything out of its scope.
    Returns (post, error_response)."""
    ok, cid, how = _inbound_caller(data)
    if not ok:
        return None, (jsonify({'error': 'Forbidden'}), 403)

    post_id = data.get('post_id')
    if not post_id:
        return None, (jsonify({'error': 'post_id is required'}), 400)

    post = db.get_post(int(post_id))
    if not post:
        return None, (jsonify({'error': 'Post not found'}), 404)

    if cid is not None and post['client_id'] != cid:
        log.warning('inbound key for client %s tried to touch post %s of client %s',
                    cid, post_id, post['client_id'])
        return None, (jsonify({'error': 'Post not found'}), 404)   # no cross-client probing
    return post, None


@app.route('/webhook/publish', methods=['POST'])
def webhook_publish():
    """Inbound endpoint — a client's scenario calls this after publishing a post.

    Authenticate with the client's own key, sent as the X-Api-Key header (or
    "api_key" in the body). The legacy shared "secret" still works until every
    scenario has moved over.

    Expected JSON body:
        { "post_id": 123, "posted_url": "https://..." }
    """
    data = request.get_json(silent=True) or {}
    post, err = _inbound_post(data)
    if err:
        return err
    post_id = post['id']

    posted_url = data.get('posted_url', '') or ''
    notes = posted_url or 'Marked posted by Make.com'
    db.update_post_status(int(post_id), 'posted', notes, changed_by='make.com',
                          posted_url=posted_url or None)
    return jsonify({'ok': True, 'post_id': post_id, 'status': 'posted'})


@app.route('/webhook/media-ingest', methods=['POST'])
def webhook_media_ingest():
    """Inbound endpoint — a Make.com scenario (or a manual call) sends media
    from a client's own accounts. It lands as a raw source in that client's
    gallery, marked 'needs_editing' — it still needs a pass through Canva or
    the video editor before it can go on a post. Authenticate with the client's
    own key in the X-Api-Key header; the key decides which client the file
    belongs to.

    Either send the file directly (multipart, field "file"), or a JSON body
    with "media_url" for the server to fetch — an https link on an allowed
    media host (MEDIA_INGEST_ALLOWED_HOSTS; Meta's CDNs by default, see
    media_ingest.py). Stored in S3 only. Either way, these fields:
        source       "instagram" | "tiktok" | "web" (default "web")
        source_url   the original post/page (http/https), for the editor's
                     context; the same source_url twice is one file
        caption_hint the original caption, if any (up to 5000 characters)
    """
    # Only a client's own key, and only in the header. The legacy shared secret
    # carries no client — accepting it here would let whoever holds it file media
    # under any client they name. Nothing calls this endpoint yet, so there is no
    # old scenario to keep working. A key in a body or a query string ends up in logs.
    api_key = (request.headers.get('X-Api-Key') or '').strip()
    cid = db.client_id_for_api_key(api_key) if api_key else None
    if cid is None or not db.get_client(cid):     # unknown, revoked, or client deleted
        return jsonify({'error': 'Forbidden'}), 403

    # The bucket or nowhere. The Render disk is 1 GB and holds the database too;
    # raw reels landing there would fill it and take the database down with them.
    if not s3_media.enabled():
        return jsonify({'error': 'Media ingest is not configured on this server.'}), 503

    is_multipart = request.mimetype == 'multipart/form-data'
    if not is_multipart and (request.content_length or 0) > media_ingest.MAX_JSON_BYTES:
        return jsonify({'error': 'Request body too large. Send the file itself as a '
                                 'multipart upload, or a media_url.'}), 413
    data = request.form if is_multipart else (request.get_json(silent=True) or {})

    try:
        source = media_ingest.clean_source(data.get('source'))
        source_url = media_ingest.clean_source_url(data.get('source_url'))
        caption_hint = media_ingest.clean_caption(data.get('caption_hint'))
    except media_ingest.Refused as e:
        return jsonify({'error': str(e)}), e.status

    # A scenario that runs again sends the same post again: answer with the file
    # already here instead of downloading and storing a second copy.
    if source_url:
        existing = db.get_media_by_source(cid, source_url)
        if existing:
            return jsonify({'ok': True, 'media_id': existing['id'], 'duplicate': True}), 200

    # Neither path holds the file in memory: werkzeug spools a sizeable upload to a
    # temp file, and a fetched link is streamed into one.
    spool = None
    try:
        if is_multipart:
            f = request.files.get('file')
            if not f or not f.filename:
                return jsonify({'error': 'No file provided.'}), 400
            mime, ext = media_ingest.resolve_type(f.mimetype, media_ingest.ext_of(f.filename))
            body = f.stream
            body.seek(0, os.SEEK_END)
            size = body.tell()
            body.seek(0)
            original = f.filename
        else:
            media_url = (data.get('media_url') or '').strip()
            if not media_url:
                return jsonify({'error': 'Provide either a "file" upload or a "media_url".'}), 400
            spool = body = tempfile.TemporaryFile()
            content_type, final_url, size = media_ingest.fetch_to_file(
                media_url, spool, app.config['MAX_CONTENT_LENGTH'])
            path = urlsplit(final_url).path
            mime, ext = media_ingest.resolve_type(content_type, media_ingest.ext_of(path))
            body.seek(0)
            original = path.rsplit('/', 1)[-1]
        if not size:
            return jsonify({'error': 'No media data received.'}), 400

        key = s3_media.build_key(cid, 'ingest.' + ext)          # server-side, from the key's client
        try:
            s3_media.upload(key, body, mime)
        except Exception:                                        # noqa: BLE001 - boto raises many kinds
            log.exception('media-ingest: storing %s for client %s failed', key, cid)
            return jsonify({'error': 'Could not store the media file. Try again later.'}), 502
    except media_ingest.Refused as e:
        log.warning('media-ingest refused for client %s: %s (%s)', cid, e, e.detail)
        return jsonify({'error': str(e)}), e.status
    except media_ingest.FetchFailed as e:
        log.warning('media-ingest fetch failed for client %s: %s', cid, e)
        return jsonify({'error': 'Could not fetch the media file from media_url.'}), 502
    finally:
        if spool is not None:
            spool.close()                                        # a TemporaryFile deletes itself

    filename = key.rsplit('/', 1)[-1]
    try:
        media_id = db.add_media(cid, filename, secure_filename(original) or filename,
                                media_rules.kind_of_filename(filename), size, caption_hint, '[]',
                                storage='s3', s3_key=key,
                                edit_status='needs_editing', source=source, source_url=source_url)
    except sqlite3.IntegrityError:
        # Two deliveries of the same post got past the check above; the index kept one.
        existing = db.get_media_by_source(cid, source_url) if source_url else None
        if not existing:
            raise
        s3_media.delete(key)
        return jsonify({'ok': True, 'media_id': existing['id'], 'duplicate': True}), 200
    return jsonify({'ok': True, 'media_id': media_id}), 201


@app.route('/webhook/test/<int:post_id>', methods=['POST'])
@roles_required('admin')
@require_content_access('post_id')
def webhook_test(post_id):
    """Manually (re)fire the Make.com webhook for a post. Admin only, and
    object-scoped: this dispatches a real publish, so it is gated the same as
    any content mutation. A client user is rejected with 403 before reaching it.
    (post_id moved into the URL so @require_content_access applies.)"""
    post = g.content_row
    ok, msg = webhooks.dispatch_post(post, current_user.id, current_user.role, request.remote_addr)
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'ok': False, 'error': msg}), 502


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
    """Called by a client's scenario ~24h after publishing with platform stats.

    Same authentication as /webhook/publish: the client's own key in X-Api-Key,
    with the legacy shared secret honoured until every scenario has moved over.

    Expected JSON body:
        {
          "post_id": 123,
          "likes": 0, "comments": 0, "shares": 0, "saves": 0,
          "reach": 0, "impressions": 0, "clicks": 0
        }
    """
    data = request.get_json(silent=True) or {}
    post, err = _inbound_post(data)
    if err:
        return err
    post_id = post['id']

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
    # Header only — a secret in the query string ends up in access logs, proxies
    # and browser history (the same rule _inbound_caller follows).
    return webhooks.verify_secret(request.headers.get('X-Secret', ''))


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
        _audit_change(post, db.update_post(post_id, merged), 'api')

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
    _audit_change(post, db.update_post(post_id, merged), 'generated caption')
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
    problem = _refuse_unknown_platform(data)
    if problem:
        return jsonify({'error': problem}), 400
    if data.get('status', 'raw') not in PRE_APPROVAL_STATUSES:
        return jsonify({'error': 'A post cannot be created past the approval gate.'}), 400
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
    return _generate_trends(request.get_json(silent=True) or {})


@app.route('/trends/generate', methods=['POST'])
@roles_required('admin', 'manager')   # org-wide and spends Claude credits: staff only
def trends_generate():
    """The Trends page's own button. Session login + CSRF, so the page never has
    to carry the machine secret into the browser."""
    return _generate_trends(request.get_json(silent=True) or {})


def _generate_trends(data):
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
    return render_template('trends.html', trends=trend_rows, platforms=PLATFORMS,
                           clients=all_clients, filter_platform=platform)


# ── Report ─────────────────────────────────────────────────────────────────────

@app.route('/users')
@roles_required('admin')     # user management: admin only
def users():
    # The one-time invite/reset link is carried in the session (not a query string
    # or a flash of the raw URL) so it renders once in a copy box, then is gone.
    new_invite = session.pop('new_invite', None)
    return render_template('users.html', users=db.get_users(),
                           clients=db.get_clients(), new_invite=new_invite)


@app.route('/users/invite', methods=['POST'])
@roles_required('admin')
def users_invite():
    email = request.form.get('email', '').strip().lower()
    # Allowlist, never the raw form value: this route mints privileges.
    role = request.form.get('role', 'client').strip().lower()
    if role not in ('admin', 'client'):
        flash('Unknown role.', 'error')
        return redirect(url_for('users'))
    client_id = request.form.get('client_id', type=int)
    if role == 'admin':
        client_id = None            # an admin is org-wide, never client-scoped
    if not email or (role == 'client' and not client_id):
        flash('Email is required, and a client user must be assigned to a client.', 'error')
        return redirect(url_for('users'))
    if db.get_user_by_email(email):
        flash('A user with that email already exists.', 'error')
        return redirect(url_for('users'))
    if role == 'client' and not db.get_client(client_id):
        flash('Client not found.', 'error')
        return redirect(url_for('users'))
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=72)).isoformat()
    new_id = db.create_pending_user(email, role, client_id, _hash_token(token), expires)
    # Never the token itself — only that an invite of this role was issued.
    db.add_audit(current_user.id, current_user.role, client_id, 'user', new_id,
                 'invite', reason=f'{role} invite issued for {email}')
    invite_url = url_for('accept_invite', token=token, _external=True)
    # Shown once, on screen only — never emailed from the app.
    session['new_invite'] = {'email': email, 'url': invite_url,
                             'kind': 'invite', 'role': role}
    return redirect(url_for('users'))


@app.route('/users/<int:user_id>/deactivate', methods=['POST'])
@roles_required('admin')
def users_deactivate(user_id):
    u = db.get_user_by_id(user_id)
    if not u:
        flash('User not found.', 'error')
        return redirect(url_for('users'))
    # Two locks that can't be left to the UI: you can't shut yourself out, and
    # the last admin standing can't be removed (nobody could get back in).
    if user_id == current_user.id:
        flash('You cannot deactivate your own account.', 'error')
        return redirect(url_for('users'))
    if u['role'] == 'admin' and db.count_active_admins(exclude_user_id=user_id) == 0:
        flash('This is the last active admin — promote another admin first.', 'error')
        return redirect(url_for('users'))
    db.set_user_active(user_id, False)
    db.add_audit(current_user.id, current_user.role, u['client_id'], 'user', user_id,
                 'deactivate', reason=f"deactivated {u['email']}")
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
    session['new_invite'] = {'email': u['email'], 'url': invite_url, 'kind': 'reset'}
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


# Admin-only Settings section (integration config). Access is enforced on the
# blueprint, not per route.
import settings
app.register_blueprint(settings.settings_bp)


# Register auth after all page routes are defined so the login guard's
# before_request runs after setup() (DB init) on the first request.
auth.init_auth(app)


if __name__ == '__main__':
    db.init_db()
    auth.bootstrap_admin()
    app.run(debug=True, port=5000)
