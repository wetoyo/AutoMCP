# Engineering notes

Done 2026-10-20 in one working session. These notes record what was built, how it behaved, what
needed a human, and what is still weak. For usage, see `README.md`.

## Success metrics (from the spec)

| Metric | Result |
|---|---|
| ≥1 working agent-generated, in-process MCP tool | **2**: `notepadpp_new_document` (agent copy) and `sumatrapdf_new_window` |
| Verified observable postcondition for every published tool | yes. Every manifest declares ≥2 out-of-process observers; the harness evaluates them before and after every call |
| Reuse after process restart | yes. The harness runs a restart phase, and `demos/run_demo.py` restarts the app under one live MCP session (server re-resolves, no regeneration) |
| Zero hand-written app-specific adapter code in the automated demo | yes. Both agent adapters were written by the agent. Only the M0 prototype (allowed) was hand-written |
| Documented failures, limitations, human interventions | this file + `docs/M3_REPORT.md` + per-run `runs/*/run.json` |

## Timeline

1. **M0 (manual).** REA/Ghidra on Notepad++: string `tab-untitled-string` → `FileManager::newEmptyDocument` → its
   caller `0x14013c7a0`, whose decompilation matches `Notepad_plus::fileNew` line for line. `this` comes from
   `GWLP_USERDATA` + `0x20`, found by a runtime self-reference scan and guarded by the `_hSelf` and `_pPublicInterface`
   back-pointers. Called on the UI thread. Exposed over MCP and checked from a separate client across a restart.
2. **Generalisation for M1.** I pulled everything app-agnostic out of the M0 adapter into `runtime/wam_lib.js`, moved
   per-app constants into `manifest.adapter_config`, and added harness-owned observers, static lint, the `./wam` tool,
   and the orchestrator.
3. **M1.** Headless Claude Code (Opus) reproduced the Notepad++ tool from the request alone: same RVA, verified on
   attempt 1, ~6 min. Its offset proof was stronger than mine: it read `ADD RCX,0x20` / `MOV [RCX],RBX` out of `runProc`
   instead of relying on a memory scan.
4. **M3.** SumatraPDF 3.6.1, same orchestrator and brief. The agent found `CreateAndShowMainWindow` via the window-class
   string. It proved the menu path passes `nullptr` by decoding the WM_COMMAND dispatcher's branch chain to
   `case 242`, and repaired its own postcondition (a tooltip makes the visible-window count +2, not +1).
5. **Polish.** Scripted demo, unit tests, repair-loop test, docs.

## Design decisions

- **Call on the GUI thread, without UI automation.** Both functions touch window state. `WAM.onUiThread` runs the call
  inside the owning thread's next `DispatchMessageW` and posts `WM_NULL` only to wake an idle loop. The operation itself
  is a direct `NativeFunction` call. The lint rejects `WM_COMMAND`, `SendInput`, and key or mouse injection.
- **Resolution = module base + RVA, guarded.** Each adapter checks the PE TimeDateStamp and the entry bytes of every
  function it relies on, and checks runtime objects structurally (back-pointers, HWND classes, plausible vector
  lengths) before calling. A different build fails closed (`version_mismatch` / `signature_mismatch`). Absolute
  addresses are rejected by lint.
- **Postconditions are not self-reported.** `validation/observers.py` reads state from the harness process (window
  messages, tab-control item counts, window titles, top-level window counts). An adapter that just returns
  `{ok: true}` fails validation.
- **Disposable instances.** Both apps run portable from a fresh temp copy, so config stays in the copy and the
  single-instance keys differ (both checked in source). SumatraPDF's update check is disabled by a shipped settings file.
- **Agent isolation.** The agent runs in `dontAsk` mode with an allowlist: file tools, Edit only in `./adapter` and
  `./scratch`, Bash only `./wam`, and REA. It runs from a fresh project path (empty memory) with
  `--setting-sources project` and `--strict-mcp-config`. Prior results are denied by rule, and every tool input in the
  transcript is audited for forbidden paths and `../../` escapes. A flagged run is marked `contaminated` and never published.
- **Approval gate.** `orchestrator/approve.py` records who approved, when, and the binary's sha256. The server publishes
  only `verified` tools, only approved ones if they change state, never destructive ones, and withholds every adapter
  that shares a tool name unless one is chosen with `--only`.

## Failures and findings

- **Snapshot leak avoided.** I meant to hand the agent a REA analysis snapshot to skip Ghidra's ~4 minute cold start.
  The snapshot turned out to contain my 11 M0 evidence records, including the `fileNew` chain, so I deleted it.
- **Hand-picked source files.** The 6 files in `targets/notepadpp/src` were chosen during M0 and would have hinted at
  the answer. The agent got the full upstream tree at the release tag instead, and `src/` is on the forbidden list.
