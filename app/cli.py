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
from app.forager.client import ForagerClient
from app.forager.credits import CreditBudget
from app.http_utils import ApiError
from app.hubspot.client import HubSpotClient
from app.hubspot.properties import ensure_properties
from app.logging_config import configure_logging

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
