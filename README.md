# effectcraft-ci-gauntlet

Empirical validation of `storytold/effectcraft`'s MCP capabilities end-to-end inside
GitHub Actions (`ubuntu-latest`, no GPU, no display, CPU render path only).

- `.github/workflows/gauntlet.yml` — builds `effectcraft-cli` from pinned upstream
  commit `ef74d82902ba486891b02afa87e42963cc9b84cc`, installs Inter into `/ci-fonts`
  (`EFFECTCRAFT_FONT_DIRS=/ci-fonts`), runs `gauntlet_test.py`, uploads artifacts.
- `gauntlet_test.py` — spawns the headless MCP server
  (`effectcraft-cli mcp --autosave --demo`) and executes Tests 1–7 over stdio
  JSON-RPC 2.0 (initialize/get_state, comp+layers, keyframes+easyEase, effects,
  renderQueue h264/prores render, font handling, malformed-command error recovery).

Artifacts land under the name `test-render-output` (render file, results.json,
gauntlet.log, server_stderr.log, font_probe.png).
