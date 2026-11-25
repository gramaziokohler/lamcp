#! python3
"""
LAMCP Bridge as a plain Rhino script, no Grasshopper canvas needed.

Same HTTP endpoint as the `LAMCP Bridge` Grasshopper component
(`POST /exec`, `GET /health` on 127.0.0.1), but started straight from
Rhino. Running the script again stops the server (toggle).

Run it from Rhino's command line:

    _-ScriptEditor _Run "C:\\path\\to\\lamcp_bridge.py"

To start it every time Rhino starts, paste that line into
Rhino Options -> General -> Command Lists ("Run these commands every
time Rhino starts"). To get a short command, add an alias
(Rhino Options -> Aliases), e.g. `Lamcp` -> the same line.

Port: set the `LAMCP_PORT` environment variable to override 8765
(must match the MCP server's `LAMCP_BRIDGE_URL`).

Binds to 127.0.0.1 only. NEVER expose beyond localhost: it exec()s
arbitrary Python with full Rhino API access.
"""

import contextlib
import io
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer

import scriptcontext as sc


SHARED_GLOBALS_KEY = "_lamcp_bridge_globals"
# Kept on `sys` (not sc.sticky) so it survives re-runs of this script for
# as long as Rhino's CPython interpreter lives.
SERVER_ATTR = "_lamcp_rhino_bridge_server"
DEFAULT_PORT = 8765


def _exec_code(code, timeout):
    """Run `code` on the HTTP-server thread and capture stdout/stderr/result."""
    out = io.StringIO()
    err = io.StringIO()
    g = sc.sticky.setdefault(SHARED_GLOBALS_KEY, {"__name__": "__lamcp_bridge__"})
    error_text = None
    result_repr = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(code, g, g)
            result_repr = repr(g.get("_"))
    except BaseException:
        error_text = traceback.format_exc()

    return {
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
        "result": result_repr,
        "error": error_text,
    }


class _ExecHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/exec":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            raw = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw)
            code = body.get("code", "")
            timeout = float(body.get("timeout", 30.0))
        except Exception as exc:
            self._send(400, {"error": "bad request: " + repr(exc)})
            return
        self._send(200, _exec_code(code, timeout))


def _start_server(port):
    server = HTTPServer(("127.0.0.1", port), _ExecHandler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="lamcp-bridge"
    )
    thread.start()
    return {"server": server, "thread": thread, "port": port}


def _stop_server(state):
    try:
        state["server"].shutdown()
        state["server"].server_close()
    except Exception:
        pass


# One of "toggle", "start", "stop". The Rhino plugin (rhp) rewrites this
# line in its embedded copy of this script; keep it exactly in this form.
_ACTION = "toggle"


def main(action):
    port = int(os.environ.get("LAMCP_PORT", DEFAULT_PORT))
    state = getattr(sys, SERVER_ATTR, None)
    running = state is not None and state["thread"].is_alive()

    if running and action in ("toggle", "stop"):
        _stop_server(state)
        setattr(sys, SERVER_ATTR, None)
        print("LAMCP bridge stopped (was on port {}).".format(state["port"]))
        return
    if running:
        print(
            "LAMCP bridge already listening on http://127.0.0.1:{}".format(
                state["port"]
            )
        )
        return
    if action == "stop":
        print("LAMCP bridge is not running.")
        return

    try:
        setattr(sys, SERVER_ATTR, _start_server(port))
    except OSError as exc:
        print(
            "LAMCP bridge: failed to bind 127.0.0.1:{} ({}). "
            "Is the Grasshopper bridge component already running?".format(port, exc)
        )
        return

    print("LAMCP bridge listening on http://127.0.0.1:{}".format(port))


main(_ACTION)
