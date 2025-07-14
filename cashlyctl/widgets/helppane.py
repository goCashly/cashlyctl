"""
HelpPane widget: top panel displaying the ASCII logo, available commands,
key bindings, and a rolling job history (last 10 commands).

Renders using Rich markup. History is updated by assigning to the
`history` reactive list from the parent App (e.g., `app.help_pane.history = [...]`).
"""

from __future__ import annotations

from typing import List

from pyfiglet import Figlet
from rich.pretty import pretty_repr
from textual.reactive import reactive
from textual.widgets import Static

__all__ = ["HelpPane"]


class HelpPane(Static):
    """Static widget that redraws when its `history` reactive changes."""

    history: reactive[List[str]] = reactive([], layout=True)

    def __init__(self, **kw):
        super().__init__("", markup=True, **kw)
        # Render the FIGlet logo once; reuse on every draw.
        self._logo = Figlet(font="slant").renderText("CASHLY TECH SERVICES")

    # ------------------------------------------------------------------ #
    # Lifecycle hooks                                                    #
    # ------------------------------------------------------------------ #
    def on_mount(self) -> None:  # noqa: D401
        """Initial render with no history."""
        self.update(self._render_content([]))

    def watch_history(self, history: List[str]) -> None:  # noqa: D401
        """Re‑render whenever history changes (Textual reactive hook)."""
        self.update(self._render_content(history))

    # ------------------------------------------------------------------ #
    # Render helper                                                      #
    # ------------------------------------------------------------------ #
    def _render_content(self, history: List[str]) -> str:
        from cashlyctl.cli import app as typer_app  # late import to avoid cycles

        available_cmds = ", ".join(sorted(typer_app.registered_commands))
        recent = "\n".join(
            f"[bold cyan]{idx + 1:>2}[/] {pretty_repr(cmd)}"
            for idx, cmd in enumerate(reversed(history[-10:]))
        ) or "— none yet —"

        return (
            f"[bold cyan]{self._logo}[/]\n"
            "[bold]Available commands:[/]\n"
            f"{available_cmds}\n\n"
            "[bold]Recent jobs:[/]\n"
            f"{recent}"
        )
