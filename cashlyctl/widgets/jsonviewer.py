# Console-like viewer that renders logs (with Rich markup) and JSON.
from __future__ import annotations

import json

try:
    from textual.widgets import TextLog  # type: ignore[attr-defined]
    HAVE_TEXTLOG = True
except Exception:
    HAVE_TEXTLOG = False


if HAVE_TEXTLOG:
    # Textual has TextLog: use it with markup + wrapping.
    class JSONViewer(TextLog):
        """Center pane that logs messages with markup and can show JSON."""
        def __init__(self, *args, **kwargs):
            super().__init__(
                *args,
                wrap=True,
                highlight=True,   # enable ANSI/color
                markup=True,      # interpret [red]...[/red]
                **kwargs
            )
            self.styles.height = "100%"
            self.styles.width = "1fr"
            self.styles.overflow_y = "auto"

        def write(self, text: str) -> None:  # log line with markup
            super().write(text)

        def clear(self) -> None:
            super().clear()

        # Optional helper you can call later
        def write_json(self, obj_or_str) -> None:
            try:
                if isinstance(obj_or_str, str):
                    data = json.loads(obj_or_str)
                else:
                    data = obj_or_str
                pretty = json.dumps(data, indent=2, ensure_ascii=False)
                # TextLog doesn't do JSON syntax coloring; still readable & wrapped
                self.write(pretty)
            except Exception:
                # Fallback to raw text if not valid JSON
                self.write(str(obj_or_str))

else:
    # Fallback: Static + Rich rendering with proper markup and JSON syntax
    from textual.widgets import Static
    from rich.text import Text
    from rich.syntax import Syntax

    class JSONViewer(Static):
        """Center pane with markup text and JSON syntax highlighting."""
        def __init__(self, *args, **kwargs):
            super().__init__("", *args, **kwargs)
            self.can_focus = True
            self.styles.height = "100%"
            self.styles.width = "1fr"
            self.styles.overflow_y = "auto"
            self.styles.overflow_x = "hidden"

        def _render_markup(self, text: str):
            self.update(Text.from_markup(text))

        def _render_json(self, text: str):
            self.update(Syntax(text, "json", word_wrap=True, line_numbers=False, theme="ansi_dark"))

        def write(self, text: str) -> None:
            """
            If 'text' is valid JSON -> pretty, highlighted JSON.
            Else -> treat as Rich markup (so [red]..[/red] renders as color).
            """
            try:
                data = json.loads(text)
                pretty = json.dumps(data, indent=2, ensure_ascii=False)
                self._render_json(pretty)
            except Exception:
                self._render_markup(text)

        def write_json(self, obj_or_str) -> None:
            try:
                if isinstance(obj_or_str, str):
                    data = json.loads(obj_or_str)
                else:
                    data = obj_or_str
                pretty = json.dumps(data, indent=2, ensure_ascii=False)
                self._render_json(pretty)
            except Exception:
                self._render_markup(str(obj_or_str))

        def clear(self) -> None:
            self.update("")
