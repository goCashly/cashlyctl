from __future__ import annotations

from pathlib import Path
from typing import Iterable

from rich.text import Text
from textual.widgets import Static

FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"


class SelectedFilesView(Static):
    """Widget that renders the list of files selected in the tree."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__("", *args, **kwargs)
        self.can_focus = False
        self.paths: list[Path] = []
        self.styles.height = "auto"
        self.styles.width = "100%"
        self.styles.border_top = ("solid", "#586e75")
        self.styles.padding = (1, 1)

    def update_selection(self, paths: Iterable[Path]) -> None:
        """Render the current selection."""

        self.paths = [Path(p) for p in paths]
        if not self.paths:
            text = Text.from_markup("[dim]No files selected.[/dim]")
        else:
            text = Text.from_markup(
                f"[b]Selected files ({len(self.paths)}):[/b]\n"
            )
            for path in self.paths:
                display = self._format_path(path)
                text.append(f"• {display}\n", style="cyan")

        self.update(text)

    def _format_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(FILES_ROOT))
        except ValueError:
            return path.name

