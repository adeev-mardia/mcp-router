"""End-to-end test of the actual `mcp-router` production path: spawn the
router itself as a subprocess (exactly how a real MCP client -- e.g. Claude
Desktop -- would), talk MCP to it over stdio, and confirm it aggregates both
example backend servers (each spawned as the router's OWN subprocess) into
one unified tool list with working dispatch.

This is the strongest proof in the suite that the whole thing works as
advertised, so it gets a slightly larger timeout budget (three subprocess
layers: this test -> router -> backend).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "examples" / "router_config.json"
E2E_TIMEOUT = 30


@pytest.mark.asyncio
async def test_router_cli_aggregates_both_backends_end_to_end():
    async def _run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_router.router_server", "--config", str(CONFIG_PATH)],
            cwd=str(REPO_ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {
                    "notes.add_note",
                    "notes.get_note",
                    "notes.list_notes",
                    "calculator.add",
                    "calculator.divide",
                    "calculator.is_prime",
                    "router.list_backends",
                    "router.search_tools",
                }.issubset(names)

                add_result = await session.call_tool("calculator.add", {"a": 3, "b": 4})
                assert not add_result.isError
                assert any("7" in (c.text or "") for c in add_result.content if c.type == "text")

                note_result = await session.call_tool("notes.add_note", {"title": "hi", "body": "there"})
                assert not note_result.isError

                search_result = await session.call_tool("router.search_tools", {"query": "prime"})
                assert any("calculator.is_prime" in (c.text or "") for c in search_result.content if c.type == "text")

                backends_result = await session.call_tool("router.list_backends", {})
                text = backends_result.content[0].text
                assert "notes: healthy" in text
                assert "calculator: healthy" in text

    await asyncio.wait_for(_run(), timeout=E2E_TIMEOUT)
