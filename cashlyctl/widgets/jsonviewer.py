from __future__ import annotations
import json
from textual.widgets import Static
from rich.syntax import Syntax
from rich.text import Text

# Try importing TextArea if available
try:
    from textual.widgets import TextArea
    HAVE_TEXTAREA = True
except Exception:
    HAVE_TEXTAREA = False


class JSONViewer(Static):
    """Displays JSON with inline editing support."""

    def __init__(self, *args, **kwargs):
        super().__init__("", *args, **kwargs)
        self.can_focus = True
        self.styles.height = "100%"
        self.styles.width = "1fr"
        self.styles.overflow_y = "auto"
        self.styles.overflow_x = "hidden"
        self.current_path: str | None = None
        self.editing: bool = False
        self.editor = None

    # ─── View mode ─────────────────────────────────────────────
    def display_json(self, text: str):
        """Render JSON syntax highlighted view."""
        try:
            data = json.loads(text)
            pretty = json.dumps(data, indent=2, ensure_ascii=False)
            syntax = Syntax(pretty, "json", word_wrap=True, theme="ansi_dark")
            self.update(syntax)
        except Exception:
            self.update(Text.from_markup(f"[red]Invalid JSON[/red]"))
        self.editing = False

    def write(self, text: str):
        self.display_json(text)

    def clear(self):
        self.update("")

    # ─── Edit mode ─────────────────────────────────────────────
    async def enter_edit_mode(self, text: str):
        """Switch to edit mode."""
        if self.editing:
            return

        self.editing = True
        self.update(Text.from_markup("[yellow][EDIT MODE][/yellow]\n"))

        if HAVE_TEXTAREA:
            self.editor = TextArea()
            self.editor.load_text(text)
        else:
            from textual.widgets import Input
            self.editor = Input(value=text)

        # synchronous mount
        self.mount(self.editor)

        # ensure app focus if available
        try:
            self.app.set_focus(self.editor)
        except Exception:
            pass

    async def exit_edit_mode(self):
        """Exit edit mode and return the current text."""
        if not self.editing or not self.editor:
            return ""

        if HAVE_TEXTAREA:
            content = self.editor.text
        else:
            content = getattr(self.editor, "value", "")

        # remove editor safely
        try:
            self.editor.remove()
        except Exception:
            pass

        self.editor = None
        self.editing = False
        return content
