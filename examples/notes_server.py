"""Tiny demo MCP server: an in-memory notes store.

Used by mcp-router's tests/demo to prove real multi-backend routing (paired
with calculator_server.py). Runnable standalone over stdio:

    python3 examples/notes_server.py
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("notes")

_notes: dict[str, str] = {}


@mcp.tool()
def add_note(title: str, body: str) -> str:
    """Create or overwrite a note under `title` with the given `body`."""
    _notes[title] = body
    return f"saved note {title!r} ({len(body)} chars)"


@mcp.tool()
def get_note(title: str) -> str:
    """Return the body of the note called `title`."""
    if title not in _notes:
        raise ValueError(f"no note titled {title!r}")
    return _notes[title]


@mcp.tool()
def list_notes() -> list[str]:
    """List the titles of all stored notes."""
    return sorted(_notes.keys())


if __name__ == "__main__":
    mcp.run(transport="stdio")
