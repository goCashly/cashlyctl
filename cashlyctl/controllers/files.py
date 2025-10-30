from pathlib import Path
import json
from ..widgets.jsonviewer import JSONViewer
from ..widgets.filetree import FileTreePanel
from .base import CommandRouter

FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"

async def command_refresh(app, args):
    filetree = app.query_one("#files", FileTreePanel)
    viewer = app.query_one("#viewer", JSONViewer)
    await filetree.reload()
    CommandRouter.sublog(app, "[green]File tree refreshed.[/green]")

async def command_open(app, args):
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

    # Display file content in the main viewer
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        viewer.clear()
        viewer.write(pretty)
        CommandRouter.sublog(app, f"[green]Opened {path.relative_to(FILES_ROOT)}[/green]")
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Error loading JSON:[/red] {e}")
