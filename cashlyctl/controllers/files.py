from pathlib import Path
from ..widgets.jsonviewer import JSONViewer
from ..widgets.filetree import FileTreePanel

FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"

async def command_refresh(app, args):
    filetree = app.query_one("#files", FileTreePanel)
    viewer = app.query_one("#viewer", JSONViewer)
    await filetree.reload()
    viewer.write("[green]File tree refreshed.[/green]")

async def command_open(app, args):
    viewer = app.query_one("#viewer", JSONViewer)
    filetree = app.query_one("#files", FileTreePanel)

    if not args:
        viewer.write("[yellow]Usage: open <filename>[/yellow]")
        return

    name = args[0]
    matches = list(FILES_ROOT.rglob(name))
    if not matches:
        viewer.write(f"[red]No file found named {name}[/red]")
        return

    await filetree.select_path(matches[0])
    viewer.write(f"[green]Opened {matches[0].relative_to(FILES_ROOT)}[/green]")
