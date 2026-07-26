# AUTHZ AUDIT — Stage 1 (roles & client portal)

Every route that touches `clients`, `content_posts`, media, or analytics, its
role access, and the exact scoping / object-level check applied.

Scope model: `security.current_scope()` returns **None** for admin/manager
(unscoped, sees all) or the **bound client_id** for a `client` user. Machine
endpoints are gated by their own X-Secret (not session) — a logged-in client
user without the secret is rejected by `_check_secret`, so they cannot hit them.

## Human / browser routes

| Route | Method | admin/manager | client | Enforcement |
|---|---|---|---|---|
| `/` dashboard | GET | all | own KPIs | `get_dashboard_stats(scope=current_scope())` |
| `/clients` | GET | all | — (bounced) | `current_scope()` → redirect to own `client_detail` |
| `/clients/new` | GET/POST | admin | 403 | `@roles_required('admin')` |
| `/clients/<id>` | GET | all | own | `@require_client_access('client_id')` |
| `/clients/<id>/edit` | GET/POST | admin | 403 | `@roles_required('admin')` |
| `/clients/<id>/gallery` | GET | all | own | `@require_client_access` |
| `/clients/<id>/media/upload` | POST | all | own | `@require_client_access` |
| `/api/media/<id>` | PATCH | all | own | object-level `can_see_client(media.client_id)` |
| `/api/media/<id>` | DELETE | all | own | object-level `can_see_client(media.client_id)` |
| `/api/media/client/<id>` | GET | all | own | `@require_client_access` |
| `/api/content/<id>/media` | POST | all | own | `@require_content_access` + media-client check |
| `/api/content/<id>/media/<mid>` | DELETE | all | own | `@require_content_access` |
| `/api/brand-voice/<id>/<platform>` | POST | all | own | `@require_client_access` |
| `/caption-generator` | GET | all | own only | dropdown = `scoped_clients()`, preselect = scope |
| `/api/generate-caption` | POST | all | own | `enforce_client_id()` overwrites body client_id |
| `/api/save-caption` | POST | all | own | `enforce_client_id()` overwrites body client_id |
| `/content` | GET | all | own | `scoped_posts()` + `scoped_clients()` |
| `/content/new` | GET/POST | all | own | `enforce_client_id()` on submit + `scoped_clients()` |
| `/content/<id>` | GET | all | own | `@require_content_access('post_id')` |
| `/content/<id>/edit` | GET/POST | all | own, **not-posted** | `@require_content_access` + 403 if client & status==posted |
| `/content/<id>/status` | POST | all | own | `@require_content_access` |
| `/content/<id>/delete` | POST | all | own, **draft/needs_review** | `@require_content_access` + status gate for client (Stage 2 → soft delete) |
| `/scheduling` | GET | all | own | `get_scheduled_posts(scope=current_scope())` |
| `/performance` | GET | all | own | `scoped_posts()` + `scoped_clients()` |
| `/api/performance/<id>` | POST | all | own | `@require_content_access` |
| `/api/clients` | GET | all | own | `scoped_clients()` |
| `/api/client-config/<id>` | GET | all | own | `@require_client_access` |
| `/report` | GET | admin/manager | **403** | `@roles_required('admin','manager')` (not in client sidebar) |
| `/api/generate-report` | POST | admin/manager | **403** | `@roles_required('admin','manager')` |
| `/trends` | GET | all | all | Trends is org-wide per the matrix — intentionally NOT scoped |
| `/users` | GET/POST | admin | 403 | `@roles_required('admin')` (built in step 3) |

## Machine routes (X-Secret, whitelisted from the login guard)
`webhook_publish`, `api_performance_inbound`, `api_content_get`,
`api_content_patch`, `api_generate_caption_for_post`, `api_generate_hook`,
`api_content_create`, `api_trends_generate`.
Not session-authenticated. A client user has no X-Secret, so `_check_secret`
rejects them (401/403). **Untouched per instruction** (do not modify webhook /
X-Secret logic). Stage 2 will add a `deleted_at is null` guard inside
`webhook_publish` so a soft-deleted post can never be published.

## The four HARD RULES — where they live
1. **Never trust body client_id for a client user** → `enforce_client_id()` in
   `api_generate_caption`, `api_save_caption`, `content_new`.
2. **Every client/content/analytics query is scoped** → `scoped_posts`,
   `scoped_clients`, `get_dashboard_stats(scope)`, `get_scheduled_posts(scope)`.
3. **Object-level checks on the object** → `@require_content_access` /
   `@require_client_access` load the row and compare `client_id`, not the URL.
4. **Client dropdown is a single fixed value** → `scoped_clients()` returns only
   the own client; the UI (step 4) renders it disabled and submits are ignored.

## Notes / follow-ups
- `trends` shows org-wide trend data to all roles by design (matrix: Trends =
  Y/Y/Y). If Gozi wants trends hidden from clients, add `@roles_required`.
- Stage 2 replaces the hard delete in `content_delete` with soft-delete + trash.
