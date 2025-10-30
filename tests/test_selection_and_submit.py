import asyncio
import json
from pathlib import Path

from cashlyctl.ui import CashlyCTL
from cashlyctl.widgets.filetree import FileTreePanel
from cashlyctl.widgets.selectedfiles import SelectedFilesView
from cashlyctl.controllers import submit as submit_module

TEST_FILE_A = Path("FILES/test_data/sample_a.json")
TEST_FILE_B = Path("FILES/test_data/sample_b.json")


def test_multi_select_updates_app_state():
    async def scenario():
        app = CashlyCTL()

        async with app.run_test() as pilot:
            filetree = app.query_one("#files", FileTreePanel)

            await filetree.select_path(TEST_FILE_A)
            await filetree.select_path(TEST_FILE_B, additive=True)

            assert app.selected_file_paths == [TEST_FILE_A.resolve(), TEST_FILE_B.resolve()]

            selection_panel = app.query_one("#selected-files", SelectedFilesView)
            renderable = selection_panel.renderable
            plain = getattr(renderable, "plain", str(renderable))
            assert "sample_a.json" in plain
            assert "sample_b.json" in plain

    asyncio.run(scenario())


def test_submit_uses_selected_files(monkeypatch):
    async def scenario():
        app = CashlyCTL()

        async with app.run_test() as pilot:
            filetree = app.query_one("#files", FileTreePanel)
            await filetree.select_path(TEST_FILE_A)
            await filetree.select_path(TEST_FILE_B, additive=True)

            submit_module.API_KEY = "test-key"
            submit_module.API_URL = "https://example.test/submit"

            calls = []

            class DummyPost:
                status = 200

                def __init__(self, payload):
                    self._payload = payload

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def text(self):
                    return json.dumps({"echo": self._payload})

            class DummySession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def post(self, url, headers=None, json=None):
                    calls.append((url, json))
                    return DummyPost(json)

            monkeypatch.setattr(
                submit_module.aiohttp,
                "ClientSession",
                lambda *args, **kwargs: DummySession(),
            )

            await submit_module.command_submit(app, [])

            assert [payload for _, payload in calls] == [
                {"id": 1, "name": "sample_a"},
                {"id": 2, "name": "sample_b"},
            ]
            assert app.selected_file_paths == []

    asyncio.run(scenario())
