from textual.widgets import DirectoryTree
from pathlib import Path
import json
from .jsonviewer import JSONViewer

FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"


class FileTreePanel(DirectoryTree):
    """Left-side tree view listing JSON files and folders under FILES/."""

    def __init__(self, *args, **kwargs):
        super().__init__(FILES_ROOT, *args, **kwargs)

    async def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Load the selected JSON file and display its content in the viewer."""
        viewer = self.app.query_one("#viewer", JSONViewer)
        path = event.path
        viewer.clear()

        # Only allow .json files
        if path.suffix.lower() != ".json":
            viewer.write(f"[yellow]Not a JSON file:[/yellow] {path.name}")
            return

        try:
            # Handle UTF-8 BOM and normal UTF-8 encodings
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)

            formatted = json.dumps(data, indent=2)
            viewer.write(formatted)

        except json.JSONDecodeError as e:
            viewer.write(f"[red]Error reading JSON:[/red] Invalid format → {e}")
        except Exception as e:
            viewer.write(f"[red]Unexpected error:[/red] {e}")

    async def focus_path(self, path: Path) -> None:
        """Expand the tree and focus on a given file path."""
        viewer = self.app.query_one("#viewer", JSONViewer)
        try:
            node = await self.expand_to_path(path)
            if node:
                # move focus to the node so it’s visually selected
                self.focus_node(node)
                self.scroll_to_node(node)
                # trigger file load
                await self.on_directory_tree_file_selected(
                    type("FileSelected", (), {"path": path})()
                )
        except Exception as e:
            viewer.write(f"[red]Could not open:[/red] {e}")
