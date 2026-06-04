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

Thread-safety (READ THIS before adding a tool that mutates the document)
------------------------------------------------------------------------
The bridge ``exec()``s our code on its own HTTP **server thread**, which is
*not* Rhino's UI thread. RhinoCommon / Grasshopper object mutation is not
thread-safe: setting a component's ``Text``/``LanguageSpec``, adding or removing
objects, expiring solutions, or saving the document from the HTTP thread can
**hard-crash the entire Rhino process** (not merely tear the bridge down).
Disabling the solver does NOT prevent this — the crash is the cross-thread
mutation itself. Every tool here that mutates the document therefore wraps its
work in :data:`_UI_THREAD_BOOTSTRAP` and runs it through ``_lamcp_run_on_ui`` so
the mutation happens on the UI thread. Read-only tools
(:func:`list_grasshopper_objects`, :func:`run_python_script` callers that only
read) do not need it.
"""

from __future__ import annotations

import base64
import os
import uuid

import httpx
from fastmcp import FastMCP

BRIDGE_URL = os.environ.get("LAMCP_BRIDGE_URL", "http://127.0.0.1:8765")
DEFAULT_TIMEOUT = 30.0

# Registered type GUIDs for the Rhino 8 (RhinoCodePluginGH) Python 3 script
# component, discovered by serializing an existing component and reading its
# GH_IO archive. These are stable for the Rhino 8 script editor plugin.
_SCRIPT_COMPONENT_GUID = "c9b2d725-6f87-4b07-af90-bd9aefef68eb"  # the component type
_SCRIPT_PARAM_GUID = "08908df5-fa14-4982-9ab2-1aa0927566aa"  # script variable param
_TYPEHINT_OBJECT_GUID = "1c282eeb-dd16-439f-94e4-7d92b542fe8b"  # "Object" type hint

# Code prepended to every document-mutating snippet. It defines
# ``_lamcp_run_on_ui(fn, timeout)``, which runs ``fn()`` on Rhino's UI thread
# and blocks the (HTTP) caller until it finishes, returning ``fn``'s result.
#
# WHY THIS EXISTS — DO NOT REMOVE: the bridge exec()s us on its HTTP server
# thread, not Rhino's UI thread, and cross-thread GH/RhinoCommon mutation
# HARD-CRASHES Rhino (see the module docstring). ``RhinoApp.InvokeOnUiThread``
# marshals the delegate onto the UI thread (where ``RhinoApp.InvokeRequired`` is
# False); a ``threading.Event`` lets the HTTP thread wait for completion so the
# tool still returns synchronously. We deliberately use ``InvokeOnUiThread``
# rather than ``ScheduleSolution`` as the marshaller because the latter forces a
# solve — with this primitive the caller's ``fn`` decides whether to expire and
# schedule, so ``solve=False`` is honoured.
_UI_THREAD_BOOTSTRAP = (
    "import Rhino, System, threading, traceback\n"
    "def _lamcp_run_on_ui(fn, timeout=20.0):\n"
    "    box = {'result': None, 'error': None}\n"
    "    ev = threading.Event()\n"
    "    def _action():\n"
    "        try:\n"
    "            box['result'] = fn()\n"
    "        except Exception:\n"
    "            box['error'] = traceback.format_exc()\n"
    "        finally:\n"
    "            ev.set()\n"
    "    if Rhino.RhinoApp.InvokeRequired:\n"
    "        Rhino.RhinoApp.InvokeOnUiThread(System.Action(_action))\n"
    "        if not ev.wait(timeout):\n"
    "            raise Exception('UI-thread operation timed out after %ss' % timeout)\n"
    "    else:\n"
    "        _action()\n"
    "    if box['error'] is not None:\n"
    "        raise Exception('UI-thread operation failed:\\n' + box['error'])\n"
    "    return box['result']\n"
)

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


@mcp.tool()
def list_grasshopper_objects() -> dict:
    """List every object on the active Grasshopper canvas.

    Handy for orienting before mutating a document: gives the .NET type,
    nickname, instance GUID and pivot of each object, plus any error/warning
    runtime messages it is currently carrying.

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`. ``result`` is the ``repr``
        of ``{"document", "path", "count", "objects": [...]}`` where each entry
        is ``{"type", "nickname", "guid", "pivot", "errors", "warnings"}``.
    """
    code = (
        "import Grasshopper as gh\n"
        "doc = gh.Instances.ActiveCanvas.Document\n"
        "L = gh.Kernel.GH_RuntimeMessageLevel\n"
        "objs = []\n"
        "if doc is not None:\n"
        "    for o in doc.Objects:\n"
        "        piv = o.Attributes.Pivot if o.Attributes else None\n"
        "        rm = getattr(o, 'RuntimeMessages', None)\n"
        "        objs.append({\n"
        "            'type': o.GetType().FullName,\n"
        "            'nickname': o.NickName,\n"
        "            'guid': str(o.InstanceGuid),\n"
        "            'pivot': [piv.X, piv.Y] if piv else None,\n"
        "            'errors': list(rm(L.Error)) if rm else [],\n"
        "            'warnings': list(rm(L.Warning)) if rm else [],\n"
        "        })\n"
        "_ = {\n"
        "    'document': None if doc is None else doc.DisplayName,\n"
        "    'path': None if doc is None else doc.FilePath,\n"
        "    'count': len(objs), 'objects': objs,\n"
        "}\n"
    )
    return run_python_script(code)


def _build_script_component_xml(code, name, inputs, outputs, x, y):
    """Build the GH_IO XML fragment for a Rhino 8 Python 3 script component.

    Inputs use the default (dynamic) type hint so they accept any incoming
    data; outputs are tagged with the generic ``Object`` type hint. The script
    ``code`` is base64-encoded into the ``Text`` item exactly as Grasshopper
    serializes it.
    """
    name_e = _xml_escape(name)
    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    comp_guid = str(uuid.uuid4())
    bx, by = x - 52, y - 22

    in_chunks = []
    for i, raw in enumerate(inputs):
        pn = _xml_escape(raw)
        g = str(uuid.uuid4())
        yy = by + 2 + i * 20
        in_chunks.append(
            '<chunk name="InputParam" index="%d"><items count="10">'
            '<item name="AllowTreeAccess" type_name="gh_bool" type_code="1">true</item>'
            '<item name="Description" type_name="gh_string" type_code="10">Input %s</item>'
            '<item name="InstanceGuid" type_name="gh_guid" type_code="9">%s</item>'
            '<item name="Name" type_name="gh_string" type_code="10">%s</item>'
            '<item name="NickName" type_name="gh_string" type_code="10">%s</item>'
            '<item name="Optional" type_name="gh_bool" type_code="1">true</item>'
            '<item name="ScriptParamAccess" type_name="gh_int32" type_code="3">0</item>'
            '<item name="ScriptParameterVersion" type_name="gh_int32" type_code="3">2</item>'
            '<item name="ShowTypeHints" type_name="gh_bool" type_code="1">true</item>'
            '<item name="SourceCount" type_name="gh_int32" type_code="3">0</item></items>'
            '<chunks count="1"><chunk name="Attributes"><items count="2">'
            '<item name="Bounds" type_name="gh_drawing_rectanglef" type_code="35">'
            "<X>%d</X><Y>%d</Y><W>35</W><H>20</H></item>"
            '<item name="Pivot" type_name="gh_drawing_pointf" type_code="31">'
            "<X>%d</X><Y>%d</Y></item>"
            "</items></chunk></chunks></chunk>"
            % (i, pn, g, pn, pn, bx + 2, yy, bx + 20, yy + 10)
        )

    out_chunks = []
    for i, raw in enumerate(outputs):
        pn = _xml_escape(raw)
        g = str(uuid.uuid4())
        yy = by + 2 + i * 20
        out_chunks.append(
            '<chunk name="OutputParam" index="%d"><items count="11">'
            '<item name="AllowTreeAccess" type_name="gh_bool" type_code="1">false</item>'
            '<item name="Description" type_name="gh_string" type_code="10">Output %s</item>'
            '<item name="InstanceGuid" type_name="gh_guid" type_code="9">%s</item>'
            '<item name="Name" type_name="gh_string" type_code="10">%s</item>'
            '<item name="NickName" type_name="gh_string" type_code="10">%s</item>'
            '<item name="Optional" type_name="gh_bool" type_code="1">false</item>'
            '<item name="ScriptParamAccess" type_name="gh_int32" type_code="3">0</item>'
            '<item name="ScriptParameterVersion" type_name="gh_int32" type_code="3">2</item>'
            '<item name="ShowTypeHints" type_name="gh_bool" type_code="1">true</item>'
            '<item name="SourceCount" type_name="gh_int32" type_code="3">0</item>'
            '<item name="TypeHintID" type_name="gh_guid" type_code="9">%s</item></items>'
            '<chunks count="2"><chunk name="Attributes"><items count="2">'
            '<item name="Bounds" type_name="gh_drawing_rectanglef" type_code="35">'
            "<X>%d</X><Y>%d</Y><W>35</W><H>20</H></item>"
            '<item name="Pivot" type_name="gh_drawing_pointf" type_code="31">'
            "<X>%d</X><Y>%d</Y></item></items></chunk>"
            '<chunk name="ConverterData"><items count="2">'
            '<item name="AssemblyName" type_name="gh_string" type_code="10">System.Private.CoreLib</item>'
            '<item name="TypeName" type_name="gh_string" type_code="10">System.Object</item>'
            "</items></chunk></chunks></chunk>"
            % (i, pn, g, pn, pn, _TYPEHINT_OBJECT_GUID, bx + 70, yy, bx + 87, yy + 10)
        )

    pd = [
        '<item name="InputCount" type_name="gh_int32" type_code="3">%d</item>'
        % len(inputs)
    ]
    for i in range(len(inputs)):
        pd.append(
            '<item name="InputId" index="%d" type_name="gh_guid" type_code="9">%s</item>'
            % (i, _SCRIPT_PARAM_GUID)
        )
    pd.append(
        '<item name="OutputCount" type_name="gh_int32" type_code="3">%d</item>'
        % len(outputs)
    )
    for i in range(len(outputs)):
        pd.append(
            '<item name="OutputId" index="%d" type_name="gh_guid" type_code="9">%s</item>'
            % (i, _SCRIPT_PARAM_GUID)
        )

    return (
        '<Fragment name="ScriptComp"><items count="11">'
        '<item name="Description" type_name="gh_string" type_code="10">%(name)s</item>'
        '<item name="GraftStandardOutputLines" type_name="gh_bool" type_code="1">true</item>'
        '<item name="InstanceGuid" type_name="gh_guid" type_code="9">%(comp)s</item>'
        '<item name="MarshGuids" type_name="gh_bool" type_code="1">true</item>'
        '<item name="Name" type_name="gh_string" type_code="10">%(name)s</item>'
        '<item name="NickName" type_name="gh_string" type_code="10">%(name)s</item>'
        '<item name="ScriptComponentVersion" type_name="gh_int32" type_code="3">3</item>'
        '<item name="UsingLibraryInputParam" type_name="gh_bool" type_code="1">false</item>'
        '<item name="UsingScriptInputParam" type_name="gh_bool" type_code="1">false</item>'
        '<item name="UsingScriptOutputParam" type_name="gh_bool" type_code="1">false</item>'
        '<item name="UsingStandardOutputParam" type_name="gh_bool" type_code="1">false</item>'
        '</items><chunks count="3">'
        '<chunk name="Attributes"><items count="2">'
        '<item name="Bounds" type_name="gh_drawing_rectanglef" type_code="35">'
        "<X>%(bx)d</X><Y>%(by)d</Y><W>104</W><H>44</H></item>"
        '<item name="Pivot" type_name="gh_drawing_pointf" type_code="31">'
        "<X>%(x)d</X><Y>%(y)d</Y></item></items></chunk>"
        '<chunk name="ParameterData"><items count="%(pdn)d">%(pd)s</items>'
        '<chunks count="%(npc)d">%(pc)s</chunks></chunk>'
        '<chunk name="Script"><items count="5">'
        '<item name="MarshGuids" type_name="gh_bool" type_code="1">true</item>'
        '<item name="MarshInputs" type_name="gh_bool" type_code="1">false</item>'
        '<item name="MarshOutputs" type_name="gh_bool" type_code="1">true</item>'
        '<item name="Text" type_name="gh_string" type_code="10">%(code)s</item>'
        '<item name="Title" type_name="gh_string" type_code="10">%(name)s</item></items>'
        '<chunks count="1"><chunk name="LanguageSpec"><items count="2">'
        '<item name="Taxon" type_name="gh_string" type_code="10">*.*.python</item>'
        '<item name="Version" type_name="gh_string" type_code="10">3.*</item>'
        "</items></chunk></chunks></chunk></chunks></Fragment>"
    ) % {
        "name": name_e,
        "comp": comp_guid,
        "bx": bx,
        "by": by,
        "x": x,
        "y": y,
        "pdn": len(pd),
        "pd": "".join(pd),
        "npc": len(inputs) + len(outputs),
        "pc": "".join(in_chunks) + "".join(out_chunks),
        "code": code_b64,
    }


def _xml_escape(text: str) -> str:
    """Escape the three XML-significant characters for element content."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@mcp.tool()
def add_python_component(
    code: str,
    name: str = "Python 3 Script",
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    x: int = 200,
    y: int = 200,
    solve: bool = True,
) -> dict:
    """Add a Rhino 8 Python 3 script component to the active Grasshopper document.

    The component is built directly as a GH_IO archive and deserialized into a
    fresh instance, so it carries your code and the exact input/output params
    you ask for — no manual paste-and-wire step.

    Your ``code`` should define a ``Script_Instance`` class whose ``RunScript``
    signature matches ``inputs`` and whose return matches ``outputs``::

        import Grasshopper

        class Script_Instance(Grasshopper.Kernel.GH_ScriptInstance):
            def RunScript(self, a, b):
                return a + b

    To import a ``.py`` module that lives next to the saved ``.gh`` file with
    live reload, use the two-part COMPAS pattern: call
    :func:`add_reloader_component` once per document to start the watcher, then
    have each consumer component begin with ``from compas_rhino import DevTools``
    / ``DevTools.ensure_path()`` before importing the side-car module. The
    document must be saved for the folder to be discoverable.

    Parameters
    ----------
    code : str
        Full Python source for the component (including the ``Script_Instance``
        class). The leading ``# r:`` / ``# requirements:`` directives are
        honoured by Rhino's script editor.
    name : str, optional
        Component name / nickname. Defaults to ``"Python 3 Script"``.
    inputs : list of str, optional
        Input parameter names (dynamic type hint). Defaults to none.
    outputs : list of str, optional
        Output parameter names (Object type hint). Defaults to ``["a"]``.
    x, y : int, optional
        Canvas pivot for the new component.
    solve : bool, optional
        If true (default), expire only the new component and schedule a
        solution on the UI thread so it computes a value. Read the result back
        with :func:`list_grasshopper_objects` or :func:`run_python_script`.

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`; ``result`` reprs
        ``{"added", "read_ok", "guid", "name"}``.
    """
    inputs = [] if inputs is None else list(inputs)
    outputs = ["a"] if outputs is None else list(outputs)
    if not outputs:
        # A zero-output script component built via EmitObject + AddObject hard
        # crashes Rhino during attribute layout (the GH UI path initializes it
        # differently and survives). Refuse rather than take Rhino down.
        raise ValueError(
            "add_python_component requires at least one output; a zero-output "
            "script component crashes Rhino when inserted via this API."
        )
    xml = _build_script_component_xml(code, name, inputs, outputs, x, y)
    xml_b64 = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    snippet = (
        _UI_THREAD_BOOTSTRAP + "import base64, System\n"
        "import Grasshopper as gh\n"
        "import GH_IO.Serialization as ser\n"
        "xml = base64.b64decode('%s').decode('utf-8')\n"
        % xml_b64
        + "SOLVE = %s\n" % bool(solve)
        + "COMP_GUID = '%s'\n" % _SCRIPT_COMPONENT_GUID
        + "def _do():\n"
        "    doc = gh.Instances.ActiveCanvas.Document\n"
        "    chunk = ser.GH_LooseChunk('ScriptComp')\n"
        "    chunk.Deserialize_Xml(xml)\n"
        "    comp = gh.Instances.ComponentServer.EmitObject(System.Guid(COMP_GUID))\n"
        "    if comp is None:\n"
        "        raise Exception('Could not instantiate the Rhino 8 Python 3 script component')\n"
        "    ok = comp.Read(chunk)\n"
        "    added = doc.AddObject(comp, False)\n"
        "    if SOLVE:\n"
        "        comp.ExpireSolution(False)\n"
        "        doc.ScheduleSolution(50)\n"
        "    return {'added': added, 'read_ok': ok, 'guid': str(comp.InstanceGuid), 'name': comp.NickName}\n"
        "_ = _lamcp_run_on_ui(_do)\n"
    )
    return run_python_script(snippet)


@mcp.tool()
def add_reloader_component(x: int = 60, y: int = 130, solve: bool = True) -> dict:
    """Add the COMPAS side-by-side hot-reload bootstrap component.

    Drops a small, parameterless Python 3 script component that runs
    ``compas_rhino.devtools.DevTools.enable_reloader()``. Solving it once
    appends the saved ``.gh`` file's folder to ``sys.path`` and starts a
    ``FileSystemWatcher`` that drops a side-car module from ``sys.modules``
    whenever its ``.py`` file changes — so edits hot-reload on the next solve.

    This is half of the COMPAS side-car workflow: add this once per document,
    then have each *consumer* component start with ``from compas_rhino import
    DevTools`` / ``DevTools.ensure_path()`` before importing the side-car
    module (see :func:`add_python_component`).

    The document must already be saved — ``enable_reloader`` needs a folder to
    watch and raises otherwise. Adding a second bootstrap component is harmless
    but redundant; check :func:`list_grasshopper_objects` first if unsure.

    Built as a single-output ``Script_Instance`` (returning a status string)
    rather than a parameterless component: a zero-output component inserted via
    this API crashes Rhino during layout.

    Parameters
    ----------
    x, y : int, optional
        Canvas pivot for the bootstrap component.
    solve : bool, optional
        If true (default), solve it immediately so the watcher starts now.

    Returns
    -------
    dict
        Same envelope as :func:`add_python_component`.
    """
    code = (
        "# r: compas\n"
        "import Grasshopper\n"
        "from compas_rhino.devtools import DevTools\n"
        "\n"
        "\n"
        "class Script_Instance(Grasshopper.Kernel.GH_ScriptInstance):\n"
        "    def RunScript(self):\n"
        "        DevTools.enable_reloader()\n"
        "        return 'COMPAS side-by-side reloader enabled'\n"
    )
    return add_python_component(
        code, name="Enable reload", inputs=[], outputs=["status"], x=x, y=y, solve=solve
    )


@mcp.tool()
def set_script_venv(
    venv: str,
    only_nicknames: list[str] | None = None,
    only_guids: list[str] | None = None,
    add_if_missing: bool = False,
    solve: bool = True,
) -> dict:
    """Point the ``# venv:`` directive of script components at one environment.

    Rhino 8 script components select their script environment with a leading
    ``# venv: <name>`` comment on the first line of the code. This tool rewrites
    that directive across every Python 3 / script component on the active
    canvas so they all reference the same ``venv``.

    The edit is made by setting the component's ``IScriptComponent.Text``
    property directly (reached by reflection — it is an explicit interface
    implementation). That mutates *only* the script body, leaving input sources
    and wiring untouched. The naive alternative — round-tripping the whole
    component through ``GH_LooseChunk`` Write/Read — re-deserializes the input
    parameters and reconnects them to a phantom source at the origin, leaving a
    ghost component on the canvas. This avoids that entirely.

    Thread + solver safety: the whole edit runs on the UI thread via
    ``_lamcp_run_on_ui`` — mutating ``Text``/``LanguageSpec`` from the bridge's
    HTTP thread hard-crashes Rhino (see the module docstring). Within that
    UI-thread call the batch is wrapped in ``doc.Enabled = False`` … restore (no
    intermediate solves fire while editing), and recompute is requested once via
    ``ScheduleSolution`` only when ``solve`` is true. Setting ``Text`` also
    resets the component's language taxon to ``*.*.*`` ("Can not determine input
    code language"), so the ``LanguageSpec`` is captured and restored around each
    ``Text`` write. The bridge component is always skipped (matched by the
    ``LAMCP Bridge`` marker in its source) so it is never repointed or torn down.

    Components are matched by ``GetType().FullName`` ending in either
    ``Python3Component`` *or* ``ScriptComponent`` — a reopened/saved document
    reports its script components as the latter. The ``Text``/``LanguageSpec``
    properties are explicit-interface implementations (non-public), so they are
    found by walking ``BaseType`` with ``BindingFlags.NonPublic``.

    Parameters
    ----------
    venv : str
        Target environment name, e.g. ``"ca-fs26-focus-work"``.
    only_nicknames : list of str, optional
        If given, only components whose ``NickName`` is in this list are
        considered. Combined with ``only_guids`` as a union.
    only_guids : list of str, optional
        If given, only components whose instance GUID is in this list are
        considered. Combined with ``only_nicknames`` as a union.
    add_if_missing : bool, optional
        If true, prepend a ``# venv:`` directive to components that have none.
        Default false — components without an existing directive are left
        alone (and reported under ``skipped``).
    solve : bool, optional
        If true (default), expire the changed components and schedule a
        solution so they recompute against the new environment.

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`; ``result`` reprs
        ``{"target", "changed": [[nick, previous], ...], "added": [...],
        "already_ok": int, "skipped": [[nick, reason], ...], "errors": [...]}``.
    """
    target = venv
    only_nn = None if only_nicknames is None else list(only_nicknames)
    only_g = None if only_guids is None else list(only_guids)
    code = (
        _UI_THREAD_BOOTSTRAP + "import re\n"
        "import Grasshopper as gh\n"
        "from System.Reflection import BindingFlags\n"
        "TARGET = %r\n"
        % target
        + "ADD_IF_MISSING = %s\n" % bool(add_if_missing)
        + "DO_SOLVE = %s\n" % bool(solve)
        + "ONLY_NN = %r\n" % only_nn
        + "ONLY_G = %r\n" % only_g
        + "flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance\n"
        "venv_re = re.compile(r'^(\\s*#\\s*venv\\s*:\\s*)(.+?)(\\s*)$', re.M)\n"
        "def prop_by_name(obj, name):\n"
        "    cur = obj.GetType()\n"
        "    while cur is not None:\n"
        "        for p in cur.GetProperties(flags):\n"
        "            if p.Name == name:\n"
        "                return p\n"
        "        cur = cur.BaseType\n"
        "    return None\n"
        "def text_prop(obj):\n"
        "    return prop_by_name(obj, 'RhinoCodePlatform.GH.IScriptComponent.Text')\n"
        "def set_text_keep_lang(obj, prop, new_txt):\n"
        "    # Setting Text resets the component's language taxon to '*.*.*'\n"
        "    # ('Can not determine input code language'); capture the LanguageSpec\n"
        "    # first and restore it afterwards so the component stays Python.\n"
        "    lp = prop_by_name(obj, 'RhinoCodePlatform.GH.IScriptComponent.LanguageSpec')\n"
        "    spec = lp.GetValue(obj) if lp is not None else None\n"
        "    prop.SetValue(obj, new_txt)\n"
        "    if lp is not None and spec is not None:\n"
        "        lp.SetValue(obj, spec)\n"
        "def _do():\n"
        "    doc = gh.Instances.ActiveCanvas.Document\n"
        "    changed = []; added = []; already = 0; skipped = []; errors = []\n"
        "    changed_objs = []\n"
        "    prev_enabled = doc.Enabled\n"
        "    doc.Enabled = False\n"
        "    try:\n"
        "        for obj in doc.Objects:\n"
        "            tn = obj.GetType().FullName\n"
        "            if not (tn.endswith('Python3Component') or tn.endswith('ScriptComponent')):\n"
        "                continue\n"
        "            if ONLY_NN is not None or ONLY_G is not None:\n"
        "                ok = (ONLY_NN is not None and obj.NickName in ONLY_NN) or \\\n"
        "                     (ONLY_G is not None and str(obj.InstanceGuid) in ONLY_G)\n"
        "                if not ok:\n"
        "                    continue\n"
        "            prop = text_prop(obj)\n"
        "            if prop is None:\n"
        "                continue\n"
        "            try:\n"
        "                txt = prop.GetValue(obj)\n"
        "            except Exception as e:\n"
        "                errors.append((obj.NickName, repr(e))); continue\n"
        "            if txt is None:\n"
        "                continue\n"
        "            if 'LAMCP Bridge' in txt:\n"
        "                skipped.append((obj.NickName, 'bridge component')); continue\n"
        "            m = venv_re.search(txt)\n"
        "            if m:\n"
        "                if m.group(2) == TARGET:\n"
        "                    already += 1; continue\n"
        "                prev = m.group(2)\n"
        "                new_txt = venv_re.sub(lambda mm: mm.group(1) + TARGET + mm.group(3), txt, 1)\n"
        "                try:\n"
        "                    set_text_keep_lang(obj, prop, new_txt)\n"
        "                    changed.append((obj.NickName, prev)); changed_objs.append(obj)\n"
        "                except Exception as e:\n"
        "                    errors.append((obj.NickName, repr(e)))\n"
        "            elif ADD_IF_MISSING:\n"
        "                try:\n"
        "                    set_text_keep_lang(obj, prop, '# venv: ' + TARGET + '\\n' + txt)\n"
        "                    added.append(obj.NickName); changed_objs.append(obj)\n"
        "                except Exception as e:\n"
        "                    errors.append((obj.NickName, repr(e)))\n"
        "            else:\n"
        "                skipped.append((obj.NickName, 'no venv directive'))\n"
        "    finally:\n"
        "        doc.Enabled = prev_enabled\n"
        "    if DO_SOLVE and changed_objs:\n"
        "        for obj in changed_objs:\n"
        "            obj.ExpireSolution(False)\n"
        "        doc.ScheduleSolution(50)\n"
        "    return {'target': TARGET, 'changed': changed, 'added': added,\n"
        "            'already_ok': already, 'skipped': skipped, 'errors': errors}\n"
        "_ = _lamcp_run_on_ui(_do)\n"
    )
    return run_python_script(code)


@mcp.tool()
def solve_grasshopper(expire_all: bool = False) -> dict:
    """Re-solve the active Grasshopper document, safely.

    The expire + schedule run on the UI thread via ``_lamcp_run_on_ui``, and the
    solution itself is requested with ``ScheduleSolution`` rather than run
    synchronously. Both matter: the bridge ``exec()``s on its own HTTP server
    thread, so expiring objects from there is an unsafe cross-thread mutation,
    and a synchronous full solve (``NewSolution``) would re-run the bridge
    component on that very thread, tearing the server down mid-request.

    Parameters
    ----------
    expire_all : bool, optional
        If true, expire every object first (full recompute). If false
        (default), only objects already expired recompute.

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`.
    """
    code = (
        _UI_THREAD_BOOTSTRAP + "import Grasshopper as gh\n"
        "expire_all = %s\n" % bool(expire_all) + "def _do():\n"
        "    doc = gh.Instances.ActiveCanvas.Document\n"
        "    if expire_all:\n"
        "        for o in doc.Objects:\n"
        "            if hasattr(o, 'ExpireSolution'):\n"
        "                o.ExpireSolution(False)\n"
        "    doc.ScheduleSolution(50)\n"
        "    return {'scheduled': True, 'expired_all': expire_all}\n"
        "_ = _lamcp_run_on_ui(_do)\n"
    )
    return run_python_script(code)


@mcp.tool()
def save_grasshopper_document(path: str | None = None) -> dict:
    """Save the active Grasshopper document to disk.

    Parameters
    ----------
    path : str, optional
        Destination ``.gh`` path. Defaults to the document's current
        ``FilePath`` (it must already have been saved once).

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`; ``result`` reprs
        ``{"saved", "path", "bytes", "error"}``.
    """
    code = (
        _UI_THREAD_BOOTSTRAP + "import os\n"
        "import Grasshopper as gh\n"
        "PATH = %r\n" % path + "def _do():\n"
        "    doc = gh.Instances.ActiveCanvas.Document\n"
        "    path = PATH or doc.FilePath\n"
        "    saved = False; size = None; err = None\n"
        "    try:\n"
        "        io = gh.Kernel.GH_DocumentIO(doc)\n"
        "        saved = io.SaveQuiet(path)\n"
        "        if path and os.path.exists(path):\n"
        "            size = os.path.getsize(path)\n"
        "    except Exception as exc:\n"
        "        err = repr(exc)\n"
        "    return {'saved': saved, 'path': path, 'bytes': size, 'error': err}\n"
        "_ = _lamcp_run_on_ui(_do)\n"
    )
    return run_python_script(code)


_PARAM_MARKER_TYPES = {
    "generic": "Param_GenericObject",
    "plane": "Param_Plane",
    "number": "Param_Number",
    "integer": "Param_Integer",
    "string": "Param_String",
    "boolean": "Param_Boolean",
    "point": "Param_Point",
    "vector": "Param_Vector",
    "curve": "Param_Curve",
    "geometry": "Param_Geometry",
}


def _parse_hex_colour(spec: str) -> tuple[int, int, int, int]:
    """Parse ``#RRGGBB`` or ``#RRGGBBAA`` into an ARGB tuple."""
    s = spec.lstrip("#")
    if len(s) == 6:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return (255, r, g, b)
    if len(s) == 8:
        r, g, b, a = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16)
        return (a, r, g, b)
    raise ValueError("Colour must be '#RRGGBB' or '#RRGGBBAA', got {!r}".format(spec))


@mcp.tool()
def add_group(
    name: str,
    member_guids: list[str],
    colour: str | None = None,
) -> dict:
    """Wrap GH objects in a named ``GH_Group`` on the active canvas.

    Groups are the primary visual structuring device on the canvas — they
    label conceptually distinct subgraphs (e.g. "ROS Connection", "Plan
    Cartesian Motion") and make the canvas readable when it grows beyond a
    handful of components. Pair with :func:`add_param_marker` to publish a
    group's outputs as labeled buses that downstream groups can wire to.

    Group geometry (its bounds) is derived from member positions, so members
    must already be on the canvas. Adding objects to a group does not modify
    their wiring or pivots; you can also call this *after* you finish
    assembling a subgraph.

    Parameters
    ----------
    name : str
        Group nickname. Shown at the top-left of the group.
    member_guids : list of str
        Instance GUIDs of objects to include. Must be non-empty.
    colour : str, optional
        Group fill colour as ``"#RRGGBB"`` or ``"#RRGGBBAA"``. Defaults to
        Grasshopper's default semi-transparent grey.

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`; ``result`` reprs
        ``{"guid", "name", "member_count"}``.
    """
    if not member_guids:
        raise ValueError("add_group requires at least one member_guid")
    argb = _parse_hex_colour(colour) if colour else None
    code = (
        _UI_THREAD_BOOTSTRAP
        + "import Grasshopper as gh, System\n"
        + "NAME = {!r}\n".format(name)
        + "GUIDS = {!r}\n".format(list(member_guids))
        + "ARGB = {!r}\n".format(argb)
        + "def _do():\n"
        "    doc = gh.Instances.ActiveCanvas.Document\n"
        "    g = gh.Kernel.Special.GH_Group()\n"
        "    g.NickName = NAME\n"
        "    for guid in GUIDS:\n"
        "        g.AddObject(System.Guid(guid))\n"
        "    if ARGB is not None:\n"
        "        a, r, gn, b = ARGB\n"
        "        g.Colour = System.Drawing.Color.FromArgb(a, r, gn, b)\n"
        "    doc.AddObject(g, False)\n"
        "    return {'guid': str(g.InstanceGuid), 'name': g.NickName, 'member_count': len(list(g.ObjectIDs))}\n"
        "_ = _lamcp_run_on_ui(_do)\n"
    )
    return run_python_script(code)


@mcp.tool()
def add_param_marker(
    name: str,
    x: int,
    y: int,
    param_type: str = "generic",
) -> dict:
    """Drop a named floating parameter on the canvas as a labeled pass-through bus.

    Param markers are the canvas-authoring idiom for "this is a meaningful
    boundary value". They make subgraphs composable: wire a group's
    important outputs into named markers, and downstream groups wire FROM
    those markers — the name labels the connection visibly and survives
    moves and refactors.

    Typical use: after assembling a planner subgraph, drop a
    ``Param_GenericObject`` named ``"planner"`` at its output and wire the
    planner component's output into it. Any downstream subgraph that needs
    the planner reads from the marker, not from the component directly.

    The marker holds no persistent value of its own — it just relays data
    through.

    Parameters
    ----------
    name : str
        Nickname displayed on the marker (e.g. ``"planner"``,
        ``"robot_cell"``, ``"motion_end_state"``).
    x, y : int
        Canvas pivot in document coordinates.
    param_type : str, optional
        One of ``"generic"`` (default), ``"plane"``, ``"number"``,
        ``"integer"``, ``"string"``, ``"boolean"``, ``"point"``,
        ``"vector"``, ``"curve"``, ``"geometry"``. ``"generic"`` accepts
        any compas_fab / COMPAS object and is the right choice for the
        cross-section bus pattern.

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`; ``result`` reprs
        ``{"guid", "name", "type"}``.
    """
    if param_type not in _PARAM_MARKER_TYPES:
        raise ValueError(
            "Unknown param_type {!r}; expected one of: {}".format(
                param_type, sorted(_PARAM_MARKER_TYPES)
            )
        )
    cls_name = _PARAM_MARKER_TYPES[param_type]
    code = (
        _UI_THREAD_BOOTSTRAP
        + "import Grasshopper as gh, System\n"
        + "NAME = {!r}\n".format(name)
        + "X = {!r}\n".format(int(x))
        + "Y = {!r}\n".format(int(y))
        + "CLS_NAME = {!r}\n".format(cls_name)
        + "def _do():\n"
        "    doc = gh.Instances.ActiveCanvas.Document\n"
        "    cls = getattr(gh.Kernel.Parameters, CLS_NAME)\n"
        "    p = cls()\n"
        "    p.NickName = NAME\n"
        "    if p.Attributes is None:\n"
        "        p.CreateAttributes()\n"
        "    p.Attributes.Pivot = System.Drawing.PointF(float(X), float(Y))\n"
        "    doc.AddObject(p, False)\n"
        "    return {'guid': str(p.InstanceGuid), 'name': p.NickName, 'type': p.GetType().Name}\n"
        "_ = _lamcp_run_on_ui(_do)\n"
    )
    return run_python_script(code)


@mcp.tool()
def describe_canvas_structure() -> dict:
    """Richer counterpart to :func:`list_grasshopper_objects` that includes group memberships and parameter source connections.

    For each object on the canvas, reports:

    - The fields already exposed by :func:`list_grasshopper_objects`
      (type, nickname, guid, pivot, errors, warnings).
    - If it is a ``GH_Group``: a ``group_members`` list of the contained
      object GUIDs.
    - If it is a component: ``inputs`` (with per-param ``sources`` listing
      the upstream owner GUID + source param name/nickname) and
      ``outputs``.
    - If it is a floating parameter (e.g. a named ``Param_GenericObject``
      bus marker): a ``sources`` list with the same shape.

    This is the inspection tool to call when you need to learn from an
    existing canvas — the pattern of who-wires-to-what plus group
    boundaries is enough to reconstruct subgraphs without a follow-up
    round-trip per object.

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`. ``result`` is the
        ``repr`` of ``{"document", "path", "count", "objects": [...]}``.
        Each object entry carries the fields described above; absent keys
        mean "not applicable" (e.g. ``group_members`` only appears on
        ``GH_Group`` entries).
    """
    code = (
        "import Grasshopper as gh\n"
        "doc = gh.Instances.ActiveCanvas.Document\n"
        "L = gh.Kernel.GH_RuntimeMessageLevel\n"
        "def _pinfo(p):\n"
        "    return {'name': getattr(p, 'Name', None), 'nickname': getattr(p, 'NickName', None), 'guid': str(p.InstanceGuid)}\n"
        "def _srcs(p):\n"
        "    out = []\n"
        "    for src in p.Sources:\n"
        "        owner = src.Attributes.GetTopLevel.DocObject if src.Attributes else None\n"
        "        out.append({'owner_guid': str(owner.InstanceGuid) if owner is not None else None,\n"
        "                    'param_name': getattr(src, 'Name', None),\n"
        "                    'param_nickname': getattr(src, 'NickName', None)})\n"
        "    return out\n"
        "objs = []\n"
        "if doc is not None:\n"
        "    for o in doc.Objects:\n"
        "        piv = o.Attributes.Pivot if o.Attributes else None\n"
        "        rm = getattr(o, 'RuntimeMessages', None)\n"
        "        entry = {\n"
        "            'type': o.GetType().FullName,\n"
        "            'nickname': o.NickName,\n"
        "            'guid': str(o.InstanceGuid),\n"
        "            'pivot': [piv.X, piv.Y] if piv else None,\n"
        "            'errors': list(rm(L.Error)) if rm else [],\n"
        "            'warnings': list(rm(L.Warning)) if rm else [],\n"
        "        }\n"
        "        if isinstance(o, gh.Kernel.Special.GH_Group):\n"
        "            entry['group_members'] = [str(g) for g in o.ObjectIDs]\n"
        "        elif hasattr(o, 'Params'):\n"
        "            entry['inputs'] = [dict(_pinfo(p), sources=_srcs(p)) for p in o.Params.Input]\n"
        "            entry['outputs'] = [_pinfo(p) for p in o.Params.Output]\n"
        "        elif hasattr(o, 'Sources'):\n"
        "            entry['sources'] = _srcs(o)\n"
        "        objs.append(entry)\n"
        "_ = {\n"
        "    'document': None if doc is None else doc.DisplayName,\n"
        "    'path': None if doc is None else doc.FilePath,\n"
        "    'count': len(objs), 'objects': objs,\n"
        "}\n"
    )
    return run_python_script(code)


_UPGRADE_COMPONENTS_SCRIPT = """
import base64
import Grasshopper as gh
import System

