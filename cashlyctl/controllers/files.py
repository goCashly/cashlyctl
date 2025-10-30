from pathlib import Path
import json, aiofiles
from ..widgets.jsonviewer import JSONViewer
from ..widgets.filetree import FileTreePanel
from .base import CommandRouter

FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"

# ─── BASIC FILE COMMANDS ─────────────────────────────────────────────

async def command_refresh(app, args):
    """Reload the file tree."""
    filetree = app.query_one("#files", FileTreePanel)
    viewer = app.query_one("#viewer", JSONViewer)
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

        # remember file path for edit/save
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
        # validate JSON before writing
        json.loads(text)
        async with aiofiles.open(viewer.current_path, "w", encoding="utf-8") as f:
            await f.write(text)
        CommandRouter.sublog(app, f"[green]Saved {viewer.current_path}[/green]")
        viewer.display_json(text)
    except Exception as e:
        CommandRouter.sublog(app, f"[red]Error saving file:[/red] {e}")
