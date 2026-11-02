"""Model backend and harness selection for the agent loops (run_m1, run_analysis).

--harness native (default): orchestrator.agent_loop calls Meta's Muse Spark (Responses API) directly and gets its
tools from orchestrator.workbench and REA over MCP. No agent CLI is involved.

--harness claude: the headless `claude` CLI. With --provider muse-spark (default) it talks to Meta's
Anthropic-compatible Messages endpoint, with every model alias pinned to the same Muse Spark model so no
background task, subagent or plan step goes to a model Meta does not serve. --provider anthropic uses Claude.
"""
from __future__ import annotations

import os

META_BASE_URL = "https://api.meta.ai"  # the CLI appends /v1/messages
META_KEY_ENV = "MODEL_API_KEY"

# USD per 1M tokens (input, cached input, output), from Meta's Model API pricing (public preview, Nov 2026).
MUSE_SPARK_PRICES = {
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.002, 0.20),
    "muse-spark-1.1": (1.25, 0.15, 4.25),
}

PROVIDERS = {
    "muse-spark": {"default_model": "muse-spark-1.3", "models": list(MUSE_SPARK_PRICES)},
    "anthropic": {"default_model": "opus", "models": None},
}
DEFAULT_PROVIDER = os.environ.get("WAM_AGENT_PROVIDER", "muse-spark")

# Anything that could route the CLI to another backend or leak another service's credentials.
_STRIP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
          "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
          "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
          "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


class ProviderError(RuntimeError):
    pass


def add_args(ap) -> None:
    ap.add_argument("--provider", choices=list(PROVIDERS), default=DEFAULT_PROVIDER,
                    help="agent model backend (default: muse-spark, via the Meta Model API)")
    ap.add_argument("--harness", choices=["native", "claude"], default=os.environ.get("WAM_AGENT_HARNESS", "native"),
                    help="native: built-in agent loop (orchestrator.agent_loop, Muse Spark only); "
                         "claude: the headless `claude` CLI")
    ap.add_argument("--model", default=None,
                    help="model id; default muse-spark-1.3 for muse-spark, opus for anthropic")


def resolve_model(a) -> str:
    p = PROVIDERS[a.provider]
    model = a.model or p["default_model"]
    if getattr(a, "harness", "native") == "native" and a.provider != "muse-spark":
        raise ProviderError("the native harness runs on Muse Spark only; pass --harness claude for --provider anthropic")
    if p["models"] is not None and model not in p["models"]:
        raise ProviderError(f"--model {model!r} is not a Muse Spark model; choose one of {p['models']}")
    return model


def agent_env(provider: str, model: str, harness: str = "claude") -> dict:
    """Environment for the agent subprocess."""
    env = dict(os.environ)
    if provider == "anthropic":
        return env
    key = env.get(META_KEY_ENV)
    if not key:
        raise ProviderError(f"{META_KEY_ENV} is not set. Create a key at https://dev.meta.ai and export it, "
                            "or pass --provider anthropic.")
    if harness == "native":
        return env
    for k in _STRIP:
        env.pop(k, None)
    env.update({
        "ANTHROPIC_BASE_URL": META_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": key,           # sent as Authorization: Bearer
        "ANTHROPIC_CUSTOM_HEADERS": "",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "ENABLE_TOOL_SEARCH": "true",          # off by default for non-Anthropic hosts
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    return env


def estimate_cost(usage: dict | None, provider: str, model: str) -> float | None:
    """Meta-priced cost of one CLI result's token usage. The CLI's own total_cost_usd uses Anthropic prices,
    so it is wrong for Muse Spark runs."""
    if provider != "muse-spark" or not usage or model not in MUSE_SPARK_PRICES:
        return None
    inp, cached, out = MUSE_SPARK_PRICES[model]
    fresh = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    total = fresh * inp + (usage.get("cache_read_input_tokens") or 0) * cached + (usage.get("output_tokens") or 0) * out
    return round(total / 1e6, 4)
