from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_router.backends import BackendCallResult, MCPBackend, BackendToolInfo

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def notes_fastmcp() -> FastMCP:
    """A real, standalone FastMCP 'notes' server, built fresh per test so
    state doesn't leak between tests."""
    app = FastMCP("notes")
    notes: dict[str, str] = {}

    @app.tool()
    def add_note(title: str, body: str) -> str:
        """Create or overwrite a note under `title`."""
        notes[title] = body
        return f"saved note {title!r} ({len(body)} chars)"

    @app.tool()
    def get_note(title: str) -> str:
        """Return the body of the note called `title`."""
        if title not in notes:
            raise ValueError(f"no note titled {title!r}")
        return notes[title]

    @app.tool()
    def list_notes() -> list[str]:
        """List the titles of all stored notes."""
        return sorted(notes.keys())

    return app


@pytest.fixture
def calculator_fastmcp() -> FastMCP:
    """A real, standalone FastMCP 'calculator' server."""
    app = FastMCP("calculator")

    @app.tool()
    def add(a: float, b: float) -> float:
        """Add two numbers."""
        return a + b

    @app.tool()
    def divide(a: float, b: float) -> float:
        """Divide a by b."""
        if b == 0:
            raise ValueError("division by zero")
        return a / b

    @app.tool()
    def is_prime(n: int) -> bool:
        """Check whether n is prime."""
        if n < 2:
            return False
        for i in range(2, int(n**0.5) + 1):
            if n % i == 0:
                return False
        return True

    return app


class FlakyBackend:
    """A hand-rolled `MCPBackend` implementation (not FastMCP-based) that
    always fails, used to prove failure isolation without depending on any
    particular backend implementation."""

    def __init__(self, name: str = "flaky") -> None:
        self.name = name

    async def list_tools(self) -> list[BackendToolInfo]:
        raise RuntimeError("backend is down")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> BackendCallResult:
        raise RuntimeError("backend is down")


@pytest.fixture
def flaky_backend() -> MCPBackend:
    return FlakyBackend()
