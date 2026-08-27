"""Regression tests for issue #61: a stdio MCP session created from a
short-lived task (e.g. a /reconnect request handler) must not die with the
task that initiated the connect. The session contexts must be entered and
exited in one dedicated runner task owned by the manager."""

import asyncio
import types
from unittest.mock import patch

from src.mcp_manager import McpManager


class _State:
    enter_task = None
    exit_task = None
    initialized = False


class FakeStdioClient:
    def __init__(self, params):
        pass

    async def __aenter__(self):
        _State.enter_task = asyncio.current_task()
        return (object(), object())

    async def __aexit__(self, *exc):
        _State.exit_task = asyncio.current_task()
        return False


class FakeClientSession:
    def __init__(self, read_stream, write_stream):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        _State.initialized = True

    async def list_tools(self):
        return types.SimpleNamespace(tools=[])


def _reset_state():
    _State.enter_task = None
    _State.exit_task = None
    _State.initialized = False


def _patched():
    return (
        patch("mcp.client.stdio.stdio_client", FakeStdioClient),
        patch("mcp.ClientSession", FakeClientSession),
    )


def test_stdio_session_lives_in_dedicated_runner_task():
    _reset_state()
    mgr = McpManager()

    async def scenario():
        # Simulate a request handler: connect from a task that exits right after.
        async def request_handler():
            return await mgr.connect_server("srv1", "fake", "stdio", command="fake-cmd", args=[])

        handler_task = asyncio.create_task(request_handler())
        ok = await handler_task
        assert ok is True
        assert _State.initialized is True

        # The contexts were entered in a dedicated runner task, not the
        # request handler's task — and that runner is still alive after the
        # handler task has completed.
        assert _State.enter_task is not None
        assert _State.enter_task is not handler_task
        assert handler_task.done()
        assert _State.exit_task is None  # session still open
        assert mgr.get_server_status("srv1")["status"] == "connected"

        # Disconnect closes the contexts in the SAME task that entered them.
        await mgr.disconnect_server("srv1")
        assert _State.exit_task is _State.enter_task
        assert _State.enter_task.done()

    p1, p2 = _patched()
    with p1, p2:
        asyncio.run(scenario())


def test_stdio_connect_failure_propagates_and_cleans_up():
    _reset_state()
    mgr = McpManager()

    class FailingSession(FakeClientSession):
        async def initialize(self):
            raise RuntimeError("handshake exploded")

    async def scenario():
        ok = await mgr.connect_server("srv2", "fake", "stdio", command="fake-cmd", args=[])
        assert ok is False
        assert mgr.get_server_status("srv2")["status"] == "error"
        # The runner task must have exited its contexts on failure.
        assert _State.exit_task is _State.enter_task

    with patch("mcp.client.stdio.stdio_client", FakeStdioClient), \
         patch("mcp.ClientSession", FailingSession):
        asyncio.run(scenario())
