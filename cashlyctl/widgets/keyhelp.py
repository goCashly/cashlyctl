from textual.widgets import Static

class KeyHelp(Static):
    """Context-sensitive key/command hints shown on the right."""

    def update_for(self, focus_id: str | None):
        focus_id = focus_id or ""
        mappings = {
            "files-tree": [
                ("↑/↓", "Navigate"),
                ("Enter", "Expand / select"),
                ("U", "Upload highlighted"),
                ("Ctrl+P", "Focus prompt"),
            ],
            "results": [
                ("Ctrl+L", "Clear output"),
                ("Ctrl+P", "Focus prompt"),
                ("Tab", "Next pane"),
            ],
            "cmd": [
                ("↑/↓", "History"),
                ("Esc", "Clear line"),
                ("Enter", "Run command"),
                ("files", "Focus files"),
                ("results", "Focus results"),
            ],
        }

        lines = "\n".join(f"{k:<10} {v}" for k, v in mappings.get(focus_id, []))
        title = (focus_id.upper() if focus_id else "FOCUS")
        self.update(f"[b]{title}[/b]\n\n{lines}")
