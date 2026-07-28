# WEBHOOK CONTRACT

The payload the app POSTs to a client's Make scenario when a post is dispatched.
**This is the source of truth every cloned scenario is built against.** ~10 scenarios
will consume it, so the rules below are strict.

## Versioning rules (do not break)

- Every payload carries `"contract_version": 1`.
- In v1: **never remove or rename a field.** Additions are allowed; removals are not.
- A breaking change ships as **v2 alongside v1**, and clients are migrated one at a
  time. v1 keeps flowing until the last client is moved off it.

## Transport

- Method: `POST`, body: JSON.
- Header today: **`Content-Type: application/json` only.**
- **`X-Secret` is NEW (per-client, added in Part 3).** It is a genuinely new outbound
  mechanism — the current global scenario sends no auth at all. **The header does
  nothing until the receiving (cloned) scenario is explicitly configured to verify
  it.** An unverified header is theatre. See ONBOARDING.md for the verification step.
- Timeout: 10s (current). Target URL: per-client in Part 3; a single global
  `MAKE_WEBHOOK_URL` today.

## Payload — v1 fields

| field | type | notes |
|---|---|---|
| `contract_version` | int | always `1` for this contract |
| `event` | str | constant `"post_approved"` |
| `post_id` | int | the content_posts id |
| `client_id` | int | **stable client id** — route/key on THIS, not the name |
| `client` | str | client display name — kept for readability; **ambiguous** (two clients can share a name) |
| `platform` | str | `"facebook"` \| `"instagram"` (TikTok/YouTube/LinkedIn later); the scenario routes internally on this |
| `idempotency_key` | str | `"<post_id>:<platform>:<random>"`, unique per dispatch attempt; use to dedupe a redelivered POST |
| `content_type` | str | `"photo"` \| `"reel"` \| `"video"` \| … (default `"photo"`) |
| `topic` | str | internal topic/title |
| `caption` | str | the caption body |
| `hashtags` | str | space/line separated, may be empty |
| `hook` | str | optional opening hook, may be empty |
| `image_url` | str | **RELATIVE path** (e.g. `/uploads/1/abc.jpg`); the scenario prepends the app domain |
| `scheduled_date` | str | may be empty |
| `approved_at` | str | ISO8601 timestamp |
| `callback_url` | str | `<APP_URL>/webhook/publish` — where a scenario should report back (see the inbound-callback note) |
| `performance_url` | str | `<APP_URL>/api/performance` |

### Example

```json
{
  "contract_version": 1,
  "event": "post_approved",
  "post_id": 42,
  "client_id": 7,
  "client": "Holly Hagan",
  "platform": "facebook",
  "idempotency_key": "42:facebook:9f2c1a7b8e4d4f0e9c1a2b3c4d5e6f70",
  "content_type": "photo",
  "topic": "Morning routine",
  "caption": "Rise and shine ☀️",
  "hashtags": "#wellness #morning",
  "hook": "",
  "image_url": "/uploads/7/abc123.jpg",
  "scheduled_date": "",
  "approved_at": "2026-07-28T09:15:00",
  "callback_url": "https://soulful-content-engine.onrender.com/webhook/publish",
  "performance_url": "https://soulful-content-engine.onrender.com/api/performance"
}
```

## Known fragilities (recorded, not being fixed now)

1. **`image_url` is a relative path** that each scenario prepends a domain to. If media
   ever moves to object storage (S3/R2) with absolute/signed URLs, **every scenario
   breaks at once** — they'd each be prepending a domain to an already-absolute URL.
   A media-storage change is therefore a contract change (likely a v2).
2. **Clients are identified by name in the original contract.** `client_id` is being
   added in v1 precisely because the name is ambiguous (two clients can be named
   "יובל"). New scenarios must key on `client_id`, never `client`.

## Inbound direction — the rule that cannot bend (settled now, before anyone builds it)

The intended end state is data flowing both ways: external systems push content INTO
this app; the app pushes OUT to social networks (this contract). The inbound half is
**not built yet**, but this is fixed now so nothing is built against a wrong assumption:

- **Content arriving from any external system lands in `needs_review`**, exactly like
  AI-generated content. **There is no inbound path that creates `approved`, `scheduled`
  or `posted` content, and no API key that grants publish.** An external system can
  *propose* content; only a human in this app *releases* it.
- Inbound lives under the reserved namespace **`/api/v1/`**; new machine endpoints go
  nowhere else. Config lives under **`/settings/api-keys`** (reserved).
- Inbound keys will be **per-client, scoped, stored hashed, revocable, with a last-used
  timestamp** — the shape of the Stage 1 invite tokens. No single global inbound secret.
- When built, inbound needs **rate limiting, request size limits, and per-key audit
  logging.**
