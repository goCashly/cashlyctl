from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input
from textual.events import Key

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

    # ─── Keyboard Shortcuts ────────────────────────────────────────────
    async def on_key(self, event: Key) -> None:
        """Handle Ctrl+S and Esc during edit mode."""
        viewer = self.query_one("#viewer")

        if not getattr(viewer, "editing", False):
            return

        key = event.key.lower()

        # Robust Ctrl+S detection across Textual versions
        ctrl_s = key in ("ctrl+s", "ctrl_s") or (
            key == "s" and getattr(event, "ctrl", False)
        )

        if ctrl_s:
            event.stop()
            from .controllers.files import command_save
            await command_save(self, [])
            header = self.query_one("#header")
            self.set_focus(header)
            return

        # Esc → Cancel editing
        if key == "escape":
            event.stop()
            await viewer.exit_edit_mode()
            from .controllers.base import CommandRouter
            CommandRouter.sublog(self, "[yellow]Edit cancelled.[/yellow]")
            header = self.query_one("#header")
            self.set_focus(header)
            return