doc = gh.Instances.ActiveCanvas.Document
server = gh.Instances.ComponentServer

CATEGORIES = set(__CATEGORIES__) if __CATEGORIES__ is not None else None
NICKNAMES = set(__NICKNAMES__) if __NICKNAMES__ is not None else None
DRY_RUN = bool(__DRY_RUN__)

# Build (Category, SubCategory, Name) -> ObjectProxy
proxy_by_key = {}
for p in server.ObjectProxies:
    d = p.Desc
    if d.Category and d.Name:
        proxy_by_key[(d.Category, d.SubCategory or '', d.Name)] = p

def _snapshot(o):
    inputs = {}
    for ip in o.Params.Input:
        srcs = []
        for s in ip.Sources:
            owner = s.Attributes.GetTopLevel.DocObject if s.Attributes else None
            if owner is None:
                continue
            srcs.append({
                'owner_guid': str(owner.InstanceGuid),
                'owner_is_component': hasattr(owner, 'Params'),
                'source_param_name': s.Name if hasattr(owner, 'Params') else '',
            })
        inputs[ip.Name] = srcs
    outputs = {}
    for op in o.Params.Output:
        tgts = []
        for other in doc.Objects:
            if other is o:
                continue
            if hasattr(other, 'Params'):
                for ip2 in other.Params.Input:
                    if any(s.InstanceGuid == op.InstanceGuid for s in ip2.Sources):
                        tgts.append({'target_guid': str(other.InstanceGuid),
                                     'target_param_name': ip2.Name,
                                     'target_is_floating': False})
            elif hasattr(other, 'Sources'):
                if any(s.InstanceGuid == op.InstanceGuid for s in other.Sources):
                    tgts.append({'target_guid': str(other.InstanceGuid),
                                 'target_param_name': '',
                                 'target_is_floating': True})
        outputs[op.Name] = tgts
    groups = []
    for other in doc.Objects:
        if isinstance(other, gh.Kernel.Special.GH_Group):
            if o.InstanceGuid in list(other.ObjectIDs):
                groups.append(str(other.InstanceGuid))
    return {'inputs': inputs, 'outputs': outputs, 'groups': groups,
            'pivot': [o.Attributes.Pivot.X, o.Attributes.Pivot.Y],
            'nickname': o.NickName}

