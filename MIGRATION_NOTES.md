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

---

# Stage 2 (Deletion, Trash & Data Lifecycle)

## Backup & Restore drill (tested)

Only the web process can see `/data` (no shell, and Render one-off jobs do NOT mount
the disk). So backups are produced in-process and restores happen at boot, before any
connection opens, via `database.restore_if_requested()` (called at the top of
`init_db()`). It is OFF by default and guarded five ways: source validation
(`PRAGMA integrity_check`), a confirmation token, a marker file (no re-run), a
pre-overwrite backup of the current DB (restore ABORTS if that backup fails), and
before/after row counts written to the logs.

### To restore the production DB from a backup

1. Pick the backup file on the disk, e.g. `/data/backups/soulful-20260727T174225Z.db`.
2. Set two env vars on the Render service (API or dashboard):
   - `RESTORE_FROM=/data/backups/soulful-20260727T174225Z.db`
   - `RESTORE_CONFIRM=soulful-20260727T174225Z.db`   ← must equal the **filename** of RESTORE_FROM
3. Trigger a deploy. On boot the app will, in order:
   validate the backup → back up the CURRENT DB to `/data/backups/pre-restore-<ts>.db`
   (aborting if that fails) → overwrite `/data/soulful_content.db` → drop stale
   `-wal`/`-shm` → write marker `/data/.restored-<filename>` → log before/after counts.
   Grep the deploy logs for `restore: RESTORED ... before=... after=...` to confirm.
4. **Remove `RESTORE_FROM` and `RESTORE_CONFIRM`** and redeploy. (The marker also stops
   a left-behind env var from re-restoring on the next restart.)
5. To restore the **same** file again later, delete its marker: `/data/.restored-<filename>`.

The pre-overwrite backup means the restore itself is reversible: to undo, restore from
the `pre-restore-<ts>.db` the same way.

> Offsite copies: `/data/backups` lives on the same disk as the DB — that is versioning,
> not backup. Encrypted upload to Cloudflare R2 is wired in the Stage 2 backup routine
> (env: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`,
> `BACKUP_ENCRYPTION_KEY`). Until that is live, pull a copy off Render manually.

## ⚠️ Blocker to resolve BEFORE Part 3 (deletion rules)

The inbound callback path appears broken: **0 posts have a `posted_url`** yet a post was
observed live on Facebook — so Make is not calling back into `/webhook/publish`, and the
app shows a `posted` status nothing verifies. Part 3's rules are written per status
("deleting a `posted` item is admin-only and does not remove the live post"), which is
meaningless if `posted` cannot be verified. Before Part 3: diagnose whether Make is
configured to call `/webhook/publish`, whether it is reachable, whether it authenticates
(the X-Secret), and whether it writes `posted_url`. Report findings then — not during
Part 1 (which is purely structural).

---

# Per-client webhooks

## Known limitation: `webhook_secret` is stored in plaintext

`client_webhooks.webhook_secret` sits in plaintext in the SQLite file on the Render
disk. This is a deliberate, accepted limitation for now, with a **per-client blast
radius** (a disk compromise exposes each client's outbound secret, not one shared
global secret). Mitigations in place:

- The secret is **masked everywhere** — UI and logs show only the last 4 characters
  (`…1234`), never the full value, and it is never written to `audit_log`.
- Rotating a client's secret is a single UI action (paste a new one), and doing so
  resets that webhook's status to `untested` so it must be re-verified.

If this needs hardening later: encrypt the column at rest with a KMS-held key (the
same `BACKUP_ENCRYPTION_KEY` pattern proposed for R2 backups), or move secrets to a
secrets manager. Not done now.

## Deploy order for per-client webhooks (Part 3)

Dispatch now resolves the target from the post's `client_id` and **refuses if the
client has no webhook row** — there is no silent global default. The currently
working client has no row yet, so:

1. **Before deploying**, set `LEGACY_WEBHOOK_FALLBACK=true` on Render (with the
   existing `MAKE_WEBHOOK_URL` still set). The un-migrated client then keeps
   dispatching via the global webhook; every use logs a WARNING naming the client.
2. Migrate clients one at a time (Settings → Webhooks): add each client's webhook,
   press **Send test ping**, confirm it passes, then publish real content for them.
3. When every client has a verified row, **remove `LEGACY_WEBHOOK_FALLBACK`** — the
   fallback code path is then dead and gets deleted.

Deploying without step 1 would break the working client's publishing on the first
approve (it would refuse with "No webhook configured for this client").
