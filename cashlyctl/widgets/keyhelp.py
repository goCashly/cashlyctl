from textual.widgets import Static


class KeyHelp(Static):
    """Right panel showing available commands."""

    def on_mount(self):
        self.update(
            "Commands:\n\n"
            "[1] View JSON\n"
            "[2] Refresh\n"
            "[Q] Quit"
        )
