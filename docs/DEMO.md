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

### 0:20 Discovery (45 s, pre-recorded and sped up)

**Screen:** the agent's REA calls. Pause on each hop of the chain.

> There are no symbols, only a stripped binary. The agent starts from a window-class string and follows it through
> Ghidra.

| Step | Finding |
|---|---|
| String | `SUMATRA_PDF_FRAME` at `0x140900cc8` |
| Xref | leads to `FUN_1401a34b4`, which matches `CreateMainWindow` in the source |
| Single caller | `0x1401a39b8`, which matches `CreateAndShowMainWindow` |
| Dispatcher | `xor ecx,ecx; call 0x1401a39b8` at `cmd == 242`, so the menu passes `nullptr` |

> It doesn't stop at a name. It proves the signature, the argument and the thread before it writes any code.

Source for this beat: `adapters/sumatrapdf/new_window_agent/DISCOVERY.md`. The original transcript is gone
(`runs/` is empty), so footage needs a fresh run. See [Before recording](#before-recording).

### 1:05 Adapter (20 s)

**Screen:** `adapter.js` and the `resolution` block of `manifest.json`.

> The result is a small Frida adapter pinned to this exact build. It checks the PE timestamp, the entry bytes and the
> live data structures, and refuses to run if anything differs. It runs on the GUI thread with no clicks and no
> `WM_COMMAND`. The lint rejects those.

### 1:25 Validation (20 s)

**Screen:** the validation output. ⚠️ Run it live, or show the stored `validation.json` (`passed: true`).

```sh
python -m validation.harness sumatrapdf new_window_agent --calls 3
```

> An adapter can't certify itself. The harness launches a disposable copy, calls the tool and checks the result from
> outside the process: one more window, every call. Then it restarts the app and does it again.

Optional line: in our fault-injection test, we corrupted a verified adapter. The harness caught it, and the agent
fixed it on its second attempt in 32 seconds.

### 1:45 Approve and publish (10 s)

```sh
python -m orchestrator.approve sumatrapdf new_window_agent
```

> A tool that changes app state is only published after a person approves it. Destructive tools are never published.

### 1:55 Reuse across a restart (35 s, the payoff) ✅

**Screen:** split view of terminal and SumatraPDF windows.

```sh
python demos/run_demo.py --app sumatrapdf
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

### 2:30 Natural language through Muse Spark (20 s) ✅

**Screen:** SumatraPDF open, then the terminal. Start the app from `targets/sumatrapdf/app/` first.

```sh
python -m mcp_server.muse_chat --server-arg=--only=sumatrapdf/new_window_agent "Open a new SumatraPDF window"
```

> Any MCP client can use the tool. Here, Muse Spark picks it from a plain request, and the model is only involved
> in choosing the call.

### 2:50 Close (10 s)

**Screen:** the results table from the README.

> Three apps so far: native C++ through Frida, and Electron through the DevTools protocol. Plain-English request in,
> validated tool out.

## Evidence for each claim

| Claim | Status | Source |
|---|---|---|
| Unit tests pass (52) | ✅ | run 2026-11-03 |
| Muse Spark accepts the tool definitions (`muse_chat --check`) | ✅ | run 2026-11-03, SumatraPDF and MarkText |
| Three adapters verified by the harness | Recorded, not rerun | `adapters/*/*/validation.json` |
| Restart demo passes (SumatraPDF, 1→2→3 twice) | ✅ | run 2026-11-03 |
| Muse Spark calling a tool on a live app (windows 1→2) | ✅ | run 2026-11-03 |
| Muse Spark generating an adapter | ❌ Never run; all adapters were generated on Claude Opus | `NOTES.md` |
| Discovery on an app the model has never seen | ❌ All targets are open source | `NOTES.md` |

## Say it accurately

- Until a Muse Spark run succeeds, say: "the pipeline runs on Muse Spark by default; these adapters were generated on
  Claude Opus." Don't say Muse Spark built them. The README's opening currently implies it did.
- Say "tool validated three times per phase", not "works reliably": that's what the reports show.
- Don't claim the tools take arguments. All of them are zero-argument so far.

## Before recording

Run everything from the main checkout with `.venv` activated. `adapters/` and `targets/` are local only, so a fresh
clone or worktree can't run the demo.

1. **Load the key.** Nothing reads `.env` automatically:
   ```powershell
   $env:MODEL_API_KEY = ((Get-Content .env | Where-Object { $_ -match '^MODEL_API_KEY=' }) -replace '^MODEL_API_KEY=','').Trim()
   ```
2. **Generate on Muse Spark for discovery footage and to close the biggest gap.** Budget 10+ minutes (about 4 of them
   Ghidra's cold start). Use a new operation name to keep the working adapter:
   ```sh
   python -m orchestrator.run_m1 --target sumatrapdf --operation new_window_muse \
       --request "Expose the application's internal function for opening a new window"
   ```
   Two adapters will then claim `sumatrapdf_new_window`, so the server publishes neither without `--only`. Decide
   which one the demo serves and update the commands above.
3. **Rehearse without approving.** Approval changes the local manifest, so save it for the take:
   ```sh
   python demos/run_demo.py --app sumatrapdf --allow-unapproved
   python -m mcp_server.muse_chat --server-arg=--include-unapproved --server-arg=--only=sumatrapdf/new_window_agent "Open a new SumatraPDF window"
   ```
4. **Close other copies of SumatraPDF.** With several matches, the server refuses to guess.
5. **Record a clean take of every beat** as a fallback, since the live parts depend on Frida and a remote API.

## Likely questions

- **Isn't this just macro automation?** No. It calls the function directly on the GUI thread, and the lint rejects
  synthesized input, `WM_COMMAND`, DOM events and IPC.
- **What happens when the app updates?** The adapter fails closed with `version_mismatch` or `signature_mismatch`.
  Rerun the agent for the new build. There's no cross-version matching.
- **Did the model already know this code?** Possibly: all targets are open source. The binary-side evidence is real
  and logged, but a closed-source app is the proper test, and we haven't run one.
- **Is it safe?** Validation only touches disposable copies. Attach only to apps you're authorized to instrument.
  Protected and anti-cheat processes are out of scope.
- **What doesn't work yet?** Tool arguments, Electron main-process targets (renderer only so far) and matching across
  versions.
