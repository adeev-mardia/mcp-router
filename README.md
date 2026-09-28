# mcp-router

A router/orchestrator that sits in front of **multiple MCP (Model Context
Protocol) servers** and exposes them to a single client as **one unified
virtual MCP server**.

## The problem

An MCP client (Claude Desktop, an agent framework, a custom app) is normally
wired up to talk to one MCP server per connection. As soon as you want tools
from several servers at once — say a finance server for numbers and a notes
server for scratch space — you're stuck either:

- manually configuring every server in every client separately, or
- writing custom glue in your agent to fan requests out to N different
  connections, worrying about tool name collisions (two servers both
  exposing `search`, `get`, `list`), and handling "one server is down" so it
  doesn't take the rest of your tool access with it.

`mcp-router` solves this by being a real MCP server itself. A client connects
to it **once**, over stdio, and sees the union of every backend server's
tools. The router transparently dispatches each call to the right backend,
namespaces tool names so they never collide, and isolates backend failures so
one dead server doesn't break the others.

## Architecture, in words

```
                 ┌─────────────────────────┐
   MCP client    │        mcp-router            │
  (Claude, an  ─▶│  (itself a FastMCP server)    │
   agent, ...)   │                               │
                 │   ┌───────────────────────┐   │
                 │   │   BackendRegistry     │   │
                 │   │ - namespacing          │   │      ┌───────────────┐
                 │   │ - capability search    │◀──┘───────│ Backend: notes   │
                 │   │ - health tracking      │   │      │ (stdio subprocess│
                 │   │ - call routing         │   │      │  or in-process)  │
                 │   └───────────────────────┘   │      └───────────────┘
                 │             │                  │
                 │             ▼                  │      ┌───────────────┐
                 │      dispatch to the            │─────▶│ Backend: calc    │
                 │      right MCPBackend            │      │  ...             │
                 └────────────────────────┘      └───────────────┘
```

- **`MCPBackend`** (`backends.py`) is a small async protocol —
  `list_tools()` / `call_tool(name, arguments)` — matching the shape of a
  real MCP server connection. Two implementations satisfy it:
  - **`StdioBackend`** — the production path. Launches a backend MCP server
    as a real subprocess and speaks MCP over stdio using the official `mcp`
    SDK's `ClientSession`, the same way Claude Desktop launches the servers
    in its own `mcpServers` config.
  - **`InProcessBackend`** — wraps a real `mcp.server.fastmcp.FastMCP`
    instance living in the same process (no subprocess needed). Useful for
    bundling a small backend directly into the router process, and it's what
    lets the test suite exercise real MCP server objects deterministically.
- **`BackendRegistry`** (`registry.py`) is the routing brain: it registers
  named backends, aggregates their tools under a `<backend>.<tool>`
  namespace, resolves a call back to the right backend (explicitly, or by
  unambiguous bare-name lookup), offers keyword-based capability search
  across all backends, and tracks per-backend health so a failure in one
  backend never brings down calls to another.
- **`RouterMCPServer`** (`router_server.py`) is a `FastMCP` subclass that
  overrides `list_tools()`/`call_tool()` to serve them dynamically from a
  `BackendRegistry` instead of static `@mcp.tool()` functions — this is what
  makes the router itself a real, connectable MCP server exposing the union
  of every backend's tools, plus two meta-tools:
  `router.list_backends` and `router.search_tools`.
- **`config.py`** defines and loads a small config file (JSON, or YAML if
  `pyyaml` is installed) listing backend servers to launch, structurally
  similar to Claude Desktop's own `mcpServers` config.

## Tool namespacing & routing

Every tool from backend `notes` with tool name `add_note` is exposed to the
client as `notes.add_note`. This avoids collisions when two backends happen
to expose tools with the same name (e.g. two servers both having a `search`
tool). A client can:

- call a tool explicitly: `notes.add_note`, `calculator.add`, ...
- call a **bare** tool name (`add_note`) and have the router resolve it
  automatically, *if and only if* exactly one registered backend exposes a
  tool by that name — otherwise the router raises a clear "ambiguous, use
  the namespaced form" error rather than guessing.
