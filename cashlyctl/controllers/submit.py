import json
import os
from pathlib import Path
from typing import Iterable
from types import SimpleNamespace

try:  # pragma: no cover - optional dependency
    import aiohttp
except Exception:  # pragma: no cover
    class _MissingClientSession:  # pragma: no cover
        __cashly_missing__ = True

        def __init__(self, *args, **kwargs):
            raise RuntimeError("aiohttp is required for submit command")

    aiohttp = SimpleNamespace(ClientSession=_MissingClientSession)

try:  # pragma: no cover - optional dependency
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*args, **kwargs):  # type: ignore[misc]
        return None

from .base import CommandRouter
from ..widgets.jsonviewer import JSONViewer

# Load environment variables
load_dotenv()

API_URL = os.getenv("API_URL", "https://crm-api.gocashly.io/v1/submit")
API_KEY = os.getenv("CASHLY_API_KEY")

FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"


async def command_submit(app, args):
    """Submit one or more JSON files to the configured API endpoint."""

    viewer = app.query_one("#viewer", JSONViewer)
    paths: list[Path] = []

    if args:
        for name in args:
            matches = list(FILES_ROOT.rglob(name))
            if not matches:
                CommandRouter.sublog(app, f"[red]No file found named {name}[/red]")
                continue
            paths.append(matches[0])
        if not paths:
            return
    else:
        selected: Iterable[Path] = getattr(app, "selected_file_paths", [])
        selected_list = [Path(p) for p in selected]
        if selected_list:
            paths = selected_list
        else:
            current = getattr(viewer, "current_path", None)
            if not current:
                CommandRouter.sublog(app, "[yellow]No file selected to submit.[/yellow]")
                return
            paths = [Path(current)]

    if getattr(aiohttp.ClientSession, "__cashly_missing__", False):
        CommandRouter.sublog(app, "[red]aiohttp is not available; cannot submit.[/red]")
        return

    if not API_KEY:
        CommandRouter.sublog(app, "[red]Missing API_KEY in .env[/red]")
        return

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": API_KEY,
    }

    try:
        async with aiohttp.ClientSession() as session:
            for path in paths:
                await _submit_single(app, viewer, session, headers, Path(path))
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Network or API error:[/red] {e}")
        return

    if not args and getattr(app, "selected_file_paths", []):
        # Clear the selection after submitting queued files.
        try:
            app.update_selected_files([])
        except Exception:
            pass


async def _submit_single(app, viewer, session, headers, path: Path) -> None:
    if not path.exists():
        CommandRouter.sublog(app, f"[red]File not found:[/red] {path}")
        return

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Invalid JSON ({path.name}):[/red] {e}")
        return

    try:
        async with session.post(API_URL, headers=headers, json=payload) as resp:
            text = await resp.text()

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
        CommandRouter.sublog(app, f"[red]Network or API error ({path.name}):[/red] {e}")