- **Observer ambiguity.** A visible `tooltips_class32` window made "first visible window" and "visible window count"
  unreliable on SumatraPDF. Fixed generically: the largest visible window is the default, and a class filter was added.
- **Demo start-up race.** The first demo version treated a window count of 0 as ready. The SumatraPDF adapter correctly
  refused (`gWindows` len 0, `state_invalid`) and changed nothing. Fixed in the demo. The task brief now tells agents to
  use `state_not_ready` for transient states, since only those are retried.
- **Duplicate tool names.** The M1 agent reused the M0 name. The server now withholds every adapter that claims a
  duplicated name unless one is selected.
- **Windows Ghidra is read-only in REA.** `set_address_name` is refused, so names live in manifests and DISCOVERY.md.
- **MCP SDK 2.x** renamed FastMCP to `MCPServer` and rejects handler parameters starting with `_`. I used a factory closure instead.

## Repair loop (M2)

Neither real run needed the orchestrator's repair loop: both agents fixed problems inside their own
`./wam validate` iterations. To exercise the loop I added `run_m1.py --inject-fault`. After attempt 1 it flips
the last nibble of the first entry-byte string in the agent's `adapter_config`, and records that in `run.json`.

Run `runs/20261020-220109-sumatrapdf-new_window_repairtest`:

| Attempt | What happened | Time |
|---|---|---|
| 1 | Agent rediscovered `CreateAndShowMainWindow` from scratch and validated it. The orchestrator then injected `…0000e8` → `…0000e7`. Independent validation **failed**: preflight `signature_mismatch`, and the adapter refused to call | 315 s |
| 2 | The orchestrator resumed the same agent session with the diagnostics. The agent restored `e8` after cross-checking three sources (file bytes via `./wam bytes`, the in-memory bytes from the failure report, and the disassembly where `e8` is the `call` opcode). It noted that the PE timestamp still matched, so the binary was unchanged, and flagged that something outside its own edits had modified the manifest. Independent validation **passed** | 32 s |

The leak audit was clean. The test's published adapter (`new_window_repairtest`, same tool name) was removed from the
store afterwards to avoid a duplicate-name conflict. The run directory keeps both attempt snapshots.

The first attempt at this test crashed the orchestrator. The audit assumed every transcript event's `message` is an
object, but `permission_denied` system events carry a string. Those denials were the allowlist working: the agent tried
`cd … && ls && ./wam identity` and `./wam` via PowerShell, and both were blocked. The audit now skips non-object
messages and records denials (`permission_denials` in `run.json`), with a regression test.

## Human interventions

| Stage | Intervention |
|---|---|
| Setup | REA + Ghidra install; app choice (user picked Notepad++ for M0; I picked SumatraPDF for M3) |
| M0 | all of it (allowed by the spec) |
| Framework | generic only: WAM lib, observers, harness, orchestrator, server, two observer fixes |
| Per target | download and integrity check, upstream source at the tag, `TARGETS` entry, (SumatraPDF) settings file |
| Per tool | user approval (`orchestrator/approve.py`); M0 tool approved by the user, agent tools still pending |
| Per adapter | none |

## Limitations and caveats

- **Prior knowledge.** Both apps are open source, and the model knows parts of their code from training. The Notepad++
  agent searched for `fileNew` before it ran any REA query. The agents' binary-side work (anchoring, xrefs, callers,
  assembly, dispatcher decoding, runtime layout probes) was real and is logged, but neither run shows discovery in an
  app the model has never seen. A closed-source or obscure target would test that properly.
- **Build-locked by design.** Adapters bind to one exact build. Supporting a new version means rerunning the
  orchestrator; no signature scanning across versions is implemented.
- **Zero-argument tools only so far.** The plumbing supports `inputSchema` and argument validation, but neither tool
  takes arguments. Marshalling strings or objects into C++ calls (lifetimes, allocators) is untested.
- **The audit is a detector, not a sandbox.** The agent's Read tool is path-restricted only by deny rules plus the
  transcript audit. A stronger setup would run the agent under a separate OS user or in a container that has only the
  workspace, the source tree and the binary.
- **REA cold start** (~4 min per agent run). A clean, evidence-free Ghidra cache would cut this, but REA's snapshot
  includes session evidence, so it can't be shared safely as-is.
- **Concurrency.** The server serialises calls per adapter and refuses ambiguous targets. It doesn't handle several
  instances of one app at once beyond `WAM_TARGET_PID`.
- **Cost.** Each agent run in the results above took ~6 min of Opus time plus Ghidra. The agent now defaults to
  Meta Muse Spark (`muse-spark-1.3`; see README "Agent model setup"), and each attempt records `est_cost_usd` at
  Meta's prices. On Muse Spark, the CLI enforces `--max-budget-usd` with Anthropic prices, so bound runs with
  `--attempt-timeout`. No Muse Spark run has been recorded yet.

