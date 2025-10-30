from .base import CommandRouter
from ..widgets.jsonviewer import JSONViewer

HELP_TEXT = """
[b][cyan]Available Commands[/cyan][/b]

[green]refresh[/green]           - Reload the file tree
[green]open <file>[/green]        - Open and display a JSON file
[green]edit <file>[/green]        - Edit a JSON file inline
[green]save[/green]               - Save the current file
[green]submit [file][/green]      - Send JSON to the Cashly API
[green]clear[/green]              - Clear viewer
[green]help[/green]               - Show this help menu

[b][cyan]Keyboard Shortcuts[/cyan][/b]
Ctrl+S           - Save current edits
Ctrl+X           - Close current edit
Esc              - Cancel edit
Q                - Quit
"""

async def command_help(app, args):
    """Display available commands in the main viewer."""
    viewer = app.query_one("#viewer", JSONViewer)
    viewer.update(HELP_TEXT)
    CommandRouter.sublog(app, "[cyan]Displayed help menu.[/cyan]")
