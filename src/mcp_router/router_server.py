"""The router itself, exposed as a real, runnable MCP server.

`RouterMCPServer` is a `mcp.server.fastmcp.FastMCP` subclass that overrides
the two handlers FastMCP normally derives from `@mcp.tool()`-decorated
functions (`list_tools` / `call_tool`) and instead serves them dynamically
from a `BackendRegistry`. That is what lets a client connect to this ONE
server and see the union of every registered backend's tools, with each call
transparently dispatched to the right backend.

Run it against a config file:

    mcp-router --config examples/router_config.json

or, once installed:

    python3 -m mcp_router.router_server --config path/to/config.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

import mcp.types as types
from mcp.server.fastmcp import FastMCP

from mcp_router.backends import BackendCallContent, MCPBackend, StdioBackend
from mcp_router.config import RouterConfig, load_config
from mcp_router.registry import BackendRegistry

logger = logging.getLogger("mcp_router.router_server")

# Reserved pseudo-backend name for the router's own meta-tools
# (`router.list_backends`, `router.search_tools`). Backend configs may not
# use this name -- `RouterConfig`/`BackendRegistry` both guard against
# collisions via the "no '.'" rule, but "router" itself is additionally
# reserved so these meta-tools can never be shadowed.
META_NAMESPACE = "router"
LIST_BACKENDS_TOOL = f"{META_NAMESPACE}.list_backends"
SEARCH_TOOLS_TOOL = f"{META_NAMESPACE}.search_tools"

_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _to_mcp_content(blocks: list[BackendCallContent]) -> list[types.ContentBlock]:
    out: list[types.ContentBlock] = []
    for b in blocks:
        if b.type == "text":
            out.append(types.TextContent(type="text", text=b.text or ""))
        else:
            # Best-effort passthrough for non-text content normalized on the
            # way in; represent it as text so nothing is silently dropped.
            out.append(types.TextContent(type="text", text=f"[{b.type} content] {b.data}"))
    return out


class RouterMCPServer(FastMCP):
    """A FastMCP server whose tool list and tool dispatch are backed by a
    `BackendRegistry` instead of static `@mcp.tool()` functions."""

    def __init__(
        self,
        registry: BackendRegistry,
        name: str = "mcp-router",
        instructions: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, instructions=instructions, **kwargs)
        self.registry = registry

    async def list_tools(self) -> list[types.Tool]:  # type: ignore[override]
        routed = await self.registry.list_all_tools()
        tools = [
            types.Tool(
                name=t.namespaced_name,
                description=t.description,
                inputSchema=t.input_schema or _EMPTY_OBJECT_SCHEMA,
            )
            for t in routed
        ]
        tools.append(
            types.Tool(
                name=LIST_BACKENDS_TOOL,
                description=(
                    "List every registered backend and whether it is currently healthy "
                    "(reachable), without calling any backend's own tools."
                ),
                inputSchema=_EMPTY_OBJECT_SCHEMA,
            )
        )
        tools.append(
            types.Tool(
                name=SEARCH_TOOLS_TOOL,
                description=(
                    "Capability search: find tool(s) across all healthy registered backends "
                    "whose name or description matches a keyword, without needing to know "
                    "which backend exposes it."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Keyword to search for."}},
                    "required": ["query"],
                },
            )
        )
        return tools

    async def call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> list[types.ContentBlock]:
        if name == LIST_BACKENDS_TOOL:
            lines = []
            for backend_name in self.registry.backend_names():
                status = "healthy" if self.registry.is_healthy(backend_name) else "unhealthy"
                err = self.registry.last_error(backend_name)
                lines.append(f"{backend_name}: {status}" + (f" ({err})" if err else ""))
            return [types.TextContent(type="text", text="\n".join(lines) or "no backends registered")]

        if name == SEARCH_TOOLS_TOOL:
            query = arguments.get("query", "")
            matches = await self.registry.search_tools(query)
            if not matches:
                text = f"no tools matched {query!r}"
            else:
                text = "\n".join(f"{t.namespaced_name}: {t.description}" for t in matches)
            return [types.TextContent(type="text", text=text)]

        routed_result = await self.registry.call(name, arguments or {})
        return _to_mcp_content(routed_result.result.content)


def build_registry_from_config(config: RouterConfig) -> tuple[BackendRegistry, list[MCPBackend]]:
    """Build a `BackendRegistry` with a `StdioBackend` for every backend in
    `config`. Returns the registry plus the list of backends (so callers can
    close them on shutdown)."""
    registry = BackendRegistry()
    backends: list[MCPBackend] = []
    for backend_name, backend_config in config.backends.items():
        backend = StdioBackend(
            name=backend_name,
            command=backend_config.command,
            args=backend_config.args,
            env=backend_config.env,
            cwd=backend_config.cwd,
        )
        registry.register(backend_name, backend)
        backends.append(backend)
    return registry, backends


def build_router_server(config: RouterConfig) -> tuple[RouterMCPServer, list[MCPBackend]]:
    registry, backends = build_registry_from_config(config)
    server = RouterMCPServer(registry, name=config.name, instructions=config.instructions)
    return server, backends


async def _run_async(config_path: str) -> None:
    config = load_config(config_path)
    server, _backends = build_router_server(config)
    # Connect every backend up front, in this same top-level task, before
    # serving any requests -- see BackendRegistry.connect_all's docstring for
    # why lazy/per-request connection would break anyio's structured
    # concurrency here.
    await server.registry.connect_all()
    try:
        await server.run_stdio_async()
    finally:
        await server.registry.aclose_all()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-router", description="Run a unified MCP server that routes to multiple backend MCP servers.")
    parser.add_argument("--config", "-c", required=True, help="Path to a JSON or YAML router config file.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging (to stderr).")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    asyncio.run(_run_async(args.config))


if __name__ == "__main__":
    main()
