from __future__ import annotations

import shlex
from pathlib import Path
from typing import List, Optional

from click.testing import CliRunner
from pyfiglet import Figlet
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Input,
    Log as TextLog,
    Static,
    Tree,
)

from .widgets.keyhelp import KeyHelp
from . import cli as cashly_cli  # noqa: E402

# --------------------------------------------------------------------------- #
# Constants & helpers                                                         #
# --------------------------------------------------------------------------- #
HISTORY_FILE = Path.home() / ".cashlyctl_history"
MAX_HISTORY = 100
FILES_ROOT = Path(__file__).resolve().parent / "FILES"
UPLOAD_COMMAND_TEMPLATE = "lender upload-file {path}"


def _load_history() -> List[str]:
    if HISTORY_FILE.exists():
        return HISTORY_FILE.read_text(encoding="utf-8").splitlines()[-MAX_HISTORY:]
    return []


def _save_history(history: List[str]) -> None:
    HISTORY_FILE.write_text("\n".join(history[-MAX_HISTORY:]), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Widgets                                                                     #
# --------------------------------------------------------------------------- #
class HelpPane(Static):
    history: reactive[List[str]] = reactive([], layout=True)

    def __init__(self, **kw):
        super().__init__("", markup=False, **kw)
        self._logo = Figlet(font="slant").renderText("CASHLY TECH SERVICES")

    def on_mount(self) -> None:
        self.update(self._render([]))

    def watch_history(self, history: List[str]) -> None:
        self.update(self._render(history))

    def _render(self, history: List[str]) -> str:
        cmds = ", ".join(sorted(cmd.name for cmd in cashly_cli.app.registered_commands))
        recent = "\n".join(
            f"{idx + 1:>2}  {h}" for idx, h in enumerate(reversed(history[-10:]))
        ) or "— none yet —"
        file_help = (
            "File browser (FILES/):\n"
            "  Ctrl+F      Focus tree\n"
            "  Ctrl+P      Focus prompt\n"
            "  ↑/↓ ←/→     Navigate\n"
            "  Enter       Expand / select\n"
            "  U           Upload highlighted file\n"
        )
        return (
            f"{self._logo}\n"
            "Available commands:\n"
            f"{cmds}\n\n"
            "Recent jobs:\n"
            f"{recent}\n\n"
            f"{file_help}"
        )


class ResultsPane(TextLog):
    def clear_and_write(self, header: str, body: str) -> None:
        self.clear()
        self.write(header)
        self.write(body)


class CommandBar(Input):
    BINDINGS = [
        ("up", "history_prev", "Prev"),
        ("down", "history_next", "Next"),
        ("escape", "clear_line", "Clear"),
    ]

    def action_history_prev(self) -> None:
        self.post_message(events.Key(self, "up"))

    def action_history_next(self) -> None:
        self.post_message(events.Key(self, "down"))

    def action_clear_line(self) -> None:
        self.value = ""


# --------------------------------------------------------------------------- #
# Modern File Tree for Textual 6.x                                            #
# --------------------------------------------------------------------------- #
class FileTree(Tree[Path]):
    """Recursive filesystem tree for Textual 6.x."""

    def __init__(self, root: Path, **kwargs):
        # Don't populate in __init__, wait for mount
        super().__init__(label=f"📂 {root.name}", data=root, id="files-tree", **kwargs)
        self.show_root = True
        self.guide_depth = 4
        self._root_path = root

    def on_mount(self) -> None:
        """Load directory structure when widget is mounted."""
        # Ensure directory exists
        self._root_path.mkdir(exist_ok=True, parents=True)
        
        # Populate the tree
        self._load_contents()

    def _load_contents(self) -> None:
        """Load all contents into the tree."""
        # Clear existing
        self.root.remove_children()
        
        # Add contents
        self._populate_node(self.root, self._root_path)
        
        # Expand root
        self.root.expand()

    def _populate_node(self, tree_node, path: Path) -> None:
        """Recursively populate a tree node."""
        if not path.exists() or not path.is_dir():
            return
        
        try:
            items = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except (PermissionError, OSError):
            return
        
        for item in items:
            # Skip hidden files
            if item.name.startswith('.'):
                continue
            
            if item.is_dir():
                # Add directory
                node = tree_node.add(f"📁 {item.name}", data=item, allow_expand=True)
                # Recursively populate
                self._populate_node(node, item)
            else:
                # Add file
                tree_node.add_leaf(f"📄 {item.name}", data=item)


# --------------------------------------------------------------------------- #
# Main Application                                                            #
# --------------------------------------------------------------------------- #
class CashlyTUI(App):
    TITLE = "CashlyCTL"
    BINDINGS = [
        ("tab", "cycle_focus", "Next"),
        ("shift+tab", "cycle_focus_back", "Prev"),
        ("ctrl+f", "focus_files", "Files"),
        ("ctrl+r", "focus_results", "Results"),
        ("ctrl+p", "focus_prompt", "Prompt"),
        ("u", "upload_selected", "Upload"),
        ("ctrl+l", "clear_results", "Clear"),
        ("f1", "toggle_help", "Help"),
        ("q", "quit", "Quit"),
    ]

    CSS = """
    HelpPane {
        height: 30%;
        border-bottom: solid gray;
        padding: 1 2;
    }

    #main {
        height: 60%;
    }

    Tree {
        width: 30%;
        min-width: 28;
        border-right: solid gray;
    }

    #content {
    }

    .right-pane {
        width: 25%;
        min-width: 20;
        border-left: solid gray;
        padding: 1 2;
    }

    ResultsPane {
        height: 70%;
        padding: 1 2;
    }

    DataTable {
        height: 30%;
        border-top: solid gray;
        padding: 0 1 1 1;
    }

    CommandBar {
        border-top: solid gray;
    }
    """

    _history: List[str]
    _history_index: int | None
    _highlighted_path: Optional[Path]

    # ----------------------------------------------------------- actions
    def action_cycle_focus(self) -> None:
        self.focus_next()

    def action_cycle_focus_back(self) -> None:
        self.focus_previous()

    def action_focus_results(self) -> None:
        self.results.focus()

    # ----------------------------------------------------------- compose
    def compose(self) -> ComposeResult:
        # Create FILES directory if it doesn't exist
        FILES_ROOT.mkdir(exist_ok=True, parents=True)
        
        # Add some debug info
        self.log(f"FILES_ROOT: {FILES_ROOT}")
        self.log(f"EXISTS: {FILES_ROOT.exists()}")
        if FILES_ROOT.exists():
            self.log(f"CONTENTS: {list(FILES_ROOT.iterdir())}")
        
        yield Vertical(
            HelpPane(id="help"),
            Horizontal(
                FileTree(FILES_ROOT),
                Vertical(
                    ResultsPane(id="results"),
                    DataTable(id="file-table"),
                    id="content",
                ),
                KeyHelp(id="key-help", classes="right-pane"),
                id="main",
            ),
            CommandBar(placeholder="> enter command …", id="cmd"),
        )

    # ----------------------------------------------------------- mount
    async def on_mount(self) -> None:
        self.help_pane: HelpPane = self.query_one("#help")
        self.results: ResultsPane = self.query_one("#results")
        self.cmd: CommandBar = self.query_one("#cmd")
        self.files_tree: FileTree = self.query_one("#files-tree")
        self.file_table: DataTable = self.query_one("#file-table")
        self.key_help: KeyHelp = self.query_one("#key-help")

        self._history = _load_history()
        self._history_index = None
        self._highlighted_path = None
        self.help_pane.history = self._history
        self._setup_file_table()

        self.set_interval(0.1, self._update_keyhelp)
        self.cmd.focus()

    def _update_keyhelp(self) -> None:
        focused = self.focused
        self.key_help.update_for(focused.id if focused else None)

    # ----------------------------------------------------------- key handling
    async def on_key(self, event: events.Key) -> None:
        if not self.cmd.has_focus or event.key not in {"up", "down"}:
            return
        if not self._history:
            return
        if self._history_index is None:
            self._history_index = len(self._history)
        self._history_index = max(
            0,
            min(
                len(self._history) - 1,
                self._history_index + (-1 if event.key == "up" else 1),
            ),
        )
        self.cmd.value = self._history[self._history_index]
        self.cmd.cursor_position = len(self.cmd.value)

    # ----------------------------------------------------------- command handling
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        if not cmd:
            return
        self.cmd.value = ""
        await self._execute_command(cmd)

    async def _process_command(self, cmd_str: str) -> None:
        if cmd_str == "help":
            cmd_str = "--help"

        args = shlex.split(cmd_str)
        from typer.main import get_command
        click_cmd = get_command(cashly_cli.app)

        runner = CliRunner()
        result = runner.invoke(
            click_cmd,
            args,
            prog_name="cashlyctl",
            standalone_mode=False,
            catch_exceptions=True,
        )

        header = f"> {cmd_str}"
        if result.exception:
            body = result.output or f"Error: {result.exception}"
        else:
            body = result.output or "✔ Command executed with no output."

        self.results.clear_and_write(header, body)

    async def _execute_command(self, cmd: str, *, record_history: bool = True) -> None:
        if cmd in {"files", "results", "prompt", "help", "quit"}:
            await self._handle_ui_command(cmd)
            return

        if record_history:
            self._history.append(cmd)
            self._history = self._history[-MAX_HISTORY:]
            _save_history(self._history)
            self.help_pane.history = self._history
            self._history_index = None

        await self._process_command(cmd)

    async def _handle_ui_command(self, cmd: str):
        match cmd:
            case "files":
                self.files_tree.focus()
            case "results":
                self.results.focus()
            case "prompt":
                self.cmd.focus()
            case "help":
                self.action_toggle_help()
            case "quit":
                self.exit()

    # ----------------------------------------------------------- app actions
    def action_clear_results(self) -> None:
        self.results.clear()

    def action_toggle_help(self) -> None:
        self.help_pane.display = not self.help_pane.display

    def action_quit(self) -> None:
        self.exit()

    def action_focus_files(self) -> None:
        self.files_tree.focus()

    def action_focus_prompt(self) -> None:
        self.cmd.focus()

    async def action_upload_selected(self) -> None:
        if not self._highlighted_path or not self._highlighted_path.is_file():
            self.bell()
            return
        cmd = UPLOAD_COMMAND_TEMPLATE.format(path=shlex.quote(str(self._highlighted_path)))
        await self._execute_command(cmd, record_history=False)

    # ----------------------------------------------------------- new tree events
    def on_tree_node_selected(self, event: Tree.NodeSelected[Path]) -> None:
        path = event.node.data
        self._set_highlighted_path(path)
        if path.is_file():
            self.bell()  # placeholder for upload trigger

    # ----------------------------------------------------------- helpers
    def _setup_file_table(self) -> None:
        if not self.file_table.columns:
            self.file_table.add_columns("Key", "Action")
        self.file_table.cursor_type = "none"
        self.file_table.zebra_stripes = True
        self._refresh_file_table()

    def _set_highlighted_path(self, path: Path | None) -> None:
        self._highlighted_path = path
        self._refresh_file_table()

    def _refresh_file_table(self) -> None:
        self.file_table.clear()
        if self._highlighted_path is not None:
            display = self._format_path(self._highlighted_path)
            if self._highlighted_path.is_file():
                self.file_table.add_row("Selected", display)
            else:
                self.file_table.add_row("Folder", f"{display}/")
        else:
            self.file_table.add_row("Selected", "—")

        self.file_table.add_row("Ctrl+F", "Focus files tree")
        self.file_table.add_row("Ctrl+P", "Focus command prompt")
        self.file_table.add_row("↑/↓ ←/→", "Navigate files and folders")
        self.file_table.add_row("Enter", "Expand folders / select file")
        self.file_table.add_row("U", "Upload highlighted file")

    def _format_path(self, path: Path) -> str:
        try:
            relative = path.relative_to(FILES_ROOT)
            text = relative.as_posix()
            return text or "FILES"
        except ValueError:
            return str(path)


# --------------------------------------------------------------------------- #
# Entrypoint                                                                  #
# --------------------------------------------------------------------------- #
def run() -> None:
    CashlyTUI().run()


if __name__ == "__main__":
    run()
