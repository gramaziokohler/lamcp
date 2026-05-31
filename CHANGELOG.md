# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## [0.1.0]

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
