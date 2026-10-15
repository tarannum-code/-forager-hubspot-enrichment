# Demo video script

A 7–8 minute walkthrough that covers everything the brief asks the video to show:
**enrichment running, a webhook firing, records filling in HubSpot, and job-title
filtering**, plus the deployed service and the source code.

Each scene lists what to show, the commands to run, and what to say. Narration is a
guide, not a script to read word for word.

---

## Before you record (checklist)

Do these the day of the recording, *not* on camera.

- [ ] Railway service is deployed. `https://<railway-domain>/health` returns `"status": "ok"`
      and `"signature_verification": true`.
- [ ] Railway env vars point to the **final** Forager and HubSpot accounts.
- [ ] The final HubSpot account has the OpenAI company and 5 complete contacts
      (`demo-openai --people 5`, Phase 8).
- [ ] HubSpot → Settings → Objects → Companies: **"Create and associate companies with
      contacts" is OFF**. Otherwise adding a contact also creates a company, which fires a
      second webhook and spends extra credits.
- [ ] GitHub repo: the latest CI run is green.
- [ ] Forager balance is checked: `python -m app.cli check`. The live webhook scenes
      cost about **20 credits** (see the table at the end).
- [ ] You picked the company and person for the live webhook scenes (Scenes 6 and 7)
      and they are **not** in HubSpot yet.
- [ ] Browser tabs, in this order:
      1. GitHub repo
      2. Railway → service → Deployments → logs
      3. `https://<railway-domain>/health`
      4. HubSpot → Companies → OpenAI
      5. HubSpot → Contacts, filtered to the OpenAI company
- [ ] Terminal open in the project folder with a large font (Ctrl + scroll). `.env`
      points to the final accounts, and the screen does not show the `.env` file.
- [ ] Close Slack, email and notifications (Windows: Focus assist / Do not disturb).

**Recording tools:** Windows Snipping Tool (Win + Shift + S → Record), Clipchamp, or OBS.
Record at 1080p and keep the mic close.

---

## Scene 1: Intro (≈ 30 s)

**Show:** GitHub repo front page.

> "Hi, I'm Tarannum. This is my solution to the Forager × HubSpot enrichment task. It's a
> Python service, with no no-code tools, deployed on Railway. It enriches companies and
> contacts, finds new contacts filtered by job title, and runs automatically from HubSpot
> webhooks. I'll show the finished OpenAI records first, then the automation running
> live."

---

## Scene 2: Code and quality (≈ 45 s)

**Show:** the repo tree (`app/forager`, `app/hubspot`, `app/enrichment`, `app/webhooks`),
then the **Actions** tab with a green run.

> "The code is split by responsibility. There's a Forager client built from Forager's
> OpenAPI spec, a HubSpot client on the date-versioned APIs, the enrichment logic, and
> the webhook server. Every push runs CI: lint, strict type checking, 137 tests on two
> Python versions, and a Docker build. The tests mock every external API, so they never
> spend Forager credits. All secrets come from environment variables. Nothing is
> hard-coded."

---

## Scene 3: Primary deliverable, OpenAI in HubSpot (≈ 1 min 15 s)

**Show:** HubSpot → OpenAI company. Scroll through the standard fields (description,
employees, annual revenue, industry, city/state/country, LinkedIn page, founded year),
then the **Forager Enrichment** property group.

> "Here's the OpenAI company. Description, headcount, revenue, industry, HQ address and
> LinkedIn page come from Forager and are mapped to HubSpot's standard properties.
> Anything without a standard property, like legal name, keywords and Forager's ID, goes
> into a custom property group. The setup script creates that group."

**Show:** the Contacts associated with OpenAI (5 rows). Open one contact.

> "Five people are associated with the company. Each has first and last name, LinkedIn
> URL, job title, employer name, domain and LinkedIn, email, phone, and city, state and
> country. The status field says `enriched`, with a timestamp. That status is also what
> stops the same record from being enriched, and paid for, twice."

---

## Scene 4: Job-title filtering (≈ 1 min)

**Show:** terminal.

```powershell
.venv\Scripts\python -m app.cli find-contacts -d openai.com --show-rejected
```

> "Contact discovery is driven by a job-title filter. The include keywords go to Forager
> as a boolean query, so we only pay for relevant people. Every result is then checked
> again locally with whole-word matching, so 'VP' matches 'VP of Sales' but not 'MVP'.
> The rejected list shows why each person was dropped: title filter, a different
> company, or a past role."

Then show it is configurable (this is a new search: **1 credit**):

```powershell
.venv\Scripts\python -m app.cli find-contacts -d openai.com --titles "Engineering Manager,Head of Engineering" --exclude "Intern" --max-credits 1
```

