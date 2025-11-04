from textual.widget import Widget
from textual.reactive import reactive
from textual import events
from rich.console import RenderableType
from rich.panel import Panel
import httpx
import asyncio
import os
from dotenv import load_dotenv
import json
from typing import Dict, Iterable, List, Tuple

load_dotenv()


class SchemaPanel(Widget):
    """Displays a compact Neo4j schema map fetched via Cashly graph-query endpoint."""

    content: reactive[str] = reactive("[grey62]Loading schema...[/grey62]")

    async def on_mount(self) -> None:
        await asyncio.sleep(0.3)
        await self.refresh_schema()

    async def refresh_schema(self) -> None:
        """Run CALL db.schema.visualization() and format it."""
        cypher = "CALL db.schema.visualization()"
        api_key = os.getenv("CASHLY_API_KEY")

        if not api_key:
            self.content = "[red]Missing CASHLY_API_KEY in .env[/red]"
            return

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://crm-api.gocashly.io/v1/graph-query",
                    json={"query": cypher},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

            # print to terminal for inspection
            print("\n🔍 Raw graph-query payload:\n", json.dumps(data, indent=2)[:1200])

            self.content = self._format_schema(data)
        except Exception as e:
            self.content = f"[red]Schema unavailable:[/red] {e}"

    def _extract_result(self, data: dict) -> Tuple[List[dict], List[dict]]:
        """Safely unwrap the deeply nested response."""

        nodes: List[dict] = []
        rels: List[dict] = []
        seen_nodes: set[str] = set()
        seen_rels: set[str] = set()

        def node_key(node: dict) -> str:
            if "id" in node:
                return f"node:{node['id']}"
            return f"node:{json.dumps(node, sort_keys=True)}"

        def rel_key(rel: dict) -> str:
            if "id" in rel:
                return f"rel:{rel['id']}"
            return "rel:" + json.dumps(rel, sort_keys=True)

        def walk(obj):
            if isinstance(obj, dict):
                maybe_nodes = obj.get("nodes")
                if isinstance(maybe_nodes, list):
                    for node in maybe_nodes:
                        key = node_key(node)
                        if key not in seen_nodes:
                            seen_nodes.add(key)
                            nodes.append(node)

                maybe_rels = obj.get("relationships")
                if isinstance(maybe_rels, list):
                    for rel in maybe_rels:
                        key = rel_key(rel)
                        if key not in seen_rels:
                            seen_rels.add(key)
                            rels.append(rel)

                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        try:
            walk(data)
        except Exception:
            return [], []

        return nodes, rels

    def _node_style_palette(self) -> Iterable[str]:
        """Colors used to represent nodes in the ASCII map."""
        return (
            "#1f77b4",
            "#9467bd",
            "#2ca02c",
            "#ff7f0e",
            "#17becf",
            "#d62728",
        )

    def _format_schema(self, data: dict) -> str:
        """Convert query result JSON to a Bloom-like ASCII map."""
        nodes, rels = self._extract_result(data)

        if not nodes:
            return "[yellow]No schema data returned (empty nodes list)[/yellow]"

        node_map: Dict[str, dict] = {str(n.get("id")): n for n in nodes}
        node_index = {str(n.get("id")): idx for idx, n in enumerate(nodes)}
        palette = list(self._node_style_palette()) or ["#1f77b4"]

        def node_label(node: dict) -> str:
            labels = node.get("labels") or []
            if isinstance(labels, list) and labels:
                return str(labels[0])
            if "name" in node:
                return str(node["name"])
            return str(node.get("id", "?"))

        def node_token(node: dict) -> str:
            idx = node_index.get(str(node.get("id")), 0)
            color = palette[idx % len(palette)]
            label = node_label(node)
            return f"[bold white on {color}] {label} [/bold white on {color}]"

        adjacency: Dict[str, List[dict]] = {str(n.get("id")): [] for n in nodes}
        for rel in rels:
            start_node = str(rel.get("startNode"))
            adjacency.setdefault(start_node, []).append(rel)

        lines: List[str] = []

        for idx, node in enumerate(nodes):
            node_id = str(node.get("id"))
            lines.append(node_token(node))
            outgoing = adjacency.get(node_id, [])

            if outgoing:
                for rel_idx, rel in enumerate(outgoing):
                    branch = "├──" if rel_idx < len(outgoing) - 1 else "└──"
                    rel_type = rel.get("type", "?")
                    end_node = node_map.get(str(rel.get("endNode")))
                    if end_node:
                        target_repr = node_token(end_node)
                    else:
                        target_label = str(rel.get("endNode", "?"))
                        target_repr = f"[bold white on #555555] {target_label} [/bold white on #555555]"
                    lines.append(
                        "    "
                        + f"[dim]{branch}[/dim] [cyan]{rel_type}[/cyan] ▶ "
                        + target_repr
                    )
            else:
                lines.append("    [dim]└──[/dim] [grey62]No outgoing relationships[/grey62]")

            if idx < len(nodes) - 1:
                lines.append("")

        return "\n".join(lines).rstrip()

    def render(self) -> RenderableType:
        return Panel(
            self.content,
            title="[bold magenta]Graph Schema[/bold magenta] [grey50](R to refresh)[/grey50]",
            border_style="magenta",
        )

    async def on_key(self, event: events.Key) -> None:
        if event.key.lower() == "r":
            await self.refresh_schema()
