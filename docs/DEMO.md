# Demo script (draft)

Target length: 3 minutes. Hero app: SumatraPDF 3.6.1. It has no symbols, and a new window is obvious on camera.

**Status legend:** ✅ checked 2026-11-03 · ⚠️ not rehearsed yet · ❌ can't be claimed yet

## The one claim

An agent turns a plain-English request into a validated tool that calls a real internal function inside a running
Windows app. After that, calling it needs no LLM.

## Run of show

### 0:00 Hook (20 s)

**Screen:** SumatraPDF open, then the request as text.

> Most desktop apps have no API. Automation means clicking pixels and hoping. Their real functions are all in there,
> just not exposed. So we ask for one in plain English.

```
Expose the application's internal function for opening a new window
```

### 0:20 Discovery (30 s, pre-recorded and sped up)

**Screen:** the agent's REA calls. Pause on each hop of the chain.

> Muse Spark reads the app's public source to work out what to call, then has to find it in a binary with no symbols.
> It starts from a window-class string and follows it through Ghidra.

| Step | Finding |
|---|---|
| String | `SUMATRA_PDF_FRAME` at `0x140900cc8` |
| Xref | leads to `FUN_1401a34b4`, which matches `CreateMainWindow` in the source |
| Wrapper | `FUN_1401a39b8`, which matches `CreateAndShowMainWindow` |
| Live probes | windows 1→2→3 on a disposable copy; a wrong build or wrong bytes fail closed |

> It doesn't stop at a name. It tests the call on a disposable copy of the app before it publishes anything.

Footage is a labelled, sped-up replay of the recorded run: 66 turns, 18.5 minutes, about $1.47 estimated. Sources:
`adapters/sumatrapdf/new_window_muse/DISCOVERY.md` and `runs/20261103-204600-sumatrapdf-new_window_muse/`.

### 0:50 Adapter (20 s)

**Screen:** `adapter.js` and the `resolution` block of `manifest.json` from `new_window_muse`.

> Muse Spark wrote this: a small Frida adapter pinned to this exact build. It checks the PE timestamp and the entry
> bytes, and refuses to run if they differ. It runs on the GUI thread with no clicks and no `WM_COMMAND`. The lint
> rejects those.

### 1:10 Validation (20 s)

**Screen:** the validation output. ✅

```sh
python -m validation.harness sumatrapdf new_window_muse --calls 3
```

> An adapter can't certify itself. The harness launches a disposable copy, calls the tool and checks the result from
> outside the process: one more window, every call. Then it restarts the app and does it again.

Optional line: in an earlier fault-injection test on Claude Opus, we corrupted a verified adapter. The harness caught
it, and the agent fixed it on its second attempt in 32 seconds.

### 1:30 Approve and publish (10 s)

```sh
python -m orchestrator.approve sumatrapdf new_window_muse
```

> A tool that changes app state is only published after a person approves it. Destructive tools are never published.

### 1:40 Reuse across a restart (35 s, the payoff) ✅

**Screen:** split view of terminal and SumatraPDF windows.

```sh
python demos/run_demo.py --app sumatrapdf --adapter sumatrapdf/new_window_muse
```

Output from the 2026-11-03 rehearsal:

```
call sumatrapdf_new_window: ok=True  SumatraPDF windows: 1 -> 2
call sumatrapdf_new_window: ok=True  SumatraPDF windows: 2 -> 3
=== sumatrapdf: restart the application (same MCP session, no regeneration)
call sumatrapdf_new_window: ok=True  SumatraPDF windows: 1 -> 2
call sumatrapdf_new_window: ok=True  SumatraPDF windows: 2 -> 3
```

> A separate MCP server calls the tool, and windows appear. Now we kill the app and relaunch it: new process, same
> MCP session. The same tool works again. No model ran in that loop and nothing was regenerated.

### 2:15 Natural language through Muse Spark (20 s) ✅

**Screen:** SumatraPDF open, then the terminal. Start the app from `targets/sumatrapdf/app/` first.

```sh
python -m mcp_server.muse_chat --server-arg=--only=sumatrapdf/new_window_muse "Open a new SumatraPDF window"
```

> Any MCP client can use the tool. Here, Muse Spark uses the tool it built, from a plain request. The model only
> chooses the call.

### 2:35 Same pipeline on Electron (15 s) ✅

**Screen:** MarkText tabs and the terminal. Pre-recorded is fine; it starts slower than SumatraPDF.

```sh
python demos/run_demo.py --app marktext
```

```
call marktext_new_untitled_tab: ok=True  editor tabs (DOM): 1 -> 2
call marktext_new_untitled_tab: ok=True  editor tabs (DOM): 2 -> 3
=== marktext: restart the application (same MCP session, no regeneration)
call marktext_new_untitled_tab: ok=True  editor tabs (DOM): 1 -> 2
call marktext_new_untitled_tab: ok=True  editor tabs (DOM): 2 -> 3
```

