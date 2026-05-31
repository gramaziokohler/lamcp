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
        "import base64, System\n"
        "import Grasshopper as gh\n"
        "import GH_IO.Serialization as ser\n"
        "xml = base64.b64decode('%s').decode('utf-8')\n"
        % xml_b64
        + "doc = gh.Instances.ActiveCanvas.Document\n"
        "chunk = ser.GH_LooseChunk('ScriptComp')\n"
        "chunk.Deserialize_Xml(xml)\n"
        "comp = gh.Instances.ComponentServer.EmitObject(System.Guid('%s'))\n"
        % _SCRIPT_COMPONENT_GUID
        + "if comp is None:\n"
        "    raise Exception('Could not instantiate the Rhino 8 Python 3 script component')\n"
        "ok = comp.Read(chunk)\n"
        "added = doc.AddObject(comp, False)\n"
        "if %s:\n" % bool(solve) + "    comp.ExpireSolution(False)\n"
        "    doc.ScheduleSolution(50)\n"
        "_ = {'added': added, 'read_ok': ok, 'guid': str(comp.InstanceGuid), 'name': comp.NickName}\n"
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

    Solver safety: setting the text expires the component, which would schedule
    a solution; with many edits one of those solutions re-runs the LAMCP Bridge
    component on its own HTTP thread and tears the server down mid-request. So
    the whole batch runs with ``doc.Enabled = False`` (no solves fire during
    editing), the solver is restored afterwards, and recompute is requested via
    ``ScheduleSolution`` on the UI thread (the same decoupling as
    :func:`solve_grasshopper`). The bridge component is always skipped (matched
    by the ``LAMCP Bridge`` marker in its source) so it is never repointed or
    torn down.

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
        "import re\n"
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
        "doc = gh.Instances.ActiveCanvas.Document\n"
        "changed = []; added = []; already = 0; skipped = []; errors = []\n"
        "changed_objs = []\n"
        "prev_enabled = doc.Enabled\n"
        "doc.Enabled = False\n"
        "try:\n"
        "    for obj in doc.Objects:\n"
        "        tn = obj.GetType().FullName\n"
        "        if not (tn.endswith('Python3Component') or tn.endswith('ScriptComponent')):\n"
        "            continue\n"
        "        if ONLY_NN is not None or ONLY_G is not None:\n"
        "            ok = (ONLY_NN is not None and obj.NickName in ONLY_NN) or \\\n"
        "                 (ONLY_G is not None and str(obj.InstanceGuid) in ONLY_G)\n"
        "            if not ok:\n"
        "                continue\n"
        "        prop = text_prop(obj)\n"
        "        if prop is None:\n"
        "            continue\n"
        "        try:\n"
        "            txt = prop.GetValue(obj)\n"
        "        except Exception as e:\n"
        "            errors.append((obj.NickName, repr(e))); continue\n"
        "        if txt is None:\n"
        "            continue\n"
        "        if 'LAMCP Bridge' in txt:\n"
        "            skipped.append((obj.NickName, 'bridge component')); continue\n"
        "        m = venv_re.search(txt)\n"
        "        if m:\n"
        "            if m.group(2) == TARGET:\n"
        "                already += 1; continue\n"
        "            prev = m.group(2)\n"
        "            new_txt = venv_re.sub(lambda mm: mm.group(1) + TARGET + mm.group(3), txt, 1)\n"
        "            try:\n"
        "                set_text_keep_lang(obj, prop, new_txt)\n"
        "                changed.append((obj.NickName, prev)); changed_objs.append(obj)\n"
        "            except Exception as e:\n"
        "                errors.append((obj.NickName, repr(e)))\n"
        "        elif ADD_IF_MISSING:\n"
        "            try:\n"
        "                set_text_keep_lang(obj, prop, '# venv: ' + TARGET + '\\n' + txt)\n"
        "                added.append(obj.NickName); changed_objs.append(obj)\n"
        "            except Exception as e:\n"
        "                errors.append((obj.NickName, repr(e)))\n"
        "        else:\n"
        "            skipped.append((obj.NickName, 'no venv directive'))\n"
        "finally:\n"
        "    doc.Enabled = prev_enabled\n"
        "if DO_SOLVE and changed_objs:\n"
        "    for obj in changed_objs:\n"
        "        obj.ExpireSolution(False)\n"
        "    doc.ScheduleSolution(50)\n"
        "_ = {'target': TARGET, 'changed': changed, 'added': added,\n"
        "     'already_ok': already, 'skipped': skipped, 'errors': errors}\n"
    )
    return run_python_script(code)


@mcp.tool()
def solve_grasshopper(expire_all: bool = False) -> dict:
    """Re-solve the active Grasshopper document, safely.

    The solution is always scheduled on Grasshopper's UI thread via
    ``ScheduleSolution`` rather than run synchronously. This matters: the
    bridge ``exec()``s on its own HTTP server thread, and a synchronous full
    solve (``NewSolution``) re-runs the bridge component on that very thread,
    tearing the server down mid-request. Scheduling decouples the two.

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
        "import Grasshopper as gh\n"
        "doc = gh.Instances.ActiveCanvas.Document\n"
        "expire_all = %s\n" % bool(expire_all) + "if expire_all:\n"
        "    for o in doc.Objects:\n"
        "        if hasattr(o, 'ExpireSolution'):\n"
        "            o.ExpireSolution(False)\n"
        "doc.ScheduleSolution(50)\n"
        "_ = {'scheduled': True, 'expired_all': expire_all}\n"
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
        "import os\n"
        "import Grasshopper as gh\n"
        "doc = gh.Instances.ActiveCanvas.Document\n"
        "path = %r or doc.FilePath\n"
        % path
        + "saved = False; size = None; err = None\n"
        "try:\n"
        "    io = gh.Kernel.GH_DocumentIO(doc)\n"
        "    saved = io.SaveQuiet(path)\n"
        "    if path and os.path.exists(path):\n"
        "        size = os.path.getsize(path)\n"
        "except Exception as exc:\n"
        "    err = repr(exc)\n"
        "_ = {'saved': saved, 'path': path, 'bytes': size, 'error': err}\n"
    )
    return run_python_script(code)


def main():
    """Run the MCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
