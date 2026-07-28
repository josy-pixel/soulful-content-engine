# Onboarding a client — connecting their own Make scenario

Follow this with **Make open in one window** and **the app's Settings → Webhooks open
in another**. It connects a client so their content publishes to **their own** Facebook
Page and Instagram account, isolated from every other client.

> **Safety:** verify the secret gate with a **test ping BEFORE you point the Facebook /
> Instagram modules at the real Page.** The test ping carries no caption and no image,
> so it can't create a real post even if something is misconfigured — but testing
> before the Page is wired removes all doubt. Do the steps in this order.

---

## Step 1 — Clone the template scenario (Make)
Duplicate the template scenario **"Soulful Content Engine — Auto Publisher"** and rename
the copy for the client, e.g. **"Publisher — Holly Hagan"**.

## Step 2 — Get the webhook URL (Make)
The first module is a **Custom webhook**. Attach a **new** webhook to it, name it clearly,
and **copy the URL** (looks like `https://hook.eu1.make.com/xxxxxxxx`). Each client needs
a different URL — the app rejects a URL already used by another client.

## Step 3 — Build the X-Secret gate FIRST (Make), before connecting any real Page
The app sends a header **`X-Secret`** on every request. **It does nothing until this
scenario checks it.** Build:

1. Right after the webhook, add a **Router**.
2. **Route A — valid secret.** Filter: `X-Secret header` **Equal to** `«the secret you
   will paste into the app»`.
   - First module on Route A: a check on the payload's **`test`** field:
     - if **`test = true`** → a **Webhook Response** module: **Status `200`**, body
       `test ok` — and **nothing after it** (a test ping must never publish).
     - else → your **Facebook / Instagram** modules, then a **Webhook Response**
       **Status `200`**.
3. **Route B — everything else (wrong or missing secret).** One module: a **Webhook
   Response**, **Status `403`**, body `Forbidden`.

> To find the exact header field: press **Run once** in Make, send a test ping from the
> app (Step 5), and look at the incoming bundle — the header shows under `headers`,
> usually `x-secret`. Point the Route-A filter at that.
>
> Once any Webhook Response module exists, Make needs one on **every** path — make sure
> both the `200` (valid) and `403` (invalid) responses are wired.

Leave the Facebook / Instagram modules **not yet pointed at the real Page** for now.

## Step 4 — Save the webhook in the app (Settings → Webhooks)
Pick the client, paste the **webhook URL** and the **secret** (identical to the Route-A
filter value), tick **Facebook** and/or **Instagram**, Save. Status becomes **Untested**.

## Step 5 — Send the test ping and get to Verified
Press **Send test ping**. The app sends two requests — one with the **correct** secret
(must be accepted, 200) and one with a **deliberately wrong** secret (must be rejected,
403). Read the result:

- **Verified** (green) — correct accepted, wrong rejected. The gate works. Continue.
- **Insecure** (red) — the scenario **accepted the wrong secret**. Route B / the filter
  is wrong. Fix Step 3 and re-test. **Do not connect the real Page while it says Insecure.**
- **Failing** (red) — the correct ping didn't get a 200 (scenario off, wrong URL, or
  Route A doesn't respond 200). Fix and re-test.

## Step 6 — Now connect the client's real accounts (Make)
Only once Step 5 is **Verified**: connect **this client's** Facebook Page and Instagram
Business account (the manual OAuth step — it cannot be automated), and point the
Facebook / Instagram modules on Route A at that connection. Double-check they are not
left on another client's Page.

## Step 7 — Turn the scenario fully ON, then publish
Turn the scenario on. Publish real content for the client from the app as usual — it now
routes to this client's webhook and their Page.

---

## Troubleshooting

| Symptom | Meaning | What to do |
|---|---|---|
| Test ping = **Insecure** | Scenario accepted a wrong secret — X-Secret not enforced | Route-A filter must compare the header to the secret; Route B must return **403** |
| **403** on test/dispatch | The secret the app sends ≠ the secret in the Route-A filter | Make them identical; re-save the secret in the app, re-test |
| **timeout (10s)** | Make didn't respond in time | Ensure a **Webhook Response 200** fires quickly on Route A (respond before slow publish steps if needed) |
| A post shows **failed** | Dispatch got a non-2xx / no response | Open the client's row in Settings → Webhooks; **Last error** shows the HTTP status. Check the scenario is ON and the URL matches |
| **"No webhook configured for this client"** | No row in Settings → Webhooks | Add one (Steps 2–5). The legacy fallback covers only the one original client, and only while `LEGACY_WEBHOOK_FALLBACK=true` |

**Rotating a secret:** paste a new one in Settings → Webhooks — it resets status to
**Untested**, so re-run the test ping. The secret is only ever shown masked (`…last4`).
