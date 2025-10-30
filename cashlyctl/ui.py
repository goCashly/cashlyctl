from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input

from .widgets.filetree import FileTreePanel
from .widgets.jsonviewer import JSONViewer
from .widgets.keyhelp import KeyHelp


class CashlyCTL(App):
    """Main TUI for exploring JSON files in FILES/."""
    CSS_PATH = "theme.tcss"

    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Command", id="header")
        with Horizontal(id="body"):
            yield FileTreePanel(id="files")
            yield JSONViewer(id="viewer")
            yield KeyHelp(id="commands")
