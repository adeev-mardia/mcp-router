"""BackendRegistry: the routing brain of mcp-router.

Owns the set of registered named backends, aggregates their tools under a
`<backend_name>.<tool_name>` namespace to avoid collisions, resolves a
namespaced (or bare) tool name back to the right backend, offers a
capability-search mode for clients that don't know which backend has what,
and tracks per-backend health so one failing backend never takes the router
down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mcp_router.backends import BackendCallResult, BackendError, BackendToolInfo, MCPBackend

logger = logging.getLogger("mcp_router.registry")

NAMESPACE_SEPARATOR = "."


def namespaced_tool_name(backend_name: str, tool_name: str) -> str:
    """Build the collision-free name a client sees for one backend's tool."""
    return f"{backend_name}{NAMESPACE_SEPARATOR}{tool_name}"


@dataclass(frozen=True)
class RoutedTool:
    """A tool as exposed by the router: which backend it came from, its
    original name, its namespaced name, and its schema/description."""

    backend_name: str
    tool_name: str
    namespaced_name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutedToolResult:
    """Result of routing a single tool call, including which backend served it."""

    backend_name: str
    tool_name: str
    result: BackendCallResult


class ToolNotFoundError(Exception):
    """Raised when a namespaced or bare tool name cannot be resolved to any
    registered, healthy backend."""


class BackendNotFoundError(Exception):
    """Raised when an explicit backend name is not registered."""


@dataclass
class _BackendEntry:
    name: str
    backend: MCPBackend
    healthy: bool = True
    last_error: str | None = None


