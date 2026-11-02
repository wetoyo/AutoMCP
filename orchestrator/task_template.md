# Task: expose an internal application function as an in-process tool

## Request (from the user, verbatim)

> {request}

## Target

- Application: {app_name} (authorized, open source). Target id: `{target}`.
- Executable (analyze with REA; do not modify): `{exe_path}`
- Matching upstream source tree (read-only reference): `{source_dir}`
- The binary ships **without debug symbols**. Source names do not appear in the binary; you must
  connect source-level understanding to binary locations with REA evidence.

## What to produce

Write exactly these files into `./adapter/`:

1. `adapter.js`: a Frida agent that calls the relevant internal function **in-process**.
2. `manifest.json`: the schema below.
3. `DISCOVERY.md`: the evidence chain (REA queries, addresses, why you believe each identification),
   failed hypotheses, and remaining uncertainties.

If you cannot establish the function, its signature, the required object/state, and the thread
context with evidence, **do not guess**: write `./adapter/UNRESOLVED.md` explaining what is missing and stop.

### adapter.js contract

The runtime prepends two things to your file:
- `const CONFIG = {{...}}`: `manifest.adapter_config` merged with `module`, `pe_timestamp` (int),
  `timeout_ms`. Strings in `adapter_config` of the form `"0x..."` arrive as integers.
- `WAM`: a generic Win32 helper library. Read `{wam_lib}` for its API (module identity, byte
  checks, window lookup, `onUiThread` for GUI-thread affinity, `errorResult`).

It must define:
```js
rpc.exports = {{
  check() {{ /* read-only: resolve everything, call nothing; return {{ok:true,...}} or {{ok:false,error,message}} */ }},
  async invoke(args) {{ /* validate args against the tool inputSchema (reject unknown keys with
                          {{ok:false,error:'invalid_arguments'}}), resolve, call the function, return {{ok:true,...}} */ }},
}};
```
Rules (the validator enforces several of these statically):
- **In-process call only**: construct a `NativeFunction` for the internal function and call it.
  Do not synthesize UI input or menu/command messages (SendInput, WM_COMMAND, key/mouse events, ...).
- **No absolute addresses**: resolve as module base + RVA, guarded by the PE timestamp and
  expected entry bytes (`WAM.module`, `WAM.expectBytes`). Resolve any object pointers at runtime
  from process state, and validate each one with a structural check before use.
- Respect thread affinity (GUI state belongs to the window's thread).
- Fail closed with explicit error codes. Never call when a check fails. Use `state_not_ready` (or
  `window_missing`) for transient start-up states, because callers retry only those, and use other codes
  (`state_invalid`, `version_mismatch`, `signature_mismatch`, ...) for conditions that will not fix themselves.

### manifest.json schema

```json
{{
  "tool": {{"name": "...", "description": "...", "inputSchema": {{...JSON Schema...}},
           "mutates_state": true, "destructive": false}},
  "app": {{"name": "...", "version": "...", "module": "...", "sha256": "...", "pe_timestamp": "0x...",
          "target": "{target}"}},
  "adapter_config": {{ ...values your adapter.js reads from CONFIG (RVA, entry bytes, offsets, ...)... }},
  "resolution": {{"strategy": "...", "function": "...", "calling_convention": "...", "thread_affinity": "...",
                 "this_or_state_resolution": "..."}},
  "postcondition": [
    {{"observer": "<kind>", "params": {{...}}, "expect": {{"delta": 1}}, "why": "..."}}
  ],
  "status": "generated"
}}
```

The tool is served to Meta Muse Spark as a function tool, so `tool` must meet its rules (the harness rejects it
otherwise): `name` is 1-64 chars of `[A-Za-z0-9_-]` (e.g. `<app>_<verb>_<noun>`); `description` is non-empty
and says what the call does; `inputSchema` has root `"type": "object"`, no `allOf`/`oneOf`, and nests at most
10 levels.

`postcondition` is evaluated **by the validation harness from outside the process**, before and after
each call, so it must observe the real effect of the operation. Read `{observers_py}` for the observer kinds.
Prefer at least two independent observers.

## Tools

Tool names depend on your client: REA tools may appear as `open_binary`, `rea_open_binary` or `mcp__rea__open_binary`, and `wam <args>` is either the shell command `./wam <args>` or the `wam` tool with `args` set to the argument list.

- REA: open the executable with `open_binary` (provider `ghidra`). The first
  Ghidra analysis takes about 4 minutes; queries may run in the background, so wait for them.
  Windows Ghidra is read-only here (renaming is unavailable).
- `wam` is the only command you can run:
  - `wam identity`: sha256, PE timestamp, image base, sections, file version.
  - `wam bytes <rva> <count>`: bytes from the file at an RVA.
  - `wam probe <file.js> [--seconds N] [--observe '<postcondition JSON list>']`: launch a
    **disposable** instance, run a Frida script with `WAM` loaded (`CONFIG` is `{{}}`), and print its
    `send()` messages. Use it to verify runtime layout and to test postcondition observers. It may crash
    the disposable instance; that is expected and harmless.
  - `wam validate`: run the full validation harness on `./adapter` (fresh instance, calls,
    harness-side postconditions, restart, call again). Iterate until it passes.
- File tools: read, list and search anywhere in this workspace and the source tree; write and edit only in `./adapter/`
  and `./scratch/`.

## Budget

At most {max_turns} turns. When `wam validate` passes, finish with a short summary.
