#!/usr/bin/env python3
"""Contract tests for explicit public APIs in the new production Python modules."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_EXPORTS = {
    "scripts/aeon_hang_guard.py": ("FLUSH_INTERVAL", "apply"),
    "scripts/gb10_embedding_activation.py": (
        "ActivationInterrupted",
        "activate",
        "main",
    ),
    "scripts/gb10_embedding_activation_checks.py": (
        "ActivationCheckError",
        "GENERATION_FIELDS",
        "Generation",
        "NEIGHBORS",
        "NEIGHBOR_FIELDS",
        "RuntimeConfig",
        "UNIT",
        "capture_baselines",
        "generation_is_new",
        "neighbors",
        "query_generation",
        "require_docker_cgroup_v2",
        "run_systemctl",
        "verify_models",
        "wait_new_generation",
    ),
    "scripts/gb10_embedding_activation_config.py": (
        "ActivationConfigError",
        "production_config",
        "test_config",
    ),
    "scripts/gb10_embedding_activation_storage.py": (
        "ActivationStorageError",
        "EXPECTED_NO_SWAP_SHA256",
        "EXPECTED_VERIFIER_AUTHORITY",
        "NO_SWAP_KEYS",
        "NO_SWAP_PRIOR_FILES",
        "NO_SWAP_PRIVATE_FILES",
        "PHASES",
        "SourceSnapshot",
        "TRANSITIONS",
        "TransactionError",
        "atomic_json",
        "atomic_write",
        "fsync_directory",
        "secure_directory",
        "secure_regular",
    ),
    "scripts/gb10_querit_canary_deploy.py": ("main",),
    "scripts/gb10_querit_canary_lifecycle.py": ("main",),
    "scripts/gb10_querit_canary_preflight.py": (),
    "scripts/gb10_verify_embedding_profile.py": (
        "CANARY_INPUTS",
        "ContainerState",
        "EXPECTED_MEMORY",
        "RuntimeConfig",
        "SystemdState",
        "UNIT",
        "cosine",
        "main",
        "validate_unit",
        "vectors",
        "verify_production",
    ),
    "scripts/gb10_verify_vllm_no_swap_core.py": ("main",),
    "scripts/hooks/receipt-store.py": ("StoreError", "main", "run"),
    "scripts/querit_canary_lifecycle.py": (
        "LifecycleCancelled",
        "main",
        "preflight",
    ),
    "scripts/querit_canary_runtime.py": (
        "ADAPTER_UNIT",
        "BACKEND_UNIT",
        "DEFAULT_MODEL",
        "EMBEDDING_UNIT",
        "GUARD_UNIT",
        "IMMUTABLE_NEIGHBORS",
        "LEGACY_RERANKER_UNIT",
        "LifecycleError",
        "MINIMUM_HEADROOM_GIB",
        "PRODUCTION_RERANKER_UNIT",
        "ServiceState",
        "SystemHost",
        "TEXT_UNIT",
    ),
    "scripts/querit_canary_transaction.py": (
        "Host",
        "activate",
        "deactivate",
        "restoring_original",
    ),
    "scripts/querit_checkpoint_convert.py": (
        "convert_snapshot",
        "main",
        "rewrite_config",
        "rewrite_head_state",
        "rewrite_weight_index",
    ),
    "scripts/querit_legacy_canary_equivalence.py": (
        "Attempt",
        "HarnessError",
        "HttpResponse",
        "NormalizationError",
        "PlanError",
        "PlannedAttempt",
        "ReceiptError",
        "SplitGroup",
        "aggregate_results",
        "assert_receipt_private",
        "auxiliary_schedule",
        "build_receipt",
        "canonical_json_bytes",
        "dry_run_receipt",
        "execute_schedule",
        "exit_code_for",
        "load_plan",
        "main",
        "main_schedule",
        "normalize_candidate_response",
        "normalize_legacy_response",
        "paired_bootstrap_lower_bound",
        "run",
        "schedule_sha256",
        "split_groups",
        "urllib_transport",
        "validate_corpus",
        "validate_plan",
        "validate_receipt",
        "warm_schedule",
        "write_owner_only_receipt",
    ),
    "scripts/verify_systemd_units.py": ("main",),
}


class ProductionModuleExportContractTests(unittest.TestCase):
    def test_new_production_modules_declare_exact_public_api(self) -> None:
        self.assertEqual(len(EXPECTED_EXPORTS), 17)
        for relative_path, expected in EXPECTED_EXPORTS.items():
            with self.subTest(module=relative_path):
                tree = ast.parse((ROOT / relative_path).read_text(), filename=relative_path)
                declarations = [
                    node
                    for node in tree.body
                    if isinstance(node, (ast.Assign, ast.AnnAssign))
                    and any(
                        isinstance(target, ast.Name) and target.id == "__all__"
                        for target in (
                            node.targets if isinstance(node, ast.Assign) else [node.target]
                        )
                    )
                ]
                self.assertEqual(len(declarations), 1, "expected one __all__ declaration")
                declaration = declarations[0]
                value = declaration.value
                if value is None:
                    self.fail("__all__ declaration has no value")
                exports = ast.literal_eval(value)
                self.assertIsInstance(exports, (list, tuple))
                self.assertEqual(tuple(exports), expected)
                self.assertEqual(len(exports), len(set(exports)))
                self.assertTrue(
                    all(isinstance(name, str) and not name.startswith("_") for name in exports)
                )


if __name__ == "__main__":
    unittest.main()
