import asyncio
import os
import httpx
from datetime import datetime
from dotenv import load_dotenv
from textual.widgets import Static
from rich.text import Text
from rich.panel import Panel
from rich import box

load_dotenv()


class NetworkPanel(Static):
    """Displays Cashly API connection status and recent network events."""

    def __init__(self, api_url: str, *args, **kwargs):
        super().__init__("", *args, **kwargs)
        self.api_url = api_url
        self.api_key = os.getenv("CASHLY_API_KEY")
        self.status_text = "Checking..."
        self.latency_ms = None
        self.last_check = None
        self.status_ok = False
        self.last_action = None
        self.refresh_interval = 10  # seconds

    async def on_mount(self) -> None:
        self.set_interval(self.refresh_interval, self.check_status)
        await self.check_status()

    async def check_status(self):
        """Ping the Cashly API using POST and X-API-KEY header."""
        start = datetime.now()
        try:
            headers = {"X-API-KEY": self.api_key} if self.api_key else {}
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.post(self.api_url, json={}, headers=headers)
            self.latency_ms = (datetime.now() - start).total_seconds() * 1000
            self.status_ok = 200 <= resp.status_code < 300
            self.status_text = f"{resp.status_code} {resp.reason_phrase}"
        except Exception as e:
            self.status_ok = False
            self.status_text = str(e)[:60]
            self.latency_ms = None
        self.last_check = datetime.now().strftime("%H:%M:%S")
        self.refresh()

    def record_submission(self, filename: str, ok: bool, latency: float | None = None):
        """Record a network event after a submit."""
        self.last_action = f"{'✅' if ok else '❌'} POST {filename}"
        if latency:
            self.latency_ms = latency
        self.status_ok = ok
        self.status_text = "200 OK" if ok else "Error"
        self.last_check = datetime.now().strftime("%H:%M:%S")
        self.refresh()

    def render(self):
        color = "green" if self.status_ok else "red"
        latency = f"{self.latency_ms:.0f} ms" if self.latency_ms else "--"
        body = Text(
            f"Status: [{color}]{self.status_text}[/{color}]\n"
            f"Latency: {latency}\n"
            f"Last Check: {self.last_check or '--'}\n"
            f"Last Action: {self.last_action or '--'}",
            justify="left",
        )
        return Panel(
            body,
            title="Cashly API",
            box=box.ROUNDED,
            border_style=color,
            padding=(0, 1),
        )