plan = []
skipped = []
for o in doc.Objects:
    if not hasattr(o, 'Params'):
        continue
    cat = getattr(o, 'Category', None) or ''
    if CATEGORIES is not None and cat not in CATEGORIES:
        continue
    if NICKNAMES is not None and o.NickName not in NICKNAMES:
        continue
    name = getattr(o, 'Name', None)
    subcat = getattr(o, 'SubCategory', None) or ''
    key = (cat, subcat, name)
    proxy = proxy_by_key.get(key)
    if proxy is None:
        skipped.append({'guid': str(o.InstanceGuid), 'key': list(key),
                        'reason': 'no matching installed userobject'})
        continue
    snap = _snapshot(o)
    fresh = proxy.CreateInstance()
    new_ins = {p.Name for p in fresh.Params.Input}
    new_outs = {p.Name for p in fresh.Params.Output}
    carried = (sum(len(v) for k, v in snap['inputs'].items() if k in new_ins)
               + sum(len(v) for k, v in snap['outputs'].items() if k in new_outs))
    dropped = []
    for in_name, srcs in snap['inputs'].items():
        if in_name not in new_ins:
            for s in srcs:
                dropped.append({'old_component_guid': str(o.InstanceGuid),
                                'side': 'input', 'name': in_name,
                                'other_guid': s['owner_guid'],
                                'reason': 'input removed in new version'})
    for out_name, tgts in snap['outputs'].items():
        if out_name not in new_outs:
            for t in tgts:
                dropped.append({'old_component_guid': str(o.InstanceGuid),
                                'side': 'output', 'name': out_name,
                                'other_guid': t['target_guid'],
                                'reason': 'output removed in new version'})
    plan.append({'old_guid': str(o.InstanceGuid), 'old_obj': o, 'fresh': fresh,
                 'name': name, 'snap': snap, 'new_ins': new_ins, 'new_outs': new_outs,
                 'carried': carried, 'dropped': dropped})

