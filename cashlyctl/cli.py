# cashlyctl/cli.py
"""
cashlyctl – Cashly command‑line interface
----------------------------------------
Usage examples:

    # Create a borrower
    $ cashlyctl borrower create --first-name Safraz --last-name Ally --annual-income 85000

    # Upload lender payloads from disk
    $ cashlyctl lender upload-dir ./lenders
"""

from __future__ import annotations

import typer

import sys
from .tui import run as run_tui

# Import the sub‑command groups
from .commands import borrower as borrower_cmd
from .commands import lender as lender_cmd

app = typer.Typer(
    name="cashlyctl",
    add_completion=False,
    help="Cashly CLI – lightweight admin and developer helper.",
)

# Register groups
app.add_typer(borrower_cmd.app, name="borrower", help="Create and manage borrowers")
app.add_typer(lender_cmd.app, name="lender", help="Upload lender JSON payloads")

# --------------------------------------------------------------------------- #
# Optional: quick health‑check command
# --------------------------------------------------------------------------- #
@app.command("ping", help="Check whether the API endpoint is reachable.")
def ping() -> None:
    """
    Simple connectivity test. Gives up after one request.
    """
    import requests
    from .config import endpoint, make_session

    session = make_session()
    try:
        resp = session.get(endpoint("/health"), timeout=5)
        status = "OK" if resp.ok else f"HTTP {resp.status_code}"
    except requests.RequestException as exc:
        status = f"ERROR – {exc.__class__.__name__}"
    typer.echo(status)


# Entrypoint for `python -m cashlyctl` or when installed via console_scripts
def _main() -> None:  # pragma: no cover
    app()

# boot into tui directly if no sub‑command is given
if len(sys.argv) == 1:      # no sub‑command
    run_tui()
    sys.exit(0)

if __name__ == "__main__":  # pragma: no cover
    _main()
