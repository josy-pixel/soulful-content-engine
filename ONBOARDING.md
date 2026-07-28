# Onboarding a client — per-client webhooks

This is the exact sequence to connect a new client so their content publishes to
**their own** Facebook Page and Instagram account. It is written for a non-developer.
You do it partly in **Make** and partly in the **app** (Settings → Webhooks).

Nothing you do here can misroute existing clients — each client is isolated by their
own webhook. But **a client is not ready until the test ping passes** (step 8).

---

## Part A — in Make

**1. Clone the template scenario.** Open the template scenario ("Soulful Content
Engine — Auto Publisher"), duplicate it, and rename the copy for the client
(e.g. "Publisher — Holly Hagan").

**2. Create the webhook and copy its URL.** On the cloned scenario, the first module
is a **Custom webhook**. Add/attach a new webhook to it, give it a clear name, and
**copy the URL** (looks like `https://hook.eu1.make.com/xxxxxxxx`). You'll paste it
into the app in Part B. Each client gets a *different* URL — the app rejects a URL
already used by another client.

**3. Connect the client's accounts (manual OAuth — cannot be automated).** Connect
**this client's** Facebook Page and Instagram Business account to Make (Make →
Connections, or when configuring the modules). This is the one step that must be done
by hand per client.

**4. Point the publish modules at this client's connection.** In the Facebook and
Instagram modules of the cloned scenario, select the connection and Page/account you
just connected. Double-check you did not leave them pointing at another client's Page.

**5. Add the secret check (the X-Secret gate) — do NOT skip this.**
The app sends a header **`X-Secret`** on every request. **The header does nothing
until this scenario verifies it.** Build the check like this:

   - Right after the webhook, add a **Router**.
   - **Route 1 — valid secret.** Set this route's filter to:
     `X-Secret header`  **Equal to**  `<the secret you will paste into the app>`.
     (To find the exact header field: press **Run once** on the scenario, send a test
     ping from the app in Part B, and look at the incoming bundle — the header appears
     under `headers`, usually as `x-secret`. Map the filter to that.)
   - **Route 2 — everything else (wrong/missing secret).** Add a **Webhook Response**
     module on this route, **Status `403`**, body `Forbidden`. This is what makes a
     wrong-secret request come back as a rejection.
   - On **Route 1**, add a small inner check on the `test` field of the payload:
       - if `test = true` → a **Webhook Response** module, **Status `200`**, body
         `test ok`, and **stop** (do not publish — this is just a verification ping);
       - otherwise → the Facebook / Instagram publish modules, then a **Webhook
         Response** **Status `200`**.

   > Once any Webhook Response module exists, Make expects one on **every** path — so
   > make sure both the 200 (valid) and 403 (invalid) responses are wired.

**6. Turn the scenario ON.**

---

## Part B — in the app (Settings → Webhooks, admin only)

**7. Save the client's webhook.** Pick the client, paste the **webhook URL** and the
**secret** (the same value you used in the Route 1 filter), tick **Facebook** and/or
**Instagram**, and Save. The status becomes **Untested**.

**8. Press "Send test ping" and confirm it passes.** The app sends **two** pings:
   - one with the **correct** secret → your scenario must **accept** it (200), and
   - one with a **deliberately wrong** secret → your scenario must **reject** it (403).

   Results:
   - **Verified** (green) → both correct — you're done, real content can flow.
   - **Insecure** (red) → the scenario **accepted the wrong secret**. It is *not*
     checking `X-Secret` (step 5 is missing or wrong). Fix the Route filter / 403
     response and re-test. **Do not publish real content while it says Insecure.**
   - **Failing** (red) → the correct-secret ping didn't get a 200 (scenario off, wrong
     URL, or the valid route doesn't respond 200).

**9. Only after "Verified", publish real content** for that client.

---

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| Test ping says **Insecure** | Scenario accepted a wrong secret — X-Secret not enforced | Re-check step 5: the Route-1 filter must compare the header to the secret, and Route-2 must return 403 |
| Test ping / dispatch says **403** | The secret the app sends ≠ the secret in the Route filter | Make them identical; re-save the secret in the app and re-test |
| **timeout (10s)** | Make didn't respond in time | Ensure a **Webhook Response** module returns 200 quickly on the valid route (respond before the slow publish steps if needed) |
| A post is marked **failed** in the app | Dispatch got a non-2xx or no response | Open the client's row in Settings → Webhooks; "Last error" shows the HTTP status. Check the scenario is ON and the URL matches |
| "**No webhook configured for this client**" | The client has no row in Settings → Webhooks | Add one (steps 7–8). During migration, the legacy fallback only covers the one original client and only while `LEGACY_WEBHOOK_FALLBACK=true` |

**A note on the secret:** it is stored masked in the app (you only ever see `…last4`).
If you need to rotate it, paste a new one — that resets the status to Untested, so
re-run the test ping.
