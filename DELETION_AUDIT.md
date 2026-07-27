# DELETION AUDIT — Stage 2, Part 1 (structural)

Part 1 makes the delete filter **unforgettable**, before any delete feature exists.
Every read of `content_posts` / `clients` goes through `database.py` and its filtered
views, so no query — present or future — can forget `deleted_at IS NULL`.

## The mechanism

- **Views** (recreated every boot in `init_db()`, after the ADD COLUMNs):
  - `v_content_active = SELECT * FROM content_posts WHERE deleted_at IS NULL`
  - `v_clients_active = SELECT * FROM clients   WHERE deleted_at IS NULL`
- **`database.py` is the repository** — the only file allowed to name the base tables.
  Enforced by `tests/test_no_raw_queries.py`, which fails CI if `FROM/UPDATE/DELETE
  content_posts|clients` appears in any other file (escape hatch: `# raw-query-ok: …`).
- **Base tables are read only by** `get_post_including_deleted()` and
  `get_clients_including_deleted()` (for trash/restore/purge, Part 3+), each marked.

## Every reader converted (base → view) and its callers

| database.py reader | reads | now via | route call sites (app.py) |
|---|---|---|---|
| `get_clients()` | clients | `v_clients_active` | dashboard, `/clients`, `/api/clients`, `client_config`, `/users` dropdown |
| `get_client(id)` | clients | `v_clients_active` | `client_detail`/edit/gallery/media, content routes, save-caption, invite |
| `get_posts()` | content+clients | `v_content_active`+`v_clients_active` | `content_list`, `performance`, `client_detail` recent, `scoped_posts()` |
| `get_post(id)` | content+clients | views | `content_detail`/edit/status/delete, `api_performance`, **machine**: `webhook_publish`, `api_content_*` |
| `get_dashboard_stats(scope)` | content+clients | views (status funnel, platform, per-client, upcoming, recent, totals, perf) | `/` dashboard |
| `get_scheduled_posts(scope)` | content+clients | views | `/scheduling`, dashboard upcoming |
| `get_report_data(a,b)` | content+clients | views | `/report` — **role-gated (admin/manager), NOT tenant-scoped**; delete-filtered anyway |
| `get_client_voice(id)` | clients | `v_clients_active` | caption generation |
| `get_users()` | clients (JOIN) | `v_clients_active` | `/users` (client_name lookup) |
| `update_post_status()` status read | content | base read + **write guard** (must SEE deleted to refuse) | status/webhook write paths |

**Machine routes get delete-filtering for free:** `webhook_publish` and the `api_content_*`
routes load via `get_post()` → `v_content_active`, so a soft-deleted post is already
invisible to them (Part 3 adds the explicit outbound re-check too).

## Write guard (refuse to mutate a deleted row — raise, never silent no-op)

`database._raise_if_deleted(conn, table, id)` reads the **base** table (it must see
deleted rows to reject them) and raises `ValueError`. Wired into:
`update_post_status` (inline), `update_post`, `set_post_error`, `set_post_voice_audit`,
`update_client`, `update_client_voice`.

## Tests (Part 1)

- `tests/test_no_raw_queries.py` — the guard: no raw base-table access outside `database.py`.
- `tests/test_soft_delete_core.py` — view/base column parity; a soft-deleted post/client is
  hidden from every reader; visible only via `get_*_including_deleted`; every guarded write
  raises on a deleted row.

## Not in Part 1 (later parts)

Deletion routes, the permission matrix, trash UI, restore, purge, erasure — Parts 3–7.
And the recorded pre-Part-3 blocker: the inbound `/webhook/publish` callback is not
recording `posted_url`, so `posted` is currently unverifiable (see MIGRATION_NOTES).
