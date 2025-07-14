"""
CommandBar widget: a single-line input that stays fixed at the bottom of the TUI.

Features
--------
* Inherits from textual.widgets.Input.
* Key bindings:
    - Up / Down arrows cycle through command history (the actual history
      list is managed by the parent App; we only emit synthetic Key events
      so the App can handle them globally).
    - Escape clears the current line.

Usage
-----
from widgets.commandbar import CommandBar
"""

from __future__ import annotations

from textual import events
from textual.widgets import Input

__all__ = ["CommandBar"]


class CommandBar(Input):
    """Persistent one‑liner command prompt at the bottom of the screen."""

    BINDINGS = [
        ("up", "history_prev", "Prev cmd"),
        ("down", "history_next", "Next cmd"),
        ("escape", "clear_line", "Clear"),
    ]

    # --------------------------------------------------------------------- #
    # Key‑binding actions                                                   #
    # --------------------------------------------------------------------- #
    def action_history_prev(self) -> None:
        """Emit a synthetic 'up' key event so the parent App can handle it."""
        self.post_message(events.Key(self, "up"))

    def action_history_next(self) -> None:
        """Emit a synthetic 'down' key event so the parent App can handle it."""
        self.post_message(events.Key(self, "down"))

    def action_clear_line(self) -> None:
        """Clear the current input."""
        self.value = ""