"""Config schema + loader for mcp-router.

The config format is deliberately close in spirit to Claude Desktop's own
`mcpServers` config: a named map of backend servers, each launched as a
subprocess over stdio via a `command` (+ `args`, `env`, `cwd`). This is what
makes the router "just usable" -- point it at a JSON (or YAML, if `pyyaml`
is installed) file and it launches and aggregates every listed backend.

Example (see examples/router_config.json for a runnable one):

    {
      "name": "my-router",
      "backends": {
        "notes": {"command": "python3", "args": ["servers/notes_server.py"]},
        "calculator": {"command": "python3", "args": ["servers/calc_server.py"]}
      }
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StdioBackendConfig(BaseModel):
    """One backend server entry: how to launch it over stdio."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., min_length=1, description="Executable to launch the backend server with.")
    args: list[str] = Field(default_factory=list, description="Arguments passed to `command`.")
    env: dict[str, str] | None = Field(default=None, description="Extra environment variables for the subprocess.")
    cwd: str | None = Field(default=None, description="Working directory for the subprocess.")

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("command must not be blank")
        return v


class RouterConfig(BaseModel):
    """Top-level router config: a name for the aggregated server plus a map
    of backend name -> how to launch it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="mcp-router", description="Name reported by the aggregated MCP server.")
    instructions: str | None = Field(default=None, description="Optional instructions text for the aggregated server.")
    backends: dict[str, StdioBackendConfig] = Field(
        default_factory=dict, description="Named backend MCP servers to launch and aggregate."
    )

    @field_validator("backends")
    @classmethod
    def _at_least_conceptually_named(cls, v: dict[str, StdioBackendConfig]) -> dict[str, StdioBackendConfig]:
        for backend_name in v:
            if not backend_name or "." in backend_name:
                raise ValueError(
                    f"backend name {backend_name!r} must be non-empty and must not contain '.' "
                    "(reserved as the namespace separator)"
                )
            if backend_name == "router":
                raise ValueError(
                    "backend name 'router' is reserved for the router's own meta-tools "
                    "(router.list_backends, router.search_tools)"
                )
        return v


def _parse_text(text: str, *, suffix: str) -> dict[str, Any]:
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - exercised only without pyyaml installed
            raise RuntimeError(
                "reading a YAML router config requires the optional 'pyyaml' dependency "
                "(install with: pip install mcp-router[yaml])"
            ) from exc
        loaded = yaml.safe_load(text)
        return loaded or {}
    return json.loads(text)


def load_config(path: str | Path) -> RouterConfig:
    """Load and validate a router config from a JSON or YAML file."""
    p = Path(path)
    raw = _parse_text(p.read_text(encoding="utf-8"), suffix=p.suffix.lower())
    return RouterConfig.model_validate(raw)


def parse_config(data: dict[str, Any]) -> RouterConfig:
    """Validate an already-loaded config dict (e.g. embedded in another
    program rather than read from disk)."""
    return RouterConfig.model_validate(data)
