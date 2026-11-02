# Windows Auto-MCP

Turn a natural-language request ("create a new document") into a validated MCP tool that calls the matching
**internal function inside a running Windows app**. An agent running on **Meta Muse Spark** finds the function
with REA/Ghidra and writes an in-process adapter (Frida for native apps, the DevTools protocol for Electron).
A harness validates it against disposable instances of the app, and the MCP server then serves it with no LLM in
the loop. Muse Spark can call the published tools directly through `mcp_server.muse_chat`.

Spec: [`docs/spec.md`](docs/spec.md) · Engineering notes: [`NOTES.md`](NOTES.md) ·
Second-app report: [`docs/M3_REPORT.md`](docs/M3_REPORT.md)

## Results

| Tool | App | Internal function | Origin |
|---|---|---|---|
| `notepadpp_new_document` | Notepad++ 8.9.8.1 x64 | `Notepad_plus::fileNew()` (RVA `0x13c7a0`) | manual (M0) and agent (M1) |
| `sumatrapdf_new_window` | SumatraPDF 3.6.1 x64 | `CreateAndShowMainWindow(nullptr)` (RVA `0x1a39b8`) | agent, first attempt |
| `marktext_new_untitled_tab` | MarkText 0.22.0 (Electron 42.1.0) | editor store `NEW_UNTITLED_TAB` | agent, first attempt |

Each tool was verified on a fresh instance and again after a restart. The repair loop was proven with
`--inject-fault`: the harness caught the corrupted adapter and the agent fixed it on its second attempt.

## How it works

```
request ──► orchestrator.run_m1 ──► agent on Muse Spark (isolated workspace)
                │                     ├─ REA MCP (Ghidra): strings, xrefs, callers, pseudocode
                │                     ├─ upstream source or extracted app.asar (read-only)
                │                     └─ wam: identity | bytes | inspect | probe | validate
                ├─ validation.harness: lint, fresh instance, N calls, out-of-process postconditions,
                │                      crash and bad-args checks, restart, repeat
                ├─ on failure: resume the agent with diagnostics (bounded attempts)
                ├─ transcript audit for reads of prior results
                └─ publish (verified) ──► orchestrator.approve (user)
                                                │
MCP client / muse_chat ──► mcp_server.server ──► Frida or DevTools ──► adapter.js ──► internal function
```

## Setup

Requires Windows, Python 3.10+, Node.js 20+ and a JDK 21+. No agent CLI is needed.

```sh
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python.exe -m unittest discover -s tests -t .
```

**Muse Spark.** Get a Meta Model API key at <https://dev.meta.ai> and export it as `MODEL_API_KEY`. The agent
loops and `muse_chat` use it; nothing else calls a model.

**REA + Ghidra.** The agent's reverse-engineering server.

```sh
.venv/Scripts/python.exe -m analysis.setup_rea --download-ghidra
```

This installs [REA](https://github.com/morluto/rea) 6.7.0 from the lockfile, downloads Ghidra 12.1.4 from the
[official release](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.4_build) (sha256
checked), finds a JDK, writes `.mcp.json` and runs a smoke test. Use `--ghidra-dir` and `--java-home` to point at
existing installs.

**Targets.** Put the app builds under `targets/<app>/app/`. The agent also needs `source/` (native) or
`extracted/` (Electron; see `analysis.js_extract`). Adapters are pinned to the exact build they were made for.

## Usage

```sh
# Generate a tool
python -m orchestrator.run_m1 --target sumatrapdf --operation new_window \
    --request "Expose the application's internal function for opening a new window"

# Revalidate, then approve it for publishing
python -m validation.harness sumatrapdf new_window --calls 3
python -m orchestrator.approve sumatrapdf new_window

# Use the tools from Muse Spark
python -m mcp_server.muse_chat --check                          # Meta accepts every tool definition
python -m mcp_server.muse_chat "Open a new SumatraPDF window"   # omit the request to chat

# Scripted demo: call, restart the app, call again
python demos/run_demo.py --app sumatrapdf --allow-unapproved
```

For Electron targets, start a debuggable instance first with `python -m runtime.electron_launch <target>`.
For static analysis of any JavaScript app, use `python -m orchestrator.run_analysis`.

**Agent options.** By default the orchestrator runs its built-in agent loop (`orchestrator.agent_loop`), which
calls Muse Spark directly and needs no agent CLI. `--harness claude` runs the headless `claude` CLI instead, on
Muse Spark or, with `--provider anthropic`, on Claude.

**Bring your own agent.** The agent's tools are an MCP server, so any MCP-capable agent can do the discovery:

```sh
python -m orchestrator.workbench --config runs/<run_id>/workbench.json
```

It serves file tools (reads scoped to the workspace and the reference source, writes only to
`adapter/`) and `wam`. Pair it with the `rea` server from `.mcp.json`; `run_m1` writes both configs for each run.

**Other MCP clients.** `mcp_server.server` is a standard stdio server:

```json
{"mcpServers": {"windows-auto-mcp": {"command": "<repo>\\.venv\\Scripts\\python.exe",
  "args": ["-m", "mcp_server.server"], "cwd": "<repo>"}}}
```

It attaches to the running process whose image sha256 matches the adapter; set `WAM_TARGET_PID` when several
match.

## Safety

- Validation and probing only touch disposable, portable-mode copies of the app.
- Adapters fail closed: they check the PE timestamp, entry bytes, runtime structures and thread before calling.
  Electron function handles are pinned by source sha256.
- Calls must be in-process. The lint rejects synthesized input, `WM_COMMAND`, DOM events and IPC.
- Postconditions are observed from outside the process; an adapter cannot certify itself.
- State-changing tools need user approval, destructive tools are never published, and every tool must meet
  Muse Spark's function-tool rules (`mcp_server/muse.py`).

## Layout

| Path | Role |
|---|---|
| `orchestrator/` | run loops (`run_m1`, `run_analysis`), built-in agent (`agent_loop.py`), agent tools as MCP (`workbench.py`), task briefs, `wam`, approval |
| `validation/` | disposable-instance harness, out-of-process observers, discovery checks |
| `runtime/` | Frida and DevTools bridges, in-process helper libraries, Electron launcher |
| `mcp_server/` | MCP server, Muse Spark tool rules and client (`muse_chat.py`) |
| `analysis/` | REA setup, JavaScript archive extraction |
| `demos/`, `docs/`, `tests/` | scripted demo and MCP client, spec and reports, offline unit tests |
