# M3: generalization to a second application

**App:** SumatraPDF 3.6.1 portable x64 (`targets/sumatrapdf/app`). The Authenticode signature is valid (Krzysztof Kowalczyk, Certum CA); the vendor publishes no checksum. Zip sha256 `98b33a51…a544`, exe sha256 `719f689b…c3d0`.
**Request given to the agent:** "Expose the application's internal function for opening a new window."
**Result:** `adapters/sumatrapdf/new_window_agent` (`sumatrapdf_new_window`) calls `CreateAndShowMainWindow(nullptr)` at RVA `0x1a39b8` on the GUI thread. It was verified by the orchestrator on the first attempt (about 6 min), with no forbidden-path access. It was also checked through `demos/client.py` and the MCP server across a restart, adding windows 1→2→3 in each round.
**Run log:** `runs/20261020-214240-sumatrapdf-new_window_agent/`

## Agent-driven (no human input)

- Choosing the function from source (`CmdNewWindow` → `CreateAndShowMainWindow`).
- Binary localisation without symbols: window-class string → `CreateMainWindow` → its single caller. The agent also decoded the WM_COMMAND dispatcher branch for `cmd == 242`, which proves the menu passes `nullptr`.
- Finding the state checks: the `gGlobalPrefs` and `gWindows` RVAs, the `Vec` layout, and the `MainWindow` HWND offsets, all confirmed with `./wam probe`.
- Writing the adapter, manifest and DISCOVERY.md, and choosing the postconditions.
- Repair: its first postcondition (visible windows +1) failed `./wam validate`. The agent diagnosed a tooltip window (+2 in reality) and fixed it without human input.

## Manual interventions (all target onboarding or generic framework, no adapter code)

| What | Why | App-specific? |
|---|---|---|
| Download the zip, verify the signature, clone source at tag `3.6.1rel` | provide the target and its source | yes (onboarding) |
| 5-line `TARGETS["sumatrapdf"]` entry (harness) + `SOURCES`/`APP_NAMES` (orchestrator) | register the target | yes (onboarding) |
| `SumatraPDF-settings.txt` with `CheckForUpdates = false` | keep disposable instances offline | yes (onboarding) |
| Checked portable-mode and single-instance logic in source | confirm each disposable copy is isolated (settings live next to the exe; instance key = exe path + appdata) | yes (onboarding review) |
| `top_level_windows` gained a `window_class` filter; the default main window became the largest visible one | a visible tooltip window made the first version ambiguous | no (generic observer fix) |

## Caveats

- SumatraPDF is less famous than Notepad++, but its source is public, so the model may still have prior knowledge of names like `CreateAndShowMainWindow`. The binary-side evidence (string → xrefs → callers → dispatcher branch) was gathered with REA in this run.
- The orchestrator's repair loop (re-prompting after an independent validation failure) has still not been triggered. In both runs the agent repaired itself inside its own `./wam validate` loop.
- Approval is still pending for both agent adapters. The server publishes them only with `--include-unapproved` (testing) or after `python -m orchestrator.approve <app> <op>`.
