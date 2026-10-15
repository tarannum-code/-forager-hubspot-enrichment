"""Enrichment use-cases shared by the CLI and the webhook worker.

Idempotency and loop prevention rely on the `forager_enrichment_status` marker stored on
every HubSpot record:

* a record with status enriched / partial / not_found is never enriched again
  automatically (only with force=True from the CLI);
* before spending credits the record is *claimed* (status in_progress + timestamp), so a
  duplicate webhook delivered meanwhile is skipped; claims older than
  ENRICHMENT_STALE_AFTER_MINUTES are treated as crashed runs and may be retried;
* contacts this service creates are written *with* a status already set, so the
  contact.creation webhooks they trigger are skipped instead of paying twice.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Settings
from app.enrichment.candidates import Candidate, rank_candidates
from app.enrichment.mapping import (
    ContactData,
    Properties,
    build_industry_lookup,
    company_properties,
    contact_properties,
    missing_required,
)
from app.enrichment.matching import best_organization
from app.enrichment.title_filter import TitleFilter
from app.forager.client import ForagerClient
from app.forager.credits import CreditBudgetExceeded
from app.forager.models import Organization, PersonDetail, PersonRoleSearchResult
from app.hubspot.client import HubSpotClient
from app.hubspot.properties import (
    SOURCE_CLI,
    SOURCE_COMPANY_FLOW,
    STATUS_ENRICHED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_NOT_FOUND,
    STATUS_PARTIAL,
)
from app.normalize import (
    FREE_EMAIL_DOMAINS,
    email_domain,
    linkedin_person_url,
    linkedin_slug,
    normalize_domain,
    normalize_email,
    normalize_phone,
)

logger = logging.getLogger(__name__)

STATUS_SKIPPED = "skipped"
_DONE_STATUSES = {STATUS_ENRICHED, STATUS_PARTIAL, STATUS_NOT_FOUND}
_EMAIL_PREFERENCE = {"valid": 0, "unknown": 1, "risky": 2}  # "invalid" is never used

COMPANY_READ = [
    "name",
    "domain",
    "website",
    "linkedin_company_page",
    "forager_enrichment_status",
    "forager_enrichment_started_at",
]
CONTACT_READ = [
    "firstname",
    "lastname",
    "email",
    "phone",
    "jobtitle",
    "company",
    "hs_linkedin_url",
    "forager_enrichment_status",
    "forager_enrichment_started_at",
]


def utc_now_iso() -> str:
    """ISO-8601 UTC with milliseconds, the format HubSpot datetime properties accept."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_hubspot_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, UTC)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class ContactLookupOptions:
    """Which (paid) contact-data lookups to run for a person."""

    work_email: bool = True
    personal_email: bool = False
    phone: bool = True
    # Only pay 15 credits for a phone once an email was found (the demo needs both).
    phone_requires_email: bool = False


@dataclass
class EnrichmentResult:
    object_type: str
    object_id: str
    status: str
    message: str = ""
    properties: Properties = field(default_factory=dict)
    organization: Organization | None = None


@dataclass
class CompanyFlowResult:
    company: EnrichmentResult
    contacts: list[EnrichmentResult] = field(default_factory=list)
    created_contact_ids: list[str] = field(default_factory=list)
    rejected_candidates: list[Candidate] = field(default_factory=list)


@dataclass
class SelectionOutcome:
    candidate: Candidate
    data: ContactData | None
    selected: bool
    reason: str = ""


