"""Backend abstractions: a small, uniform interface (`MCPBackend`) that both an
in-process FastMCP instance and a real subprocess MCP server can satisfy.

The router never talks to a backend's native SDK types directly -- it talks to
this Protocol. That is what lets `BackendRegistry` and `router_server` treat a
FastMCP object living in the same process and a fully separate MCP server
spawned over stdio identically.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class BackendError(Exception):
    """Raised when a backend fails to list or execute a tool.

    Carries enough context for the router to turn it into a clean, isolated
    MCP-style error for a single call without taking down the whole router.
    """

    def __init__(self, backend_name: str, message: str, *, tool_name: str | None = None) -> None:
        self.backend_name = backend_name
        self.tool_name = tool_name
        super().__init__(f"[backend={backend_name}] {message}")


@dataclass(frozen=True)
class BackendToolInfo:
    """Normalized description of a single tool exposed by a backend."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendCallContent:
    """One normalized content block returned from a tool call.

    Mirrors the shape of MCP's TextContent/ImageContent well enough for a
    client to render, without forcing callers to import `mcp.types`.
    """

    type: str
    text: str | None = None
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackendCallResult:
    """Normalized result of a single tool call against a backend."""

    content: list[BackendCallContent]
    is_error: bool = False
    structured_content: dict[str, Any] | None = None


@runtime_checkable
class MCPBackend(Protocol):
    """The shape every backend (in-process or subprocess) must satisfy.

    This intentionally matches the surface of a real MCP server connection
    (`list_tools` / `call_tool`), so anything implementing it -- including a
    hand-rolled test double -- is routable by `BackendRegistry`.
    """

    async def list_tools(self) -> list[BackendToolInfo]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> BackendCallResult: ...


def _content_block_to_normalized(block: Any) -> BackendCallContent:
    """Convert an mcp ContentBlock (TextContent/ImageContent/...) into our
    normalized, dependency-light representation."""
    block_type = getattr(block, "type", "text")
    if block_type == "text":
        return BackendCallContent(type="text", text=getattr(block, "text", str(block)))
    # Non-text content (image, audio, resource links, ...): keep a best-effort
    # dict representation rather than dropping it.
    data: dict[str, Any]
    if hasattr(block, "model_dump"):
        data = block.model_dump(mode="json", exclude_none=True)
    else:  # pragma: no cover - defensive fallback
        data = {"repr": repr(block)}
    return BackendCallContent(type=block_type, data=data)


class InProcessBackend:
    """Wraps a real `mcp.server.fastmcp.FastMCP` instance living in the same
    process.

    This is a legitimate way to exercise real MCP server objects in tests and
    in lightweight deployments (e.g. bundling a small backend server directly
    inside the router process) without paying for a subprocess + stdio
    round-trip.
    """

    def __init__(self, name: str, server: Any) -> None:
        self.name = name
        self._server = server

    async def list_tools(self) -> list[BackendToolInfo]:
        try:
            tools = await self._server.list_tools()
        except Exception as exc:  # noqa: BLE001 - normalize any backend failure
            raise BackendError(self.name, f"list_tools failed: {exc}") from exc
        return [
            BackendToolInfo(
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema or {},
            )
            for t in tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> BackendCallResult:
        try:
            result = await self._server.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(self.name, str(exc), tool_name=name) from exc

        # FastMCP.call_tool returns either `Sequence[ContentBlock]` or
        # `(Sequence[ContentBlock], structured_dict)` depending on whether the
        # tool declares a structured output schema.
        structured: dict[str, Any] | None = None
        if isinstance(result, tuple) and len(result) == 2:
            blocks, structured = result
        else:
            blocks = result

        content = [_content_block_to_normalized(b) for b in blocks]
        return BackendCallResult(content=content, is_error=False, structured_content=structured)


class StdioBackend:
    """Launches a backend MCP server as a real subprocess and speaks MCP over
    stdio using the official `mcp` Python SDK client session.

    This is the production path: pointing the router at independently
    developed/deployed MCP servers exactly the way Claude Desktop's
    `mcpServers` config does, via a command + args (+ optional env/cwd).

    The session is started lazily on first use via `connect()` / async
    context manager, and can be torn down with `aclose()`.
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.name = name
        self._params = StdioServerParameters(
            command=command, args=args or [], env=env, cwd=cwd
        )
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._connect_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> None:
        if self._session is not None:
            return
        async with self._connect_lock:
            if self._session is not None:  # re-check after acquiring the lock
                return
            stack = AsyncExitStack()
            try:
                read, write = await stack.enter_async_context(stdio_client(self._params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except Exception as exc:  # noqa: BLE001
                await stack.aclose()
                raise BackendError(self.name, f"failed to start/initialize subprocess: {exc}") from exc
            self._stack = stack
            self._session = session

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def __aenter__(self) -> "StdioBackend":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def list_tools(self) -> list[BackendToolInfo]:
        await self.connect()
        assert self._session is not None
        try:
            result = await self._session.list_tools()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(self.name, f"list_tools failed: {exc}") from exc
        return [
            BackendToolInfo(
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema or {},
            )
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> BackendCallResult:
        await self.connect()
        assert self._session is not None
        try:
            result: types.CallToolResult = await self._session.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(self.name, str(exc), tool_name=name) from exc

        content = [_content_block_to_normalized(b) for b in result.content]
        if result.isError:
            message = content[0].text if content and content[0].text else "tool reported an error"
            raise BackendError(self.name, message, tool_name=name)
        return BackendCallResult(
            content=content,
            is_error=False,
            structured_content=result.structuredContent,
        )
