from typing import Callable, Dict, Any
import inspect

class CommandRouter:
    """Registry and executor for CLI commands."""

    def __init__(self):
        self.commands: Dict[str, Callable[[Any, list[str]], Any]] = {}

    def register(self, name: str, func: Callable[[Any, list[str]], Any]):
        self.commands[name.lower()] = func

    def register_module(self, module):
        for attr in dir(module):
            if attr.startswith("command_"):
                name = attr.replace("command_", "")
                func = getattr(module, attr)
                if callable(func):
                    self.register(name, func)

    async def execute(self, app, cmdline: str):
        viewer = app.query_one("#viewer")
        logview = None
        try:
            logview = app.query_one("#log")
        except Exception:
            pass

        parts = cmdline.strip().split()
        if not parts:
            return

        cmd, args = parts[0].lower(), parts[1:]
        func = self.commands.get(cmd)

        # echo command
        if logview:
            logview.write(f"[dim]> {cmdline}[/dim]")

        if not func:
            (logview or viewer).write(f"[red]Unknown command:[/red] {cmd}")
            return

        try:
            result = func(app, args)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            (logview or viewer).write(f"[red]Command error:[/red] {e}")

    @staticmethod
    def sublog(app, message: str, indent: int = 4):
        """Log an indented message under the last command."""
        try:
            log = app.query_one("#log")
            pad = " " * indent
            log.write(f"{pad}{message}")
        except Exception:
            pass
