from typing import Callable, Dict, Any
import inspect

class CommandRouter:
    """Registry and executor for CLI commands."""

    def __init__(self):
        self.commands: Dict[str, Callable[[Any, list[str]], Any]] = {}

    def register(self, name: str, func: Callable[[Any, list[str]], Any]):
        """Register a command handler by name."""
        self.commands[name.lower()] = func

    def register_module(self, module):
        """Register all functions from a module that have a 'command_' prefix."""
        for attr in dir(module):
            if attr.startswith("command_"):
                name = attr.replace("command_", "")
                func = getattr(module, attr)
                if callable(func):
                    self.register(name, func)

    async def execute(self, app, cmdline: str):
        """Parse and execute a command string, routing output to log pane."""
        viewer = app.query_one("#viewer")
        logview = None
        try:
            logview = app.query_one("#log")
        except Exception:
            pass  # if not present yet

        parts = cmdline.strip().split()
        if not parts:
            return

        cmd, args = parts[0].lower(), parts[1:]
        func = self.commands.get(cmd)

        # Echo command to log
        if logview:
            logview.write(f"[dim]> {cmdline}[/dim]")

        if not func:
            if logview:
                logview.write(f"[red]Unknown command:[/red] {cmd}")
            else:
                viewer.write(f"[red]Unknown command:[/red] {cmd}")
            return

        try:
            result = func(app, args)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            target = logview or viewer
            target.write(f"[red]Command error:[/red] {e}")
