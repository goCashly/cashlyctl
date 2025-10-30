from typing import Callable, Dict, Any, Awaitable
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
        """Parse and execute a command string."""
        viewer = app.query_one("#viewer")
        parts = cmdline.strip().split()
        if not parts:
            return

        cmd, args = parts[0].lower(), parts[1:]
        func = self.commands.get(cmd)
        if not func:
            viewer.write(f"[red]Unknown command:[/red] {cmd}")
            return

        try:
            result = func(app, args)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            viewer.write(f"[red]Command error:[/red] {e}")
