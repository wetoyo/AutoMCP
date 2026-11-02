import argparse
import os
import unittest
from unittest import mock

from orchestrator import provider


def parse(*argv):
    ap = argparse.ArgumentParser()
    provider.add_args(ap)
    return ap.parse_args(list(argv))


class ProviderTests(unittest.TestCase):
    def test_default_is_muse_spark(self):
        if "WAM_AGENT_PROVIDER" not in os.environ:
            self.assertEqual(parse().provider, "muse-spark")
        self.assertEqual(provider.resolve_model(parse("--provider", "muse-spark")), "muse-spark-1.3")
        self.assertEqual(provider.resolve_model(parse("--provider", "anthropic", "--harness", "claude")), "opus")

    def test_rejects_non_muse_model(self):
        a = parse("--provider", "muse-spark", "--model", "opus")
        with self.assertRaises(provider.ProviderError):
            provider.resolve_model(a)

    def test_env_pins_every_alias_and_strips_other_credentials(self):
        ambient = {"MODEL_API_KEY": "meta-key", "ANTHROPIC_API_KEY": "sk-ant-real",
                   "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway: secret", "CLAUDE_CODE_USE_BEDROCK": "1"}
        with mock.patch.dict(os.environ, ambient):
            env = provider.agent_env("muse-spark", "muse-spark-1.3")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.meta.ai")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "meta-key")
        for k in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                  "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            self.assertEqual(env[k], "muse-spark-1.3", k)

    def test_missing_key_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(provider.ProviderError):
                provider.agent_env("muse-spark", "muse-spark-1.3")

    def test_anthropic_passthrough(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            self.assertEqual(provider.agent_env("anthropic", "opus")["ANTHROPIC_API_KEY"], "k")

    def test_cost_uses_meta_prices(self):
        usage = {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}
        self.assertAlmostEqual(provider.estimate_cost(usage, "muse-spark", "muse-spark-1.3"), 1.25 + 0.15 + 4.25)
        self.assertIsNone(provider.estimate_cost(usage, "anthropic", "opus"))


if __name__ == "__main__":
    unittest.main()
