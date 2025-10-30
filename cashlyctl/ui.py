from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input

from .widgets.filetree import FileTreePanel
from .widgets.jsonviewer import JSONViewer
from .widgets.keyhelp import KeyHelp
from .controllers import build_router


class CashlyCTL(App):
    """Main TUI for exploring JSON files in FILES/."""
    CSS_PATH = "theme.tcss"
    BINDINGS = [("q", "quit", "Quit")]

    def on_mount(self):
        self.router = build_router()

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Command line", id="header")
        with Horizontal(id="body"):
            yield FileTreePanel(id="files")
            yield JSONViewer(id="viewer")
            yield KeyHelp(id="commands")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmdline = event.value.strip()
        event.input.value = ""
        if cmdline:
            await self.router.execute(self, cmdline)
