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
    """Displays JSON with inline editing support (stable/compatible)."""

    def __init__(self, *args, **kwargs):
        super().__init__("", *args, **kwargs)
        self.can_focus = True
        self.styles.height = "100%"
        self.styles.width = "1fr"
        self.styles.overflow_y = "auto"
        self.styles.overflow_x = "hidden"
        self.current_path: str | None = None
        self.editing: bool = False
        self.editor = None  # mounted editor widget when in edit mode

    # ── View mode ───────────────────────────────────────────────
    def display_json(self, text: str) -> None:
        """Render JSON with syntax highlight; tolerate invalid JSON."""
        try:
            data = json.loads(text)
            pretty = json.dumps(data, indent=2, ensure_ascii=False)
            syntax = Syntax(pretty, "json", word_wrap=True, theme="ansi_dark")
            self.update(syntax)
        except Exception:
            # If it's not valid JSON, still show the raw text so user can fix it
            self.update(Text.from_markup("[red]Invalid JSON[/red]\n") + Text(text))
        self.editing = False

    def write(self, text: str) -> None:
        self.display_json(text)

    def clear(self) -> None:
        self.update("")

    # ── Edit mode ───────────────────────────────────────────────
    async def enter_edit_mode(self, text: str) -> None:
        """Switch to edit mode (single editor instance)."""
        if self.editing:
            return

        self.editing = True
        self.update(Text.from_markup("[yellow][EDIT MODE][/yellow]\n"))

        if HAVE_TEXTAREA:
            # Use load_text for widest Textual compatibility
            self.editor = TextArea()
            # guard: some builds want load_text, some accept .value
            try:
                self.editor.load_text(text)
            except Exception:
                try:
                    self.editor.value = text  # type: ignore[attr-defined]
                except Exception:
                    pass
        else:
            from textual.widgets import Input
            self.editor = Input(value=text)

        # mount synchronously; avoid awaits for older versions
        self.mount(self.editor)
        # focus best-effort
        try:
            await self.editor.focus()
        except Exception:
            try:
                self.app.set_focus(self.editor)
            except Exception:
                pass

    async def exit_edit_mode(self) -> str:
        """Exit edit mode and return the text that was being edited."""
        if not self.editing or not self.editor:
            return ""

        # get content robustly across Textual variants
        content = ""
        try:
            content = getattr(self.editor, "text")
        except Exception:
            pass
        if not content:
            content = getattr(self.editor, "value", "")

        # remove only the editor; keep this container alive
        try:
            self.editor.remove()
        except Exception:
            pass

        self.editor = None
        self.editing = False
        return content