class BackendRegistry:
    """Registers named MCP backends and routes tool calls to them."""

    def __init__(self) -> None:
        self._backends: dict[str, _BackendEntry] = {}

    # -- registration -----------------------------------------------------

    def register(self, name: str, backend: MCPBackend) -> None:
        """Register a backend under a unique name. Re-registering an existing
        name replaces it (and resets its health to unknown/healthy)."""
        if not name or NAMESPACE_SEPARATOR in name:
            raise ValueError(
                f"backend name {name!r} must be non-empty and must not contain "
                f"the namespace separator {NAMESPACE_SEPARATOR!r}"
            )
        if name == "router":
            raise ValueError("backend name 'router' is reserved for the router's own meta-tools")
        self._backends[name] = _BackendEntry(name=name, backend=backend)

    def unregister(self, name: str) -> None:
        self._backends.pop(name, None)

    async def connect_all(self) -> None:
        """Eagerly connect every backend that supports it (e.g. `StdioBackend`
        launching its subprocess and doing the MCP handshake).

        This matters beyond convenience: a `StdioBackend`'s persistent
        connection holds a background anyio task group (for reading the
        subprocess's stdout) that must be opened in the SAME long-lived task
        that will eventually close it -- structured concurrency requires a
        task group's lifetime to be nested correctly within one task. If a
        backend were instead connected lazily from inside a single incoming
        request's short-lived handler task (as `list_tools`/`call_tool` would
        do on first use), that per-request task finishing would try to tear
        down a cancel scope whose task group is still alive, which anyio
        rejects. So a long-running server calls this once, up front, in its
        own main task, before it starts serving requests -- see
        `router_server._run_async`.

        A backend that fails to connect is marked unhealthy rather than
        raising, so one bad backend config doesn't prevent the router from
        starting up and serving the rest.
        """
        for entry in self._backends.values():
            connect = getattr(entry.backend, "connect", None)
            if connect is None:
                continue
            try:
                await connect()
            except BackendError as exc:
                self._mark_unhealthy(entry, str(exc))
            except Exception as exc:  # noqa: BLE001
                self._mark_unhealthy(entry, f"unexpected error connecting: {exc}")

    async def aclose_all(self) -> None:
        """Close every backend that supports it (e.g. `StdioBackend`,
        whose subprocess/stdio session must be torn down explicitly).

        Backends are closed in reverse registration order, because a
        `StdioBackend`'s underlying anyio task group must be exited in the
        opposite order it was entered in -- closing out of order raises from
        anyio rather than actually failing to clean up. Each backend's
        failure to close is isolated from the others so one stuck backend
        doesn't stop the rest from shutting down.
        """
        for entry in reversed(list(self._backends.values())):
            aclose = getattr(entry.backend, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning("error closing backend %r: %s", entry.name, exc)

    def backend_names(self) -> list[str]:
        return list(self._backends.keys())

    def healthy_backend_names(self) -> list[str]:
        return [b.name for b in self._backends.values() if b.healthy]

    def is_healthy(self, name: str) -> bool:
        entry = self._backends.get(name)
        return bool(entry and entry.healthy)

    def last_error(self, name: str) -> str | None:
        entry = self._backends.get(name)
        return entry.last_error if entry else None

    # -- aggregation --------------------------------------------------------

    async def list_all_tools(self, *, only_healthy: bool = False) -> list[RoutedTool]:
        """List every tool from every registered backend, namespaced.

        A backend that fails to answer `list_tools` is marked unhealthy and
        simply contributes no tools -- it does not raise out of this call, so
        one bad backend never breaks discovery for the rest.
        """
        routed: list[RoutedTool] = []
        for entry in self._backends.values():
            if only_healthy and not entry.healthy:
                continue
            try:
                tools: list[BackendToolInfo] = await entry.backend.list_tools()
            except BackendError as exc:
                self._mark_unhealthy(entry, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - isolate unexpected backend bugs too
                self._mark_unhealthy(entry, f"unexpected error in list_tools: {exc}")
                continue
            self._mark_healthy(entry)
            for t in tools:
                routed.append(
                    RoutedTool(
                        backend_name=entry.name,
                        tool_name=t.name,
                        namespaced_name=namespaced_tool_name(entry.name, t.name),
                        description=t.description,
                        input_schema=t.input_schema,
                    )
                )
        return routed

    async def search_tools(self, query: str) -> list[RoutedTool]:
        """Capability search: find tool(s) across all registered, healthy
        backends whose name or description contains `query` (case-insensitive).

        This is for a client that doesn't know in advance which backend
        exposes a capability it wants -- it can ask "who has something like
        'invoice'" instead of guessing a backend name.
        """
        query_l = query.lower()
        all_tools = await self.list_all_tools(only_healthy=True)
        return [
            t
            for t in all_tools
            if query_l in t.tool_name.lower()
            or query_l in t.namespaced_name.lower()
            or query_l in t.description.lower()
        ]

    # -- resolution + calling ------------------------------------------------

    def resolve(self, name: str) -> tuple[str, str]:
        """Resolve a name the client used into (backend_name, bare_tool_name).

        Accepts an explicit namespaced name (`backend.tool`) or, if exactly
        one registered backend exposes a tool with that bare name, the bare
        name itself. Raises `ToolNotFoundError` / `BackendNotFoundError` if it
        cannot be resolved unambiguously.
        """
        if NAMESPACE_SEPARATOR in name:
            backend_name, tool_name = name.split(NAMESPACE_SEPARATOR, 1)
            if backend_name not in self._backends:
                raise BackendNotFoundError(f"no backend registered as {backend_name!r}")
            return backend_name, tool_name
        raise ToolNotFoundError(
            f"tool name {name!r} is not namespaced; call resolve_bare() for bare-name lookup"
        )

    async def resolve_bare(self, tool_name: str) -> tuple[str, str]:
        """Resolve a bare (non-namespaced) tool name by searching every
        healthy backend. Succeeds only if exactly one backend exposes it."""
        matches = [
            (t.backend_name, t.tool_name)
            for t in await self.list_all_tools(only_healthy=True)
            if t.tool_name == tool_name
        ]
        if not matches:
            raise ToolNotFoundError(f"no backend exposes a tool named {tool_name!r}")
        if len(matches) > 1:
            backends = ", ".join(b for b, _ in matches)
            raise ToolNotFoundError(
                f"tool name {tool_name!r} is ambiguous across backends [{backends}]; "
                f"use the namespaced form '<backend>.{tool_name}'"
            )
        return matches[0]

    async def call(self, namespaced_or_bare_name: str, arguments: dict[str, Any]) -> RoutedToolResult:
        """Route a single tool call to the right backend.

        - Accepts `backend.tool` (explicit selection) or a bare `tool` name
          (resolved automatically if unambiguous).
        - A failure in the target backend is isolated: it's raised as
          `BackendError` (never crashes the registry or affects other
          backends) and marks only that backend unhealthy.
        """
        if NAMESPACE_SEPARATOR in namespaced_or_bare_name:
            backend_name, tool_name = self.resolve(namespaced_or_bare_name)
        else:
            backend_name, tool_name = await self.resolve_bare(namespaced_or_bare_name)

        entry = self._backends.get(backend_name)
        if entry is None:
            raise BackendNotFoundError(f"no backend registered as {backend_name!r}")

        try:
            result = await entry.backend.call_tool(tool_name, arguments)
        except BackendError as exc:
            self._mark_unhealthy(entry, str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 - isolate unexpected backend bugs
            wrapped = BackendError(backend_name, f"unexpected error calling {tool_name!r}: {exc}", tool_name=tool_name)
            self._mark_unhealthy(entry, str(wrapped))
            raise wrapped from exc

        self._mark_healthy(entry)
        return RoutedToolResult(backend_name=backend_name, tool_name=tool_name, result=result)

    # -- health bookkeeping ---------------------------------------------------

    def _mark_unhealthy(self, entry: _BackendEntry, error: str) -> None:
        entry.healthy = False
        entry.last_error = error
        logger.warning("backend %r marked unhealthy: %s", entry.name, error)

    def _mark_healthy(self, entry: _BackendEntry) -> None:
        if not entry.healthy:
            logger.info("backend %r recovered", entry.name)
        entry.healthy = True
        entry.last_error = None
