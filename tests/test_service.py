"""End-to-end service flows: real ForagerClient on mocked HTTP + in-memory HubSpot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.config import Settings
from app.enrichment.service import ContactLookupOptions, EnrichmentService
from app.enrichment.title_filter import TitleFilter
from app.forager.client import ForagerClient
from app.forager.credits import CreditBudget
from app.http_utils import ApiError
from tests.conftest import forager_url, load_fixture
from tests.fakes import FakeHubSpot

JOB = "11111111-2222-3333-4444-555555555555"
FULL = ContactLookupOptions(work_email=True, phone=True)
TITLES = TitleFilter.parse("VP,Head of,Chief", "Intern")


@pytest.fixture
def fake_hubspot() -> FakeHubSpot:
    return FakeHubSpot()


@pytest.fixture
def service(forager: ForagerClient, fake_hubspot: FakeHubSpot) -> EnrichmentService:
    return EnrichmentService(forager, fake_hubspot, Settings(_env_file=None))  # type: ignore[arg-type,call-arg]


def mock_forager(router: respx.MockRouter) -> dict[str, respx.Route]:
    routes = {
        "autocomplete": router.get(forager_url("autocomplete/organizations/")).respond(
            json=load_fixture("forager/autocomplete_organizations.json")
        ),
        "org": router.post(forager_url("organization_search/")).respond(
            json=load_fixture("forager/organization_search.json")
        ),
        "roles": router.post(forager_url("person_role_search/")).respond(
            json=load_fixture("forager/person_role_search.json")
        ),
        "detail": router.post(forager_url("person_detail_lookup/")).respond(
            json=load_fixture("forager/person_detail.json")
        ),
        "phone": router.post(forager_url("person_contacts_lookup/phone_numbers/")).respond(
            json=load_fixture("forager/phone_numbers.json")
        ),
        "email_submit": router.post(
            forager_url("person_contacts_lookup/work_emails/", v2=True)
        ).respond(202, json=load_fixture("forager/work_email_submitted.json")),
        "email_poll": router.get(
            forager_url(f"person_contacts_lookup/work_emails/{JOB}/", v2=True)
        ).respond(json=load_fixture("forager/work_email_found.json")),
    }
    return routes


# ----------------------------------------------------------------------- companies


async def test_enrich_company(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_forager(mock_router)
    company_id = fake_hubspot.add("companies", {"name": "Acme", "domain": "acmeai.com"})

    result = await service.enrich_company(company_id)

    props = fake_hubspot.props("companies", company_id)
    assert result.status == "enriched"
    assert props["name"] == "Acme"  # user's name kept
    assert props["numberofemployees"] == 3200
    assert props["forager_enrichment_status"] == "enriched"
    assert props["forager_enriched_at"]
    # The record was claimed (in_progress) before credits were spent.
    first_update = fake_hubspot.updates[0][2]
    assert first_update["forager_enrichment_status"] == "in_progress"
    assert service.forager.budget.spent == 1


async def test_already_enriched_company_costs_nothing(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    company_id = fake_hubspot.add(
        "companies", {"domain": "acmeai.com", "forager_enrichment_status": "enriched"}
    )
    result = await service.enrich_company(company_id)
    assert result.status == "skipped"
    assert routes["org"].call_count == 0 and fake_hubspot.updates == []


async def test_in_progress_claim_blocks_duplicates_until_stale(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_forager(mock_router)
    fresh = datetime.now(UTC).isoformat()
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    busy = fake_hubspot.add(
        "companies",
        {
            "domain": "acmeai.com",
            "forager_enrichment_status": "in_progress",
            "forager_enrichment_started_at": fresh,
        },
    )
    crashed = fake_hubspot.add(
        "companies",
        {
            "domain": "acmeai.com",
            "forager_enrichment_status": "in_progress",
            "forager_enrichment_started_at": stale,
        },
    )
    assert (await service.enrich_company(busy)).status == "skipped"
    assert (await service.enrich_company(crashed)).status == "enriched"


async def test_company_not_in_forager(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_router.post(forager_url("organization_search/")).respond(
        json={"search_results": [], "total_search_results": 0}
    )
    company_id = fake_hubspot.add("companies", {"domain": "nowhere.example"})
    assert (await service.enrich_company(company_id)).status == "not_found"
    assert fake_hubspot.props("companies", company_id)["forager_enrichment_status"] == "not_found"


async def test_company_failure_is_recorded(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_router.post(forager_url("organization_search/")).respond(400, json={"detail": "bad"})
    company_id = fake_hubspot.add("companies", {"domain": "acmeai.com"})
    with pytest.raises(ApiError):
        await service.enrich_company(company_id)
    props = fake_hubspot.props("companies", company_id)
    assert props["forager_enrichment_status"] == "failed"
    assert "400" in props["forager_last_error"]


# ----------------------------------------------------------------------- contacts


async def test_enrich_contact_by_linkedin(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    company_id = fake_hubspot.add("companies", {"name": "Acme AI", "domain": "acmeai.com"})
    contact_id = fake_hubspot.add(
        "contacts", {"hs_linkedin_url": "https://www.linkedin.com/in/jane-doe-smith/"}
    )

    result = await service.enrich_contact(contact_id, FULL)

    props = fake_hubspot.props("contacts", contact_id)
    assert result.status == "enriched", result.message
    assert props["firstname"] == "Jane" and props["lastname"] == "Doe-Smith"
    assert props["email"] == "jane@acmeai.com"
    assert props["phone"] == "+1 415-555-2671"
    assert props["company"] == "Acme AI"
    assert (contact_id, company_id) in fake_hubspot.associations
    assert routes["detail"].call_count == 1
    assert service.forager.budget.spent == 1 + 5 + 15


async def test_contact_existing_email_and_phone_are_not_bought_again(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    contact_id = fake_hubspot.add(
        "contacts",
        {
            "hs_linkedin_url": "https://www.linkedin.com/in/jane-doe-smith",
            "email": "jane@acmeai.com",
            "phone": "+1 415 555 0000",
        },
    )
    await service.enrich_contact(contact_id, FULL)
    assert routes["email_submit"].call_count == 0 and routes["phone"].call_count == 0
    assert service.forager.budget.spent == 1


async def test_contact_not_found(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_router.post(forager_url("person_detail_lookup/")).respond(404, json={})
    contact_id = fake_hubspot.add("contacts", {"hs_linkedin_url": "linkedin.com/in/ghost"})
    assert (await service.enrich_contact(contact_id, FULL)).status == "not_found"


async def test_email_only_contact_uses_reverse_lookup(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_forager(mock_router)
    reverse = mock_router.post(forager_url("person_detail_reverse_lookup/by_email/")).respond(
        json=load_fixture("forager/person_detail.json")
    )
    contact_id = fake_hubspot.add("contacts", {"email": "jane@gmail.com"})
    result = await service.enrich_contact(contact_id, ContactLookupOptions(phone=False))
    assert result.status == "enriched"
    assert reverse.call_count == 1
    assert fake_hubspot.props("contacts", contact_id)["email"] == "jane@gmail.com"  # kept


# ----------------------------------------------------------------------- webhook flows


async def test_company_flow_creates_contacts_and_prevents_loops(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    company_id = fake_hubspot.add("companies", {"name": "Acme AI", "domain": "acmeai.com"})

    flow = await service.process_company_created(
        company_id, title_filter=TITLES, options=FULL, max_new_contacts=2
    )

    # Only Jane survives the filters (intern, "MVP", other-company rows are rejected).
    assert len(flow.created_contact_ids) == 1
    assert {c.reject_reason for c in flow.rejected_candidates} == {
        "title filter",
        "different organization",
    }
    contact_id = flow.created_contact_ids[0]
    props = fake_hubspot.props("contacts", contact_id)
    assert props["forager_source"] == "company_flow"
    assert props["forager_enrichment_status"] == "enriched"
    assert (contact_id, company_id) in fake_hubspot.associations
    # Created with a marker -> the contact.creation webhook it triggers must be a no-op.
    created_props = fake_hubspot.creates[0][1]
    assert created_props["forager_enrichment_status"] == "in_progress"
    spent = service.forager.budget.spent
    again = await service.process_contact_created(contact_id, FULL)
    assert again.status == "skipped" and service.forager.budget.spent == spent
    # Search used the org id, and no detail lookup was needed (search data reused).
    assert b'"organizations":[501]' in routes["roles"].calls.last.request.content
    assert routes["detail"].call_count == 0


async def test_company_flow_enriches_existing_contacts_instead_of_searching(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    company_id = fake_hubspot.add("companies", {"name": "Acme AI", "domain": "acmeai.com"})
    contact_id = fake_hubspot.add(
        "contacts", {"hs_linkedin_url": "https://www.linkedin.com/in/jane-doe-smith"}
    )
    fake_hubspot.associations.add((contact_id, company_id))

    flow = await service.process_company_created(
        company_id, title_filter=TITLES, options=FULL, max_new_contacts=2
    )
    assert [c.object_id for c in flow.contacts] == [contact_id]
    assert routes["roles"].call_count == 0 and flow.created_contact_ids == []


async def test_company_flow_picks_up_unassociated_contacts_by_email_domain(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_forager(mock_router)
    company_id = fake_hubspot.add("companies", {"name": "Acme AI", "domain": "acmeai.com"})
    contact_id = fake_hubspot.add(
        "contacts",
        {
            "email": "jane@acmeai.com",
            "hs_linkedin_url": "https://www.linkedin.com/in/jane-doe-smith",
        },
    )
    flow = await service.process_company_created(
        company_id, title_filter=TITLES, options=FULL, max_new_contacts=2
    )
    assert (contact_id, company_id) in fake_hubspot.associations
    assert [c.object_id for c in flow.contacts] == [contact_id]


async def test_company_flow_skipped_when_company_already_enriched(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    company_id = fake_hubspot.add(
        "companies",
        {
            "domain": "acmeai.com",
            "forager_enrichment_status": "in_progress",
            "forager_enrichment_started_at": datetime.now(UTC).isoformat(),
        },
    )
    flow = await service.process_company_created(
        company_id, title_filter=TITLES, options=FULL, max_new_contacts=2
    )
    assert flow.company.status == "skipped" and routes["roles"].call_count == 0


# ----------------------------------------------------------------------- demo selection


async def test_selection_only_buys_phone_after_email_found(
    service: EnrichmentService, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    # Sam has no work email: polling returns not_found for a different job id.
    routes["email_submit"].side_effect = [
        httpx.Response(202, json={"job_id": JOB, "status": "queued"}),
    ]
    org = await service.resolve_organization(domain="acmeai.com")
    assert org is not None
    accepted, _ = await service.find_candidates(org, TITLES)
    outcomes = await service.select_complete_contacts(
        org,
        accepted,
        wanted=5,
        options=ContactLookupOptions(work_email=True, phone=True, phone_requires_email=True),
        require_email=True,
        require_phone=True,
    )
    assert [o.selected for o in outcomes] == [True]
    assert outcomes[0].data is not None and outcomes[0].data.phone == "+1 415-555-2671"


async def test_dry_run_reports_instead_of_spending(
    forager: ForagerClient, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_forager(mock_router)
    service = EnrichmentService(forager, fake_hubspot, Settings(_env_file=None))  # type: ignore[arg-type,call-arg]
    org = await service.resolve_organization(domain="acmeai.com")
    assert org is not None
    accepted, _ = await service.find_candidates(org, TITLES)
    forager.budget = CreditBudget(limit=0)
    outcomes = await service.select_complete_contacts(
        org, accepted, wanted=5, options=FULL, require_email=True, require_phone=True, dry_run=True
    )
    assert outcomes[0].reason == "needs paid lookups (work_email)"
    # A real run stops at the budget and returns what it has, instead of losing it.
    outcomes = await service.select_complete_contacts(
        org, accepted, wanted=5, options=FULL, require_email=True, require_phone=True
    )
    assert [o.reason for o in outcomes] == ["budget reached (work_email)"]


# ----------------------------------------------------------------------- robustness


async def test_failure_marker_does_not_mask_the_original_error(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_router.post(forager_url("organization_search/")).respond(400, json={"detail": "bad"})
    company_id = fake_hubspot.add("companies", {"domain": "acmeai.com"})
    real_update = fake_hubspot.update_object

    async def flaky_update(object_type: str, object_id: str | int, props: dict) -> dict:  # type: ignore[type-arg]
        if props.get("forager_enrichment_status") == "failed":
            raise RuntimeError("HubSpot is down too")
        return await real_update(object_type, object_id, props)

    fake_hubspot.update_object = flaky_update  # type: ignore[method-assign]
    with pytest.raises(ApiError, match="400"):  # the Forager error, not the HubSpot one
        await service.enrich_company(company_id)


def test_resume_ignores_own_claim_but_not_finished_records(service: EnrichmentService) -> None:
    fresh = {
        "forager_enrichment_status": "in_progress",
        "forager_enrichment_started_at": datetime.now(UTC).isoformat(),
    }
    assert service.skip_reason(fresh) == "enrichment already in progress"
    assert service.skip_reason(fresh, resume=True) is None
    done = {"forager_enrichment_status": "enriched"}
    assert service.skip_reason(done, resume=True) == "already enriched"


async def test_upsert_reuses_contact_with_same_email(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    mock_forager(mock_router)
    org = await service.resolve_organization(domain="acmeai.com")
    assert org is not None
    accepted, _ = await service.find_candidates(org, TITLES)
    data = service.contact_data_from_candidate(accepted[0], org)
    data.work_email = "jane@acmeai.com"
    # Added by hand: same email, no LinkedIn URL. Creating another would be a 409.
    existing_id = fake_hubspot.add("contacts", {"email": "jane@acmeai.com", "firstname": "J"})

    result = await service.upsert_contact(data, company_id=None, source="cli")

    assert result.object_id == existing_id
    assert fake_hubspot.creates == []
    assert fake_hubspot.props("contacts", existing_id)["firstname"] == "J"  # identity kept


async def test_acquired_company_sharing_the_domain_is_not_picked(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    # Real case: Octane was acquired by Stripe and renamed "Stripe" in Forager, keeps
    # stripe.com, has 2 employees, and was the only result of the domain search.
    acquired = {
        "id": 900,
        "name": "Acme AI",
        "legal_name": "Octane (We've joined Acme AI!)",
        "domain": "acmeai.com",
        "website": "https://acmeai.com",
        "employees_amount": 2,
    }
    routes["org"].side_effect = [
        httpx.Response(200, json={"search_results": [acquired], "total_search_results": 1}),
        httpx.Response(200, json=load_fixture("forager/organization_search.json")),
    ]
    company_id = fake_hubspot.add("companies", {"name": "Acme AI", "domain": "acmeai.com"})

    result = await service.enrich_company(company_id)

    # The free autocomplete knew the canonical id (501); it was fetched and won on size.
    assert result.organization is not None and result.organization.id == 501
    assert routes["autocomplete"].call_count == 1
    assert b'"organization_ids":[501]' in routes["org"].calls.last.request.content
    assert service.forager.budget.spent == 2


async def test_no_extra_credit_when_autocomplete_agrees(
    service: EnrichmentService, fake_hubspot: FakeHubSpot, mock_router: respx.MockRouter
) -> None:
    routes = mock_forager(mock_router)
    company_id = fake_hubspot.add("companies", {"name": "Acme AI", "domain": "acmeai.com"})
    await service.enrich_company(company_id)
    assert routes["autocomplete"].call_count == 1  # free
    assert routes["org"].call_count == 1
    assert service.forager.budget.spent == 1
