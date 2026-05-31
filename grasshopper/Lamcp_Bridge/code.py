"""
LAMCP Bridge: local HTTP server inside Rhino 8 / Grasshopper CPython.

Exposes `POST /exec` (JSON: `{"code": "...", "timeout": 30}`) which
exec()s `code` and returns
`{"stdout": "...", "stderr": "...", "result": "...", "error": null|str}`.

Pair with the `lamcp` FastMCP server, which translates Claude's
`run_python_script` MCP tool calls into POSTs to this endpoint.

Binds to 127.0.0.1 only. NEVER expose beyond localhost — it exec()s
arbitrary Python with full Rhino API access.

LAMCP v0.1.0
"""

import contextlib
import io
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer

import Grasshopper
import scriptcontext as sc


SHARED_GLOBALS_KEY = "_lamcp_bridge_globals"
SERVER_KEY = "_lamcp_bridge_server"


def _exec_code(code, timeout):
    """Run `code` on the HTTP-server thread and capture stdout/stderr/result.

    NOTE: this is intentionally NOT marshalled to Rhino's UI thread for the
    spike — `Rhino.RhinoApp.InvokeOnUiThread` was queueing actions that
    never fired in Rhino 8 CPython. Most read-only RhinoCommon access is
    safe cross-thread; mutations may crash. Add Eto-based marshalling
    back here when we need it.
    """
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
    # Silence default request logging so it doesn't clog the Rhino command line.
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
        result = _exec_code(code, timeout)
        self._send(200, result)


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


class LamcpBridgeComponent(Grasshopper.Kernel.GH_ScriptInstance):
    def RunScript(self, enable: bool, port: int):
        port = int(port) if port else 8765

        state = sc.sticky.get(SERVER_KEY)

        if not enable:
            if state is not None:
                _stop_server(state)
                del sc.sticky[SERVER_KEY]
            return "stopped"

        # Already running on the requested port: no-op.
        if (
            state is not None
            and state.get("port") == port
            and state["thread"].is_alive()
        ):
            return "listening on http://127.0.0.1:{}".format(port)

        # Port changed or server died: tear down and restart.
        if state is not None:
            _stop_server(state)
            del sc.sticky[SERVER_KEY]

        try:
            sc.sticky[SERVER_KEY] = _start_server(port)
        except OSError as exc:
            ghenv.Component.AddRuntimeMessage(  # noqa: F821
                Grasshopper.Kernel.GH_RuntimeMessageLevel.Error,
                "Failed to bind 127.0.0.1:{}: {}".format(port, exc),
            )
            return "error: {}".format(exc)

        return "listening on http://127.0.0.1:{}".format(port)
