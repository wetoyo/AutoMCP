# Task: locate the internal JavaScript function behind an operation (static analysis only)

## Request (from the user, verbatim)

> {request}

## Target

- Application: {app_name}. Its logic ships as JavaScript, extracted for you (read-only) at:
  `{root}`
- Extraction inventory (archives, hashes, file counts): `{manifest}`
- This is **static analysis only**. Do not run, load, or launch any of this code or the application.

## What to produce

Write into `./analysis/`:

1. `discovery.json`: machine-checked; schema below.
2. `DISCOVERY.md`: the evidence chain (searches, REA queries, files read), the reasoning that connects
   each step, failed hypotheses, and what remains uncertain. Mark each claim as **observed** (seen in code)
   or **inferred**.

If the operation cannot be located with evidence, write `./analysis/UNRESOLVED.md` explaining what is
missing instead of guessing.

### discovery.json schema

All `file` paths are relative to the extracted root above (forward slashes). All `line` values are 1-based.

```json
{{
  "operation": "<the operation, in your words>",
  "function": {{"name": "<name as written in code; may be qualified like Obj.method>",
               "file": "<path>", "line": <line where it is defined>,
               "signature": "<parameters and meaning>", "kind": "function|method|arrow|getter"}},
  "call_chain": [{{"symbol": "<caller>", "file": "<path>", "line": <line of the call>, "role": "<why it matters>"}}],
  "entry_points": [{{"symbol": "<command/listener/IPC handler/menu id>", "file": "<path>", "line": <line>,
                    "role": "<how the user's action reaches the chain>"}}],
  "execution_context": "<where this code runs: which process/window/global scope, privileges, how to obtain any `this`/owner object>",
  "inputs": [{{"name": "...", "type": "...", "description": "..."}}],
  "observable_effects": ["<state changes an outside observer could check>"],
  "confidence": "high|medium|low",
  "unknowns": ["..."],
  "rea_evidence": ["<REA evidence ids you relied on>"]
}}
```

The checker verifies that each cited file and line exists, that `function` is **defined** at or just after
its cited line, and that every `call_chain`/`entry_points` symbol appears within ±3 lines of its citation.
It cannot verify your reasoning, so make `DISCOVERY.md` explain it.

## Tools

- REA MCP tools: `mcp__rea__analyze_javascript_application` takes `input_path` = a directory (or `.asar`)
  and builds a static application graph. The tree is large, so pass `source_paths` (exact relative paths)
  for the files you care about, and use `detail: "summary"`. `mcp__rea__trace_javascript_semantics` traces
  callers and data flow from that evidence.
- Read / Grep / Glob over the extracted root and this workspace. Edit/Write only in `./analysis/`.
- Shell: only `./check`, which runs the discovery checker on `./analysis/discovery.json`.
  Iterate until it passes.

When `./check` passes and DISCOVERY.md is complete, finish with a short summary.
