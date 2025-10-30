from ..widgets.jsonviewer import JSONViewer

def command_clear(app, args):
    viewer = app.query_one("#viewer", JSONViewer)
    viewer.clear()
    viewer.write("[dim]Viewer cleared.[/dim]")

def command_help(app, args):
    viewer = app.query_one("#viewer", JSONViewer)
    viewer.write(
        "[cyan]Available commands:[/cyan]\n"
        "  clear            – clear the viewer\n"
        "  refresh          – reload file tree\n"
        "  open <filename>  – open a file by name\n"
        "  help             – show this help text"
    )
