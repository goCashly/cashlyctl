from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from rich.text import Text
from textual import events
from textual.widgets import DirectoryTree
from textual.widgets._tree import TreeNode

from .jsonviewer import JSONViewer

FILES_ROOT = Path(__file__).resolve().parent.parent.parent / "FILES"


class FileTreePanel(DirectoryTree):
    """Left-side tree view listing JSON files and folders under FILES/."""

    def __init__(self, *args, **kwargs):
        super().__init__(FILES_ROOT, *args, **kwargs)
        self._selected_nodes: dict[str, Path] = {}
        self._ctrl_click = False

    async def on_mouse_down(self, event: events.MouseDown) -> None:
        """Track whether Ctrl was pressed for multi-selection clicks."""

        self._ctrl_click = event.ctrl

    async def handle_key(self, event: events.Key) -> bool:  # type: ignore[override]
        ctrl_pressed = getattr(event, "ctrl", False)
        result = await super().handle_key(event)
        if ctrl_pressed and event.key.lower() in {"up", "down", "left", "right"}:
            await self._select_cursor_node(additive=True)
        return result

    async def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        """Handle file selection from mouse and keyboard interactions."""

        await self._select_node(event.node, event.path, additive=self._ctrl_click)
        self._ctrl_click = False

    async def focus_path(self, path: Path) -> None:
        """Expand the tree and focus on a given file path."""
        viewer = self.app.query_one("#viewer", JSONViewer)
        try:
            node = await self._find_node_for_path(path)
            if node:
                self.focus_node(node)
                self.scroll_to_node(node)
                await self._select_node(node, path, additive=False)
        except Exception as e:
            viewer.write(f"[red]Could not open:[/red] {e}")

    async def select_path(self, path: Path, *, additive: bool = False) -> None:
        """Programmatically select a path (used by commands/tests)."""

        node = await self._find_node_for_path(path)
        await self._select_node(node, path, additive=additive)

    async def _select_cursor_node(self, additive: bool = False) -> None:
        node = self.cursor_node
        if not node:
            return
        dir_entry = node.data
        if dir_entry is None:
            return
        path = Path(dir_entry.path)
        await self._select_node(node, path, additive=additive)

    async def _select_node(
        self, node: TreeNode | None, path: Path, *, additive: bool
    ) -> None:
        """Apply selection logic for the provided node and path."""

        if path.suffix.lower() != ".json":
            viewer = self.app.query_one("#viewer", JSONViewer)
            viewer.write(f"[yellow]Not a JSON file:[/yellow] {path.name}")
            return

        node_id = node.id if node else str(path)
        path = path.resolve()

        if not additive:
            self._clear_selection()

        if additive and node_id in self._selected_nodes:
            # toggle off when already selected
            self._selected_nodes.pop(node_id, None)
            if node:
                self._style_node(node, selected=False)
        else:
            self._selected_nodes[node_id] = path
            if node:
                self._style_node(node, selected=True)
            await self._load_into_viewer(path)

        self.app.update_selected_files(self._selected_nodes.values())

    async def _load_into_viewer(self, path: Path) -> None:
        viewer = self.app.query_one("#viewer", JSONViewer)
        viewer.clear()
        viewer.current_path = path

        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            formatted = json.dumps(data, indent=2)
            viewer.write(formatted)
        except json.JSONDecodeError as e:
            viewer.write(f"[red]Error reading JSON:[/red] Invalid format → {e}")
        except Exception as e:
            viewer.write(f"[red]Unexpected error:[/red] {e}")

    def _clear_selection(self, *, keep: Iterable[str] | None = None) -> None:
        keep_ids = set(keep or [])
        for node_id in list(self._selected_nodes.keys()):
            if node_id in keep_ids:
                continue
            node = self.get_node_by_id(node_id)
            if node is not None:
                self._style_node(node, selected=False)
            self._selected_nodes.pop(node_id, None)

    def _style_node(self, node: TreeNode, *, selected: bool) -> None:
        dir_entry = node.data
        if dir_entry is None:
            return
        label = Text(Path(dir_entry.path).name)
        if selected:
            label.stylize("bold green")
            label.append(" ✓", style="green")
        node.set_label(label)

    async def _find_node_for_path(self, path: Path) -> TreeNode | None:
        """Walk the tree to locate (and expand) the node for a given path."""

        target = Path(path).resolve()
        node = getattr(self, "root", None)
        if node is None or node.data is None:
            return None

        if Path(node.data.path).resolve() == target:
            return node

        current = node
        while True:
            await self._add_to_load_queue(current)
            for child in current.children:
                if child.data is None:
                    continue
                child_path = Path(child.data.path).resolve()
                if child_path == target:
                    return child
                try:
                    if target.is_relative_to(child_path):
                        current = child
                        break
                except ValueError:
                    continue
            else:
                return None
