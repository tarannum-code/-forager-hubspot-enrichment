# Forager × HubSpot Enrichment

[![CI](https://github.com/tarannum-code/-forager-hubspot-enrichment/actions/workflows/ci.yml/badge.svg)](https://github.com/tarannum-code/-forager-hubspot-enrichment/actions/workflows/ci.yml)

A Python service that enriches HubSpot **companies** and **contacts** with
[Forager](https://forager.ai) data. It also finds new contacts at a company, filtered by
job title, and runs automatically from **live HubSpot webhooks**.

It is written as plain code, with no no-code tools. It is deployed on Railway and fully
tested: 141 tests run against mocked APIs, so the tests never spend real Forager credits.

**Live service:** https://forager-hubspot-enrichment-production.up.railway.app/health  
**Video walkthrough (7 min):** https://drive.google.com/file/d/1kZjMuisse5vTYHkpymO73CdBzZgBJFBD/view?usp=sharing

| Brief requirement | Where it lives |
|---|---|
| 1. Enrich companies (description, revenue, headcount, …) | `enrich-company`, company webhook |
| 2. Enrich contacts (all required fields) | `enrich-contact`, contact webhook |
| 3. Find contacts at a company, filtered by job title | `find-contacts`, `demo-openai`, company webhook |
| 4. Company webhook: enrich the company, then enrich its contacts or find new ones | `POST /webhooks/hubspot` → `process_company_created` |
| 5. Contact webhook: enrich the new contact | `POST /webhooks/hubspot` → `process_contact_created` |
| Extra details for discussion | [EXTRA_FIELDS.md](EXTRA_FIELDS.md) |

---

## How it works

```
HubSpot ──(company/contact created)──► POST /webhooks/hubspot  (FastAPI on Railway)
                                          │ 1. verify v3 signature (HMAC-SHA256, 5-min window)
                                          │ 2. de-duplicate, enqueue, reply 200 at once
                                          ▼
                                     background worker
                                          │ 3. claim the record (status = in_progress)
                                          │ 4. Forager lookups, within a credit budget,
                                          │    every response cached to disk
                                          │ 5. map + clean → HubSpot properties
                                          ▼
HubSpot  ◄── update record, create + associate contacts, status = enriched / partial / not_found
```

**Company flow.** The webhook fires and the company is enriched by domain, LinkedIn page
or name. Then:

- **If the company already has contacts** in HubSpot (associated, or sharing its email
  domain), those contacts are enriched.
- **If it has none,** the service searches Forager for people matching the job-title
  filter, creates up to `WEBHOOK_MAX_NEW_CONTACTS` of them, associates them with the
  company and enriches them.

**Contact flow.** The webhook fires and the person is identified by the cheapest reliable
method:

1. LinkedIn URL (1 credit)
2. Name plus company domain (1 credit)
3. Reverse email lookup (5 credits)

The service then fills title, location and employer, looks up the work email, and
associates the contact with its employer's HubSpot company if that company exists.

### Design decisions

- **Idempotency and loop prevention.** Every record carries
  `forager_enrichment_status`. Finished records are never re-enriched automatically.
  Contacts this service creates are written *with* a status, so the `contact.creation`
  webhooks they trigger are no-ops. The service never creates companies from the contact
  flow; doing so would fire `company.creation` and start a second paid flow.
- **Duplicates.** Duplicates are blocked at two levels:
  - The in-memory queue ignores an event for the same object while it is queued or
    running, and for an hour after.
  - The HubSpot status marker blocks duplicates across restarts.
  - Before creating a contact, the service looks it up by LinkedIn URL and by email, so
    it updates the existing record instead of creating a duplicate.
- **Credits are money.** Several safeguards protect them:
  - Every paid call reserves its worst-case cost *before* it is sent. Runs have hard
    caps: per CLI run, per webhook event, and per day for all webhooks.
  - Every Forager response, including "not found", is cached on disk. A repeat lookup
    costs 0.
  - Phone numbers (15 credits) are only bought after an email is found.
  - Paid calls are never retried after a read timeout, because the first attempt may
    already have been charged.
  - Every charge is appended to a ledger (`credit_ledger.jsonl`).
- **Clean data.** The service cleans values before writing them:
  - Names are cased properly, and initials like "LS" are kept.
  - Phones are formatted internationally (`phonenumbers`).
  - Forager's location strings are parsed into city, state and country. Contradictory
    geocodes are dropped instead of guessed.
  - LinkedIn URLs are canonicalised.
  - Identity fields a person typed (name, email, phone, company name and domain) are
    never overwritten.
- **Reliability.** Calls are retried with exponential backoff and jitter, and the
  service honours `Retry-After` on 429 and 5xx responses. A client-side rate limiter
  keeps HubSpot calls under 100 per 10 s and search calls under 5 per second. Webhook
  jobs that fail on a transient error are re-queued after 1 and 5 minutes.
- **API versions.** The HubSpot client uses the date-versioned APIs (`2026-09`). The
  Forager client was written from Forager's OpenAPI specification. Endpoints and shapes
  were not guessed.

---

## Quick start (local)

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # then fill in the keys (see below)

python -m app.cli check --save-account-id   # verifies both APIs, shows credit balance (0 credits)
python -m app.cli setup-hubspot             # creates the custom properties (idempotent, 0 credits)
```

### CLI

Paid commands print the worst-case cost first, and refuse to spend unless you pass
`--max-credits` (or set `FORAGER_MAX_CREDITS_PER_RUN`).

| Command | What it does | Credits |
|---|---|---|
| `check` | Verify Forager and HubSpot credentials, show balance | 0 |
| `setup-hubspot` | Create or verify the custom property group and properties | 0 |
| `lookup-company -d openai.com` | Show Forager's data for a company (no HubSpot writes) | 1 |
| `find-contacts -d openai.com -t "VP,Head of" -x "Intern" --show-rejected` | Job-title filtered discovery, with reasons for every rejection | 1 per page |
| `enrich-company --domain openai.com [--create]` | Enrich one HubSpot company | 1 |
| `enrich-contact --linkedin <url>` / `--contact-id <id>` | Enrich one contact (`--no-phone`, `--personal-email`) | 1 + 5 email + 15 phone |
| `demo-openai -n 5 [--dry-run]` | Primary deliverable: the company plus N *complete* contacts (email and phone) | varies; `--dry-run` = 0 |
| `simulate-webhook company <id> [--url …]` | Send a correctly signed v3 webhook, as HubSpot would | 0 |

All paid commands accept `--max-credits N`, `--yes` and `--no-cache`.

### Job-title filtering

Include keywords become a boolean Forager query (`"VP" OR "Head of"`), so credits are
only spent on relevant people. Every result is then re-checked locally with whole-word
matching: `VP` matches "VP of Sales" but not "MVP Lead", and `Head of` does not match
"Header". Exclude keywords (`Intern`, `Former`, …) reject a person. Candidates are
ranked by keyword order, so `Chief` comes before `VP` if listed first. They are also
rejected if the role is not current, belongs to a different organization, or has no
LinkedIn profile.

Defaults come from `CONTACT_TITLES` and `CONTACT_TITLE_EXCLUDES`. The CLI overrides
them with `--titles` and `--exclude`.

---

## Configuration

Everything is read from environment variables (or `.env` locally). No secret has a
default value, and `.env` is git-ignored. See [`.env.example`](.env.example) for the
full list.

| Variable | Purpose |
|---|---|
| `FORAGER_API_KEY`, `FORAGER_ACCOUNT_ID` | Forager credentials (`check --save-account-id` finds the ID) |
| `HUBSPOT_ACCESS_TOKEN` | HubSpot service key or the project app's static token |
| `HUBSPOT_CLIENT_SECRET` | Project app's client secret; verifies webhook signatures |
| `PUBLIC_BASE_URL` | Public URL of the service; used to rebuild the exact signed URL behind Railway's proxy |
| `ADMIN_TOKEN` | Bearer token for `GET /jobs` (disabled in production without it) |
| `CONTACT_TITLES`, `CONTACT_TITLE_EXCLUDES` | Job-title filter (comma-separated) |
| `WEBHOOK_MAX_CREDITS_PER_EVENT` / `_PER_DAY` | Credit caps for webhook processing (25 and 50 by default) |
| `WEBHOOK_MAX_NEW_CONTACTS` | Contacts the company flow may create (2) |
| `WEBHOOK_LOOKUP_PHONE` | Buy phones in webhook flows (off: 15 credits each) |
| `FORAGER_MAX_CREDITS_PER_RUN` | Default cap for CLI runs (0 = must pass `--max-credits`) |

---

## Deployment

### Railway

1. **New Project → Deploy from GitHub repo.** `railway.json` builds the `Dockerfile`:
   - Python 3.12 slim image
   - runs as a non-root user
   - `uvicorn` on `$PORT`
   - health check at `/health`
2. Add the variables above with `ENVIRONMENT=production`.
3. **Networking → Generate Domain**, then set `PUBLIC_BASE_URL` to it.
4. Add a **volume at `/data`**, so the Forager cache and credit ledger survive redeploys.
   Set `RAILWAY_RUN_UID=0` so the volume is writable.

Endpoints:
- `GET /health`: configuration, worker status, storage, and credits spent today. Makes
  no external calls.
- `GET /jobs`: recent webhook jobs (IDs, status and credits only). Requires
  `ADMIN_TOKEN`.
- `POST /webhooks/hubspot`: the webhook receiver.

### HubSpot app and webhooks

`hubspot-project/` is a HubSpot developer-platform project (platform `2026.03`): a
private, static-auth app with `company.creation` and `contact.creation` subscriptions.

```bash
npm install -g @hubspot/cli
hs account auth                     # personal access key of the target portal
# set targetUrl in hubspot-project/src/app/webhooks/webhooks-hsmeta.json
cd hubspot-project && hs project upload
```

Then, in HubSpot, open **Development → Projects → forager-enrichment → app**:
1. Click **Install**.
2. Copy the **client secret** into `HUBSPOT_CLIENT_SECRET`.
3. Optionally, use the app's static access token as `HUBSPOT_ACCESS_TOKEN`.

**Recommended HubSpot setting:** turn **off** "Automatically create and associate
companies with contacts". Otherwise every new contact also creates a company, which
fires a second, paid webhook.

---

## Testing

```bash
pytest                 # 141 tests, all HTTP mocked with respx: real Forager is never called
ruff check . && ruff format --check .
mypy app               # strict
```

GitHub Actions runs all of the above on Python 3.11 and 3.12, and builds the Docker
image, on every push.

The tests cover:
- signature verification (including replay)
- de-duplication, loop prevention and stale-claim recovery
- credit reservation and caps
- cache behaviour
- job-title matching
- location parsing on anonymised real Forager data
- the full company and contact flows against an in-memory HubSpot

---

## Project layout

```
app/
  config.py            settings from env (pydantic-settings)
  http_utils.py        error types, retry policy (Retry-After, backoff, idempotency rules)
  normalize.py         names, phones, domains, LinkedIn URLs, location parsing
  forager/             API client, disk cache, credit budget + ledger, response models
  hubspot/             CRM client (rate-limited), custom property definitions
  enrichment/          service (flows, markers), mapping, matching, candidates, title filter
  webhooks/            v3 signature, event parsing, background worker
  main.py              FastAPI app
  cli.py               Typer CLI
hubspot-project/       HubSpot app + webhook subscriptions
tests/                 pytest suite + fixtures
```

## Known limitations

- **Forager's free tier now gives 50 credits, not 150.** A complete contact costs about
  20 credits (phone is 15), so the demo budget is tight. The cache makes every lookup a
  one-time cost.
- **A complete contact costs about 20 credits, but email and phone misses are free.**
  This was verified on live runs: a phone lookup is only charged when numbers come back,
  and a work-email lookup only when an address is found.
- **Some people have no email or phone in Forager.** Required fields still missing (such
  as city or state) mark the contact `partial`, and `forager_last_error` names them.
- **Forager's domain search can miss the canonical company.** A popular domain can
  return many organizations: GPT pages for openai.com, or acquired companies such as
  "Octane (We've joined Stripe!)", which Forager has renamed "Stripe" for stripe.com.
  The service cross-checks Forager's free autocomplete (name + domain) and keeps the
  best-scoring organization. In live testing, though, the canonical record didn't
  always come back from the paid search. I'm also verifying whether Forager's `page`
  parameter is 0-based: two domains returned `total_search_results: 10` with an empty
  first page. If it is, the first page of every search was being skipped. That is the
  next thing to confirm once more credits are available.
- **Single replica.** The in-memory queue is fine for one replica. Scaling out would
  need a shared queue (e.g. Redis), but the HubSpot markers already keep processing
  idempotent across replicas.
