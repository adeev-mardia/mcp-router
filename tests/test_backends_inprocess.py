"""Core proof that routing works against a REAL FastMCP server object, not a
fake: InProcessBackend wraps a genuine `mcp.server.fastmcp.FastMCP` instance
and must correctly proxy list_tools/call_tool end to end."""

from __future__ import annotations

import pytest

from mcp_router.backends import BackendError, InProcessBackend


@pytest.mark.asyncio
async def test_list_tools_reflects_real_fastmcp_tools(notes_fastmcp):
    backend = InProcessBackend("notes", notes_fastmcp)
    tools = await backend.list_tools()
    names = {t.name for t in tools}
    assert names == {"add_note", "get_note", "list_notes"}
    # descriptions/schemas actually come from the real FastMCP tool manager
    add_note = next(t for t in tools if t.name == "add_note")
    assert "note" in add_note.description.lower()
    assert add_note.input_schema.get("type") == "object"
    assert {"title", "body"} <= set(add_note.input_schema.get("properties", {}))


@pytest.mark.asyncio
async def test_call_tool_round_trip_against_real_server(notes_fastmcp):
    backend = InProcessBackend("notes", notes_fastmcp)

    result = await backend.call_tool("add_note", {"title": "todo", "body": "buy milk"})
    assert result.is_error is False
    assert any("todo" in (c.text or "") for c in result.content)

    result2 = await backend.call_tool("get_note", {"title": "todo"})
    assert any("buy milk" in (c.text or "") for c in result2.content)

    result3 = await backend.call_tool("list_notes", {})
    assert any("todo" in (c.text or "") for c in result3.content)


@pytest.mark.asyncio
async def test_call_tool_that_raises_inside_the_real_tool_is_isolated(notes_fastmcp):
    backend = InProcessBackend("notes", notes_fastmcp)
    with pytest.raises(BackendError) as exc_info:
        await backend.call_tool("get_note", {"title": "does-not-exist"})
    assert exc_info.value.backend_name == "notes"
    assert exc_info.value.tool_name == "get_note"


@pytest.mark.asyncio
async def test_call_unknown_tool_raises_backend_error(notes_fastmcp):
    backend = InProcessBackend("notes", notes_fastmcp)
    with pytest.raises(BackendError):
        await backend.call_tool("no_such_tool", {})


@pytest.mark.asyncio
async def test_two_independent_real_backends_stay_isolated(notes_fastmcp, calculator_fastmcp):
    notes = InProcessBackend("notes", notes_fastmcp)
    calc = InProcessBackend("calculator", calculator_fastmcp)

    notes_tools = {t.name for t in await notes.list_tools()}
    calc_tools = {t.name for t in await calc.list_tools()}
    assert notes_tools.isdisjoint(calc_tools)

    result = await calc.call_tool("add", {"a": 2, "b": 3})
    assert any("5" in (c.text or "") for c in result.content)
