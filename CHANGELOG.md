# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Changed

* `upgrade_components` now matches Rhino 8 RhinoCode *script* components, which
  report a generic `Maths`/`Script` category on the canvas that never equals
  their installed userobject proxy's (e.g. `COMPAS FAB`/`Robot Cell`) — and can
  even differ between freshly-dropped and saved instances. When the
  `(Category, SubCategory, Name)` triple misses, the tool falls back to matching
  by `Name` alone (only when that Name maps to exactly one installed proxy, so an
  unrelated generic script is never swapped by accident), and applies
  `only_categories` against the resolved **proxy's** category so
  `["COMPAS FAB"]` still selects them. New `match_by_name` parameter (default
  `True`) toggles the fallback; each `updated` entry now reports whether it
  matched `by` `"triple"` or `"name"`.

## [0.6.1] - 2026-06-04

### Fixed

* Linter issues

## [0.6.0] - 2026-06-04

### Added

* `upgrade_components(only_categories, only_nicknames, dry_run)` — replace
  on-canvas instances of userobject-based GH components with their latest
  installed version. Identifies each instance against the installed
  `ComponentServer` proxies by the `(Category, SubCategory, Name)` triple
  (*not* by proxy GUID — userobject rebuilds invalidate those — and *not*
  by `NickName` alone — users customise it). Snapshots pivot, NickName,
  group memberships, and per-pin wires (recording the **other** end of
  each wire so source GUIDs dying on removal don't matter), then swaps in
  a fresh instance and restores everything by name. Pins that were
  renamed/removed in the new version surface as *dropped wires* in the
  return value. Default scope `["COMPAS FAB"]`; pass `dry_run=True` to
  preview before mutating. Mutating, UI-thread safe.

## [0.5.0] - 2026-06-02

### Added

* `add_group(name, member_guids, colour)` — wrap GH objects in a named
  `GH_Group` on the active canvas. Groups are the primary visual structuring
  device on a busy canvas (one named group per conceptually distinct
  subgraph). The optional `colour` accepts `"#RRGGBB"` or `"#RRGGBBAA"`;
  group geometry is derived from member positions, so members must already
  be on the canvas. Mutating, runs on the UI thread via
  `_UI_THREAD_BOOTSTRAP`.
* `add_param_marker(name, x, y, param_type)` — drop a labeled floating
  parameter (`Param_GenericObject` by default; also `plane`, `number`,
  `integer`, `string`, `boolean`, `point`, `vector`, `curve`, `geometry`).
  Pairs with `add_group` to publish a subgraph's outputs as named buses
  that downstream subgraphs can wire to — the marker name labels the
  connection visibly and survives moves and refactors. Mutating, UI-thread
  safe.
* `describe_canvas_structure()` — richer counterpart to
  `list_grasshopper_objects`. For each object adds (when applicable):
  `group_members` on `GH_Group` entries; `inputs[].sources` listing the
  upstream `{owner_guid, param_name, param_nickname}` per input on
  components; `sources` on floating params. Read-only; lets an agent
  learn from an existing canvas in one call.

## [0.4.1] - 2026-05-31

### Fixed

* All document-mutating tools (`set_script_venv`, `solve_grasshopper`,
  `add_python_component`, `save_grasshopper_document`) now run their mutation on
  Rhino's UI thread via `RhinoApp.InvokeOnUiThread` (new `_UI_THREAD_BOOTSTRAP`
  helper). The bridge `exec()`s on its own HTTP server thread, and mutating GH
  components (`Text`/`LanguageSpec`), adding objects, expiring solutions, or
  saving from that thread is not thread-safe and **hard-crashes the whole Rhino
  process**. Disabling the solver did not prevent this — the crash was the
  cross-thread mutation itself, which `set_script_venv` previously did directly.
  The helper marshals the work onto the UI thread and uses a `threading.Event`
  so the tools still return synchronously, and (unlike `ScheduleSolution`) does
  not force a solve, so `solve=False` is honoured.

## [0.4.0] - 2026-05-31

### Added

* `set_script_venv(venv, only_nicknames, only_guids, add_if_missing, solve)` —
  rewrite the `# venv:` directive on every Python 3 / script component in the
  active document so they all point at one environment. Mutates only the
  script body (via reflection on `IScriptComponent.Text`) so wiring stays
  intact; round-tripping through `GH_LooseChunk` Write/Read would
  re-deserialize the input params and leave a ghost source at the origin.
  Disables the solver during the batch and re-schedules a single solution
  afterwards so cascading solves don't tear the LAMCP Bridge component down
  mid-request. The bridge itself is skipped by source marker.

## [0.3.0] - 2026-05-31

### Added

* `add_reloader_component(x, y, solve)` — drop the COMPAS side-by-side
  hot-reload bootstrap (a single-output component that runs
  `DevTools.enable_reloader()`). Pair it with consumer components that start
  with `DevTools.ensure_path()` before importing side-car modules.

### Fixed

* `add_python_component` now raises on an empty `outputs` list instead of
  inserting a zero-output script component, which hard-crashes Rhino during
  attribute layout when built via `EmitObject` + `AddObject` (the GH UI path
  initialises such a component differently and survives).

## [0.2.0] - 2026-05-31

### Added

* Four Grasshopper authoring/inspection tools, built on top of the bridge:
  * `list_grasshopper_objects()` — enumerate canvas objects (type, nickname,
    GUID, pivot, error/warning messages).
  * `add_python_component(code, name, inputs, outputs, x, y, solve)` — drop a
    Rhino 8 Python 3 script component (with the given code and I/O params)
    onto the canvas by deserializing a GH_IO archive into a fresh instance.
  * `solve_grasshopper(expire_all)` — re-solve via `ScheduleSolution` on the
    UI thread. Never solves synchronously: the bridge `exec()`s on its own
    HTTP thread, so a synchronous full solve re-runs the bridge component on
    that thread and tears the server down mid-request.
  * `save_grasshopper_document(path)` — save the active document to disk.

## [0.1.1] - 2026-05-31

### Fixed

* Add PyPI classifiers (Python versions, MIT license, intended audience,
  topics) so the shields.io badges on the README actually populate.
  0.1.0 shipped without any classifiers — both the Python-versions
  badge and the License badge rendered as "missing".

## [0.1.0] - 2026-05-31

### Added

* Initial release.
* `lamcp` FastMCP server with three tools:
  * `run_python_script(code, timeout)` — exec arbitrary Python inside the
    bridge process, captures stdout / stderr / `repr(_)` / traceback.
  * `unload_python_modules(prefix)` — drop `sys.modules[prefix.*]` to
    pick up on-disk module edits without restarting Rhino.
  * `bridge_health()` — ping the bridge over loopback HTTP.
* `LAMCP Bridge` Grasshopper component (Rhino 8, CPython 3.9) that
  hosts an `http.server` on `127.0.0.1:8765` (configurable port) and
  `exec()`s incoming code against a shared globals dict so state
  persists across calls.
* GitHub release ships the pre-built `Lamcp_Bridge.ghuser` alongside
  the Python wheel/sdist, so users can drop the component into their
  Grasshopper Libraries folder without a manual paste-and-configure
  step.