> Same pipeline, different runtime. MarkText is Electron, so there's no Frida here: the adapter reaches the editor
> store's own `NEW_UNTITLED_TAB` through the DevTools protocol. Same validation, same restart. This adapter was
> generated on Claude Opus.

### 2:50 Close (10 s)

**Screen:** the results table from the README.

> Three apps so far: native C++ through Frida, and Electron through the DevTools protocol. Plain-English request in,
> validated tool out.

## Evidence for each claim

| Claim | Status | Source |
|---|---|---|
| Unit tests pass (52) | ✅ | run 2026-11-03 |
| Muse Spark accepts the tool definitions (`muse_chat --check`) | ✅ | run 2026-11-03, SumatraPDF and MarkText |
| Muse Spark adapter passes the harness, including a restart | ✅ | run 2026-11-03 |
| Notepad++ and MarkText adapters verified by the harness | Recorded, harness not rerun | `adapters/*/*/validation.json` |
| Restart demo passes (SumatraPDF, 1→2→3 twice) | ✅ | run 2026-11-03 |
| Restart demo passes (MarkText/Electron, 1→2→3 twice) | ✅ | run 2026-11-03 |
| Muse Spark calling a tool on a live app (windows 1→2) | ✅ | run 2026-11-03 |
| Muse Spark generating an adapter (SumatraPDF, attempt 1, clean audit) | ✅ | `runs/20261103-204600-sumatrapdf-new_window_muse/` |
| Muse Spark generating the Notepad++ or MarkText adapter | ❌ Those were generated on Claude Opus | `NOTES.md` |
| Discovery on an app the model has never seen | ❌ All targets are open source | `NOTES.md` |

## Say it accurately

- Muse Spark built the SumatraPDF adapter. Opus built the MarkText and Notepad++ ones. Don't blur this.
- Muse Spark had the upstream source. Don't imply it worked from the binary alone.
- Muse Spark and Opus independently reached the same function and RVA (`0x1a39b8`). The sandbox blocked access to the
  earlier adapter, and the audit found no leaks.
- Say "validated three times per phase", not "works reliably": that's what the reports show.
- The Muse Spark adapter is less guarded than Opus's: no `gWindows` check and one postcondition rather than two. It can
  also return `state_not_ready` after a window has opened. Don't describe live data-structure checks.
- Don't claim the tools take arguments. All of them are zero-argument so far.
- Electron means the renderer only so far, and the demo launches MarkText with a local debugging port.

## Before recording

Run everything from the main checkout with `.venv` activated. `adapters/` and `targets/` are local only, so a fresh
clone or worktree can't run the demo.

1. **Load the key.** Nothing reads `.env` automatically:
   ```powershell
   $env:MODEL_API_KEY = ((Get-Content .env | Where-Object { $_ -match '^MODEL_API_KEY=' }) -replace '^MODEL_API_KEY=','').Trim()
   ```
2. **Close other copies of SumatraPDF and MarkText.** With several matches, the server refuses to guess.
3. **Clear the screen for any new takes.** Close private windows and turn on Do Not Disturb.

The SumatraPDF adapter and the MarkText tool are approved. To show approval on camera, record that step separately.

## Takes

All are in `runs/demo-takes/`, which is local only.

| Step | File | Shows |
|---|---|---|
| Discovery | `1-discovery-replay.mp4` | Labelled, sped-up replay of the Muse Spark run |
| Validation | `2-validation.mp4` | Real harness run with `--no-write`, printed as a summary of its report |
| Reuse across a restart | `final-3-restart-sumatrapdf.mp4` | Terminal and app windows, approved tool |
| Muse Spark | `final-4-muse-chat.mp4` | Terminal and app windows, approved tool |
| Electron | `final-5-restart-marktext.mp4` | Terminal and app windows, approved tool |

The remaining numbered takes are terminal-only fallbacks recorded before approval. They show `--allow-unapproved`.
The final takes run about 15-20 seconds, with windows opening quickly, so slow them down in the edit if needed.

## Likely questions

- **Isn't this just macro automation?** No. It calls the function directly on the GUI thread, and the lint rejects
  synthesized input, `WM_COMMAND`, DOM events and IPC.
- **What happens when the app updates?** The adapter fails closed with `version_mismatch` or `signature_mismatch`.
  Rerun the agent for the new build. There's no cross-version matching.
- **Did the model already know this code?** Possibly: all targets are open source, and the agent reads the upstream
  source. The binary-side evidence is real and logged, but a closed-source app is the proper test, and we haven't run
  one.
- **Is it safe?** Validation only touches disposable copies. Attach only to apps you're authorized to instrument.
  Protected and anti-cheat processes are out of scope.
- **What doesn't work yet?** Tool arguments, Electron main-process targets (renderer only so far) and matching across
  versions.
