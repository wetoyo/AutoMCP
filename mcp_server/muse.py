"""Meta Muse Spark compatibility for published tools.

The Meta Model API has no MCP connector: a client lists the MCP server's tools and sends each one to Muse Spark as
a Responses API `function` tool, then runs the calls it gets back. This module holds that conversion and the rules
a tool must meet to be accepted (enforced by the harness at validation time and by the server at publish time).

Meta's rules (dev.meta.ai/docs/tool-calling, /docs/structured-output): function names match ^[a-zA-Z0-9_.-]+$ with
at most one dot; `parameters` is a JSON Schema object, max nesting depth 10; `allOf`/`oneOf` are unsupported; the
root may not be an `anyOf`/`enum`/`not`. We are stricter on names (no dots, <= 64 chars) so a name means the same
thing to every MCP client.
"""
from __future__ import annotations

import re

RESPONSES_URL = "https://api.meta.ai/v1/responses"
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_DEPTH = 10
_ROOT_FORBIDDEN = ("anyOf", "oneOf", "allOf", "enum", "not")
_NOWHERE = ("allOf", "oneOf")


def _depth(node, level=1) -> int:
    if isinstance(node, dict):
        return max([level] + [_depth(v, level + 1) for v in node.values() if isinstance(v, (dict, list))])
    if isinstance(node, list):
        return max([level] + [_depth(v, level) for v in node if isinstance(v, (dict, list))])
    return level


def _keys_anywhere(node, keys) -> set[str]:
    found = set()
    if isinstance(node, dict):
        found |= {k for k in node if k in keys}
        for v in node.values():
            found |= _keys_anywhere(v, keys)
    elif isinstance(node, list):
        for v in node:
            found |= _keys_anywhere(v, keys)
    return found


def tool_problems(tool: dict) -> list[str]:
    """Why Muse Spark would reject this tool (manifest `tool` section, or an MCP tool as a dict)."""
    problems = []
    name = tool.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        problems.append(f"tool.name {name!r}: Muse Spark needs 1-64 chars of [A-Za-z0-9_-]")
    if not str(tool.get("description") or "").strip():
        problems.append("tool.description: Muse Spark picks tools by description; it must not be empty")
    schema = tool.get("inputSchema", {"type": "object"})
    if not isinstance(schema, dict):
        return problems + ["tool.inputSchema must be a JSON Schema object"]
    if schema.get("type", "object") != "object":
        problems.append("tool.inputSchema: root type must be 'object'")
    for k in _ROOT_FORBIDDEN:
        if k in schema:
            problems.append(f"tool.inputSchema: root '{k}' is rejected by Muse Spark")
    for k in sorted(_keys_anywhere(schema, _NOWHERE)):
        problems.append(f"tool.inputSchema: '{k}' is not supported by Muse Spark")
    if _depth(schema) > MAX_DEPTH:
        problems.append(f"tool.inputSchema: nesting deeper than {MAX_DEPTH} levels")
    return problems


def function_tool(name: str, description: str, input_schema: dict | None) -> dict:
    """An MCP tool as a Muse Spark Responses API function tool."""
    params = {k: v for k, v in (input_schema or {}).items() if k not in ("title", "$schema")}
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return {"type": "function", "name": name, "description": description or "", "parameters": params,
            "strict": False}
