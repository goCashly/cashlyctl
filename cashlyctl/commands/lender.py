# cashlyctl/commands/lender.py
"""Typer commands for uploading lender payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import typer
import requests
from requests import HTTPError, RequestException
from rich.console import Console
from rich.syntax import Syntax

from ..api import lender as api_lender
from ..config import make_session

console = Console()
app = typer.Typer(help="Upload lender payloads from JSON files.")


def _render_response(response: dict[str, object]) -> None:
    console.print(
        Syntax.from_json(json.dumps(response, indent=2), theme="monokai", line_numbers=False),
        overflow="ignore",
    )


def _load_payload(path: Path) -> dict:
    data = path.read_text(encoding="utf-8")
    return json.loads(data)


def _upload_payloads(
    payloads: Iterable[dict[str, object]], *, session: requests.Session | None = None
) -> list[dict[str, object]]:
    return api_lender.submit_payloads(payloads, session=session)


@app.command("upload-file")
def upload_file(
    file_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, resolve_path=True, path_type=Path
    ),
    show_response: bool = typer.Option(
        True,
        "--show-response/--no-show-response",
        help="Render the API's JSON response.",
    ),
) -> None:
    """Upload a single lender JSON payload."""

    try:
        payload = _load_payload(file_path)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive formatting
        console.print(f"[red]Failed to parse JSON in {file_path}: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    try:
        response = _upload_payloads([payload])[0]
    except HTTPError as exc:
        console.print(f"[red]API error {exc.response.status_code} for {file_path}[/red]")
        try:
            console.print(exc.response.json())
        except Exception:  # pragma: no cover - fallback formatting
            console.print(exc.response.text)
        raise typer.Exit(code=1) from exc
    except RequestException as exc:
        console.print(f"[red]Request error while uploading {file_path}: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Uploaded {file_path} successfully.[/green]")
    if show_response:
        _render_response(response)


@app.command("upload-dir")
def upload_dir(
    directory: Path = typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True, resolve_path=True, path_type=Path
    ),
    pattern: str = typer.Option(
        "*.json", "--pattern", "-p", help="Glob pattern for files to upload."
    ),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Recurse into sub-directories."),
    show_responses: bool = typer.Option(
        False,
        "--show-responses/--hide-responses",
        help="Render API responses for each successful upload.",
    ),
) -> None:
    """Upload every JSON payload matching ``pattern`` within ``directory``."""

    file_iter = directory.rglob(pattern) if recursive else directory.glob(pattern)
    files = sorted(path for path in file_iter if path.is_file())

    if not files:
        console.print(
            f"[yellow]No files matching pattern '{pattern}' found in {directory}. Nothing to upload.[/yellow]"
        )
        raise typer.Exit(code=0)

    successes = 0
    failures = 0

    session: requests.Session | None = None

    for file_path in files:
        console.print(f"[cyan]Uploading {file_path}…[/cyan]")
        try:
            payload = _load_payload(file_path)
        except json.JSONDecodeError as exc:
            console.print(f"  [red]Invalid JSON: {exc}[/red]")
            failures += 1
            continue

        try:
            # Lazily create a reusable session for the batch upload
            if session is None:
                session = make_session()
            response = _upload_payloads([payload], session=session)[0]
        except HTTPError as exc:
            console.print(f"  [red]API error {exc.response.status_code}: {exc.response.reason}[/red]")
            try:
                console.print(exc.response.json())
            except Exception:  # pragma: no cover - fallback formatting
                console.print(exc.response.text)
            failures += 1
            continue
        except RequestException as exc:
            console.print(f"  [red]Request error: {exc}[/red]")
            failures += 1
            continue

        console.print("  [green]Success[/green]")
        if show_responses:
            _render_response(response)
        successes += 1

    console.print(
        f"\n[bold]Summary:[/bold] {successes} succeeded, {failures} failed out of {len(files)} file(s)."
    )

    if session is not None:
        session.close()

    if failures:
        raise typer.Exit(code=1)


__all__ = ["app"]
