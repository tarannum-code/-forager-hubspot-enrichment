# Extra fields Forager surfaces: proposed for discussion

The brief asks to flag "interesting extra details" so we can decide together whether to
keep them. Everything below is **proposed for discussion**. Nothing here is required by
the brief.

**How extras are stored now.** Extras the service already writes go into a separate
HubSpot property group, **Forager Enrichment**. They never overwrite standard
properties, and the whole group can be hidden or deleted without touching the required
fields.

**Coverage numbers** come from the real OpenAI search results used for the demo (top 10
people matching the title filter, October 2026). They show how often each field is
actually filled.

---

## Contacts

### Already captured (free: part of the search result we pay for anyway)

| HubSpot property | Forager field | Coverage | Why it may be useful |
|---|---|---|---|
| `forager_headline` | `person.headline` | 10/10 | One-line positioning, e.g. "Head of Platform @ OpenAI". Good for personalising outreach. |
| `forager_about` | `person.description` (LinkedIn "About") | 6/10 | The person's own summary. Strongest signal for personalisation. This is the example given in the brief. |
| `forager_skills` | `person.skills` (first 25) | 7/10, about 21 per person | Lets you segment by expertise, e.g. "Kubernetes" or "Go-to-market". |
| `forager_role_start_date` | `role.start_date` | 10/10 | Time in role. New leaders (under 6 months) are a classic buying trigger. |
| `forager_email_status` | work-email `validation_status` | with every email | `valid` or `risky`. Tells sales how safe the address is to send to. |
| `forager_person_id` | `person.id` | 10/10 | A stable ID for cheap re-lookups and de-duplication. |

### Captured only when the person-detail lookup runs (+1 credit per person)

The detail lookup runs when a contact is identified by its LinkedIn URL, for example
through the contact webhook.

| HubSpot property | Forager field | Notes |
|---|---|---|
| `forager_education` | `educations[]` (degree, field, school, years) | Useful for alumni-based outreach. |
| `forager_languages` | `languages[]` (name, proficiency) | Useful for routing to regional teams. |

**Question for you:** should the company flow also buy the detail lookup for every
contact it creates, to always have education and languages? That would add 1 credit per
person.

### Available but not stored (yet)

| Forager field | Coverage | Why not stored, and the proposal |
|---|---|---|
| `role.description` (description of the current role) | 7/10 | Often a short list of responsibilities. Could go into a `forager_role_description` property, free. **Proposed: add.** |
| `person.photo` | 9/10 | Profile-photo URLs from data providers are usually signed and expire, so a stored link would break. If wanted, download the image and upload it to HubSpot files. |
| `gender` | 0/10 | Never populated for these people, and storing inferred gender is sensitive under GDPR. **Proposed: don't store.** |
| `certifications`, `courses`, `honors`, `patents`, `publications`, `projects`, `volunteering` (detail lookup) | varies | Niche. Could go into one JSON or text property if a use case appears, e.g. "has patents" for R&D outreach. |
| Personal email | n/a | Supported (`--personal-email`), but **off by default**: B2B outreach should use work email, and personal email raises consent and privacy issues. |
| Phone in webhook flows | n/a | Supported, but **off for webhooks** (`WEBHOOK_LOOKUP_PHONE=false`), because a phone lookup costs 15 credits. It is used in the curated OpenAI demo. |

---

## Companies

### Already captured

| HubSpot property | Forager field | Notes |
|---|---|---|
| `forager_legal_name` | `legal_name` | Useful for contracts and invoicing. |
| `forager_linkedin_industry` | `linkedin_info.industry` | The original LinkedIn label. HubSpot's `industry` dropdown is filled too when a matching option exists. |
| `forager_keywords` | `keywords[]` | Rich tags (e.g. "Artificial Intelligence, Research"), good for segmentation and lists. |
| `forager_founded_date` | `founded_date` | The full date. HubSpot's standard `founded_year` only takes the year. |
| `forager_operating_status` | `operating_status` | Flags companies that are closed or acquired before sales spends time on them. |
| `forager_domain_rank` | `domain_rank` | Web-traffic rank. A rough proxy for company size and reach. |
| `forager_hq_address` | `addresses[].summary` | The full HQ address as one line. Street, city, ZIP and country also go to standard fields. |
| `forager_organization_id` | `id` | A stable ID for re-lookups and de-duplication. |

### Available but deliberately not written

| Forager field | Why |
|---|---|
| `logo` | The URL is signed and expires, so HubSpot would show a broken image later. HubSpot can fill company logos from the domain itself. |
| `employees_range` | Inconsistent with `employees_amount`: for OpenAI the range was "1001 - 5000" while the count was 10,480. The count is written to `numberofemployees`, and the range is skipped to avoid contradicting data. |

---

## Decisions we need from you

1. Keep the free contact extras (headline, About, skills, role start date) visible to
   sales, or hide the property group?
2. Add `role.description` as a new property (free)?
3. Pay +1 credit per contact for education and languages in the automated flows?
4. Turn on phone lookups for webhook-created contacts (+15 credits each), or keep phone
   only for curated lists?
5. Photos: skip, or download and host them in HubSpot?