- use **capability search** (`router.search_tools` with a `query` keyword)
  to find which backend(s) expose something matching a name or description,
  useful for a client that doesn't know the backend layout in advance.
- use `router.list_backends` to see every registered backend and whether
  it's currently healthy.

## Health & failure isolation

`BackendRegistry` tracks per-backend health. If a backend's `list_tools()`
or `call_tool()` raises, only that backend is marked unhealthy — `list_all_tools(only_healthy=True)`
then omits its tools, and calls to *other* backends are entirely unaffected.
A failure surfaces to the client as a normal MCP tool-call error (via
`isError=True` on the `CallToolResult`, courtesy of the underlying MCP SDK),
never as a crash of the router process.

## Configuration

```json
{
  "name": "example-router",
  "instructions": "Unified router exposing a notes server and a calculator server as one MCP server.",
  "backends": {
    "notes": { "command": "python3", "args": ["examples/notes_server.py"] },
    "calculator": { "command": "python3", "args": ["examples/calculator_server.py"] }
  }
}
```

Each entry under `backends` accepts `command` (required), `args`, `env`, and
`cwd` — exactly what's needed to launch that backend as a subprocess over
stdio, the same shape as Claude Desktop's `mcpServers` entries. Backend
names may not contain `.` (reserved as the namespace separator) and may not
be `router` (reserved for the router's own meta-tools).

## Running it for real

```bash
pip install -e ".[dev]"

# Runs mcp-router as a real MCP server over stdio, launching both example
# backends as subprocesses and aggregating their tools.
mcp-router --config examples/router_config.json
```

Point any MCP client at that command (e.g. as an entry in Claude Desktop's
own config, with `mcp-router` as the command and `--config <path>` as an
arg) and it will see `notes.add_note`, `notes.get_note`, `notes.list_notes`,
`calculator.add`, `calculator.divide`, `calculator.is_prime`,
`router.list_backends`, and `router.search_tools` — all through one
connection.

## What the test suite actually proves — and what it doesn't

Be honest about this distinction, because it matters:

- **What's fully, deterministically proven by automated tests:** all of the
  router's actual logic — namespacing, resolution (explicit and bare),
  capability search, failure isolation, health tracking, config
  parsing/validation, and the `RouterMCPServer` dispatch layer — tested
  against **real** `mcp.server.fastmcp.FastMCP` server objects via
  `InProcessBackend`. This is not a mock or a stub: it's the actual MCP SDK
  tool manager being called through the actual FastMCP `list_tools`/
  `call_tool` methods. This is where the interesting bugs (namespacing edge
  cases, error isolation, resolution ambiguity) actually live, and it's
  fully covered.
- **What additionally has real, non-skipped coverage in this environment:**
  `StdioBackend` was tested against the two example servers spawned as real
  subprocesses speaking MCP over stdio (`tests/test_stdio_backend.py`) — the
  literal production code path — and it worked reliably here, so those
  tests run for real rather than being skipped. Subprocess spawning can be
  flakier in some sandboxes/CI runners than plain in-process calls; if you
  see those specific tests skipped or flaking on your machine, that's a
  sandboxing/CI limitation, not a statement that `StdioBackend` doesn't
  work — the code path is identical to what `mcp-router` uses when you run
  it for real (see "Running it for real" above), and `manual `mcp-router
  --config examples/router_config.json` connected to a real client is the
  strongest way to confirm it end-to-end in your own environment.

## Package layout

```
src/mcp_router/
  __init__.py        # public exports
  backends.py         # MCPBackend protocol, InProcessBackend, StdioBackend
  registry.py         # BackendRegistry: namespacing, search, health, routing
  router_server.py    # RouterMCPServer (FastMCP subclass) + config-driven runner
  config.py            # RouterConfig schema + JSON/YAML loader
examples/
  notes_server.py      # tiny demo FastMCP backend (3 tools)
  calculator_server.py # tiny demo FastMCP backend (3 tools)
  router_config.json   # example config wiring both of the above together
tests/                 # pytest + pytest-asyncio, deterministic, no network
```

## Development

```bash
pip install -e ".[dev]"
python3 -m pytest -v
```
