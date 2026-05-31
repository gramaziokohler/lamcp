# LAMCP

**LA**mbda **MCP**: teach your LLM to do Grasshopper tricks.

Lets Claude Code (or any MCP client) introspect and mutate a
live Grasshopper session in real time: inspect the canvas,
wire components, read/write slider values, run `RhinoCommon`
calls, hot-reload modules -all from inside an AI agent loop, without rebuilding userobjects or restarting Rhino.

## Architecture

```text
LLM ──MCP stdio──▶ lamcp (Python 3.10+)
                          │
                          │  HTTP POST /exec  {"code": "...", "timeout": 30}
                          ▼
                  LAMCP Bridge GH component (Rhino 8 CPython 3.9)
                     ├─ http.server on 127.0.0.1:8765
                     ├─ exec() with shared globals
                     └─ returns {stdout, stderr, result, error}
```

Why split: Rhino 8's CPython runtime is pinned to 3.9. `fastmcp` and the
underlying `mcp` SDK require 3.10+. So the MCP-speaking half runs in a
system Python and forwards over loopback HTTP to a stdlib-only HTTP server
living inside Rhino as a regular Grasshopper component.

## Setup

### 1. Register with Claude Code

The friction-free path uses [`uvx`](https://docs.astral.sh/uv/) so no
explicit install of `lamcp` is needed — it'll be fetched and cached on
first invocation:

```bash
claude mcp add lamcp -- uvx lamcp
```

Or, if you'd rather install `lamcp` into your environment first:

```bash
pip install lamcp     # or: uv tool install lamcp
claude mcp add lamcp -- lamcp
```

To scope the registration to a single project (writes a `.mcp.json` in
the repo, checked into git so collaborators get it too):

```bash
claude mcp add lamcp --scope project -- uvx lamcp
```

Verify:

```bash
claude mcp list
```

The new tools are available in any new Claude Code conversation.

> Using a different MCP client? Point it at the `lamcp` command (or
> `uvx lamcp`) over stdio.

### 2. Install the bridge in Grasshopper

**Option A — drop the pre-built userobject (recommended).**

1. Download `Lamcp_Bridge.ghuser` from the [latest release](https://github.com/gramaziokohler/lamcp/releases/latest).
2. In Grasshopper: *File → Special Folders → Components Folder*. Move the
   `.ghuser` file there.
3. Restart Grasshopper. `LAMCP Bridge` appears under the `LAMCP` tab.
4. Drop it on the canvas, wire a `Boolean Toggle` (set to `True`) into
   `enable`. The `status` output reads `listening on http://127.0.0.1:8765`.

**Option B — paste the source manually (for hacking).**

1. Drop a Python 3 Script component on the canvas. Paste the contents of
   [`grasshopper/Lamcp_Bridge/code.py`](grasshopper/Lamcp_Bridge/code.py) in.
2. Add two inputs: `enable` (bool) and `port` (int). Add one output: `status`.
3. Wire a `Boolean Toggle` (set to `True`) into `enable`.
4. The `status` output should read `listening on http://127.0.0.1:8765`.

Either way, your MCP client now has a `run_python_script` tool that
exec()s code inside your live Rhino session.

## Tools exposed

| Tool                    | Purpose                                                                  |
| ----------------------- | ------------------------------------------------------------------------ |
| `run_python_script`     | exec() arbitrary Python inside Rhino, capture stdout / stderr / repr(_)  |
| `unload_python_modules` | drop `sys.modules[prefix.*]` so the next import re-reads from disk       |
| `bridge_health`         | ping the bridge to verify it's reachable                                 |

### Return contract for `run_python_script`

```json
{
  "stdout": "...",            // captured stdout
  "stderr": "...",            // captured stderr
  "result": "repr of _",      // assign to `_` to return a value
  "error": null               // formatted traceback if exception raised
}
```

Globals persist between calls, so you can `import` once and reuse:

```python
# call 1
import scriptcontext as sc; doc = sc.doc.ActiveDoc
# call 2
print(doc.Name)   # `doc` is still bound
```

## Environment variables

| Variable           | Default                  | Purpose                          |
| ------------------ | ------------------------ | -------------------------------- |
| `LAMCP_BRIDGE_URL` | `http://127.0.0.1:8765`  | URL of the bridge's HTTP server  |

## Caveats

- **UI thread**: code runs on the HTTP server thread, not the Rhino UI
  thread. Most read-only `RhinoCommon` / `Grasshopper` access works
  cross-thread, but heavy mutations (bulk `RemoveObject`, etc.) can crash
  Rhino. Eto-based UI marshalling is a planned addition.
- **`isinstance` doesn't always work**: in Rhino 8 CPython, `isinstance`
  against concrete .NET types often returns False due to interface interop.
  Use `obj.GetType().Name == "..."` instead.
- **`RemoveSource(IGH_Param)` is a silent no-op**: use the
  `RemoveSource(Guid)` overload.
- **`float(System.Decimal)` raises**: wrap with `System.Convert.ToDouble(x)`
  or `float(str(x))`.

## Security

The bridge listens on `127.0.0.1` only and accepts no auth: it runs
arbitrary Python in your Rhino with no sandboxing. **Never expose it
beyond localhost**, and stop it (`enable=False`) when you're done.

## Development

Install with the `dev` extra to pull in `ruff`:

```bash
pip install -e ".[dev]"
```

Lint + format checks (same commands CI runs):

```bash
ruff check .                # lint
ruff format --check .       # formatting (non-destructive)
```

Auto-fix:

```bash
ruff check . --fix          # fix lint issues
ruff format .               # reformat
```

For one-off runs without installing into your env, `uvx ruff ...` works
identically.

Releases are tag-driven — see [RELEASING.md](RELEASING.md).

## License

MIT
