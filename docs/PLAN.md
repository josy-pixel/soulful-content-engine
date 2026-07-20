# Soulful Content Engine v2 — build plan

## Two versions, kept separate
- **v1 (live, do not touch):** Flask + Bootstrap on Render (`soulful-content-engine.onrender.com`),
  repo `josy-pixel/soulful-content-engine` (branch `master`). Working demo, seed data, weak voice engine.
  Stays running as the current demo.
- **v2 (this folder):** React + Vite + Tailwind + Supabase. The real product per the master prompt.
  Goes in a **new repo** so v1/Render is never broken.

## Salvaged from v1
- `make-blueprint-v3.json`, `make-scenario.json` — existing Make automations (reuse).
- Platform/length/emoji guides from `claude_api.py` — good logic, ported into v2 generation.
- Information architecture (nav: Clients · Caption Generator · Content Library · Scheduling · Performance · Trends · Reports).

## Why the rebuild (validates the master prompt)
v1's voice engine injects only tone/style/audience + ONE sample caption → generic output (the founder's #1 complaint).
v2 injects the FULL voice document + 10–15 sample captions + a voice-audit pass. Uses current Claude models
(v1 used the outdated `claude-sonnet-4-6`).

## Phases
1. **Multi-client core & onboarding** — auth+roles, onboarding wizard, per-client dashboard, asset library + drag-sort.
2. **Voice-faithful bulk generation** — `generate-batch` Edge Function (full voice inject) + voice-audit pass + batch review.
3. **Approval & scheduling** — client one-tap weekly approval → scheduler (swappable publishing adapter: Graph API / Buffer / Business Suite).
4. **Instagram ingestion & trends** — daily Graph API pull (official only, no scraping) + UK trend calendar + weekly brief generator.

## Data model
`supabase/migrations/0001_init.sql` — 9 tables, multi-tenant, RLS on every table.

## Open decisions (need founder/Yuval)
- New repo name for v2? (recommend `soulful-content-engine-v2` or a fresh repo).
- Supabase project — create new (needs Josy/Yuval Supabase login).
- Meta app + per-client OAuth (Phase 3/4). Anthropic API key for Edge Functions (Phase 2).
