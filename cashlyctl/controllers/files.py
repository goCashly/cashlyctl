from pathlib import Path
import json
import os
import asyncio
try:  # pragma: no cover - optional dependency
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*args, **kwargs):  # type: ignore[misc]
        return None

try:  # pragma: no cover - optional dependency
    import aiofiles
except Exception:  # pragma: no cover
    class _AsyncFile:
        def __init__(self, path, mode, encoding=None):
            self.path = path
            self.mode = mode
            self.encoding = encoding
            self._file = None

        async def __aenter__(self):
            self._file = open(self.path, self.mode, encoding=self.encoding)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if self._file:
                await asyncio.to_thread(self._file.close)

        async def read(self):
            return await asyncio.to_thread(self._file.read)

        async def write(self, data):
            return await asyncio.to_thread(self._file.write, data)

    class aiofiles:  # type: ignore[override]
        @staticmethod
        def open(path, mode="r", encoding=None):
            return _AsyncFile(path, mode, encoding)

try:  # pragma: no cover - optional dependency
    import httpx
except Exception:  # pragma: no cover
    httpx = None

from ..widgets.jsonviewer import JSONViewer
from ..widgets.filetree import FileTreePanel
from ..widgets.networkpanel import NetworkPanel
from .base import CommandRouter

# Load environment variables
load_dotenv()

API_URL = os.getenv("CASHLY_API_URL", "https://crm-api.gocashly.io/v1/submit")
API_KEY = os.getenv("CASHLY_API_KEY")
FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"


# ─── BASIC FILE COMMANDS ─────────────────────────────────────────────

async def command_refresh(app, args):
    """Reload the file tree."""
    filetree = app.query_one("#files", FileTreePanel)
    await filetree.reload()
    CommandRouter.sublog(app, "[green]File tree refreshed.[/green]")


async def command_open(app, args):
    """Open and display a JSON file."""
    viewer = app.query_one("#viewer", JSONViewer)
    filetree = app.query_one("#files", FileTreePanel)

    if not args:
        CommandRouter.sublog(app, "[yellow]Usage: open <filename>[/yellow]")
        return

    name = args[0]
    matches = list(FILES_ROOT.rglob(name))
    if not matches:
        CommandRouter.sublog(app, f"[red]No file found named {name}[/red]")
        return

    path = matches[0]
    await filetree.focus_path(path)

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)

        viewer.current_path = path
        viewer.clear()
        viewer.write(pretty)

        CommandRouter.sublog(app, f"[green]Opened {path.relative_to(FILES_ROOT)}[/green]")
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Error loading JSON:[/red] {e}")


# ─── EDIT / SAVE COMMANDS ─────────────────────────────────────────────

async def command_edit(app, args):
    """Enter inline editing mode for a file (or current one if none provided)."""
    viewer = app.query_one("#viewer", JSONViewer)
    filetree = app.query_one("#files", FileTreePanel)

    # if filename provided → open + edit immediately
    if args:
        name = args[0]
        matches = list(FILES_ROOT.rglob(name))
        if not matches:
            CommandRouter.sublog(app, f"[red]No file found named {name}[/red]")
            return

        path = matches[0]
        await filetree.focus_path(path)
        viewer.current_path = path

        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read()
            viewer.clear()
            await viewer.enter_edit_mode(text)
            CommandRouter.sublog(app, f"[cyan]Editing {path.relative_to(FILES_ROOT)}[/cyan]")
        except Exception as e:
            CommandRouter.sublog(app, f"[red]Error opening file:[/red] {e}")
        return

    # if no filename → edit current file
    if not getattr(viewer, "current_path", None):
        CommandRouter.sublog(app, "[yellow]No file open.[/yellow]")
        return

    with open(viewer.current_path, "r", encoding="utf-8-sig") as f:
        text = f.read()

    await viewer.enter_edit_mode(text)
    CommandRouter.sublog(app, "[cyan]Editing mode enabled. Type 'save' to persist changes.[/cyan]")


async def command_save(app, args):
    """Save changes made in edit mode."""
    viewer = app.query_one("#viewer", JSONViewer)
    if not getattr(viewer, "editing", False):
        CommandRouter.sublog(app, "[yellow]Not in edit mode.[/yellow]")
        return

    text = await viewer.exit_edit_mode()

    try:
        json.loads(text)
        async with aiofiles.open(viewer.current_path, "w", encoding="utf-8") as f:
            await f.write(text)
        CommandRouter.sublog(app, f"[green]Saved {viewer.current_path}[/green]")
        viewer.display_json(text)
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Error saving file:[/red] {e}")


# ─── SUBMIT COMMAND ──────────────────────────────────────────────────

async def command_submit(app, args):
    """Submit a JSON file to the Cashly API."""
    viewer = app.query_one("#viewer", JSONViewer)
    network_panel = app.query_one("#network", NetworkPanel)

    if httpx is None:
        CommandRouter.sublog(app, "[red]httpx is not available; cannot submit.[/red]")
        return

    # Determine which file to submit
    if args:
        name = args[0]
        matches = list(FILES_ROOT.rglob(name))
        if not matches:
            CommandRouter.sublog(app, f"[red]No file found named {name}[/red]")
            return
        path = matches[0]
    else:
        path = getattr(viewer, "current_path", None)
        if not path:
            CommandRouter.sublog(app, "[yellow]No file open.[/yellow]")
            return

    # Read and submit
    try:
        async with aiofiles.open(path, "r", encoding="utf-8-sig") as f:
            body = await f.read()

        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["X-API-KEY"] = API_KEY

        start = asyncio.get_event_loop().time()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(API_URL, data=body, headers=headers)
        latency = (asyncio.get_event_loop().time() - start) * 1000

        if resp.status_code < 400:
            CommandRouter.sublog(app, f"[green]Submitted successfully:[/green] {path.name}")
            network_panel.record_submission(path.name, ok=True, latency=latency)
            try:
                viewer.display_json(json.dumps(resp.json(), indent=2))
            except Exception:
                viewer.display_json(resp.text)
        else:
            CommandRouter.sublog(app, f"[red]Submit failed ({resp.status_code}):[/red] {resp.text[:120]}")
            network_panel.record_submission(path.name, ok=False, latency=latency)
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Error submitting file:[/red] {e}")
        try:
            network_panel.record_submission(path.name, ok=False)
        except Exception:
            pass
