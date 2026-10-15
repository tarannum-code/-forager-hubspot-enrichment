"""Command-line interface. Run `python -m app.cli --help`.

Paid Forager commands refuse to spend credits unless you pass --max-credits (or set
FORAGER_MAX_CREDITS_PER_RUN), and they print the worst-case cost before calling the API.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from app.config import ConfigError, Settings, get_settings
from app.enrichment.candidates import rank_candidates
from app.enrichment.matching import best_organization
from app.enrichment.title_filter import TitleFilter
from app.forager.client import ForagerClient
from app.forager.credits import CREDIT_COSTS, CreditBudget, CreditBudgetExceeded
from app.forager.models import PersonRoleSearchResult
from app.http_utils import ApiError
from app.hubspot.client import HubSpotClient
from app.hubspot.properties import ensure_properties
from app.logging_config import configure_logging
from app.normalize import (
    clean_job_title,
    normalize_domain,
    parse_location,
    smart_name_case,
)

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()
T = TypeVar("T")


def _run(coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(2) from exc
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except CreditBudgetExceeded as exc:
        console.print(f"[yellow]Stopped before spending:[/yellow] {exc}")
        raise typer.Exit(3) from exc


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else settings.log_level, settings.log_json)


# ---------------------------------------------------------------------------- check


@app.command()
def check(
    save_account_id: bool = typer.Option(
        False, "--save-account-id", help="Write the detected Forager account id into .env."
    ),
) -> None:
    """Verify Forager + HubSpot credentials and show the credit balance (0 credits)."""
    _run(_check(get_settings(), save_account_id))


async def _check(settings: Settings, save_account_id: bool) -> None:
    ok = True

    # Forager: /api/users/current/ is free and returns the account id + balance.
    if not settings.forager_api_key:
        console.print("[red]x[/red] Forager: FORAGER_API_KEY is not set")
        ok = False
    else:
        async with ForagerClient(
            api_key=settings.forager_api_key.get_secret_value(),
            account_id=settings.forager_account_id or 0,
            budget=CreditBudget(limit=0, label="check"),
            base_url=settings.forager_base_url,
        ) as forager:
            try:
                user = await forager.current_user()
            except ApiError as exc:
                console.print(f"[red]x[/red] Forager: {exc}")
                ok = False
            else:
                accounts = user.get("accounts") or []
                for account in accounts:
                    sub = account.get("subscription") or {}
                    console.print(
                        f"[green]ok[/green] Forager account [bold]{account.get('id')}[/bold] "
                        f"({account.get('name')}) - credits balance: "
                        f"[bold]{sub.get('credits_balance')}[/bold]"
                    )
                ids = [a.get("id") for a in accounts]
                if settings.forager_account_id and settings.forager_account_id not in ids:
                    console.print(
                        f"[red]x[/red] FORAGER_ACCOUNT_ID={settings.forager_account_id} "
                        f"is not one of your accounts: {ids}"
                    )
                    ok = False
                elif not settings.forager_account_id and len(ids) == 1:
                    if save_account_id:
                        _write_env_value(Path(".env"), "FORAGER_ACCOUNT_ID", str(ids[0]))
                        console.print(f"   Saved FORAGER_ACCOUNT_ID={ids[0]} to .env")
                    else:
                        console.print(
                            f"   Set FORAGER_ACCOUNT_ID={ids[0]} in .env "
                            "(or re-run with --save-account-id)"
                        )

    # HubSpot: account details (free, no CRM data touched).
    if not settings.hubspot_access_token:
        console.print("[red]x[/red] HubSpot: HUBSPOT_ACCESS_TOKEN is not set")
        ok = False
    else:
        async with HubSpotClient.from_settings(settings) as hubspot:
            try:
                details = await hubspot.account_details()
                await hubspot.list_properties("contacts")
                await hubspot.list_properties("companies")
            except ApiError as exc:
                console.print(f"[red]x[/red] HubSpot: {exc}")
                if exc.status_code in (401, 403):
                    console.print(
                        "   Check the token and that it has the crm.objects.* and "
                        "crm.schemas.* contacts/companies scopes."
                    )
                ok = False
            else:
                console.print(
                    f"[green]ok[/green] HubSpot portal [bold]{details.get('portalId')}[/bold] "
                    f"({details.get('accountType', 'unknown type')}) "
                    f"- API {settings.hubspot_api_version}"
                )
    if not settings.hubspot_client_secret:
        console.print(
            "[yellow]![/yellow] HUBSPOT_CLIENT_SECRET not set - needed only for webhooks (Phase 7)"
        )
    raise typer.Exit(0 if ok else 1)


def _write_env_value(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    text = pattern.sub(line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------- setup


@app.command("setup-hubspot")
def setup_hubspot() -> None:
    """Create/verify the custom HubSpot properties (idempotent, 0 credits)."""
    _run(_setup_hubspot(get_settings()))


async def _setup_hubspot(settings: Settings) -> None:
    async with HubSpotClient.from_settings(settings) as hubspot:
        report = await ensure_properties(hubspot)
    table = Table("Object", "Property", "Result")
    for object_type, name, action in report:
        style = {"created": "green", "updated": "cyan", "ok": "dim"}.get(action, "red")
        table.add_row(object_type, name, f"[{style}]{action}[/{style}]")
    console.print(table)
    if any(action.startswith("CONFLICT") for *_, action in report):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------- credit helpers

MaxCreditsOpt = typer.Option(
    None,
    "--max-credits",
    help="Most Forager credits this run may spend (default: FORAGER_MAX_CREDITS_PER_RUN).",
)
YesOpt = typer.Option(False, "--yes", "-y", help="Skip the spend confirmation prompt.")
NoCacheOpt = typer.Option(
    False, "--no-cache", help="Ignore cached Forager responses (forces real, paid calls)."
)


def _budget(settings: Settings, max_credits: int | None, label: str) -> CreditBudget:
    limit = settings.forager_max_credits_per_run if max_credits is None else max_credits
    ledger = settings.forager_cache_dir.parent / "credit_ledger.jsonl"
    return CreditBudget(limit=limit, label=label, ledger_path=ledger)


def _confirm_spend(plan: list[tuple[str, int]], budget: CreditBudget, yes: bool) -> None:
    """Print the worst-case cost of the planned paid calls and ask before spending."""
    worst = sum(CREDIT_COSTS[endpoint] * count for endpoint, count in plan)
    calls = ", ".join(f"{n} x {ep} ({CREDIT_COSTS[ep]} cr)" for ep, n in plan)
    console.print(
        f"[bold]Planned Forager calls:[/bold] {calls} -> worst case [bold]{worst}[/bold] "
        f"credits (cached lookups are free). Run budget: {budget.limit}."
    )
    if budget.limit == 0:
        console.print(
            "[yellow]Budget is 0: only cached results can be shown. Pass --max-credits N.[/yellow]"
        )
    elif not yes and not typer.confirm("Proceed?", default=False):
        raise typer.Exit(0)


def _forager(settings: Settings, budget: CreditBudget, no_cache: bool) -> ForagerClient:
    client = ForagerClient.from_settings(settings, budget)
    if no_cache:
        client.cache.enabled = False
    return client


async def _print_spend(forager: ForagerClient, before: float | None) -> None:
    after = await forager.credits_balance()
    console.print(
        f"Credits spent this run (our accounting): [bold]{forager.budget.spent}[/bold] | "
        f"account balance {before} -> {after}"
    )


# ---------------------------------------------------------------------------- lookup-company


@app.command("lookup-company")
def lookup_company(
    domain: str = typer.Option(..., "--domain", "-d", help="Company domain, e.g. openai.com"),
    max_credits: int | None = MaxCreditsOpt,
    yes: bool = YesOpt,
    no_cache: bool = NoCacheOpt,
) -> None:
    """Show what Forager knows about a company (1 credit, no HubSpot writes)."""
    _run(_lookup_company(get_settings(), domain, max_credits, yes, no_cache))


async def _lookup_company(
    settings: Settings, domain: str, max_credits: int | None, yes: bool, no_cache: bool
) -> None:
    clean = normalize_domain(domain)
    if not clean:
        raise typer.BadParameter(f"Not a valid domain: {domain}")
    budget = _budget(settings, max_credits, f"lookup-company {clean}")
    _confirm_spend([("organization_search", 1)], budget, yes)
    async with _forager(settings, budget, no_cache) as forager:
        before = await forager.credits_balance()
        result = await forager.search_organizations(domains=[clean])
        best = best_organization(result.search_results, domain=clean)
        if best is None:
            console.print(f"[yellow]Forager has no organization for {clean}[/yellow]")
        others = [o for o in result.search_results if best and o.id != best.id]
        for org in [o for o in [best] if o]:
            loc = parse_location(org.location)
            linkedin = org.linkedin_info
            rows = {
                "Domain": org.domain,
                "Website": org.website,
                "LinkedIn": linkedin.public_profile_url if linkedin else None,
                "Industry": linkedin.industry.name if linkedin and linkedin.industry else None,
                "Employees": f"{org.employees_amount} ({org.employees_range})",
                "Revenue": org.finance_info.revenue if org.finance_info else None,
                "Founded": org.founded_date,
                "HQ": ", ".join(filter(None, [loc.city, loc.state, loc.country])),
                "Description": (org.description or "")[:300],
            }
            table = Table(title=f"Best match: {org.name} (Forager id {org.id})", show_header=False)
            for key, value in rows.items():
                table.add_row(key, str(value))
            console.print(table)
        if others:
            names = ", ".join(f"{o.name} ({o.employees_amount or 0} emp)" for o in others[:5])
            console.print(
                f"[dim]Also returned for this domain (ignored): {names}"
                f"{' ...' if len(others) > 5 else ''}[/dim]"
            )
        console.print(f"Total matches: {result.total_search_results}")
        await _print_spend(forager, before)


# ---------------------------------------------------------------------------- find-contacts


@app.command("find-contacts")
def find_contacts(
    domain: str = typer.Option(..., "--domain", "-d", help="Company domain, e.g. openai.com"),
    titles: str | None = typer.Option(
        None, "--titles", "-t", help='Title keywords, e.g. "VP,Head of,Chief" (default: env).'
    ),
    exclude: str | None = typer.Option(
        None, "--exclude", "-x", help='Keywords to reject, e.g. "Intern" (default: env).'
    ),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Result pages, 1 credit each."),
    show_rejected: bool = typer.Option(False, "--show-rejected", help="List filtered-out people."),
    max_credits: int | None = MaxCreditsOpt,
    yes: bool = YesOpt,
    no_cache: bool = NoCacheOpt,
) -> None:
    """Discover people at a company filtered by job title (1 credit/page, no HubSpot writes)."""
    settings = get_settings()
    title_filter = TitleFilter.from_lists(
        titles.split(",") if titles is not None else settings.contact_titles,
        exclude.split(",") if exclude is not None else settings.contact_title_excludes,
    )
    _run(
        _find_contacts(
            settings, domain, title_filter, pages, show_rejected, max_credits, yes, no_cache
        )
    )


async def _find_contacts(
    settings: Settings,
    domain: str,
    title_filter: TitleFilter,
    pages: int,
    show_rejected: bool,
    max_credits: int | None,
    yes: bool,
    no_cache: bool,
) -> None:
    clean = normalize_domain(domain)
    if not clean:
        raise typer.BadParameter(f"Not a valid domain: {domain}")
    console.print(
        f"Title filter: include {list(title_filter.include)} | "
        f"exclude {list(title_filter.exclude)}\n"
        f"Forager role_title query: [cyan]{title_filter.to_forager_query()}[/cyan]"
    )
    budget = _budget(settings, max_credits, f"find-contacts {clean}")
    # The company is resolved first (cached after the first lookup -> free), so people are
    # searched by Forager organization id rather than by a domain shared by many orgs.
    _confirm_spend([("organization_search", 1), ("person_role_search", pages)], budget, yes)

    async with _forager(settings, budget, no_cache) as forager:
        before = await forager.credits_balance()
        orgs = await forager.search_organizations(domains=[clean])
        company = best_organization(orgs.search_results, domain=clean)
        if company is None:
            console.print(f"[yellow]Forager has no organization for {clean}[/yellow]")
            return
        console.print(f"Company: [bold]{company.name}[/bold] (Forager id {company.id})")
        rows: list[PersonRoleSearchResult] = []
        total = 0
        for page in range(1, pages + 1):
            query = forager.person_role_query(
                role_title=title_filter.to_forager_query(),
                organization_ids=[company.id],
                page=page,
            )
            result = await forager.search_person_roles(query)
            total = result.total_search_results
            rows.extend(result.search_results)
            if not result.search_results:
                break
        accepted, rejected = rank_candidates(rows, company, title_filter)

        table = Table(title=f"People at {company.name} matching the title filter ({len(accepted)})")
        for column in ("#", "Name", "Title", "Location", "LinkedIn", "Quality"):
            table.add_column(column)
        for i, candidate in enumerate(accepted, 1):
            person, loc = candidate.row.person, candidate.location
            first, last = smart_name_case(person.first_name), smart_name_case(person.last_name)
            table.add_row(
                str(i),
                " ".join(filter(None, [first, last])),
                clean_job_title(candidate.row.role_title) or "",
                ", ".join(filter(None, [loc.city, loc.state, loc.country])) or "-",
                candidate.linkedin_slug or "",
                f"{candidate.score}"
                + (f" ({'; '.join(candidate.notes)})" if candidate.notes else ""),
            )
        console.print(table)
        if show_rejected and rejected:
            rejected_table = Table(title=f"Filtered out ({len(rejected)})")
            for column in ("Name", "Title", "Reason"):
                rejected_table.add_column(column)
            for candidate in rejected:
                rejected_table.add_row(
                    smart_name_case(candidate.row.person.full_name) or "",
                    candidate.row.role_title or "",
                    candidate.reject_reason or "",
                )
            console.print(rejected_table)
        console.print(
            f"Forager matches for this query: {total} | accepted {len(accepted)}, "
            f"rejected {len(rejected)}"
        )
        await _print_spend(forager, before)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
