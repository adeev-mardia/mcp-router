"""mcp_router: aggregate multiple MCP servers behind one unified virtual MCP server."""

from mcp_router.backends import (
    BackendError,
    BackendToolInfo,
    InProcessBackend,
    MCPBackend,
    StdioBackend,
)
from mcp_router.registry import BackendRegistry, RoutedToolResult

__all__ = [
    "BackendError",
    "BackendToolInfo",
    "InProcessBackend",
    "MCPBackend",
    "StdioBackend",
    "BackendRegistry",
    "RoutedToolResult",
]

__version__ = "0.1.0"
