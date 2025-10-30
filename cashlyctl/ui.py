from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input
from textual.events import Key

from .widgets.filetree import FileTreePanel
from .widgets.jsonviewer import JSONViewer
from .widgets.logview import LogView
from .widgets.networkpanel import NetworkPanel
from .controllers import build_router


class CashlyCTL(App):
    CSS_PATH = "theme.tcss"
    BINDINGS = [("q", "quit", "Quit")]

    def on_mount(self):
        self.router = build_router()

    def compose(self) -> ComposeResult:
        """Build the layout of the TUI."""
        yield Input(placeholder="Command line", id="header")
        with Horizontal(id="body"):
            yield FileTreePanel(id="files")
            yield JSONViewer(id="viewer")
            with Vertical(id="right"):
                yield NetworkPanel("https://crm-api.gocashly.io/v1/submit", id="network")
                yield LogView(id="log")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Execute commands typed into the header input."""
        cmdline = event.value.strip()
        event.input.value = ""
        if cmdline:
            await self.router.execute(self, cmdline)

    # ─── Keyboard Shortcuts ────────────────────────────────────────────
    async def on_key(self, event: Key) -> None:
        """Handle Ctrl+S, Ctrl+X, and Esc during edit mode."""
        viewer = self.query_one("#viewer")

        # if we're not inside edit mode, ignore
        if not getattr(viewer, "editing", False):
            return

        k = event.key.lower()
        is_ctrl_s = (k in ("ctrl+s", "ctrl_s")) or (k == "s" and event.ctrl)
        is_ctrl_x = (k in ("ctrl+x", "ctrl_x")) or (k == "x" and event.ctrl)
        is_escape = k == "escape"

        # ─── Save (Ctrl+S)
        if is_ctrl_s:
            event.stop()
            from .controllers.files import command_save
            await command_save(self, [])
            self.set_focus(self.query_one("#header"))
            return

        # ─── Discard (Ctrl+X)
        if is_ctrl_x:
            event.stop()
            from .controllers.base import CommandRouter
            await viewer.exit_edit_mode()
            CommandRouter.sublog(self, "[yellow]Edit closed without saving.[/yellow]")

            # reload original JSON
            try:
                with open(viewer.current_path, "r", encoding="utf-8-sig") as f:
                    text = f.read()
                viewer.display_json(text)
            except Exception as e:
                CommandRouter.sublog(self, f"[red]Error reloading file:[/red] {e}")

            self.set_focus(self.query_one("#header"))
            return

        # ─── Cancel (Esc)
        if is_escape:
            event.stop()
            from .controllers.base import CommandRouter
            await viewer.exit_edit_mode()
            CommandRouter.sublog(self, "[yellow]Edit cancelled.[/yellow]")
            self.set_focus(self.query_one("#header"))
            return
