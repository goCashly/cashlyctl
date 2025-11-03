from textual.widget import Widget
from textual.reactive import reactive
from textual import events
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
import httpx
import asyncio
import os
from dotenv import load_dotenv
import json

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

    def _extract_result(self, data: dict):
        """Safely unwrap the deeply nested response."""
        try:
            inner = (
                data.get("response", {})
                .get("response", [{}])[0]
                .get("results", {})
                .get("results", [{}])[0]
            )
            nodes = inner.get("nodes", [])
            rels = inner.get("relationships", [])
            return nodes, rels
        except Exception:
            return [], []

    def _format_schema(self, data: dict) -> RenderableType:
        """Convert query result JSON to a Rich table."""
        nodes, rels = self._extract_result(data)

        if not nodes:
            return "[yellow]No schema data returned (empty nodes list)[/yellow]"

        node_map = {n.get("id"): n for n in nodes}

        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Node")
        table.add_column("Outgoing Relations")

        for node in nodes:
            label = node.get("name") or node.get("labels", ["?"])[0]
            outgoing = []
            for rel in rels:
                if rel.get("startNode") == node.get("id"):
                    end_node = node_map.get(rel.get("endNode"))
                    end_label = end_node.get("labels", ["?"])[0] if end_node else "?"
                    rel_type = rel.get("type", "?")
                    outgoing.append(f"[cyan]{rel_type}[/cyan] → {end_label}")
            table.add_row(f"[bold]{label}[/bold]", "\n".join(outgoing) or "—")

        return table

    def render(self) -> RenderableType:
        return Panel(
            self.content,
            title="[bold magenta]Graph Schema[/bold magenta] [grey50](R to refresh)[/grey50]",
            border_style="magenta",
        )

    async def on_key(self, event: events.Key) -> None:
        if event.key.lower() == "r":
            await self.refresh_schema()