> "Different titles mean a different set of people. In production the same filter comes
> from the `CONTACT_TITLES` environment variable, so the business can change who gets
> surfaced without touching code."

---

## Scene 5: CLI enrichment and cost safety (≈ 45 s)

```powershell
.venv\Scripts\python -m app.cli demo-openai -n 5 --dry-run
.venv\Scripts\python -m app.cli enrich-company --domain openai.com
```

> "Forager credits cost money, so the tool is careful with them. A dry run shows what is
> already cached and what would be bought, before spending anything. Every Forager
> response is cached to disk, so nothing is paid for twice. Phone numbers, the most
> expensive lookup, are only bought after an email is found. Re-running enrichment on
> OpenAI is skipped because it's already enriched, so it costs zero credits."

---

## Scene 6: Live company webhook (≈ 1 min 30 s), the key scene

**Show:** split screen if possible, with HubSpot on the left and Railway logs on the
right.

1. HubSpot → Companies → **Create company**. Enter only the **domain** of the company you
   picked (and the name), then save.
2. Switch to Railway logs. Within a few seconds: `Webhook received` → `Webhook event
   queued` → `Company enriched` → contacts created.
3. Back in HubSpot, refresh the company. The fields are filled, and new contacts are
   associated.
4. Optional: show the job list.

```powershell
curl.exe -H "Authorization: Bearer <ADMIN_TOKEN>" https://<railway-domain>/jobs
```

> "I'm creating a new company in HubSpot with just its domain. HubSpot fires a webhook
> to the Railway service. The service checks HubSpot's v3 signature, replies 200
> immediately, and does the work in a background worker, so HubSpot never times out.
> It enriched the company and, because it had no contacts, found people matching the
> title filter and created them, associated with the company. Contacts the service
> creates are tagged with an enrichment status first, so their own creation webhooks
> are skipped. That prevents loops and double charges. Each event also has a credit
> cap, and there's a daily cap."

---

## Scene 7: Live contact webhook (≈ 1 min)

1. HubSpot → Contacts → **Create contact**. Enter first name, last name, and the person's
   **LinkedIn URL**, then save.
2. Railway logs: `Contact enriched`.
3. Refresh the contact: title, location, employer and work email are filled, and the
   contact is associated with its employer company if that company exists.

> "Same for contacts. When a contact is created, the webhook enriches it. The service
> identifies the person using the cheapest reliable method first: the LinkedIn URL,
> then name plus company domain, then reverse email lookup. Fields a person typed in,
> like name or email, are never overwritten."

---

## Scene 8: Security and reliability (≈ 30 s)

```powershell
curl.exe -i -X POST https://<railway-domain>/webhooks/hubspot -H "Content-Type: application/json" -d "[]"
```

> "A request without a valid HubSpot signature is rejected with 401. Duplicate
> deliveries of the same event are ignored. Temporary failures, like a 503 from either
> API, are retried with backoff. Read timeouts on paid calls are deliberately not
> retried, because the call might already have been charged."

---

## Scene 9: Extra fields and wrap-up (≈ 30 s)

**Show:** `EXTRA_FIELDS.md` on GitHub.

> "Forager returns more than the brief asked for: LinkedIn headline, About section,
> skills, role start date, and, with one more lookup, education and languages. I've
> stored these in a separate property group, and listed them in EXTRA_FIELDS.md as
> proposed for discussion, with what each one costs and any data-quality caveats.
> Thanks for watching. The repo, the live URL and the HubSpot records are all ready to
> review."

---

## Credits needed for the video (final account)

| Scene | Calls | Credits |
|---|---|---|
| 3, 5 | everything cached | 0 |
| 4 | one new title search | 1 |
| 6 | org search 1 + people search 1 + 2 work emails (5 each, only if found); phone is off for webhooks | ≈ 12 |
| 7 | LinkedIn lookup 1 + work email 5 | ≈ 6 |
| **Total** | | **≈ 19** |

Email misses are free, so the real cost is often lower. Keep about 20 credits spare
after the 5-person demo.

## If something goes wrong on camera

- **Webhook doesn't arrive:** check Railway logs for `Webhook rejected`. A wrong
  `HUBSPOT_CLIENT_SECRET` or `PUBLIC_BASE_URL` gives `signature mismatch`. Pause the
  recording, fix it, and re-record the scene.
- **Job shows `skipped`:** the record already had an enrichment status. Use a company or
  contact that isn't in HubSpot yet.
- **Job shows `budget_exceeded`:** the per-event or daily credit cap was hit. Check
  `/health` → `credits_spent_today`.
- Record scene by scene and join the clips in Clipchamp. That's easier than one perfect
  take.
