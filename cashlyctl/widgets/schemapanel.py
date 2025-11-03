from textual.widget import Widget
from textual.reactive import reactive
from textual import events
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
import httpx
import asyncio


class SchemaPanel(Widget):
    """Displays a compact Neo4j schema map fetched via Cashly graph-query endpoint."""

    content: reactive[str] = reactive("[grey62]Loading schema...[/grey62]")

    async def on_mount(self) -> None:
        # fetch once after a short delay so the UI draws first
        await asyncio.sleep(0.3)
        await self.refresh_schema()

    async def refresh_schema(self) -> None:
        """Run CALL db.schema.visualization() and format it."""
        cypher = "CALL db.schema.visualization()"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    "http://ec2-18-191-189-128.us-east-2.compute.amazonaws.com:8000/v1/graph-query",
                    json={"query": cypher},
                )
                resp.raise_for_status()
                data = resp.json()
            self.content = self._format_schema(data)
        except Exception as e:
            self.content = f"[red]Schema unavailable:[/red] {e}"

    def _format_schema(self, data: dict) -> RenderableType:
        """Convert query result JSON to a Rich table."""
        result = data.get("result") or data.get("data") or data
        if not result:
            return "[yellow]No schema data returned[/yellow]"

        nodes = result.get("nodes", [])
        rels = result.get("relationships", [])

        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Node")
        table.add_column("Outgoing Relations")

        for node in nodes:
            name = node.get("name") or node.get("labels", ["?"])[0]
            out = [
                f"[cyan]{r['type']}[/cyan] → {r['endNode']}"
                for r in rels
                if r.get("startNode") == name
            ]
            table.add_row(f"[bold]{name}[/bold]", "\n".join(out) or "—")

        return table

    def render(self) -> RenderableType:
        return Panel(self.content, title="[bold]Graph Schema[/bold]", border_style="magenta")

    async def on_key(self, event: events.Key) -> None:
        """Press R to refresh schema."""
        if event.key.lower() == "r":
            await self.refresh_schema()
