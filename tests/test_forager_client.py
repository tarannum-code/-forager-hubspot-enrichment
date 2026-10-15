"""Forager client tests — all HTTP is mocked; the real API is never called."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.forager.client import ForagerClient
from app.forager.credits import CreditBudget, CreditBudgetExceeded
from app.http_utils import ApiError
from tests.conftest import FORAGER_BASE, forager_url, load_fixture


async def test_org_search_parses_and_charges_one_credit(
    forager: ForagerClient, mock_router: respx.MockRouter, budget: CreditBudget
) -> None:
    route = mock_router.post(forager_url("organization_search/")).respond(
        json=load_fixture("forager/organization_search.json")
    )
    result = await forager.search_organizations(domains=["acmeai.com"])

    assert route.calls.last.request.headers["X-API-KEY"] == "test-key"
    assert result.search_results[0].linkedin_info.public_identifier == "acme-ai"  # type: ignore[union-attr]
    assert result.search_results[0].finance_info.revenue == 3_700_000_000  # type: ignore[union-attr]
    assert budget.spent == 1


async def test_cache_prevents_paying_twice(
    forager: ForagerClient, mock_router: respx.MockRouter, budget: CreditBudget
) -> None:
    route = mock_router.post(forager_url("organization_search/")).respond(
        json=load_fixture("forager/organization_search.json")
    )
    await forager.search_organizations(domains=["acmeai.com"])
    await forager.search_organizations(domains=["acmeai.com"])

    assert route.call_count == 1
    assert budget.spent == 1


async def test_budget_refuses_before_sending(
    forager: ForagerClient, mock_router: respx.MockRouter
) -> None:
    forager.budget = CreditBudget(limit=10)
    route = mock_router.post(forager_url("person_contacts_lookup/phone_numbers/"))

    with pytest.raises(CreditBudgetExceeded):
        await forager.phone_numbers(linkedin_slug="jane-doe-smith")
    assert route.call_count == 0  # nothing was sent, nothing was spent
    assert forager.budget.spent == 0


async def test_phone_lookup(forager: ForagerClient, mock_router: respx.MockRouter) -> None:
    route = mock_router.post(forager_url("person_contacts_lookup/phone_numbers/")).respond(
        json=load_fixture("forager/phone_numbers.json")
    )
    phones = await forager.phone_numbers(linkedin_slug="jane-doe-smith")

    assert [p.phone_number for p in phones] == ["4155552671", "n/a"]
    assert route.calls.last.request.content == b'{"linkedin_public_identifier":"jane-doe-smith"}'
    assert forager.budget.spent == 15


async def test_empty_phone_lookup_is_free(
    forager: ForagerClient, mock_router: respx.MockRouter
) -> None:
    mock_router.post(forager_url("person_contacts_lookup/phone_numbers/")).respond(json=[])
    assert await forager.phone_numbers(linkedin_slug="jane-doe-smith") == []
    assert forager.budget.spent == 0


async def test_not_found_is_cached_and_free(
    forager: ForagerClient, mock_router: respx.MockRouter
) -> None:
    route = mock_router.post(forager_url("person_detail_lookup/")).respond(
        404, json={"detail": "Not found."}
    )
    assert await forager.person_detail(linkedin_slug="nobody") is None
    assert await forager.person_detail(linkedin_slug="nobody") is None
    assert route.call_count == 1
    assert forager.budget.spent == 0


async def test_rate_limit_is_retried(forager: ForagerClient, mock_router: respx.MockRouter) -> None:
    route = mock_router.post(forager_url("person_detail_lookup/"))
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}, json={"detail": "slow down"}),
        httpx.Response(200, json=load_fixture("forager/person_detail.json")),
    ]
    person = await forager.person_detail(linkedin_slug="jane-doe-smith")

    assert person is not None and person.current_role().organization.domain == "acmeai.com"  # type: ignore[union-attr]
    assert route.call_count == 2
    assert forager.budget.spent == 1


async def test_client_error_releases_reservation(
    forager: ForagerClient, mock_router: respx.MockRouter
) -> None:
    mock_router.post(forager_url("person_detail_lookup/")).respond(401, json={"detail": "bad key"})
    with pytest.raises(ApiError) as exc:
        await forager.person_detail(linkedin_slug="jane")
    assert exc.value.status_code == 401
    assert forager.budget.spent == 0 and forager.budget.reserved == 0


async def test_read_timeout_on_paid_call_is_not_retried(
    forager: ForagerClient, mock_router: respx.MockRouter
) -> None:
    route = mock_router.post(forager_url("person_contacts_lookup/phone_numbers/"))
    route.side_effect = httpx.ReadTimeout("timed out")
    with pytest.raises(httpx.ReadTimeout):
        await forager.phone_numbers(linkedin_slug="jane")
    assert route.call_count == 1  # never retried -> no risk of double charge
    assert forager.budget.spent == 15  # unknown outcome is counted conservatively


async def test_work_email_polls_until_found(
    forager: ForagerClient, mock_router: respx.MockRouter
) -> None:
    job_id = "11111111-2222-3333-4444-555555555555"
    submit = mock_router.post(forager_url("person_contacts_lookup/work_emails/", v2=True)).respond(
        202, json=load_fixture("forager/work_email_submitted.json")
    )
    poll = mock_router.get(forager_url(f"person_contacts_lookup/work_emails/{job_id}/", v2=True))
    poll.side_effect = [
        httpx.Response(200, json=load_fixture("forager/work_email_running.json")),
        httpx.Response(200, json=load_fixture("forager/work_email_found.json")),
    ]
    job = await forager.work_email(linkedin_slug="jane-doe-smith")

    assert job is not None
    assert job.addresses()[0].email == "Jane@AcmeAI.com"
    assert job.addresses()[0].validation_status == "valid"
    assert submit.call_count == 1 and poll.call_count == 2
    assert forager.budget.spent == 5

    # Second call is served from cache: no submit, no credits.
    again = await forager.work_email(linkedin_slug="jane-doe-smith")
    assert again is not None and submit.call_count == 1 and forager.budget.spent == 5


async def test_work_email_not_found_costs_nothing(
    forager: ForagerClient, mock_router: respx.MockRouter
) -> None:
    job_id = "22222222-2222-3333-4444-555555555555"
    mock_router.post(forager_url("person_contacts_lookup/work_emails/", v2=True)).respond(
        202, json={"job_id": job_id, "status": "queued"}
    )
    mock_router.get(forager_url(f"person_contacts_lookup/work_emails/{job_id}/", v2=True)).respond(
        json=load_fixture("forager/work_email_not_found.json")
    )

    assert await forager.work_email(linkedin_slug="sam-lee-123") is None
    assert forager.budget.spent == 0


def test_work_email_accepts_string_entries() -> None:
    from app.forager.models import WorkEmailJob

    job = WorkEmailJob(
        job_id="x",
        status="completed",
        result="found",
        emails=["a@b.com (valid)", "c@b.com - invalid", "junk"],
    )
    assert [(e.email, e.validation_status) for e in job.addresses()] == [
        ("a@b.com", "valid"),
        ("c@b.com", "invalid"),
    ]


async def test_free_endpoints_do_not_touch_budget(
    forager: ForagerClient, mock_router: respx.MockRouter
) -> None:
    mock_router.get(f"{FORAGER_BASE}/api/users/current/").respond(
        json=load_fixture("forager/current_user.json")
    )
    mock_router.get(forager_url("autocomplete/organizations/")).respond(
        json=load_fixture("forager/autocomplete_organizations.json")
    )
    assert await forager.credits_balance() == 150.0
    items = await forager.autocomplete_organizations("Acme AI")
    assert (items[0].name, items[0].domain) == ("Acme AI", "acmeai.com")
    assert forager.budget.spent == 0


def test_person_role_query_omits_empty_filters() -> None:
    body = ForagerClient.person_role_query(
        role_title='"VP" OR "Head of"', organization_domains=["acmeai.com"]
    )
    assert body == {
        "page": 1,
        "role_is_current": True,
        "role_title": '"VP" OR "Head of"',
        "organization_domains": ["acmeai.com"],
    }


def test_person_ref_requires_identifier() -> None:
    with pytest.raises(ValueError):
        ForagerClient.person_ref()
