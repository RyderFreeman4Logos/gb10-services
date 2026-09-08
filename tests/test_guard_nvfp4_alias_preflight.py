"""Source-owned NVFP4 Guard alias and reserved-identity preflight."""

from __future__ import annotations

import copy
import tomllib
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GUARD_CONFIG = ROOT / "config/llm-guard-proxy/config.toml"
PROFILE_GUARD_CONFIG = (
    ROOT / "profile/aeon-ultimate-uncensored-nvfp4/llm-guard-proxy/config.toml"
)
PUBLIC_ALIASES = [
    "abliterated-qwen-latest-27b-nvfp4-none",
    "abliterated-qwen-latest-27b-nvfp4-low",
    "abliterated-qwen-latest-27b-nvfp4-medium",
]
RESERVED_INGRESS_MODEL_IDS = [
    "abliterated-qwen-latest-27b-nvfp4",
    "aeon",
    "aeon-ultimate",
]
STALE_PUBLIC_ALIASES = {
    "abliterated-qwen-latest-27b-none",
    "abliterated-qwen-latest-27b-low",
    "abliterated-qwen-latest-27b-medium",
}
CANONICAL_UPSTREAM = "abliterated-qwen-latest-27b-nvfp4"
FORCED_ALIAS_PROFILES = [
    {
        "alias": "abliterated-qwen-latest-27b-nvfp4-none",
        "upstream_model": CANONICAL_UPSTREAM,
        "thinking_mode": "force_disable",
        "output_cap": 16384,
        "temperature": 0.7,
        "top_p": 0.80,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    },
    {
        "alias": "abliterated-qwen-latest-27b-nvfp4-low",
        "upstream_model": CANONICAL_UPSTREAM,
        "thinking_mode": "force_thinking",
        "thinking_budget": 65536,
        "output_cap": 16384,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    },
    {
        "alias": "abliterated-qwen-latest-27b-nvfp4-medium",
        "upstream_model": CANONICAL_UPSTREAM,
        "thinking_mode": "force_thinking",
        "thinking_budget": 65536,
        "output_cap": 16384,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    },
]


def load_guard_config() -> dict[str, Any]:
    return tomllib.loads(GUARD_CONFIG.read_text(encoding="utf-8"))


def preflight_errors(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    reserved = config.get("upstream", {}).get("reserved_ingress_model_ids")
    if reserved != RESERVED_INGRESS_MODEL_IDS:
        errors.append(
            "upstream.reserved_ingress_model_ids must exactly match the reserved identities"
        )
    if config.get("forced_model_alias_profiles") != FORCED_ALIAS_PROFILES:
        errors.append(
            "forced_model_alias_profiles must exactly match the reviewed aliases"
        )
    default_chat = next(
        (
            upstream
            for upstream in config.get("upstreams", [])
            if isinstance(upstream, dict)
            and upstream.get("name") == "aeon-default-no-think"
        ),
        None,
    )
    if default_chat is None:
        errors.append("aeon-default-no-think routing profile is missing")
        return errors
    if default_chat.get("match_models") != PUBLIC_ALIASES:
        errors.append(
            "aeon-default-no-think match_models must exactly match the public NVFP4 aliases"
        )
    if default_chat.get("upstream_model") != "aeon-ultimate":
        errors.append("aeon-default-no-think upstream_model must remain aeon-ultimate")
    routed = set(default_chat.get("match_models") or [])
    if routed & set(RESERVED_INGRESS_MODEL_IDS):
        errors.append("reserved identities must not appear in public chat routing")
    if routed & STALE_PUBLIC_ALIASES:
        errors.append("stale public aliases must not remain in chat routing")
    profiles = {
        upstream["name"]: upstream
        for upstream in config.get("upstreams", [])
        if isinstance(upstream, dict) and "name" in upstream
    }
    embedding = profiles.get("qwen3-embedding-8b", {}).get("match_models")
    reranker = profiles.get("qwen3-reranker-8b", {}).get("match_models")
    if embedding != ["qwen3-embedding-8b", "Qwen/Qwen3-Embedding-8B"]:
        errors.append("embedding match_models must remain unchanged")
    if reranker != ["qwen3-reranker-8b", "Qwen/Qwen3-Reranker-8B"]:
        errors.append("reranker match_models must remain unchanged")
    return errors


class GuardNvfp4AliasPreflightTests(unittest.TestCase):
    def test_source_config_passes_semantic_preflight(self) -> None:
        self.assertEqual(preflight_errors(load_guard_config()), [])
        self.assertEqual(GUARD_CONFIG.read_bytes(), PROFILE_GUARD_CONFIG.read_bytes())

    def test_reserved_identities_fail_closed_for_missing_extra_and_stale(self) -> None:
        source = load_guard_config()
        mutations = {
            "absent": None,
            "missing": ["abliterated-qwen-latest-27b-nvfp4", "aeon-ultimate"],
            "extra": [*RESERVED_INGRESS_MODEL_IDS, "extra"],
            "stale": [
                "abliterated-qwen-latest-27b-nvfp4",
                "aeon",
                "qwen3.6-27b-decensor-by-aeon",
            ],
        }
        for label, reserved in mutations.items():
            with self.subTest(mutation=label):
                candidate = copy.deepcopy(source)
                if reserved is None:
                    candidate["upstream"].pop("reserved_ingress_model_ids")
                else:
                    candidate["upstream"]["reserved_ingress_model_ids"] = reserved
                self.assertIn(
                    "upstream.reserved_ingress_model_ids must exactly match the reserved identities",
                    preflight_errors(candidate),
                )

    def test_profile_and_routing_membership_fail_closed(self) -> None:
        source = load_guard_config()
        cases = {
            "absent-profiles": lambda candidate: candidate.pop(
                "forced_model_alias_profiles"
            ),
            "extra-profile": lambda candidate: candidate[
                "forced_model_alias_profiles"
            ].append(
                {
                    **candidate["forced_model_alias_profiles"][0],
                    "alias": "abliterated-qwen-latest-27b-nvfp4-xhigh",
                }
            ),
            "stale-alias": lambda candidate: candidate["forced_model_alias_profiles"][
                0
            ].__setitem__("alias", "abliterated-qwen-latest-27b-none"),
            "stale-routing": lambda candidate: next(
                upstream
                for upstream in candidate["upstreams"]
                if upstream["name"] == "aeon-default-no-think"
            ).__setitem__(
                "match_models",
                [
                    "abliterated-qwen-latest-27b-none",
                    "abliterated-qwen-latest-27b-low",
                    "abliterated-qwen-latest-27b-medium",
                ],
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(mutation=label):
                candidate = copy.deepcopy(source)
                mutate(candidate)
                self.assertTrue(preflight_errors(candidate))


if __name__ == "__main__":
    unittest.main()
