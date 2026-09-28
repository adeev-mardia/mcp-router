"""Registry-level tests: namespacing/resolution, capability search, failure
isolation, and health tracking -- driven against real InProcessBackend
wrappers of real FastMCP servers, plus one deliberately-broken backend."""

from __future__ import annotations

import pytest

from mcp_router.backends import BackendError, InProcessBackend
from mcp_router.registry import (
    BackendNotFoundError,
    BackendRegistry,
    ToolNotFoundError,
    namespaced_tool_name,
)


@pytest.fixture
async def registry(notes_fastmcp, calculator_fastmcp) -> BackendRegistry:
    reg = BackendRegistry()
    reg.register("notes", InProcessBackend("notes", notes_fastmcp))
    reg.register("calculator", InProcessBackend("calculator", calculator_fastmcp))
    return reg


# -- namespacing / resolution ------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_tools_is_namespaced_backend_dot_tool(registry):
    tools = await registry.list_all_tools()
    namespaced = {t.namespaced_name for t in tools}
    assert "notes.add_note" in namespaced
    assert "calculator.add" in namespaced
    # every entry follows the convention exactly
    for t in tools:
        assert t.namespaced_name == namespaced_tool_name(t.backend_name, t.tool_name)


@pytest.mark.asyncio
async def test_call_with_explicit_namespaced_name_routes_to_right_backend(registry):
    result = await registry.call("calculator.add", {"a": 4, "b": 5})
    assert result.backend_name == "calculator"
    assert result.tool_name == "add"
    assert any("9" in (c.text or "") for c in result.result.content)


@pytest.mark.asyncio
async def test_call_with_unambiguous_bare_name_resolves_automatically(registry):
    result = await registry.call("add_note", {"title": "x", "body": "y"})
    assert result.backend_name == "notes"
    assert result.tool_name == "add_note"


@pytest.mark.asyncio
async def test_register_backend_name_with_dot_is_rejected(registry):
    with pytest.raises(ValueError):
        registry.register("bad.name", InProcessBackend("bad.name", object()))


@pytest.mark.asyncio
async def test_register_backend_named_router_is_rejected(registry):
    with pytest.raises(ValueError):
        registry.register("router", InProcessBackend("router", object()))


@pytest.mark.asyncio
async def test_bare_name_ambiguous_across_backends_raises(notes_fastmcp, calculator_fastmcp):
    # Both fastmcp servers happen to have a tool called "add" once we give
    # notes one too -- prove the registry refuses to guess.
    @notes_fastmcp.tool()
    def add(a: int, b: int) -> int:  # noqa: ANN001 - test helper
        return a + b

    reg = BackendRegistry()
    reg.register("notes", InProcessBackend("notes", notes_fastmcp))
    reg.register("calculator", InProcessBackend("calculator", calculator_fastmcp))

    with pytest.raises(ToolNotFoundError):
        await reg.call("add", {"a": 1, "b": 2})

    # explicit namespacing still resolves it unambiguously
    result = await reg.call("calculator.add", {"a": 1, "b": 2})
    assert result.backend_name == "calculator"


# -- calling a nonexistent backend/tool fails cleanly ------------------------


@pytest.mark.asyncio
async def test_call_nonexistent_backend_raises_backend_not_found(registry):
    with pytest.raises(BackendNotFoundError):
        await registry.call("ghost.some_tool", {})


@pytest.mark.asyncio
async def test_call_nonexistent_bare_tool_raises_tool_not_found(registry):
    with pytest.raises(ToolNotFoundError):
        await registry.call("totally_made_up_tool", {})


@pytest.mark.asyncio
async def test_call_existing_backend_nonexistent_tool_raises_backend_error(registry):
    with pytest.raises(BackendError):
        await registry.call("calculator.not_a_real_tool", {})


# -- capability search --------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tools_finds_matches_by_tool_name(registry):
    matches = await registry.search_tools("note")
    names = {t.namespaced_name for t in matches}
    assert names == {"notes.add_note", "notes.get_note", "notes.list_notes"}


@pytest.mark.asyncio
async def test_search_tools_finds_matches_by_description(registry):
    matches = await registry.search_tools("prime")
    assert any(t.namespaced_name == "calculator.is_prime" for t in matches)


@pytest.mark.asyncio
async def test_search_tools_no_match_returns_empty(registry):
    matches = await registry.search_tools("nonexistent-capability-xyz")
    assert matches == []


# -- failure isolation + health tracking -------------------------------------


@pytest.mark.asyncio
async def test_one_backend_failing_does_not_affect_others(registry, flaky_backend):
    registry.register("flaky", flaky_backend)

    with pytest.raises(BackendError):
        await registry.call("flaky.whatever", {})

    # the other, healthy backends still work fine
    result = await registry.call("calculator.add", {"a": 1, "b": 1})
    assert any("2" in (c.text or "") for c in result.result.content)


@pytest.mark.asyncio
async def test_health_tracking_reflects_backend_going_down(registry, flaky_backend):
    registry.register("flaky", flaky_backend)
    assert registry.is_healthy("flaky") is True  # unknown/default is healthy until proven otherwise

    with pytest.raises(BackendError):
        await registry.call("flaky.whatever", {})

    assert registry.is_healthy("flaky") is False
    assert registry.last_error("flaky") is not None
    assert "flaky" not in registry.healthy_backend_names()
    assert "notes" in registry.healthy_backend_names()
    assert "calculator" in registry.healthy_backend_names()


@pytest.mark.asyncio
async def test_list_all_tools_skips_unhealthy_backend_without_raising(registry, flaky_backend):
    registry.register("flaky", flaky_backend)
    # list_tools calling the flaky backend fails internally but must not
    # propagate -- it should just contribute zero tools for that backend.
    tools = await registry.list_all_tools()
    backend_names = {t.backend_name for t in tools}
    assert "flaky" not in backend_names
    assert {"notes", "calculator"} <= backend_names
    assert registry.is_healthy("flaky") is False


@pytest.mark.asyncio
async def test_only_healthy_filter_excludes_unhealthy_backend_tools(registry, flaky_backend):
    registry.register("flaky", flaky_backend)
    await registry.list_all_tools()  # marks flaky unhealthy as a side effect
    healthy_tools = await registry.list_all_tools(only_healthy=True)
    assert all(t.backend_name != "flaky" for t in healthy_tools)


@pytest.mark.asyncio
async def test_backend_recovers_health_after_a_successful_call(registry, flaky_backend):
    registry.register("flaky", flaky_backend)
    with pytest.raises(BackendError):
        await registry.call("flaky.whatever", {})
    assert registry.is_healthy("flaky") is False

    # swap in a working backend under the same name (simulates recovery) and
    # confirm a successful call flips health back to True
    from mcp_router.backends import InProcessBackend
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("flaky")

    @app.tool()
    def ping() -> str:
        return "pong"

    registry.register("flaky", InProcessBackend("flaky", app))
    result = await registry.call("flaky.ping", {})
    assert registry.is_healthy("flaky") is True
    assert any("pong" in (c.text or "") for c in result.result.content)
