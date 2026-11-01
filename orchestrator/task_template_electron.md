# Task: expose an internal Electron application function as an in-process tool

## Request (from the user, verbatim)

> {request}

## Target

- Application: {app_name} (authorized, open source). Target id: `{target}`. It is an Electron app.
- Executable (do not modify): `{exe_path}`
- Its `app.asar`, extracted for you (read-only): `{source_dir}`. This is the shipped, bundled JavaScript:
  bundler output, possibly minified, with no source maps. Treat it as the binary.
- Run `./wam identity` first: it reports the hashes, the Electron version, the fuses, and which execution
  contexts are reachable (`renderer` pages through the remote-debugging port; `main`, the Node main process,
  only when the `EnableNodeCliInspectArguments` fuse allows the inspector).

## How the runtime calls into the app

The bridge launches a disposable instance (fresh user-data dir, 127.0.0.1 debug ports) and speaks the
DevTools protocol to one execution context: a renderer page whose URL matches `context.url_pattern`, or
the main process. It does **not** inject UI input. For every call it:

1. resolves each entry of `manifest.handles` into a live value, without running page code where it can avoid it:
   an `anchor` expression (evaluated side-effect free) followed by `path` steps:
   - `{{"prop": "name"}}`: property read (own or inherited; data properties are read without running code)
   - `{{"internal": "[[Target]]"}}`: an internal slot, e.g. the raw target behind a `Proxy`
     (V8 refuses to run proxy traps in side-effect-free mode, so unwrap proxies first)
   - `{{"scope": "name"}}`: the variable `name` from the current **function's closure scopes** (`[[Scopes]]`).
     Bundles keep most functions module-private: reach one through any reachable function that closes over
     it (its module scope lists the module's top-level bindings)
   - `{{"get": "function () {{ return this.x.get('k'); }}"}}`: an accessor run on the current value (side-effect free)
2. checks each handle's guards: `expect` (`"function"` / `"object"` / other `typeof`) and, for functions,
   `source_sha256` = sha256 of `Function.prototype.toString` (pins the exact shipped code, like entry bytes)
3. evaluates your `adapter.js` in that context and calls `check(h)` or `invoke(h, args)`, where `h.<name>`
   holds the resolved handles.

If any resolution or guard fails, the adapter is not called.

## What to produce

Write exactly these files into `./adapter/`:

1. `adapter.js`: calls the internal function **in-process** through a handle.
2. `manifest.json`: the schema below.
3. `DISCOVERY.md`: the evidence chain (files and lines in the bundle, REA queries, `./wam inspect` results,
   why each identification holds), failed hypotheses, and remaining uncertainties.

If you cannot establish the function, its arguments, the required `this`/owner object, and the context with
evidence, **do not guess**: write `./adapter/UNRESOLVED.md` explaining what is missing and stop.

### adapter.js contract

The runtime prepends `const CONFIG = {{...}}` (`manifest.adapter_config` + `timeout_ms` + the tool's
`inputSchema`) and `WAM`, a small generic helper library (read `{wam_lib}`: `AdapterError`, `errorResult`,
`waitFor`, `validateArgs`, `expectType`). It must define:

```js
rpc.exports = {{
  check(h) {{ /* read-only: verify h.* and the app state; call nothing; return {{ok:true,...}} or {{ok:false,error,message}} */ }},
  async invoke(h, args) {{ /* WAM.validateArgs(args, CONFIG.inputSchema) (unknown keys -> 'invalid_arguments'),
                              re-check state, call the internal function via h.<name>, confirm its effect, return {{ok:true,...}} */ }},
}};
```

Rules (several are enforced statically):
- **Call the internal function directly** through a handle (`h.fn.call(owner, ...)`). Do not synthesize input
  or route through UI/message plumbing: no `sendInputEvent`, `dispatchEvent`, DOM `.click()`, `execCommand`,
  `ipcRenderer`/`webContents.send`, `.emit(...)`, `postMessage`, `executeJavaScript`.
- **Pin the code**: every `expect: "function"` handle needs `source_sha256`. `./wam inspect` prints it.
- Resolve owner objects at runtime through handles and validate them before use. Never cache anything across calls.
- Fail closed with explicit codes. Use `state_not_ready` for transient start-up states (callers retry only those,
  plus `handle_missing` during start-up) and other codes (`state_invalid`, `signature_mismatch`, ...) otherwise.

### manifest.json schema

```json
{{
  "tool": {{"name": "...", "description": "...", "inputSchema": {{...JSON Schema...}},
           "mutates_state": true, "destructive": false}},
  "app": {{"name": "...", "version": "...", "kind": "electron", "exe": "...", "sha256": "...", "asar_sha256": "...",
          "electron_version": "...", "target": "{target}"}},
  "context": {{"kind": "renderer", "url_pattern": "<regex over the page URL>"}},
  "handles": {{
    "<name>": {{"anchor": "<side-effect-free expression>", "path": [{{"prop": "..."}}, {{"scope": "..."}}],
               "expect": "function", "source_sha256": "<sha256>", "why": "<what this is, with file:line>"}}
  }},
  "adapter_config": {{ ...values your adapter.js reads from CONFIG... }},
  "resolution": {{"strategy": "...", "function": "<name, bundle file:line, signature>",
                 "execution_context": "<process/page, how `this`/owner is obtained>"}},
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

`postcondition` is evaluated **by the validation harness**, before and after each call, so it must observe
the real effect of the operation. Read `{observers_py}` for the observer kinds. For Electron, `page_targets`
(page count, read from outside), `page_eval` (an expression the engine forces to be side-effect free) and the
Win32 kinds (`window_title`, `top_level_windows`: Electron windows are ordinary top-level windows) all work.
Use at least two independent observers, and at least one that is not computed from the same object your
adapter mutates.

## Tools

- REA MCP tools: `mcp__rea__analyze_javascript_application` (`input_path` = a directory or `.asar`; pass
  `source_paths` for the files you care about and `detail: "summary"`), `mcp__rea__trace_javascript_semantics`.
- Shell: only `./wam ...` is permitted. Each command launches its own disposable instance (a few seconds):
  - `./wam identity`
  - `./wam inspect --anchor '<expr>' [--path '<JSON steps>'] [--url-pattern RX] [--limit N]`: resolve exactly as
    the bridge does and describe the value: properties, internal slots, and for functions their closure-scope
    variables and `source_sha256`. This is how you find and pin handles.
  - `./wam probe <file.js> [--handles <manifest.json or handles.json>] [--observe '<postcondition JSON list>']`:
    run the file as the body of `async (h) => {{ ... }}` with `WAM` loaded and print the returned value. Use it to
    test calls and observers. It may break the disposable instance; that is harmless.
  - `./wam validate`: the full validation harness on `./adapter` (fresh instance, calls, harness-side
    postconditions, restart, call again). Iterate until it passes.
- File tools: Read/Grep/Glob anywhere in this workspace and the extracted tree; Write/Edit only in `./adapter/`
  and `./scratch/`.

## Budget

At most {max_turns} turns. When `./wam validate` passes, finish with a short summary.
