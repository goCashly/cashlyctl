from __future__ import annotations

# Try to import TextLog (newer Textual versions)
try:
    from textual.widgets import TextLog  # type: ignore[attr-defined]
    HAVE_TEXTLOG = True
except Exception:
    HAVE_TEXTLOG = False


if HAVE_TEXTLOG:
    class LogView(TextLog):
        """Bottom-right panel showing logs and command feedback."""
        def __init__(self, *args, **kwargs):
            super().__init__(
                *args,
                wrap=True,
                highlight=True,
                markup=True,
                **kwargs
            )
            self.styles.height = "1fr"
            self.styles.width = "100%"
            self.styles.border_top = ("solid", "#586e75")

        def log(self, text: str):
            self.write(text)

else:
    # Fallback: use Static with markup rendering
    from textual.widgets import Static
    from rich.text import Text

    class LogView(Static):
        """Fallback version of LogView using Rich Text."""
        def __init__(self, *args, **kwargs):
            super().__init__("", *args, **kwargs)
            self.can_focus = True
            self.styles.height = "1fr"
            self.styles.width = "100%"
            self.styles.border_top = ("solid", "#586e75")
            self.styles.overflow_y = "auto"

        def write(self, text: str):
            """Append a line of text with Rich markup."""
            existing = self.renderable or Text()
            new_line = Text.from_markup(text + "\n")
            existing.append(new_line)
            self.update(existing)

        def log(self, text: str):
            self.write(text)
