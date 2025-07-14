"""
ResultsPane widget: middle panel that displays Rich‑rendered output from
executed commands.

Inherits from `textual.widgets.Log` (aliased to `TextLog` when importing)
so we can scroll and auto‑append Rich markup lines.

Helper `clear_and_write(header, body)` clears the pane and writes two
lines: a bold cyan header (usually the command string) and the body of
output (error / success / JSON pretty result).
"""

from __future__ import annotations

from textual.widgets import Log as TextLog

__all__ = ["ResultsPane"]


class ResultsPane(TextLog):
    """Scrollable log widget for command output."""

    def clear_and_write(self, header: str, body: str) -> None:
        """Utility to reset contents and add header + body."""
        self.clear()
        self.write(header, markup=True, wrap=False)
        self.write(body, markup=True, wrap=False)
