"""Admin-only Settings section. Access is enforced on the blueprint (before_request),
not per route, so a new page cannot be added unprotected by accident."""
from flask import (Blueprint, render_template, request, redirect, url_for, flash,
                   jsonify, abort)
from flask_login import current_user

import database as db
import webhooks

WEBHOOK_PLATFORMS = ['facebook', 'instagram']   # FB + IG only for now; the list is future-proof

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')


@settings_bp.before_request
def _admin_only():
    # The global login guard has already run (redirects anonymous users). Here we
    # require the admin role for EVERY settings route.
    if not getattr(current_user, 'is_authenticated', False):
        return
    if getattr(current_user, 'role', None) != 'admin':
        abort(403)


@settings_bp.route('/')
def index():
    return redirect(url_for('settings.webhooks_page'))


@settings_bp.route('/webhooks')
def webhooks_page():
    by_client = {r['client_id']: r for r in db.get_all_client_webhooks()}
    clients = db.get_clients()
    for c in clients:
        w = by_client.get(c['id'])
        if w:
            w['secret_masked'] = webhooks.mask_secret(w['webhook_secret'])
        c['webhook'] = w
    return render_template('settings/webhooks.html', clients=clients,
                           platforms=WEBHOOK_PLATFORMS, active='webhooks')


@settings_bp.route('/webhooks/save', methods=['POST'])
def webhooks_save():
    client_id = request.form.get('client_id', type=int)
    url = (request.form.get('webhook_url') or '').strip()
    secret = (request.form.get('webhook_secret') or '').strip()
    platforms = [p for p in request.form.getlist('platforms') if p in WEBHOOK_PLATFORMS]

    if not client_id or not db.get_client(client_id):
        flash('Pick a valid client.', 'error'); return redirect(url_for('settings.webhooks_page'))
    if not url:
        flash('Webhook URL is required.', 'error'); return redirect(url_for('settings.webhooks_page'))
    if not platforms:
        flash('Enable at least one platform.', 'error'); return redirect(url_for('settings.webhooks_page'))

    existing = db.get_client_webhook(client_id)
    secret_changed = bool(secret)
    if not secret:
        if existing:
            secret = existing['webhook_secret']          # keep current
        else:
            flash('A secret is required for a new webhook.', 'error')
            return redirect(url_for('settings.webhooks_page'))
    try:
        db.upsert_client_webhook(client_id, url, secret, ','.join(platforms))
    except ValueError as e:
        flash(str(e), 'error'); return redirect(url_for('settings.webhooks_page'))

    db.add_audit(current_user.id, current_user.role, client_id, 'webhook', client_id, 'config',
                 metadata={'platforms': platforms, 'secret_changed': secret_changed},  # never the secret
                 request_ip=request.remote_addr)
    flash('Webhook saved — status reset to untested. Send a test ping to verify it.', 'success')
    return redirect(url_for('settings.webhooks_page'))


@settings_bp.route('/webhooks/<int:client_id>/test', methods=['POST'])
def webhooks_test(client_id):
    return jsonify(webhooks.send_test_ping(client_id, current_user.id, current_user.role,
                                           request.remote_addr))


@settings_bp.route('/webhooks/<int:client_id>/disable', methods=['POST'])
def webhooks_disable(client_id):
    db.set_client_webhook_enabled(client_id, False)
    db.add_audit(current_user.id, current_user.role, client_id, 'webhook', client_id, 'disable',
                 request_ip=request.remote_addr)
    flash('Webhook disabled — dispatch will refuse for this client.', 'success')
    return redirect(url_for('settings.webhooks_page'))


@settings_bp.route('/webhooks/<int:client_id>/enable', methods=['POST'])
def webhooks_enable(client_id):
    db.set_client_webhook_enabled(client_id, True)
    db.add_audit(current_user.id, current_user.role, client_id, 'webhook', client_id, 'enable',
                 request_ip=request.remote_addr)
    flash('Webhook re-enabled — status reset to untested, re-test it.', 'success')
    return redirect(url_for('settings.webhooks_page'))


@settings_bp.route('/webhooks/<int:client_id>/delete', methods=['POST'])
def webhooks_delete(client_id):
    db.delete_client_webhook(client_id)
    db.add_audit(current_user.id, current_user.role, client_id, 'webhook', client_id, 'delete',
                 request_ip=request.remote_addr)
    flash('Webhook removed.', 'success')
    return redirect(url_for('settings.webhooks_page'))


@settings_bp.route('/api-keys')
def api_keys():
    return render_template('settings/placeholder.html', active='api-keys', title='API Keys',
                           blurb='Inbound API access — per-client, scoped keys — will live here.')


@settings_bp.route('/general')
def general():
    return render_template('settings/placeholder.html', active='general', title='General',
                           blurb='Model selection and defaults will live here.')
