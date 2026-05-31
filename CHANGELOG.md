# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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
