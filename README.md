# Windows Auto-MCP

Turn a natural-language request ("create a new document") into a validated MCP tool that calls the
matching **internal function inside a running Windows app**. A coding agent finds the function with REA/Ghidra
in a symbol-less binary and writes a Frida adapter. A harness validates the adapter against disposable
instances, checking real postconditions from outside the process. The MCP server then serves the saved
adapter with no LLM involved.

**Built for Meta Muse Spark.** Both ends of the pipeline use Meta's Muse Spark series through the Meta Model API
(default model `muse-spark-1.3`):

- **Building tools.** The discovery agent runs on Muse Spark. See
  [Agent model setup](#agent-model-setup-meta-muse-spark).
- **Using tools.** Every published tool is checked against Muse Spark's function-tool rules, by the harness at
  validation time and by the server at publish time. `mcp_server.muse_chat` connects Muse Spark to the MCP
  server. See [Use the tools from Muse Spark](#use-the-tools-from-muse-spark).

Spec: [`docs/spec.md`](docs/spec.md) · Engineering notes: [`NOTES.md`](NOTES.md) ·
M3 report: [`docs/M3_REPORT.md`](docs/M3_REPORT.md)

## Results

| Tool | App | Internal function (RVA) | Origin | Validation |
|---|---|---|---|---|
| `notepadpp_new_document` | Notepad++ 8.9.8.1 x64 | `Notepad_plus::fileNew()` (`0x13c7a0`) | manual M0 (`adapters/notepadpp/new_document`, **approved**) | verified, restart-safe |
| `notepadpp_new_document` | Notepad++ 8.9.8.1 x64 | `Notepad_plus::fileNew()` (`0x13c7a0`) | agent M1 (`adapters/notepadpp/new_document_agent`) | verified on attempt 1, ~6 min |
| `sumatrapdf_new_window` | SumatraPDF 3.6.1 x64 | `CreateAndShowMainWindow(nullptr)` (`0x1a39b8`) | agent M3 (`adapters/sumatrapdf/new_window_agent`) | verified on attempt 1, ~6 min |
| `marktext_new_untitled_tab` | MarkText 0.22.0 x64 (Electron 42.1.0) | editor store `NEW_UNTITLED_TAB` (renderer bundle `index-DlO1BZCi.js:190677`, pinned by source sha256) | agent, Electron pipeline (`adapters/marktext/new_tab_agent`) | verified on attempt 1, 198 s, $1.06 |

| Milestone | Status |
|---|---|
| M0: manual end-to-end proof | done |
| M1: agent-generated adapter | done (no address or adapter code supplied; transcript audited for leaks) |
| M2: automated validation and persistence | done (harness-side postconditions, restart test; bounded repair loop proven with `--inject-fault`: failure caught, agent repaired it on attempt 2) |
| M3: second application | done (manual work was target onboarding only) |
| JS static analysis (extra) | done: generic discovery for JavaScript apps (Electron `.asar`, `omni.ja`/zip). Analysis only, nothing executed; Firefox example located `Tabbrowser.addTab` |
| Electron pipeline (extra) | done: full discover → adapter → validate → serve loop for Electron apps over the DevTools protocol; MarkText tool agent-generated, verified, restart-safe, served by MCP |

## Quickstart

From the repo root (Git Bash; PowerShell works with backslashes):

```sh
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python.exe -m unittest discover -s tests -t .              # offline unit tests

# Hackathon demo: disposable app -> separate MCP client -> call -> restart app -> call again (no LLM)
.venv/Scripts/python.exe demos/run_demo.py --allow-unapproved            # or approve first, then drop the flag; --app marktext for Electron

# Validate a saved adapter against disposable instances (fresh + restart)
.venv/Scripts/python.exe -m validation.harness sumatrapdf new_window_agent --calls 3

# One-time setup for the agent loops: REA + Ghidra (see REA setup), then MODEL_API_KEY + the `claude` CLI (see Agent model setup)
.venv/Scripts/python.exe -m analysis.setup_rea --download-ghidra

# Ask the agent for a new tool (runs on Muse Spark)
# (add --provider anthropic to run on Claude instead)
.venv/Scripts/python.exe -m orchestrator.run_m1 --target sumatrapdf --operation <dir_name> \
    --request "Expose the application's internal function for ..."

# Static analysis of a JavaScript app (Electron .asar, omni.ja/.xpi/.zip): extract, then locate a function
.venv/Scripts/python.exe -m analysis.js_extract --out targets/<name>/extracted <archive> [<archive> ...]
.venv/Scripts/python.exe -m orchestrator.run_analysis --target-dir targets/<name> --app-name "<App x.y>"     --request "Find the application's internal function that ..."

# Electron apps (in-process over the DevTools protocol): onboard, then the same agent loop
.venv/Scripts/python.exe -m analysis.js_extract --out targets/marktext/extracted targets/marktext/app/resources/app.asar
.venv/Scripts/python.exe -m orchestrator.run_m1 --target marktext --operation <dir_name> \
    --request "Expose MarkText's internal function for ..."
.venv/Scripts/python.exe -m validation.harness marktext new_tab_agent --calls 3
.venv/Scripts/python.exe -m runtime.electron_launch marktext        # debuggable instance for the MCP server
.venv/Scripts/python.exe -m runtime.electron_launch marktext --stop

# Approve a verified, state-changing tool so the server publishes it
.venv/Scripts/python.exe -m orchestrator.approve sumatrapdf new_window_agent

# Use the tools from Muse Spark (needs MODEL_API_KEY; starts the MCP server for you)
.venv/Scripts/python.exe -m mcp_server.muse_chat --check                          # Meta accepts every published tool?
.venv/Scripts/python.exe -m mcp_server.muse_chat "Open a new SumatraPDF window"   # one request; omit it for a chat

# Serve tools to any MCP client over stdio
.venv/Scripts/python.exe -m mcp_server.server [--only app/op] [--include-unapproved]
```

## Use the tools from Muse Spark

The Meta Model API has no MCP connector; Muse Spark only calls function tools. `mcp_server.muse_chat` is the
connector. It starts `mcp_server.server` over stdio, sends each published tool to Muse Spark as a Responses API
function tool, and runs every call the model makes through MCP.

1. **Set the key:** `MODEL_API_KEY`, the same key the agent uses (see [Agent model setup](#agent-model-setup-meta-muse-spark)).
2. **Start the target app** (a build that matches the adapter). For Electron targets, run
   `python -m runtime.electron_launch <target>`.
3. **Check the tools:** `python -m mcp_server.muse_chat --check`. This sends the tool definitions to Meta once
   and lists which ones were accepted.
4. **Use them:** `python -m mcp_server.muse_chat "<request>"`, or run it without a request to chat. Each tool call
   and its JSON result are printed to stderr.

Options:
- `--model` chooses `muse-spark-1.3` (default), `muse-spark-1.1` or `muse-spark-1.3-contributor`.
- `--server-arg=...` is passed through to the server, for example `--server-arg=--include-unapproved`.
- `--print-tools` prints the function-tool JSON without calling Meta. Paste it into your own Responses API app,
  which then makes the MCP calls itself.

A tool is published only if Muse Spark accepts it. The rules live in `mcp_server/muse.py` and are enforced by the
harness and the server:
- **Name:** 1-64 chars of `[A-Za-z0-9_-]`.
- **Description:** non-empty.
- **Input schema:** a root `"type": "object"`, no `allOf`/`oneOf`, at most 10 levels deep.

All the tools in [Results](#results) pass.

### Other MCP clients

The server is a standard stdio MCP server. To use it from Claude Code or another MCP client, add a stdio server
entry:
```json
{"mcpServers": {"windows-auto-mcp": {"command": "D:\\Files\\Code\\AutoMCP\\.venv\\Scripts\\python.exe",
  "args": ["-m", "mcp_server.server"], "cwd": "D:\\Files\\Code\\AutoMCP"}}}
```
For native adapters the server attaches to the single running process whose image sha256 matches the adapter. Set `WAM_TARGET_PID` to choose a process when several match. Electron adapters need an instance with a debugging port: start it with `python -m runtime.electron_launch <target>`, or set `WAM_TARGET_PID` + `WAM_CDP_PORT`. The server checks the exe and `app.asar` sha256 before it calls anything.

## Agent model setup (Meta Muse Spark)

The agent loops (`orchestrator.run_m1` and `orchestrator.run_analysis`) run on **Meta Muse Spark** by default.
Apart from those, only `mcp_server.muse_chat` calls a model. The harness, the demos and the MCP server work
offline. The `claude` CLI is only the agent harness (tools, permissions, sessions); the orchestrator points it at
Meta's Anthropic-compatible endpoint and pins every model alias to Muse Spark.

1. **Get a Meta Model API key** at <https://dev.meta.ai>. The API is in public preview, US only; new accounts
   get $20 of credit.
2. **Install the agent harness:** `npm install -g @anthropic-ai/claude-code`. You don't need an Anthropic
   account or login; the orchestrator points the CLI at Meta.
3. **Export the key** as `MODEL_API_KEY`:
   ```sh
   export MODEL_API_KEY=...            # Git Bash
   $env:MODEL_API_KEY = "..."          # PowerShell
   ```
4. **Run an agent loop** as shown in [Quickstart](#quickstart). If the key isn't set, the orchestrator refuses
   to start.

For each agent subprocess, `orchestrator/provider.py` sets these variables:

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `https://api.meta.ai` (Meta's Anthropic-compatible `/v1/messages`) |
| `ANTHROPIC_AUTH_TOKEN` | `$MODEL_API_KEY` (sent as `Authorization: Bearer`) |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` | the chosen Muse Spark model, so no request goes to a model Meta doesn't serve |
| `ANTHROPIC_CUSTOM_HEADERS` | empty, so no inherited gateway header reaches Meta |
| `ENABLE_TOOL_SEARCH` | `true` (the CLI turns it off for non-Anthropic hosts) |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` |

It also removes `ANTHROPIC_API_KEY` and any Bedrock, Vertex or Foundry switches from that environment, so an
Anthropic credential already in your environment is never used or sent to Meta.

| Model (`--model`) | Input / cached / output, USD per 1M tokens | Notes |
|---|---|---|
| `muse-spark-1.3` (default) | 1.25 / 0.15 / 4.25 | latest; coding and agentic work, 1M context |
| `muse-spark-1.3-contributor` | 0.10 / 0.002 / 0.20 | same model; Meta may train on prompts and outputs, so avoid it on private targets |
| `muse-spark-1.1` | 1.25 / 0.15 / 4.25 | original release |

Each attempt in `run.json` records `est_cost_usd` at these prices. The CLI's own `total_cost_usd` and
`--max-budget-usd` use Anthropic's price table, so they're unreliable on Muse Spark. Bound runs with
`--attempt-timeout` and `--attempts` instead.

**Claude fallback.** To use the original Claude backend, set `WAM_AGENT_PROVIDER=anthropic` or pass
`--provider anthropic [--model opus]`. All the tools in [Results](#results) were generated on Claude Opus.

## Setup (local-only payloads)

Git holds the framework, the saved adapters and the docs. These stay out of git (see `.gitignore`):

- **Targets** (`targets/<app>/app`, `source`, `extracted`, `dl`): the harness, demos and `./wam` need `app/`;
  the agent loop also needs `source/` (native) or `extracted/` (Electron). Adapters are build-locked, so use the
  exact builds listed in [Results](#results) (sha256s are in each `manifest.json`).
- **REA + Ghidra** (`analysis/rea-runtime/node_modules/`, `analysis/tools/`) and **`.mcp.json`**: set up with
  `python -m analysis.setup_rea`. See [REA setup](#rea-setup-reverse-engineering-for-the-agent).
- **Run logs** (`runs/`): each agent run writes here. The original transcripts cited in `NOTES.md` were not
  carried over in the port. Their results are kept in each adapter's `validation.json` and `DISCOVERY.md`.

## REA setup (reverse engineering for the agent)

[REA](https://github.com/morluto/rea) (`rea-agents` 6.7.0) is the MCP server the discovery agent uses. It finds
strings, cross-references, callers, pseudocode and assembly in symbol-less binaries, and builds the JavaScript
module graph for Electron apps. For native binaries it drives **Ghidra 12.1.4**. Only the agent loops
(`run_m1`, `run_analysis`) need REA; the harness, demos, MCP server and `muse_chat` don't.

**Requirements:**
- Node.js 20+ and npm
- a JDK 21+ (e.g. [Adoptium](https://adoptium.net))
- about 1.5 GB of disk for Ghidra

**One command:** from the repo root, run

```sh
.venv/Scripts/python.exe -m analysis.setup_rea --download-ghidra
```

It does this:
1. Installs REA from the pinned lockfile (`npm ci` in `analysis/rea-runtime/`).
2. Downloads Ghidra 12.1.4 from the
   [official NSA release](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.4_build), checks
   its sha256 (`ddac49f9…d4db`), and unpacks it into `analysis/tools/`. Without `--download-ghidra`, it looks for
   an existing copy.
3. Finds a JDK 21+: `--java-home`, then `JAVA_HOME`, then `java` on `PATH`.
4. Writes `.mcp.json` (git-ignored) with this checkout's absolute paths. The orchestrator copies the `rea` entry
   into each agent run.
5. Runs `rea capabilities` as a smoke test.

To use a Ghidra you already have, pass `--ghidra-dir <extracted ghidra_12.1.4_PUBLIC>`; any other version is
rejected. Rerun the script after moving the repo. If REA isn't set up, `run_m1` and `run_analysis` refuse to start
and print this command. Restart Claude Code afterwards so it also picks up the `rea` server for interactive use.

## Pipeline

```
request ──► orchestrator/run_m1.py ──► headless agent: claude CLI on Meta Muse Spark (isolated workspace)
                │                        ├─ REA MCP (Ghidra): strings, xrefs, callers, pseudocode, assembly
                │                        ├─ upstream source tree (read-only)
                │                        └─ ./wam: identity | bytes | probe (disposable instance) | validate
                │                                   writes adapter/{adapter.js, manifest.json, DISCOVERY.md}
                ├─ validation/harness.py   (independent: lint, fresh instance, N calls, out-of-process
                │                           postconditions, crash check, bad-args check, restart, repeat)
                ├─ on failure: resume the same agent session with diagnostics (bounded attempts)
                ├─ transcript audit for reads of prior results  ──► runs/<run_id>/ (all evidence)
                └─ publish ──► adapters/<app>/<op>/  (status=verified, unapproved)
                                   │  orchestrator/approve.py (user)
                                   ▼
MCP client ──► mcp_server/server.py ──► runtime/bridge.py ──► Frida ──► adapter.js ──► internal function
                                   └──► runtime/electron_bridge.py ──► DevTools protocol (127.0.0.1)
                                          resolve handles (anchor → prop / [[Target]] / [[Scopes]] var),
                                          guard source_sha256 ──► adapter.js ──► internal JS function
```

Electron targets go through the same orchestrator, harness, approval gate and server. Only the runtime layer
differs. The agent gets the extracted `app.asar` as its "binary", and its `./wam` has `inspect` (walk properties,
proxy targets and closure scopes the way the bridge does) in place of `bytes`.

## Layout

| Path | Role |
|---|---|
| `orchestrator/run_m1.py` | agent loop: workspace, permissions, attempts, audit, publish. `--inject-fault` tests the repair path |
| `orchestrator/provider.py` | agent model backend: Meta Muse Spark by default (env, model pinning, Meta-priced cost); `--provider anthropic` fallback |
| `orchestrator/task_template.md` | generic task brief given to the agent (no app internals) |
| `orchestrator/wam_cli.py` | the agent's only shell command |
| `orchestrator/approve.py` | records user approval for a verified state-changing tool |
| `runtime/bridge.py` | identity-checked Frida attach; injects `CONFIG` (from the manifest) and `WAM` |
| `runtime/wam_lib.js` | generic Win32/Frida helpers: module identity, byte guards, window lookup, `onUiThread` |
| `runtime/electron_bridge.py` | Electron identity (exe/asar sha256, fuses), disposable launch, handle resolution incl. `[[Scopes]]`, adapter calls over CDP |
| `runtime/cdp.py`, `runtime/wam_electron.js` | minimal DevTools-protocol client; generic in-page helpers (`validateArgs`, `waitFor`, ...) |
| `runtime/electron_launch.py` | starts a debuggable Electron instance for the server (`runtime/live/<target>.json`) |
| `orchestrator/task_template_electron.md` | generic Electron task brief (no app internals) |
| `validation/harness.py` | disposable-instance validation; `TARGETS` registry |
| `validation/observers.py` | out-of-process postcondition observers (an adapter can't self-certify) |
| `mcp_server/server.py` | publishes verified, approved, uniquely named, Muse-compatible tools; reattaches after restarts |
| `mcp_server/muse.py` | Muse Spark function-tool rules (used by the harness and server) and MCP → function-tool conversion |
| `mcp_server/muse_chat.py` | connects Muse Spark (Responses API) to the MCP server: `--check`, `--print-tools`, one request or chat |
| `adapters/<app>/<op>/` | `adapter.js`, `manifest.json`, `DISCOVERY.md` or `evidence.json`, `validation.json` |
| `runs/<run_id>/` | per-run agent transcripts (`attempt-N.jsonl`), validation reports, adapter snapshots, `run.json` |
| `demos/` | `run_demo.py` (scripted demo), `client.py` (minimal MCP client) |
| `docs/` | `spec.md` (MVP spec), `M3_REPORT.md` (second-app report) |
| `targets/<app>/` | `app/` (portable binaries), `source/` (upstream at the matching tag), `dl/` (downloads) |
| `analysis/` | REA runtime (`rea-runtime/package.json`, pinned lockfile) + Ghidra 12.1.4 in `tools/` (git-ignored) |
| `analysis/setup_rea.py` | installs REA, fetches and verifies Ghidra, finds a JDK, writes `.mcp.json`, smoke-tests |
| `analysis/js_extract.py` | extracts JS app archives (zip family incl. Mozilla optimized jars, Electron `.asar`) with traversal checks |
| `orchestrator/run_analysis.py`, `task_template_analysis.md` | static-analysis agent loop: REA JS graph + file tools, writes `discovery.json` + `DISCOVERY.md`; no shell except `./check` |
| `validation/discovery_check.py` | verifies every cited file/line exists, the function is defined where claimed, and each cited symbol is present |
| `tests/` | offline unit tests |

## Safety properties

- Only disposable copies of the selected app are instrumented during validation and probing. Each copy keeps its config in its own folder (portable mode), so your real settings are never touched.
- Adapters fail closed. They refuse to call unless the PE timestamp, the entry bytes at the RVA, structural checks on runtime objects and thread affinity all check out. No absolute addresses are allowed (lint-enforced).
- The tool must make an in-process call: the lint rejects `SendInput`, `WM_COMMAND`, and key or mouse injection.
- Electron: each instance gets a fresh temp `--user-data-dir`, and debug ports bind to 127.0.0.1. Handles are resolved without running page code where possible, and function handles are pinned by the sha256 of their source. The lint rejects synthesized input, DOM `.click()`/`dispatchEvent`, IPC sends and event-bus `.emit()`. `page_eval` observers run under V8 `throwOnSideEffect`, so they cannot change state.
- Postconditions are evaluated by the harness from outside the process, not reported by the adapter.
- Tools that change state are published only after user approval. Destructive tools are never published.
- Status is truthful: `generated` → `verified` / `failed_validation`. Unapproved tools stay hidden unless you pass `--include-unapproved`.

See [`NOTES.md`](NOTES.md) for limitations and caveats.