if not DRY_RUN and plan:
    def _do():
        guid_map = {}
        # Phase A: remove old, add new at the same pivot with the same NickName.
        for e in plan:
            fresh = e['fresh']
            snap = e['snap']
            if fresh.Attributes is None:
                fresh.CreateAttributes()
            fresh.Attributes.Pivot = System.Drawing.PointF(float(snap['pivot'][0]), float(snap['pivot'][1]))
            fresh.NickName = snap['nickname']
            doc.RemoveObject(e['old_obj'], True)
            doc.AddObject(fresh, False)
            guid_map[e['old_guid']] = fresh

        def _resolve(guid_str):
            if guid_str in guid_map:
                return guid_map[guid_str]
            return doc.FindObject(System.Guid(guid_str), True)

        # Phase B: rewire inputs + outputs and restore group memberships.
        for e in plan:
            fresh = e['fresh']
            snap = e['snap']
            for in_name, srcs in snap['inputs'].items():
                if in_name not in e['new_ins']:
                    continue
                tgt_param = next(p for p in fresh.Params.Input if p.Name == in_name)
                for s in srcs:
                    other = _resolve(s['owner_guid'])
                    if other is None:
                        continue
                    if s['owner_is_component'] and hasattr(other, 'Params'):
                        src_param = next((p for p in other.Params.Output if p.Name == s['source_param_name']), None)
                    else:
                        src_param = other
                    if src_param is not None:
                        tgt_param.AddSource(src_param)
            for out_name, tgts in snap['outputs'].items():
                if out_name not in e['new_outs']:
                    continue
                src_param = next(p for p in fresh.Params.Output if p.Name == out_name)
                for t in tgts:
                    other = _resolve(t['target_guid'])
                    if other is None:
                        continue
                    if t['target_is_floating']:
                        if hasattr(other, 'AddSource'):
                            other.AddSource(src_param)
                    elif hasattr(other, 'Params'):
                        tp = next((p for p in other.Params.Input if p.Name == t['target_param_name']), None)
                        if tp is not None:
                            tp.AddSource(src_param)
            for grp_guid in snap['groups']:
                grp = doc.FindObject(System.Guid(grp_guid), True)
                if grp is not None:
                    grp.AddObject(fresh.InstanceGuid)
        return {'updated_count': len(plan)}

    _lamcp_run_on_ui(_do)