## Where the evidence is

- `adapters/notepadpp/new_document/evidence.json`: the REA evidence bundle for the M0 discovery.
- `adapters/*/new_*_agent/DISCOVERY.md`: the agent's evidence chain, failed hypotheses and uncertainties.
- `runs/<run_id>/attempt-N.jsonl`: full agent transcripts (stream-json). `validation-N.json`: independent validation reports.
  `adapter-attempt-N/`: per-attempt snapshots. `run.json`: summary including the leak audit.

## JavaScript static analysis (analysis only)

A generic discovery mode for apps whose logic ships as JavaScript. It covers the discovery half of the pipeline only:
nothing in the target is launched, loaded or executed, and no adapter or tool is produced.

- **Onboarding:** `analysis/js_extract.py` handles the zip family and Electron `.asar` (with `.asar.unpacked`). Mozilla's
  optimized jars put the central directory at offset 4, which Python's `zipfile` rejects, so a fallback reader follows the
  end record's absolute directory offset. Archive member paths are traversal-checked.
- **Agent:** read-only file tools, `Edit` only in `./analysis/`, REA (`analyze_javascript_application`,
  `trace_javascript_semantics`) and a single shell command, `./check`. It writes `discovery.json` (function, file:line,
  signature, call chain, entry points, execution context, inputs, observable effects, unknowns) and `DISCOVERY.md`.
- **Validation:** `validation/discovery_check.py` mechanically verifies the evidence. Cited files and lines exist, the
  function is defined at the cited line, and every call-chain and entry-point symbol appears within ±3 lines. It can't
  verify the reasoning that connects the citations; DISCOVERY.md has to make that case.
- **Example run** `runs/20261020-225224-firefox-analysis`: Firefox 158.0.1, request "Find the application's internal
  function that opens a new tab for a given URL". The agent located `Tabbrowser.addTab(uriString, options)`
  (`omni/moz-src/browser/components/tabbrowser/Tabbrowser.sys.mjs:3682`). It traced the route from the
  `cmd_newNavigatorTab` command through `BrowserCommands.openTab` → `openTrustedLinkIn` → `URILoadingHelper.openLinkIn`
  (`case "tab"` → `w.gBrowser.addTab`), and found the required `triggeringPrincipal` argument. Passed the check on
  attempt 1 in 208 s, with a clean audit. I spot-checked the key citations by hand.
- **Caveat:** Firefox is open source and heavily documented, so the model's prior knowledge applies here too. An Electron
  app with a minified bundle would be the harder test. REA's source-recovery tool (`recover_javascript_sources`) is
  Linux-only, so minified bundles are analyzed as-is on Windows.

## Electron pipeline (in-process, full loop)

The JS static-analysis mode extended to the whole pipeline for Electron apps: the agent discovers the function,
writes an adapter, the harness validates it against disposable instances, and the MCP server serves it. The
orchestrator, harness, observers, approval gate and server are shared with the native path; only the runtime differs.