class EnrichmentService:
    def __init__(self, forager: ForagerClient, hubspot: HubSpotClient, settings: Settings) -> None:
        self.forager = forager
        self.hubspot = hubspot
        self.settings = settings
        self._industry_lookup: dict[str, str] | None = None

    # ================================================================== markers

    def skip_reason(
        self, props: Properties, *, force: bool = False, resume: bool = False
    ) -> str | None:
        """Why a record must not be enriched now, or None. `resume` is a retry of our own
        interrupted job, so its in_progress claim doesn't block it (finished records still do)."""
        if force:
            return None
        status = props.get("forager_enrichment_status")
        if status in _DONE_STATUSES:
            return f"already {status}"
        if status == STATUS_IN_PROGRESS and not resume:
            started = _parse_hubspot_datetime(props.get("forager_enrichment_started_at"))
            stale_after = timedelta(minutes=self.settings.enrichment_stale_after_minutes)
            if started is None or datetime.now(UTC) - started < stale_after:
                return "enrichment already in progress"
        return None

    async def _claim(self, object_type: str, object_id: str) -> None:
        await self.hubspot.update_object(
            object_type,
            object_id,
            {
                "forager_enrichment_status": STATUS_IN_PROGRESS,
                "forager_enrichment_started_at": utc_now_iso(),
                "forager_last_error": "",
            },
        )

    async def _finish(
        self,
        object_type: str,
        object_id: str,
        props: Properties,
        status: str,
        note: str = "",
    ) -> None:
        final = {
            **props,
            "forager_enrichment_status": status,
            "forager_last_error": note[:1000],
        }
        if status in (STATUS_ENRICHED, STATUS_PARTIAL):
            final["forager_enriched_at"] = utc_now_iso()
        await self.hubspot.update_object(object_type, object_id, final)

    async def _mark_failed(self, object_type: str, object_id: str, exc: BaseException) -> None:
        """Record a failure on the HubSpot record without masking the original error
        (if HubSpot itself is down, this write fails too)."""
        try:
            await self._finish(object_type, object_id, {}, STATUS_FAILED, _error_note(exc))
        except Exception:
            logger.exception(
                "Could not mark record as failed",
                extra={"object_type": object_type, "object_id": object_id},
            )

    # ================================================================== companies

    async def industry_lookup(self) -> dict[str, str]:
        if self._industry_lookup is None:
            prop = await self.hubspot.get_property("companies", "industry")
            self._industry_lookup = build_industry_lookup((prop or {}).get("options") or [])
        return self._industry_lookup

    async def resolve_organization(
        self,
        *,
        domain: str | None = None,
        linkedin_url: str | None = None,
        name: str | None = None,
    ) -> Organization | None:
        """Find the Forager organization for a company (1 credit unless cached)."""
        domain = normalize_domain(domain)
        slug = linkedin_slug(linkedin_url, "company") if linkedin_url else None
        if domain:
            result = await self.forager.search_organizations(domains=[domain])
            return best_organization(
                result.search_results, domain=domain, linkedin_url=linkedin_url
            )
        if slug:
            result = await self.forager.search_organizations(linkedin_public_identifiers=[slug])
            return best_organization(result.search_results, linkedin_url=linkedin_url)
        if name:
            # Free autocomplete turns a bare name into an id, then 1 paid search for details.
            items = await self.forager.autocomplete_organizations(name)
            exact = [i for i in items if i.name.casefold() == name.strip().casefold()]
            pool = exact or items
            if not pool or not pool[0].id.isdigit():
                return None
            item = pool[0]
            result = await self.forager.search_organizations(organization_ids=[int(item.id)])
            return best_organization(result.search_results, domain=item.domain)
        return None

    async def enrich_company(
        self, company_id: str, *, force: bool = False, claimed: bool = False, resume: bool = False
    ) -> EnrichmentResult:
        record = await self.hubspot.get_object("companies", company_id, COMPANY_READ)
        if record is None:
            raise LookupError(f"HubSpot company {company_id} not found")
        props: Properties = record.get("properties") or {}
        if not claimed:
            reason = self.skip_reason(props, force=force, resume=resume)
            if reason:
                logger.info("Skipping company", extra={"company_id": company_id, "reason": reason})
                return EnrichmentResult("company", company_id, STATUS_SKIPPED, reason)
            await self._claim("companies", company_id)

        try:
            org = await self.resolve_organization(
                domain=props.get("domain") or props.get("website"),
                linkedin_url=props.get("linkedin_company_page"),
                name=props.get("name"),
            )
            if org is None:
                await self._finish("companies", company_id, {}, STATUS_NOT_FOUND)
                return EnrichmentResult("company", company_id, STATUS_NOT_FOUND, "no Forager match")
            mapped = company_properties(
                org, industry_lookup=await self.industry_lookup(), existing=props
            )
            await self._finish("companies", company_id, mapped, STATUS_ENRICHED)
            logger.info(
                "Company enriched",
                extra={"company_id": company_id, "forager_org": org.id, "fields": len(mapped)},
            )
            return EnrichmentResult("company", company_id, STATUS_ENRICHED, "", mapped, org)
        except Exception as exc:
            await self._mark_failed("companies", company_id, exc)
            raise

    # ================================================================== people

    async def find_candidates(
        self, org: Organization, title_filter: TitleFilter, *, pages: int = 1
    ) -> tuple[list[Candidate], list[Candidate]]:
        """Title-filtered people at an organization, best profiles first (1 credit/page)."""
        rows: list[PersonRoleSearchResult] = []
        for page in range(1, pages + 1):
            query = self.forager.person_role_query(
                role_title=title_filter.to_forager_query(),
                organization_ids=[org.id],
                page=page,
            )
            result = await self.forager.search_person_roles(query)
            rows.extend(result.search_results)
            if len(result.search_results) == 0:
                break
        return rank_candidates(rows, org, title_filter)

    @staticmethod
    def contact_data_from_detail(detail: PersonDetail) -> ContactData:
        role = detail.current_role()
        return ContactData(
            person=detail,
            role_title=role.role_title if role else None,
            role_start_date=role.start_date if role else None,
            employer=role.organization if role else None,
            employer_name=role.organization_name if role else None,
        )

    @staticmethod
    def contact_data_from_candidate(candidate: Candidate, org: Organization) -> ContactData:
        row = candidate.row
        return ContactData(
            person=row.person,
            role_title=row.role_title,
            role_start_date=row.start_date,
            employer=row.organization or org,
            employer_name=(row.organization or org).name,
        )

    async def _company_domain_for_contact(self, contact_id: str) -> str | None:
        for company_id in await self.hubspot.associated_ids("contacts", contact_id, "companies"):
            company = await self.hubspot.get_object("companies", company_id, ["domain"])
            domain = normalize_domain(((company or {}).get("properties") or {}).get("domain"))
            if domain:
                return domain
        return None

    async def resolve_person(self, contact_id: str, props: Properties) -> ContactData | None:
        """Identify a HubSpot contact in Forager, cheapest reliable method first."""
        slug = linkedin_slug(props.get("hs_linkedin_url"))
        if slug:  # 1 credit, exact
            detail = await self.forager.person_detail(linkedin_slug=slug)
            return self.contact_data_from_detail(detail) if detail else None

        email = normalize_email(props.get("email"))
        domain = email_domain(email)
        if domain in FREE_EMAIL_DOMAINS:
            domain = None
        domain = domain or await self._company_domain_for_contact(contact_id)
        first, last = props.get("firstname"), props.get("lastname")
        if first and last and domain:  # 1 credit
            query = self.forager.person_role_query(
                person_name=f"{first} {last}", organization_domains=[domain]
            )
            result = await self.forager.search_person_roles(query)
            wanted = {first.casefold(), last.casefold()}
            for row in result.search_results:
                full = (row.person.full_name or "").casefold()
                if row.is_current and all(part in full for part in wanted):
                    return ContactData(
                        person=row.person,
                        role_title=row.role_title,
                        role_start_date=row.start_date,
                        employer=row.organization,
                        employer_name=row.organization.name if row.organization else None,
                    )

        if email and self.settings.contact_reverse_email_lookup:  # 5 credits
            detail = await self.forager.person_by_email(email)
            return self.contact_data_from_detail(detail) if detail else None
        return None

    async def lookup_contact_info(
        self, data: ContactData, options: ContactLookupOptions, existing: Properties | None = None
    ) -> None:
        """Fill email / phone on `data` using the paid contact lookups the options allow."""
        existing = existing or {}
        slug = data.linkedin_slug
        person_id = data.person.id

        if options.work_email and not (existing.get("email") or data.email):
            job = None
            if slug:
                job = await self.forager.work_email(linkedin_slug=slug)
            else:
                first, last = data.names()
                domain = normalize_domain(data.employer.domain) if data.employer else None
                if first and last and domain:
                    job = await self.forager.work_email(
                        first_name=first, last_name=last, domain=domain
                    )
            usable = [
                a
                for a in (job.addresses() if job else [])
                if (a.validation_status or "unknown") in _EMAIL_PREFERENCE
            ]
            if usable:
                best = min(
                    usable, key=lambda a: _EMAIL_PREFERENCE[a.validation_status or "unknown"]
                )
                data.work_email = normalize_email(best.email)
                data.email_status = best.validation_status

        if options.personal_email and not (existing.get("email") or data.email):
            emails = await self.forager.personal_emails(person_id=person_id, linkedin_slug=slug)
            if emails:
                data.personal_email = normalize_email(emails[0].email)

        has_email = bool(existing.get("email") or data.email)
        if options.phone and not existing.get("phone"):
            if options.phone_requires_email and not has_email:
                return
            phones = await self.forager.phone_numbers(person_id=person_id, linkedin_slug=slug)
            country = data.location.country
            for phone in phones:
                formatted = normalize_phone(phone.phone_number, country)
                if formatted:
                    data.phone = formatted
                    break

    async def enrich_contact(
        self,
        contact_id: str,
        options: ContactLookupOptions,
        *,
        force: bool = False,
        claimed: bool = False,
        resume: bool = False,
        prefetched: ContactData | None = None,
    ) -> EnrichmentResult:
        record = await self.hubspot.get_object("contacts", contact_id, CONTACT_READ)
        if record is None:
            raise LookupError(f"HubSpot contact {contact_id} not found")
        props: Properties = record.get("properties") or {}
        if not claimed:
            reason = self.skip_reason(props, force=force, resume=resume)
            if reason:
                logger.info("Skipping contact", extra={"contact_id": contact_id, "reason": reason})
                return EnrichmentResult("contact", contact_id, STATUS_SKIPPED, reason)
            await self._claim("contacts", contact_id)

        try:
            data = prefetched or await self.resolve_person(contact_id, props)
            if data is None:
                await self._finish("contacts", contact_id, {}, STATUS_NOT_FOUND)
                return EnrichmentResult("contact", contact_id, STATUS_NOT_FOUND, "no Forager match")
            await self.lookup_contact_info(data, options, existing=props)
            mapped = contact_properties(data, existing=props)
            missing = missing_required({**props, **mapped})
            status = STATUS_PARTIAL if missing else STATUS_ENRICHED
            note = f"Not available in Forager: {', '.join(missing)}" if missing else ""
            await self._finish("contacts", contact_id, mapped, status, note)
            await self.associate_with_employer(contact_id, data)
            logger.info(
                "Contact enriched",
                extra={"contact_id": contact_id, "status": status, "fields": len(mapped)},
            )
            return EnrichmentResult("contact", contact_id, status, note, mapped)
        except Exception as exc:
            await self._mark_failed("contacts", contact_id, exc)
            raise

    async def associate_with_employer(
        self, contact_id: str, data: ContactData, company_id: str | None = None
    ) -> str | None:
        """Link the contact to its employer's HubSpot company (never creates companies:
        that would fire company.creation and start another paid enrichment)."""
        if company_id is None:
            if await self.hubspot.associated_ids("contacts", contact_id, "companies"):
                return None
            domain = normalize_domain(data.employer.domain) if data.employer else None
            if not domain:
                return None
            company = await self.hubspot.find_one("companies", "domain", domain, ["name"])
            if company is None:
                return None
            company_id = str(company["id"])
        await self.hubspot.associate_default("contacts", contact_id, "companies", company_id)
        return company_id

    async def find_contact_by_linkedin(self, slug: str | None) -> dict[str, Any] | None:
        url = linkedin_person_url(slug)
        if not url:
            return None
        return await self.hubspot.find_one("contacts", "hs_linkedin_url", url, CONTACT_READ)

    async def find_existing_contact(self, data: ContactData) -> dict[str, Any] | None:
        """Match by LinkedIn URL, then by email (HubSpot rejects a second contact with the
        same email with 409, and a person may have been added by hand without LinkedIn)."""
        found = await self.find_contact_by_linkedin(data.linkedin_slug)
        email = normalize_email(data.email)
        if found is None and email:
            found = await self.hubspot.find_one("contacts", "email", email, CONTACT_READ)
        return found

    async def upsert_contact(
        self,
        data: ContactData,
        *,
        company_id: str | None,
        source: str,
        status: str = STATUS_ENRICHED,
    ) -> EnrichmentResult:
        """Create (or update) a HubSpot contact from fully gathered data, already marked
        with an enrichment status so its contact.creation webhook is a no-op."""
        existing = await self.find_existing_contact(data)
        existing_props: Properties = (existing or {}).get("properties") or {}
        mapped = contact_properties(data, existing=existing_props)
        missing = missing_required({**existing_props, **mapped})
        status = STATUS_PARTIAL if missing and status == STATUS_ENRICHED else status
        final = {
            **mapped,
            "forager_enrichment_status": status,
            "forager_enriched_at": utc_now_iso(),
            "forager_last_error": f"Not available in Forager: {', '.join(missing)}"
            if missing
            else "",
        }
        if existing:
            contact_id = str(existing["id"])
            await self.hubspot.update_object("contacts", contact_id, final)
        else:
            final["forager_source"] = source
            created = await self.hubspot.create_object("contacts", final)
            contact_id = str(created["id"])
        await self.associate_with_employer(contact_id, data, company_id)
        return EnrichmentResult("contact", contact_id, status, final["forager_last_error"], final)

    # ================================================================== webhook flows

    async def process_contact_created(
        self, contact_id: str, options: ContactLookupOptions, *, resume: bool = False
    ) -> EnrichmentResult:
        return await self.enrich_contact(contact_id, options, resume=resume)

    async def process_company_created(
        self,
        company_id: str,
        *,
        title_filter: TitleFilter,
        options: ContactLookupOptions,
        max_new_contacts: int,
        resume: bool = False,
    ) -> CompanyFlowResult:
        # On a retry the company may already be enriched while contact discovery failed;
        # re-running it is free (the Forager organization is in the disk cache).
        company = await self.enrich_company(company_id, force=resume)
        flow = CompanyFlowResult(company=company)
        if company.status == STATUS_SKIPPED:
            return flow  # already handled (e.g. created by this service): no loop

        contact_ids = await self._contacts_of_company(company_id, company.properties)
        if contact_ids:
            for contact_id in contact_ids:
                flow.contacts.append(await self._safe_enrich(contact_id, options, resume=resume))
            return flow

        org = company.organization
        if org is None or max_new_contacts <= 0:
            return flow
        accepted, rejected = await self.find_candidates(org, title_filter)
        flow.rejected_candidates = rejected
        for candidate in accepted:
            if len(flow.created_contact_ids) >= max_new_contacts:
                break
            if await self.find_contact_by_linkedin(candidate.linkedin_slug):
                continue  # already in HubSpot under another company: don't duplicate
            data = self.contact_data_from_candidate(candidate, org)
            seed = contact_properties(data)
            seed.update(
                {
                    "forager_enrichment_status": STATUS_IN_PROGRESS,
                    "forager_enrichment_started_at": utc_now_iso(),
                    "forager_source": SOURCE_COMPANY_FLOW,
                }
            )
            created = await self.hubspot.create_object("contacts", seed)
            contact_id = str(created["id"])
            flow.created_contact_ids.append(contact_id)
            await self.associate_with_employer(contact_id, data, company_id)
            flow.contacts.append(
                await self._safe_enrich(contact_id, options, claimed=True, prefetched=data)
            )
        return flow

    async def _safe_enrich(
        self, contact_id: str, options: ContactLookupOptions, **kwargs: Any
    ) -> EnrichmentResult:
        """Enrich one contact without letting its failure abort the whole company flow."""
        try:
            return await self.enrich_contact(contact_id, options, **kwargs)
        except CreditBudgetExceeded:
            raise
        except Exception as exc:
            logger.exception("Contact enrichment failed", extra={"contact_id": contact_id})
            return EnrichmentResult("contact", contact_id, STATUS_FAILED, _error_note(exc))

    async def _contacts_of_company(self, company_id: str, mapped: Properties) -> list[str]:
        """Contacts already associated, plus unassociated ones sharing the email domain
        (which get associated now)."""
        ids = await self.hubspot.associated_ids("companies", company_id, "contacts")
        record = await self.hubspot.get_object("companies", company_id, ["domain"])
        domain = normalize_domain(((record or {}).get("properties") or {}).get("domain"))
        domain = domain or normalize_domain(mapped.get("domain"))
        if domain and domain not in FREE_EMAIL_DOMAINS:
            matches = await self.hubspot.search_objects(
                "contacts",
                [{"propertyName": "hs_email_domain", "operator": "EQ", "value": domain}],
                ["email"],
                limit=50,
            )
            for contact in matches:
                contact_id = str(contact["id"])
                if contact_id not in ids:
                    await self.hubspot.associate_default(
                        "contacts", contact_id, "companies", company_id
                    )
                    ids.append(contact_id)
        return ids

    # ================================================================== demo

    async def ensure_company(self, org: Organization) -> tuple[str, bool]:
        """Find the HubSpot company for an org by domain, or create it already claimed
        (so the company.creation webhook doesn't start a second, paid flow)."""
        domain = normalize_domain(org.domain)
        if domain:
            found = await self.hubspot.find_one("companies", "domain", domain, COMPANY_READ)
            if found:
                return str(found["id"]), False
        created = await self.hubspot.create_object(
            "companies",
            {
                "name": org.name,
                "domain": domain,
                "forager_enrichment_status": STATUS_IN_PROGRESS,
                "forager_enrichment_started_at": utc_now_iso(),
            },
        )
        return str(created["id"]), True

    async def select_complete_contacts(
        self,
        org: Organization,
        candidates: Iterable[Candidate],
        *,
        wanted: int,
        options: ContactLookupOptions,
        require_email: bool,
        require_phone: bool,
        dry_run: bool = False,
    ) -> list[SelectionOutcome]:
        """Walk candidates best-first, buying contact data until `wanted` people have the
        required fields. In dry-run, uncached lookups are reported instead of bought."""
        outcomes: list[SelectionOutcome] = []
        selected = 0
        for candidate in candidates:
            if selected >= wanted:
                break
            data = self.contact_data_from_candidate(candidate, org)
            try:
                await self.lookup_contact_info(data, options)
            except CreditBudgetExceeded as exc:
                if not dry_run:
                    outcomes.append(SelectionOutcome(candidate, data, False, str(exc)))
                    raise
                outcomes.append(
                    SelectionOutcome(candidate, data, False, f"needs paid lookups ({exc.endpoint})")
                )
                continue
            if require_email and not data.email:
                outcomes.append(SelectionOutcome(candidate, data, False, "no email in Forager"))
                continue
            if require_phone and not data.phone:
                outcomes.append(SelectionOutcome(candidate, data, False, "no phone in Forager"))
                continue
            outcomes.append(SelectionOutcome(candidate, data, True))
            selected += 1
        return outcomes

    async def write_selected(
        self, company_id: str, outcomes: list[SelectionOutcome], *, source: str = SOURCE_CLI
    ) -> list[EnrichmentResult]:
        results = []
        for outcome in outcomes:
            if outcome.selected and outcome.data is not None:
                results.append(
                    await self.upsert_contact(outcome.data, company_id=company_id, source=source)
                )
        return results


def _error_note(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:1000]
