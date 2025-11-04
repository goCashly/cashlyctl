import os
from datetime import datetime
from typing import Iterable, Mapping

try:  # pragma: no cover - optional dependency
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - tests run without python-dotenv
    def load_dotenv(*args, **kwargs):  # type: ignore[misc]
        return None

from rich import box
from rich.panel import Panel
from rich.text import Text
from textual.widgets import Static

try:  # pragma: no cover - optional dependency
    import httpx  # type: ignore
except Exception:  # pragma: no cover - tests run without httpx
    httpx = None

load_dotenv()


class NetworkPanel(Static):
    """Displays Cashly API connection status and recent network events."""

    def __init__(
        self,
        endpoints: str | Mapping[str, str] | Iterable[tuple[str, str]],
        *args,
        **kwargs,
    ):
        super().__init__("", *args, **kwargs)

        if isinstance(endpoints, str):
            pairs: list[tuple[str, str]] = [("Submit", endpoints)]
        elif isinstance(endpoints, Mapping):
            pairs = list(endpoints.items())
        else:
            pairs = list(endpoints)

        if not pairs:
            raise ValueError("NetworkPanel requires at least one endpoint")

        self.endpoints: list[tuple[str, str]] = pairs
        self.primary_endpoint = pairs[0][0]
        self.api_key = os.getenv("CASHLY_API_KEY")
        self.endpoint_statuses: dict[str, dict[str, object]] = {
            name: {
                "status": "Checking...",
                "ok": False,
                "latency": None,
                "last_check": None,
            }
            for name, _ in self.endpoints
        }
        self.status_ok = False
        self.last_action = None
        self.refresh_interval = 10  # seconds

    async def on_mount(self) -> None:
        self.set_interval(self.refresh_interval, self.check_status)
        await self.check_status()

    async def check_status(self):
        """Ping the Cashly API endpoints and track their health."""

        timestamp = datetime.now().strftime("%H:%M:%S")

        if httpx is None:
            for status in self.endpoint_statuses.values():
                status.update(
                    {
                        "status": "httpx unavailable",
                        "ok": False,
                        "latency": None,
                        "last_check": timestamp,
                    }
                )
            self.status_ok = False
            self.refresh()
            return

        headers = {"X-API-KEY": self.api_key} if self.api_key else {}

        async with httpx.AsyncClient(timeout=3) as client:
            for name, url in self.endpoints:
                start = datetime.now()
                payload = {}
                if "graph-query" in url:
                    payload = {"query": "RETURN 1"}

                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    latency = (datetime.now() - start).total_seconds() * 1000
                    ok = 200 <= resp.status_code < 300
                    status_text = f"{resp.status_code} {resp.reason_phrase}"
                except Exception as e:
                    latency = None
                    ok = False
                    status_text = str(e)[:60]

                status = self.endpoint_statuses[name]
                status.update(
                    {
                        "status": status_text,
                        "ok": ok,
                        "latency": latency,
                        "last_check": datetime.now().strftime("%H:%M:%S"),
                    }
                )

        self.status_ok = all(status["ok"] for status in self.endpoint_statuses.values())
        self.refresh()

    def record_submission(self, filename: str, ok: bool, latency: float | None = None):
        """Record a network event after a submit."""

        self.last_action = f"{'✅' if ok else '❌'} POST {filename}"

        primary = self.endpoint_statuses.get(self.primary_endpoint)
        if primary is not None:
            primary.update(
                {
                    "status": "200 OK" if ok else "Error",
                    "ok": ok,
                    "latency": latency,
                    "last_check": datetime.now().strftime("%H:%M:%S"),
                }
            )

        self.status_ok = all(status["ok"] for status in self.endpoint_statuses.values())
        self.refresh()

    def render(self):
        color = "green" if self.status_ok else "red"

        lines = []
        for name, status in self.endpoint_statuses.items():
            status_color = "green" if status["ok"] else "red"
            latency = status.get("latency")
            latency_text = f"{latency:.0f} ms" if isinstance(latency, (int, float)) else "--"
            last_check = status.get("last_check") or "--"
            lines.append(
                f"{name}: [{status_color}]{status['status']}[/{status_color}] | "
                f"Latency: {latency_text} | Checked: {last_check}"
            )

        lines.append(f"Last Action: {self.last_action or '--'}")

        body = Text("\n".join(lines), justify="left")
        return Panel(
            body,
            title="Cashly API",
            box=box.ROUNDED,
            border_style=color,
            padding=(0, 1),
        )