- **Target:** MarkText 0.22.0 portable x64 (Electron 42.1.0, Chromium 148), from the release zip (sha256 matched
  GitHub's asset digest). Vue 3 + Pinia renderer, scope-hoisted single-file bundle, no source maps.
- **Channel.** The `EnableNodeCliInspectArguments` fuse is disabled, and Electron also drops `NODE_OPTIONS=--inspect`
  in packaged apps, so the main process is unreachable. That is typical of current apps. The renderer is reached over
  `--remote-debugging-port` (a Chromium switch, not fused). The bridge supports both, and `./wam identity` reports
  which is available.
- **Disposable instances.** A fresh temp `--user-data-dir` per instance (it also keys MarkText's single-instance lock).
  A restart reuses the dir, as a real restart would. The app dir is never written. Ports bind to 127.0.0.1. Whole
  process trees are killed (`taskkill /T`).
- **Resolution = handles.** A handle is an anchor expression plus `prop` / `internal` (`[[Target]]`) / `scope`
  (`[[Scopes]]` closure variable) / `get` steps. The `scope` step is what reaches module-private bundle functions.
  Property reads go through `Runtime.getProperties`, so no page code runs. Everything else runs with V8
  `throwOnSideEffect`.
  Finding: Vue reactive objects are Proxies, and V8 treats their traps (dependency tracking) as side effects, so even
  `Object.keys(store)` is refused. Hence the explicit `[[Target]]` step.
- **Guards.** Exe sha256 + `app.asar` sha256 before every session. For functions, `source_sha256` of
  `Function.prototype.toString`, the analogue of entry bytes. A corrupted hash fails at preflight with
  `signature_mismatch` and zero calls (tested live).
- **Lint.** Rejects synthesized input, `dispatchEvent`, DOM `.click()`, `execCommand`, `ipcRenderer`/`ipcMain`,
  `webContents.send`, `.emit(`, `postMessage`, `executeJavaScript` (IPC and event buses are the JS `WM_COMMAND`). It
  requires every function handle to carry `source_sha256` and the adapter to call through `h.<name>`. A unit test
  caught a bug in my first regex: a leading `\b` never matched `.click(` after `)`.
- **Observers.** `page_targets` (read from `/json/list`, nothing runs in the app) and `page_eval` (an expression V8
  forces to be side-effect free; `document.title = 'x'` is refused). The Win32 observers also work, because Electron
  windows are ordinary HWNDs.

**Runs** (request: "Expose MarkText's internal function for creating a new, empty untitled document tab"):

| Run | Outcome |
|---|---|
| `20261020-232425-marktext-new_tab_agent` | Adapter passed validation on attempt 1 (345 s, $1.58), but the run was **marked contaminated and not published**. The audit flagged two Greps of the agent's *own* spilled tool output (`~/.claude/projects/<workspace>/<its session id>/tool-results/`; a large `./wam inspect` result). That was a false positive. The audit now exempts only that session's `tool-results` dir (regression test added). Re-auditing the transcript gives zero violations. Route found: `window.__VUE_INSTANCE_SETTERS__[0]` → module scope → `useEditorStore` → `options.actions.NEW_UNTITLED_TAB` |
| `20261020-233156-marktext-new_tab_agent` | Fresh rerun: **passed on attempt 1, 198 s, 31 turns, $1.06, clean audit, published** (2 Bash calls outside `./wam` blocked by the allowlist). Route: `#app.__vue_app__` → `$pinia._s.get('editor')` → `[[Target]]` → `NEW_UNTITLED_TAB` → `scope: fn` (the original function behind Pinia's wrapper), called as `fn.call(store, {selected: true})`. Postconditions: DOM tab count +1 (`page_eval`) and window title changed |

Both runs pinned the same `source_sha256` (`0b0d19ca…`), even though they reached the function by different paths.
`demos/run_demo.py --app marktext` passes: the separate MCP server process makes 2 calls, the app restarts, and the
same session makes 2 more calls, each one +1 tab. The native demos still pass after the shared-code changes.

**Caveats**

- **Prior knowledge again.** MarkText is open source and Pinia's internals are well known. The agent's runtime work
  (inspect, probe, scope walks) is real and logged, but this is not a test against an unseen app.
- **Bundle-locked.** A new build changes the asar hash and the function source, so the adapter fails closed. There is
  no cross-version matching.
- **Debug port exposure.** While an instance from `runtime.electron_launch` runs, any local process can drive the app
  through its 127.0.0.1 port. Use it only for authorized, non-sensitive instances. The server never enables
  debugging on an app you started yourself.
- **Renderer-only for hardened apps.** Operations that live only in the main process (native dialogs, app-level
  menus) are out of reach when the inspector fuse is off. An adapter must then find a renderer-side function that
  performs the operation, and must not route through IPC (the lint rejects that).
- **Zero-argument tools only** in the server, as for native tools. The Electron bridge already passes `args` and
  `WAM.validateArgs` enforces the schema, but `make_handler` takes no parameters.

## Open work

- **First Muse Spark runs.** The agent loops and `mcp_server.muse_chat` default to Muse Spark, but every adapter in
  the Results table was generated on Claude Opus. Next: `muse_chat --check`, then one `run_m1` run on Muse Spark.
- **An app the model hasn't seen.** MarkText is open source, so the model's prior knowledge probably helped. A
  closed-source Electron app with a minified bundle would be a real test of discovery. To onboard one, add a
  `TARGETS` entry in `validation/harness.py` and `SOURCES`/`APP_NAMES` entries in `orchestrator/run_m1.py`.
- **Tool arguments.** `make_handler` takes no parameters, so every tool is zero-argument. Build the handler from
  `inputSchema`, then add a harness phase that tests valid arguments.
- **Agent sandboxing.** The native harness enforces path rules in `orchestrator/workbench.py`. With
  `--harness claude`, the CLI's `Read(...)` deny rules may not cover Grep/Glob, so the audit is the only check.
- **Electron main process.** Only the renderer path has run live. It needs a target whose inspector fuse is on.
- **Duplicate Notepad++ tool name.** The manual M0 adapter and the agent's M1 adapter both claim
  `notepadpp_new_document`. Without `--only`, the server publishes neither.
- **Approvals.** The three agent-built tools are verified but not approved (`python -m orchestrator.approve`).
