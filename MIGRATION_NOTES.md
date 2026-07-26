# MIGRATION NOTES — Phase 2, Stage 1 (Roles & Client Portal)

Branch: `feat/roles-client-portal` → merges into `master`.

## Target environment (confirmed, do not assume)

- **Database: SQLite**, file `/data/soulful_content.db`.
- **Render** web service `soulful-content-engine` (srv-d7qa7kkm0tmc73cvh8ig), region Ohio.
- **Persistent disk IS mounted** — `dsk-d9h22tvlk1mc738t3t50` at mount point `/data`
  (env `DB_PATH=/data/soulful_content.db`, `UPLOAD_PATH=/data/uploads`).
  The database and uploads therefore **survive deploys and restarts**. There is no
  Postgres/Supabase in this deployment — all migrations here are SQLite `ALTER TABLE`s.
- Migrations are applied by `database.init_db()`, which runs **automatically on the
  first request after each deploy** (`app.py` `@app.before_request setup()` → idempotent
  `ALTER TABLE ... ADD COLUMN` guarded by try/except). There is **no separate migrate
  command** and no Render `preDeploy` step — deploying the code *is* running the migration.

### SQLite constraint note
SQLite cannot add `CHECK` or `FOREIGN KEY` constraints to an existing table via `ALTER`.
The tenancy invariant ("a `client` user must have a `client_id`; a non-client user must
not") is therefore enforced in **application code** (`database.create_user`, which raises
`ValueError`), not by the schema. This is covered by two tests in `tests/test_authz.py`.

## What this migration adds

New columns on `users` (all nullable / defaulted → **behaviour-neutral** on existing rows):

| Column | Type | Default | Purpose |
|---|---|---|---|
| `client_id` | INTEGER | NULL | tenant binding for `client`-role users |
| `invite_token_hash` | TEXT | NULL | sha256 of a single-use invite token |
| `invite_expires_at` | TEXT | NULL | ISO timestamp, 72h expiry |
| `last_login_at` | TEXT | NULL | audit |
| `is_active` | INTEGER | 1 | deactivation flag |

No columns are dropped or renamed. No data is rewritten. Existing users keep
`role` (default `'admin'`), `client_id = NULL`, `is_active = 1` → they behave exactly
as before.

## Deploy order

1. **Restore point first.** Prod is `master` @ `8b55a77`, tagged
   `restore-point-pre-roles`. Roll back with `git reset --hard restore-point-pre-roles`
   + redeploy if anything misbehaves. The persistent DB is forward-compatible (extra
   nullable columns are simply ignored by old code), so a code rollback is safe without
   a DB rollback.
2. **Merge `feat/roles-client-portal` → `master`** only after CI is green.
3. **Deploy `master` to Render.** On the first request, `init_db()` runs the additive
   `ALTER`s against `/data/soulful_content.db`. Order within the deploy is:
   **migrate (auto) → (no backfill needed) → serve.**
   No backfill is required because every new column has a safe default for existing rows.
4. **Verify** after deploy: log in as the existing admin (unchanged), open `/users`
   (admin-only), confirm the sidebar is unchanged for admin and the app serves normally.

## HARD RULE — client-user creation is gated

> role defaults to 'admin' and no client users exist, so partial deploys are
> behaviour-neutral. Do not create, seed, or document creating any client-role user
> until steps 1 and 2 are both green.

Steps 1 (route scoping + `AUTHZ_AUDIT.md`) and 2 (`tests/test_authz.py`, incl. the
forged-`client_id` case) are both green (16 tests passing locally and in CI). Even so:
**do not create the first real client user via `/users/invite` until you have deployed,
smoke-tested the admin experience in production, and explicitly decided to onboard a
client.** Until a `client`-role user exists, every route behaves identically to
pre-Stage-1 (all users are admin → `current_scope()` is always `None` → no filtering
path changes behaviour). The moment a client user logs in, the scoping and portal UI
become live for that session only.

## Rollback

- **Code:** `git reset --hard restore-point-pre-roles` on `master`, redeploy.
- **DB:** no action needed — the added columns are nullable/defaulted and ignored by
  the old code. If you nonetheless want a clean DB, the disk snapshot on Render can be
  restored, but this is not required for a code rollback.
