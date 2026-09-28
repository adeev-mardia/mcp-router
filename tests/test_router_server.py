"""Tests for the RouterMCPServer itself: the union-of-tools view, dynamic
dispatch, and the router's own meta-tools (list_backends / search_tools)."""

from __future__ import annotations

import pytest

from mcp_router.backends import BackendError, InProcessBackend
from mcp_router.registry import BackendRegistry
from mcp_router.router_server import (
    LIST_BACKENDS_TOOL,
    SEARCH_TOOLS_TOOL,
    RouterMCPServer,
)


@pytest.fixture
def router(notes_fastmcp, calculator_fastmcp) -> RouterMCPServer:
    registry = BackendRegistry()
    registry.register("notes", InProcessBackend("notes", notes_fastmcp))
    registry.register("calculator", InProcessBackend("calculator", calculator_fastmcp))
    return RouterMCPServer(registry, name="test-router")


@pytest.mark.asyncio
async def test_list_tools_returns_union_of_both_backends_plus_meta_tools(router):
    tools = await router.list_tools()
    names = {t.name for t in tools}
    assert {"notes.add_note", "notes.get_note", "notes.list_notes"} <= names
    assert {"calculator.add", "calculator.divide", "calculator.is_prime"} <= names
    assert LIST_BACKENDS_TOOL in names
    assert SEARCH_TOOLS_TOOL in names


@pytest.mark.asyncio
async def test_call_tool_dispatches_to_correct_backend(router):
    content = await router.call_tool("calculator.divide", {"a": 10, "b": 2})
    assert any("5" in c.text for c in content if c.type == "text")

    content2 = await router.call_tool("notes.add_note", {"title": "t", "body": "b"})
    assert any("t" in c.text for c in content2 if c.type == "text")


@pytest.mark.asyncio
async def test_call_tool_unknown_namespaced_tool_raises(router):
    with pytest.raises(Exception):
        await router.call_tool("calculator.not_real", {})


@pytest.mark.asyncio
async def test_meta_list_backends_reports_all_registered_backends(router):
    content = await router.call_tool(LIST_BACKENDS_TOOL, {})
    text = content[0].text
    assert "notes" in text
    assert "calculator" in text
    assert "healthy" in text


@pytest.mark.asyncio
async def test_meta_search_tools_finds_cross_backend_matches(router):
    content = await router.call_tool(SEARCH_TOOLS_TOOL, {"query": "note"})
    text = content[0].text
    assert "notes.add_note" in text


@pytest.mark.asyncio
async def test_one_backend_down_does_not_break_router_list_or_other_calls(router, flaky_backend):
    router.registry.register("flaky", flaky_backend)

    # listing tools must not raise even though 'flaky' fails internally
    tools = await router.list_tools()
    names = {t.name for t in tools}
    assert not any(n.startswith("flaky.") for n in names)
    assert "calculator.add" in names

    # calling the healthy backend still works
    content = await router.call_tool("calculator.add", {"a": 1, "b": 2})
    assert any("3" in c.text for c in content if c.type == "text")
