"""Tests for StdioBackend: the production path, where the router launches a
backend MCP server as a real subprocess and speaks MCP over stdio via the
official `mcp` SDK client session.

Subprocess-spawning tests can be flaky in constrained sandboxes, so every
test here bounds its ENTIRE body (connect, use, and teardown together, as one
`asyncio.wait_for`-wrapped coroutine so anyio's cancel scopes stay in a single
task) with a generous-but-bounded timeout. In this environment these were
verified to run reliably, so they are NOT skipped -- if you're running this
suite somewhere subprocess stdio is unreliable, these are the tests to mark
`skip` (with that reason), while InProcessBackend coverage stays fully
exercised regardless.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_router.backends import BackendError, StdioBackend

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
STARTUP_TIMEOUT = 20  # generous bound; subprocess start + MCP handshake


def _notes_backend() -> StdioBackend:
    return StdioBackend(
        "notes",
        command=sys.executable,
        args=[str(EXAMPLES_DIR / "notes_server.py")],
    )


def _calculator_backend() -> StdioBackend:
    return StdioBackend(
        "calculator",
        command=sys.executable,
        args=[str(EXAMPLES_DIR / "calculator_server.py")],
    )


@pytest.mark.asyncio
async def test_stdio_backend_lists_tools_from_real_subprocess():
    async def _run() -> None:
        backend = _notes_backend()
        try:
            tools = await backend.list_tools()
            names = {t.name for t in tools}
            assert names == {"add_note", "get_note", "list_notes"}
        finally:
            await backend.aclose()

    await asyncio.wait_for(_run(), timeout=STARTUP_TIMEOUT)


@pytest.mark.asyncio
async def test_stdio_backend_calls_tool_on_real_subprocess():
    async def _run() -> None:
        backend = _notes_backend()
        try:
            await backend.call_tool("add_note", {"title": "hello", "body": "world"})
            result = await backend.call_tool("get_note", {"title": "hello"})
            assert any("world" in (c.text or "") for c in result.content)
        finally:
            await backend.aclose()

    await asyncio.wait_for(_run(), timeout=STARTUP_TIMEOUT)


@pytest.mark.asyncio
async def test_stdio_backend_tool_error_is_isolated_and_raises_backend_error():
    async def _run() -> None:
        backend = _notes_backend()
        try:
            with pytest.raises(BackendError):
                await backend.call_tool("get_note", {"title": "does-not-exist"})
        finally:
            await backend.aclose()

    await asyncio.wait_for(_run(), timeout=STARTUP_TIMEOUT)


@pytest.mark.asyncio
async def test_stdio_backend_bad_command_raises_backend_error_not_hang():
    async def _run() -> None:
        backend = StdioBackend("bogus", command="this-executable-does-not-exist-xyz")
        try:
            with pytest.raises(BackendError):
                await backend.list_tools()
        finally:
            await backend.aclose()

    await asyncio.wait_for(_run(), timeout=STARTUP_TIMEOUT)


@pytest.mark.asyncio
async def test_two_real_subprocess_backends_via_registry():
    """End-to-end: two independently-spawned subprocess MCP servers,
    registered together and routed through BackendRegistry -- the real-world
    scenario this whole package exists for."""

    async def _run() -> None:
        from mcp_router.registry import BackendRegistry

        registry = BackendRegistry()
        notes = _notes_backend()
        calc = _calculator_backend()
        registry.register("notes", notes)
        registry.register("calculator", calc)
        try:
            tools = await registry.list_all_tools()
            namespaced = {t.namespaced_name for t in tools}
            assert "notes.add_note" in namespaced
            assert "calculator.add" in namespaced

            result = await registry.call("calculator.add", {"a": 2, "b": 3})
            assert any("5" in (c.text or "") for c in result.result.content)
        finally:
            # Close in reverse registration order (see BackendRegistry.aclose_all
            # docstring: anyio task groups must be exited in the opposite order
            # they were entered in).
            await registry.aclose_all()

    await asyncio.wait_for(_run(), timeout=STARTUP_TIMEOUT)
