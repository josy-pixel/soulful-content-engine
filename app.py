import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from dotenv import load_dotenv
import database as db
import claude_api as ai

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

PLATFORMS = ['instagram', 'facebook', 'tiktok', 'linkedin', 'youtube']
STATUSES = ['draft', 'needs_review', 'approved', 'scheduled', 'posted']
STATUS_TRANSITIONS = {
    'draft':        ['needs_review', 'approved'],
    'needs_review': ['draft', 'approved'],
    'approved':     ['scheduled', 'posted', 'needs_review'],
    'scheduled':    ['approved', 'posted'],
    'posted':       [],
}


@app.before_request
def setup():
    db.init_db()


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    stats = db.get_dashboard_stats()
    return render_template('dashboard.html', stats=stats, platforms=PLATFORMS, statuses=STATUSES)


# ── Clients ────────────────────────────────────────────────────────────────────

@app.route('/clients')
def clients():
    all_clients = db.get_clients()
    return render_template('clients.html', clients=all_clients)


@app.route('/clients/new', methods=['GET', 'POST'])
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
        flash('Client updated.', 'success')
        return redirect(url_for('client_detail', client_id=client_id))
    return render_template('client_form.html', client=client)


# ── Brand Voice ────────────────────────────────────────────────────────────────

@app.route('/api/brand-voice/<int:client_id>/<platform>', methods=['POST'])
def save_brand_voice(client_id, platform):
    if platform not in PLATFORMS + ['general']:
        return jsonify({'error': 'Invalid platform'}), 400
    data = request.get_json()
    db.upsert_brand_voice(client_id, platform, data)
    return jsonify({'ok': True})


# ── Caption Generator ──────────────────────────────────────────────────────────

@app.route('/caption-generator')
def caption_generator():
    all_clients = db.get_clients()
    preselect_client = request.args.get('client_id', type=int)
    preselect_platform = request.args.get('platform', '')
    return render_template('caption_generator.html', clients=all_clients,
                           platforms=PLATFORMS, preselect_client=preselect_client,
                           preselect_platform=preselect_platform)


@app.route('/api/generate-caption', methods=['POST'])
def api_generate_caption():
    data = request.get_json()
    client_id = data.get('client_id')
    platform = data.get('platform')
    topic = data.get('topic', '').strip()
    extra = data.get('extra_context', '').strip()

    if not all([client_id, platform, topic]):
        return jsonify({'error': 'client_id, platform, and topic are required.'}), 400

    client = db.get_client(client_id)
    if not client:
        return jsonify({'error': 'Client not found.'}), 404

    brand_voice = db.get_brand_voice(client_id, platform) or db.get_brand_voice(client_id, 'general') or {}

    caption, error = ai.generate_caption(client['name'], brand_voice, platform, topic, extra)
    if error:
        return jsonify({'error': error}), 500

    hashtags = ai.generate_hashtags(client['name'], brand_voice, platform, topic, caption)

    return jsonify({'caption': caption, 'hashtags': hashtags})


@app.route('/api/save-caption', methods=['POST'])
def api_save_caption():
    data = request.get_json()
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
    posts = db.get_posts(
        client_id=client_id or None,
        platform=platform or None,
        status=status or None,
        limit=50
    )
    all_clients = db.get_clients()
    return render_template('content_list.html', posts=posts, clients=all_clients,
                           platforms=PLATFORMS, statuses=STATUSES,
                           filter_client=client_id, filter_platform=platform,
                           filter_status=status)


@app.route('/content/new', methods=['GET', 'POST'])
def content_new():
    all_clients = db.get_clients()
    if request.method == 'POST':
        data = {
            'client_id': int(request.form['client_id']),
            'platform': request.form['platform'],
            'topic': request.form['topic'].strip(),
            'caption': request.form['caption'].strip(),
            'hashtags': request.form.get('hashtags', '').strip(),
            'status': request.form.get('status', 'draft'),
            'scheduled_date': request.form.get('scheduled_date') or None,
            'notes': request.form.get('notes', '').strip(),
        }
        if not data['topic'] or not data['caption']:
            flash('Topic and caption are required.', 'error')
            return render_template('content_form.html', post=None, clients=all_clients,
                                   platforms=PLATFORMS, statuses=STATUSES)
        post_id = db.create_post(data)
        flash('Post created successfully.', 'success')
        return redirect(url_for('content_detail', post_id=post_id))
    preselect = {
        'client_id': request.args.get('client_id', ''),
        'platform': request.args.get('platform', ''),
    }
    return render_template('content_form.html', post=None, clients=all_clients,
                           platforms=PLATFORMS, statuses=STATUSES, preselect=preselect)


@app.route('/content/<int:post_id>')
def content_detail(post_id):
    post = db.get_post(post_id)
    if not post:
        flash('Post not found.', 'error')
        return redirect(url_for('content_list'))
    history = db.get_approval_history(post_id)
    metrics = db.get_performance(post_id)
    allowed_transitions = STATUS_TRANSITIONS.get(post['status'], [])
    return render_template('content_detail.html', post=post, history=history,
                           metrics=metrics, allowed_transitions=allowed_transitions,
                           statuses=STATUSES)


@app.route('/content/<int:post_id>/edit', methods=['GET', 'POST'])
def content_edit(post_id):
    post = db.get_post(post_id)
    if not post:
        flash('Post not found.', 'error')
        return redirect(url_for('content_list'))
    all_clients = db.get_clients()
    if request.method == 'POST':
        data = {
            'topic': request.form['topic'].strip(),
            'caption': request.form['caption'].strip(),
            'hashtags': request.form.get('hashtags', '').strip(),
            'scheduled_date': request.form.get('scheduled_date') or None,
            'notes': request.form.get('notes', '').strip(),
        }
        db.update_post(post_id, data)
        flash('Post updated.', 'success')
        return redirect(url_for('content_detail', post_id=post_id))
    return render_template('content_form.html', post=post, clients=all_clients,
                           platforms=PLATFORMS, statuses=STATUSES, preselect={})


@app.route('/content/<int:post_id>/status', methods=['POST'])
def content_status(post_id):
    new_status = request.form.get('status')
    notes = request.form.get('notes', '')
    if new_status not in STATUSES:
        flash('Invalid status.', 'error')
        return redirect(url_for('content_detail', post_id=post_id))
    db.update_post_status(post_id, new_status, notes)
    flash(f'Status updated to "{new_status.replace("_", " ").title()}".', 'success')
    return redirect(url_for('content_detail', post_id=post_id))


@app.route('/content/<int:post_id>/delete', methods=['POST'])
def content_delete(post_id):
    db.delete_post(post_id)
    flash('Post deleted.', 'success')
    return redirect(url_for('content_list'))


# ── Scheduling ─────────────────────────────────────────────────────────────────

@app.route('/scheduling')
def scheduling():
    scheduled = db.get_scheduled_posts()
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
    all_clients = db.get_clients()

    # Only show posted posts
    posted_posts = db.get_posts(platform=platform or None, client_id=client_id or None,
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
def api_add_performance(post_id):
    data = request.get_json()
    db.add_performance(post_id, data)
    return jsonify({'ok': True})


# ── Report ─────────────────────────────────────────────────────────────────────

@app.route('/report')
def report():
    end = datetime.now()
    start = end - timedelta(days=7)
    return render_template('report.html',
                           default_start=start.strftime('%Y-%m-%d'),
                           default_end=end.strftime('%Y-%m-%d'))


@app.route('/api/generate-report', methods=['POST'])
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


if __name__ == '__main__':
    db.init_db()
    app.run(debug=True, port=5000)
