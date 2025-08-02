# cashlyctl/commands/pathfinder.py
"""Pathfinder CLI commands."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.syntax import Syntax
from requests.exceptions import HTTPError

from ..api import pathfinder as api_pathfinder

console = Console()


def pathfinder(
    id_mortgage_app: str = typer.Option(
        ..., "--id-mortgage-app", "-i", help="IDMortgageApp value"
    )
):
    """Call the /v1/pathfinder endpoint with the given ID."""
    payload = {"IDMortgageApp": id_mortgage_app}
    try:
        result = api_pathfinder.run(payload)
    except HTTPError as exc:
        console.print(f"[red]API error {exc.response.status_code}[/red]")
        try:
            console.print(exc.response.json())
        except Exception:
            console.print(exc.response.text)
        raise typer.Exit(code=1)

    console.print(
        Syntax.from_json(result, theme="monokai", line_numbers=False), overflow="ignore"
    )
