from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Input, Log, DirectoryTree
from pathlib import Path
from .ui import CashlyCTL
import json

if __name__ == "__main__":
    CashlyCTL().run()

FILES_ROOT = Path(__file__).resolve().parent.parent / "FILES"


class CashlyCTL(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #header {
        height: 3;
        background: #002b36;
        color: #93a1a1;
    }
    #body {
        layout: horizontal;
    }
    #files {
        width: 30%;
        border: solid #586e75;
    }
    #viewer {
        width: 40%;
        border: solid #586e75;
    }
    #commands {
        width: 30%;
        border: solid #586e75;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Command", id="header")
        with Horizontal(id="body"):
            yield DirectoryTree(FILES_ROOT, id="files")
            yield Log(id="viewer")
            yield Static("Commands:\n\n[1] View JSON\n[2] Refresh\n[Q] Quit", id="commands")

    async def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Triggered when a file is selected in the tree."""
        log = self.query_one("#viewer", Log)
        path = event.path
        if path.suffix.lower() == ".json":
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                formatted = json.dumps(data, indent=2)
                log.write(formatted)
            except Exception as e:
                log.write(f"[red]Error reading JSON:[/red] {e}")
        else:
            log.write(f"[yellow]Not a JSON file: {path.name}[/yellow]")


if __name__ == "__main__":
    app = CashlyCTL()
    app.run()
