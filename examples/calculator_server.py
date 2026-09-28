"""Tiny demo MCP server: a basic calculator.

Used by mcp-router's tests/demo to prove real multi-backend routing (paired
with notes_server.py). Runnable standalone over stdio:

    python3 examples/calculator_server.py
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("calculator")


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@mcp.tool()
def divide(a: float, b: float) -> float:
    """Divide `a` by `b`. Raises on division by zero."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b


@mcp.tool()
def is_prime(n: int) -> bool:
    """Check whether `n` is a prime number."""
    if n < 2:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True


if __name__ == "__main__":
    mcp.run(transport="stdio")