_ = {
    'document': doc.DisplayName,
    'dry_run': DRY_RUN,
    'updated': [{'old_guid': e['old_guid'],
                 'new_guid': str(e['fresh'].InstanceGuid),
                 'name': e['name'],
                 'carried_wires': e['carried'],
                 'dropped_wires': len(e['dropped'])} for e in plan],
    'dropped_wires': [w for e in plan for w in e['dropped']],
    'skipped': skipped,
}
"""


@mcp.tool()
def upgrade_components(
    only_categories: list[str] | None = None,
    only_nicknames: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Replace canvas instances of userobject-based GH components with their latest installed version.

    Identifies each on-canvas component against the installed
    ``ComponentServer`` proxies by the ``(Category, SubCategory, Name)`` triple
    — *not* by component proxy GUID (userobject rebuilds generate new GUIDs)
    and *not* by ``NickName`` alone (users customise it). For each match the
    tool snapshots the current instance's pivot, ``NickName``, group
    memberships, and per-input/per-output wires (recording the **other**
    end of each wire by GUID + param name, since the local end's GUIDs die
    on removal), then removes the old instance and drops a fresh one
    in-place. Wires are restored by name match against the new component's
    pins; any input or output that was renamed/removed in the new version
    becomes a *dropped wire* reported in the result.

    Default scope is ``only_categories=["COMPAS FAB"]`` — pass an explicit
    list to widen or narrow. Use ``dry_run=True`` first to see what would be
    updated and which wires would be dropped, then call again with
    ``dry_run=False`` to perform the swap.

    Parameters
    ----------
    only_categories : list of str, optional
        Limit the upgrade to components whose ``Category`` is in this list.
        Defaults to ``["COMPAS FAB"]``.
    only_nicknames : list of str, optional
        Further restrict by on-canvas ``NickName`` (matched exactly).
        Useful for "upgrade just this one Cf_PlanMotion".
    dry_run : bool, optional
        If True, report what would change without mutating the document.
        Defaults to False.

    Returns
    -------
    dict
        Same envelope as :func:`run_python_script`; ``result`` reprs
        ``{"document", "dry_run", "updated": [...], "dropped_wires": [...],
        "skipped": [...]}``. ``updated`` entries carry ``carried_wires`` and
        ``dropped_wires`` counts; ``dropped_wires`` lists each lost wire with
        the side (input/output), pin name, the other end's GUID, and the
        reason.
    """
    if only_categories is None:
        only_categories = ["COMPAS FAB"]
    script_body = (
        _UPGRADE_COMPONENTS_SCRIPT.replace(
            "__CATEGORIES__", repr(list(only_categories))
        )
        .replace(
            "__NICKNAMES__",
            repr(list(only_nicknames)) if only_nicknames is not None else "None",
        )
        .replace("__DRY_RUN__", repr(bool(dry_run)))
    )
    return run_python_script(_UI_THREAD_BOOTSTRAP + script_body)


def main():
    """Run the MCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
