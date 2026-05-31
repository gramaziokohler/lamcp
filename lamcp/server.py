"""LAMCP: Lambda MCP server bridging LLMs to Grasshopper.

Architecture::

    Claude ──MCP stdio──▶ lamcp (this process, Python 3.10+)
                              │
                              │  HTTP POST /exec
                              ▼
                      LAMCP Bridge GH component (Rhino 8 CPython 3.9)
                          ├─ http.server on 127.0.0.1:8765
                          ├─ exec() with shared globals
                          └─ returns stdout/stderr/repr(_)

The bridge URL is read from the ``LAMCP_BRIDGE_URL`` env var
(default ``http://127.0.0.1:8765``).
"""

from __future__ import annotations

import os

import httpx
from fastmcp import FastMCP

BRIDGE_URL = os.environ.get("LAMCP_BRIDGE_URL", "http://127.0.0.1:8765")
DEFAULT_TIMEOUT = 30.0

mcp = FastMCP("lamcp")


@mcp.tool()
def run_python_script(code: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Execute Python code inside the running Rhino 8 instance.

    The code runs in Rhino 8's CPython 3.9 runtime with full access to
    RhinoCommon and Grasshopper APIs (active document, GH component
    lookup, slider mutation, etc.). A shared globals dict persists across
    calls so state accumulates (e.g., ``import`` once, reuse the binding
    on later calls).

    To return a value, assign it to ``_`` — the bridge returns ``repr(_)``.

    Parameters
    ----------
    code : str
        Python source to execute inside Rhino.
    timeout : float, optional
        Max seconds to wait for the execution to complete.

    Returns
    -------
    dict
        ``{"stdout": str, "stderr": str, "result": str|None, "error": str|None}``.
        ``result`` is ``repr(_)`` if ``_`` was assigned, else ``"None"``.
        ``error`` is a formatted traceback if the script raised, else ``None``.
    """
    try:
        with httpx.Client(base_url=BRIDGE_URL, timeout=timeout + 5) as client:
            response = client.post("/exec", json={"code": code, "timeout": timeout})
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as exc:
        return {
            "stdout": "",
            "stderr": "",
            "result": None,
            "error": "Failed to reach LAMCP bridge at {}: {!r}".format(BRIDGE_URL, exc),
        }


@mcp.tool()
def unload_python_modules(prefix: str) -> dict:
    """Unload every imported Python module whose name starts with ``prefix``.

    Useful during dev iteration: after editing a Python module on disk
    that's imported by Rhino-side code, Rhino's CPython runtime keeps the
    old version cached in ``sys.modules``. This tool drops all cached
    entries under ``prefix`` so the next import re-reads from disk.

    Note: this only updates ``sys.modules``. If your Grasshopper script's
    top-level ``from foo import bar`` already bound a name, that binding
    is unaffected — you also need to re-save / re-run the script for the
    import to be re-evaluated.

    Parameters
    ----------
    prefix : str
        Dotted module prefix (e.g. ``"compas_fab"``, ``"my_pkg.sub"``).
        All modules with names equal to or starting with ``prefix + "."`` are
        unloaded.

    Returns
    -------
    dict
        ``{"unloaded": [list of module names], "count": int}``.
    """
    code = (
        "import sys\n"
        "prefix = {!r}\n"
        "to_drop = [n for n in list(sys.modules) if n == prefix or n.startswith(prefix + '.')]\n"
        "for n in to_drop: del sys.modules[n]\n"
        "_ = {{'unloaded': sorted(to_drop), 'count': len(to_drop)}}\n"
    ).format(prefix)
    return run_python_script(code)


@mcp.tool()
def bridge_health() -> dict:
    """Check whether the LAMCP Bridge HTTP server is reachable.

    Returns
    -------
    dict
        ``{"reachable": bool, "url": str, "detail": str}``.
    """
    try:
        with httpx.Client(base_url=BRIDGE_URL, timeout=5.0) as client:
            response = client.get("/health")
            response.raise_for_status()
            return {"reachable": True, "url": BRIDGE_URL, "detail": response.text}
    except httpx.RequestError as exc:
        return {"reachable": False, "url": BRIDGE_URL, "detail": repr(exc)}


def main():
    """Run the MCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
