from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input

from .widgets.filetree import FileTreePanel
from .widgets.jsonviewer import JSONViewer
from .widgets.keyhelp import KeyHelp
from .widgets.logview import LogView
from .controllers import build_router


class CashlyCTL(App):
    CSS_PATH = "theme.tcss"
    BINDINGS = [("q", "quit", "Quit")]

    def on_mount(self):
        self.router = build_router()

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Command line", id="header")
        with Horizontal(id="body"):
            yield FileTreePanel(id="files")
            yield JSONViewer(id="viewer")
            with Vertical(id="right"):
                yield KeyHelp(id="commands")
                yield LogView(id="log")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmdline = event.value.strip()
        event.input.value = ""
        if cmdline:
            await self.router.execute(self, cmdline)
