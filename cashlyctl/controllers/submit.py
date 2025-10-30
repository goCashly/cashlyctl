import json
import aiohttp
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

from .base import CommandRouter
from ..widgets.jsonviewer import JSONViewer

# Load environment variables
load_dotenv()

API_URL = os.getenv("API_URL", "https://crm-api.gocashly.io/v1/submit")
API_KEY = os.getenv("CASHLY_API_KEY")

FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"


async def command_submit(app, args):
    """Submit a JSON file to the configured API endpoint."""
    viewer = app.query_one("#viewer", JSONViewer)

    # ─── Resolve which file to submit ────────────────────────────────────────────
    if args:
        name = args[0]
        matches = list(FILES_ROOT.rglob(name))
        if not matches:
            CommandRouter.sublog(app, f"[red]No file found named {name}[/red]")
            return
        path = matches[0]
    else:
        if not getattr(viewer, "current_path", None):
            CommandRouter.sublog(app, "[yellow]No file open to submit.[/yellow]")
            return
        path = viewer.current_path

    if not path.exists():
        CommandRouter.sublog(app, f"[red]File not found:[/red] {path}")
        return

    # ─── Load and validate JSON ─────────────────────────────────────────────────
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Invalid JSON:[/red] {e}")
        return

    if not API_KEY:
        CommandRouter.sublog(app, "[red]Missing API_KEY in .env[/red]")
        return

    # ─── Perform API call ───────────────────────────────────────────────────────
    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": API_KEY,
            }
            async with session.post(API_URL, headers=headers, json=payload) as resp:
                text = await resp.text()

                # Try to parse and render API response
                try:
                    parsed = json.loads(text)
                    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
                    viewer.display_json(pretty)
                    CommandRouter.sublog(app, f"[green]Submitted successfully:[/green] {path.name}")
                except Exception:
                    viewer.write(text)
                    CommandRouter.sublog(app, f"[yellow]Submitted (non-JSON response):[/yellow] {path.name}")

                if resp.status != 200:
                    CommandRouter.sublog(app, f"[red]API error {resp.status}[/red]")
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Network or API error:[/red] {e}")
