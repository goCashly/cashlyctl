# Compatible JSON viewer with vertical scaling and line wrapping.
from __future__ import annotations

try:
    from textual.widgets import TextLog  # type: ignore[attr-defined]
    HAVE_TEXTLOG = True
except Exception:
    HAVE_TEXTLOG = False

if HAVE_TEXTLOG:
    class JSONViewer(TextLog):
        """Central viewer panel displaying JSON content with wrapping."""
        def __init__(self, *args, **kwargs):
            super().__init__(*args, wrap=True, highlight=False, markup=False, **kwargs)
            # Ensure it expands vertically
            self.styles.height = "100%"
            self.styles.width = "1fr"

        def write(self, text: str) -> None:
            super().write(text)

        def clear(self) -> None:
            super().clear()
else:
    from textual.widgets import Static
    from rich.syntax import Syntax

    class JSONViewer(Static):
        """JSON viewer using Rich Syntax with word-wrapping (Textual<0.47 fallback)."""

        def __init__(self, *args, **kwargs):
            super().__init__("", *args, **kwargs)
            self.can_focus = True
            # Fill available space vertically and horizontally
            self.styles.height = "100%"
            self.styles.width = "1fr"
            self.styles.overflow_y = "auto"  # scrollable if needed
            self.styles.overflow_x = "hidden"

        def write(self, text: str) -> None:
            """Display JSON string with syntax highlighting and wrapping."""
            syntax = Syntax(text, "json", word_wrap=True, line_numbers=False)
            self.update(syntax)

        def clear(self) -> None:
            self.update("")
