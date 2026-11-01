# Windows Auto-MCP — MVP Specification

## Objective

Build an agent-driven system that takes a **user-selected, running Windows desktop application** and a natural-language operation, discovers a way to call the relevant **internal function inside that process**, and publishes a reusable, validated MCP tool.

The MVP is **not** a universal integration solution. It demonstrates the complete pipeline on one supported, open-source Windows application, with a second application as a stretch goal. Use only applications and processes you own or are authorized to instrument.

## Core user flow

1. **Select target:** User selects a running process and requests an operation (e.g., “create a new document”).
2. **Discover:** The agent uses REA to inspect the executable, loaded modules, symbols, call sites, and relevant functions. It records candidate entry points and evidence.
3. **Plan invocation:** The agent identifies the function signature, argument representations, required application state, and appropriate thread or execution context. If these cannot be established, it stops rather than guessing.
4. **Generate adapter:** The agent writes an application-specific Frida adapter that resolves the function in the current process and invokes it in-process. The adapter includes input validation and explicit error handling.
5. **Validate:** A test harness runs the adapter against an isolated, restartable instance of the target app. It verifies an observable postcondition, not merely that the call returned without error.
6. **Repair or reject:** On failure, the harness supplies diagnostics to the agent for a bounded number of retries. Crashes, ambiguous effects, or unsafe operations cause rejection.
7. **Publish MCP tool:** After successful validation and user approval, the system generates an MCP tool schema and maps it to the saved adapter.
8. **Reuse:** A separate MCP client invokes the tool. The MCP server routes arguments to the existing adapter without repeating reverse engineering or invoking the LLM on each call.

## System components

| Component | Responsibility |
|---|---|
| Orchestrator | Manages the agent, execution state, retries, and artifacts |
| REA analysis interface | Supplies reverse-engineering evidence and candidate functions |
| Runtime bridge (Frida) | Attaches to the authorized process and executes validated adapters |
| Adapter store | Saves per-app invocation code, signatures, versions, and validation results |
| Test harness | Launches isolated app instances, checks postconditions, and captures crashes/logs |
| MCP server | Publishes validated tools and forwards calls to the runtime bridge |

## Minimal implementation milestones

### M0 — Manual end-to-end proof
- Choose a small open-source Windows application with a verifiable internal operation.
- Manually identify one function and implement a Frida adapter that invokes it in-process.
- Verify the operation changed application state as intended.
- Expose the adapter as one MCP tool and call it from an independent MCP client.

**Exit criterion:** One MCP call reliably triggers a real in-process operation.

### M1 — Agent-generated adapter
- Connect the coding agent to REA inspection results and the adapter workspace.
- Ask the agent to find and implement the same operation without manually supplying the function address or adapter code.
- Log all discovery evidence, generated files, and failed attempts.

**Exit criterion:** Agent produces the adapter from a natural-language operation request.

### M2 — Automated validation and persistence
- Add a machine-checkable postcondition and bounded repair loop.
- Save a working adapter with app identity/version, function resolution strategy, input schema, and test results.
- Reattach after restarting the application and call the MCP tool again without LLM involvement.

**Exit criterion:** A generated tool passes validation and survives a clean app restart.

### M3 — Generalization (stretch)
- Repeat on a second open-source application without writing a custom integration by hand.
- Report which parts required manual intervention and which were agent-driven.

## Example request and resulting tool

**User request:** “Expose the application's internal function for creating a document.”

**Illustrative generated MCP tool:**

```json
{
  "name": "create_document",
  "description": "Create a new document in the selected running application",
  "inputSchema": {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": false
  }
}
```

The schema is only an example; the actual callable interface must be derived from the target application's verified behavior.

## Important engineering constraints

- **In-process, not UI automation:** The adapter calls an internal operation in the original running process. It does not simulate clicks or independently reimplement the feature.
- **No assumed signatures:** Correct addresses alone are insufficient. ABI, object layout, lifetime, initialization, and thread affinity must be validated.
- **Restart-safe resolution:** Avoid hard-coded absolute addresses; store an appropriate verified symbol, module-relative offset, or signature, plus application-version checks.
- **Crash containment:** Test in a disposable instance and retain a restart path. An in-process fault can crash the target.
- **Authorization:** Only attach to explicitly selected, authorized applications. Do not bypass security controls, anti-tamper protections, or protected processes.
- **Approval for writes:** Require user approval before publishing tools that mutate application state. Default to denying destructive operations.
- **Bounded agent loop:** Set limits on time, token usage, retries, and executed operations.
- **Truthful status:** Distinguish discovered, generated, tested, and verified tools; never label an untested adapter as working.

## Suggested project structure

```text
windows-auto-mcp/
  orchestrator/
  analysis/
  runtime/
  validation/
  mcp_server/
  adapters/
  targets/
  demos/
  README.md
```

## Hackathon demo script

1. Launch the authorized target application.
2. Select its process and type a natural-language operation.
3. Show the agent using REA to locate a candidate internal function.
4. Show the generated Frida adapter and its validation results.
5. Publish the generated MCP tool.
6. Invoke the tool from a separate MCP client and show the application changing state.
7. Restart the application and invoke the saved tool again **without** asking the LLM to regenerate it.

## Success metrics

- At least **one** working agent-generated, in-process MCP tool.
- A verified observable postcondition for every published tool.
- Successful reuse after process restart.
- Zero manually written application-specific adapter code in the automated demonstration (manual M0 prototype is allowed).
- Documented failures, limitations, and required human interventions.

## Out of scope for MVP

Arbitrary Windows application support, kernel-level instrumentation, anti-cheat/protected processes, automated privilege escalation, destructive actions, cross-platform compatibility, and guaranteed support for every C++ or managed runtime.
