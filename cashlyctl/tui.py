"""
textual‑based interactive shell for CashlyCTL
Compatible with:
  • Textual ~=0.58  (API: Log widget, no markup kwarg)
  • Typer  >=0.12   (new public API, no .main attribute)
  • Click  >=8.1

Launch behaviour
----------------
Running `cashlyctl` *without* a sub‑command starts this TUI (handled in
cli.py).  All Typer/Click commands registered in `cashlyctl.cli.app`
can be executed in the bottom prompt.  Command history is persisted to
`~/.cashlyctl_history`.
"""

from __future__ import annotations

import io
import shlex
from contextlib import redirect_stdout
from pathlib import Path
from typing import List

from click.testing import CliRunner
from pyfiglet import Figlet
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Input, Log as TextLog, Static

# --------------------------------------------------------------------------- #
# Import Typer root app (late import to avoid circular refs)                  #
# --------------------------------------------------------------------------- #
from . import cli as cashly_cli  # noqa: E402

# --------------------------------------------------------------------------- #
# Constants & helpers                                                         #
# --------------------------------------------------------------------------- #
HISTORY_FILE = Path.home() / ".cashlyctl_history"
MAX_HISTORY = 100


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
    """Displays ASCII logo, available commands, and recent job list."""

    history: reactive[List[str]] = reactive([], layout=True)

    def __init__(self, **kw):
        super().__init__("", markup=False, **kw)
        self._logo = Figlet(font="slant").renderText("CASHLY TECH SERVICES")

    def on_mount(self) -> None:  # noqa: D401
        self.update(self._render([]))

    def watch_history(self, history: List[str]) -> None:  # noqa: D401
        self.update(self._render(history))

    def _render(self, history: List[str]) -> str:
        cmds = ", ".join(sorted(cmd.name for cmd in cashly_cli.app.registered_commands))
        recent = "\n".join(
            f"{idx + 1:>2}  {h}" for idx, h in enumerate(reversed(history[-10:]))
        ) or "— none yet —"
        return (
            f"{self._logo}\n"
            "Available commands:\n"
            f"{cmds}\n\n"
            "Recent jobs:\n"
            f"{recent}"
        )


class ResultsPane(TextLog):
    """Scrollable pane for command results and errors."""

    def clear_and_write(self, header: str, body: str) -> None:
        self.clear()
        self.write(header)
        self.write(body)


class CommandBar(Input):
    """Bottom prompt with ↑/↓ history navigation."""

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
# Main Application                                                            #
# --------------------------------------------------------------------------- #


class CashlyTUI(App):
    TITLE = "CashlyCTL"
    BINDINGS = [
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
    ResultsPane {
        height: 60%;
        padding: 1 2;
    }
    CommandBar {
        border-top: solid gray;
    }
    """

    _history: List[str]
    _history_index: int | None

    # -------------------------------------------------------------- compose
    def compose(self) -> ComposeResult:  # noqa: D401
        yield Vertical(
            HelpPane(id="help"),
            ResultsPane(id="results"),
            CommandBar(placeholder="> enter command …", id="cmd"),
        )

    # -------------------------------------------------------------- mount
    def on_mount(self) -> None:  # noqa: D401
        self.help_pane: HelpPane = self.query_one("#help")
        self.results: ResultsPane = self.query_one("#results")
        self.cmd: CommandBar = self.query_one("#cmd")

        self._history = _load_history()
        self._history_index = None
        self.help_pane.history = self._history
        self.cmd.focus()

    # -------------------------------------------------------- key handling
    async def on_key(self, event: events.Key) -> None:  # noqa: D401
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

    # --------------------------------------------- input submitted event
    async def on_input_submitted(self, event: Input.Submitted) -> None:  # noqa: D401
        cmd = event.value.strip()
        if not cmd:
            return

        # persist history
        self._history.append(cmd)
        self._history = self._history[-MAX_HISTORY:]
        _save_history(self._history)
        self.help_pane.history = self._history
        self.cmd.value = ""  # clear prompt
        self._history_index = None

        await self._process_command(cmd)

    # ------------------------------------------------ command processing
    async def _process_command(self, cmd_str: str) -> None:
        """Execute a Typer command through Click's test runner and render output."""

        # Map bare 'help' → global help flag
        if cmd_str == "help":
            cmd_str = "--help"

        args = shlex.split(cmd_str)

        # Typer 0.12 no longer exposes .main; get the underlying Click command
        from typer.main import get_command  # lazy import to avoid cycles
        click_cmd = get_command(cashly_cli.app)

        runner = CliRunner()
        result = runner.invoke(
            click_cmd,
            args,
            prog_name="cashlyctl",
            standalone_mode=False,
            catch_exceptions=True,  # capture UsageError instead of crashing
        )

        header = f"> {cmd_str}"
        if result.exception:
            body = result.output or f"Error: {result.exception}"
        else:
            body = result.output or "✔ Command executed with no output."

        self.results.clear_and_write(header, body)

    # ------------------------------------------------ app actions
    def action_clear_results(self) -> None:  # noqa: D401
        self.results.clear()

    def action_toggle_help(self) -> None:  # noqa: D401
        self.help_pane.display = not self.help_pane.display

    def action_quit(self) -> None:  # noqa: D401
        self.exit()


# --------------------------------------------------------------------------- #
# Entrypoint                                                                  #
# --------------------------------------------------------------------------- #

def run() -> None:  # pragma: no cover
    CashlyTUI().run()


if __name__ == "__main__":  # pragma: no cover
    run()
