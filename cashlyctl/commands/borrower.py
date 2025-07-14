# cashlyctl/commands/borrower.py
"""
Borrower‑related CLI commands.

Currently supports: `borrower create`
Deletion will be wired once an API endpoint exists.
"""

from __future__ import annotations

import typer
import rich
from rich.console import Console
from rich.syntax import Syntax
from requests.exceptions import HTTPError

from ..api import borrower as api_borrower

console = Console()
app = typer.Typer(help="Create and (soon) delete Borrowers.")


@app.command("create")
def create(
    # ── Flags reflecting ontology.yaml ────────────────────────────────────────
    first_name: str = typer.Option(..., "--first-name", "-f", help="Borrower first name"),
    last_name: str = typer.Option(..., "--last-name", "-l", help="Borrower last name"),
    phone: str = typer.Option(None, "--phone", "-p", help="Phone number"),
    email: str = typer.Option(None, "--email", "-e", help="Email address"),
    annual_income: float = typer.Option(
        None, "--annual-income", "-i", help="Annual income in dollars"
    ),
    id_borrower: str = typer.Option(
        None,
        "--id-borrower",
        "-b",
        help="Optional explicit IDBorrower (otherwise backend/CLI generates)",
    ),
):
    """
    Insert a Borrower through the Cashly API.
    """
    payload = {
        "IDBorrower": id_borrower,
        "firstName": first_name,
        "lastName": last_name,
        "phone": phone,
        "email": email,
        "annualIncome": annual_income,
    }

    # Strip keys with None values so we don’t send nulls needlessly
    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        result = api_borrower.create(payload)
    except HTTPError as exc:
        console.print(f"[red]API error {exc.response.status_code}[/red]")
        try:
            console.print(exc.response.json())
        except Exception:
            console.print(exc.response.text)
        raise typer.Exit(code=1)

    # Pretty‑print the backend response (often a Cypher string + meta)
    console.print(
        Syntax.from_json(result, theme="monokai", line_numbers=False),
        overflow="ignore",
    )


# Placeholder for future delete sub‑command
@app.command("delete", hidden=True)
def delete():
    console.print("[yellow]Delete endpoint not implemented yet.[/yellow]")
