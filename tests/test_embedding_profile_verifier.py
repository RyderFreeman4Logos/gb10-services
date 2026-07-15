from __future__ import annotations

import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = ROOT / "scripts" / "gb10_verify_embedding_profile.py"
UNIT = ROOT / "systemd" / "vllm-embedding.service"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from embedding_profile_fixtures import (  # noqa: E402
    CONTAINER_ID,
    CURRENT_INVOCATION,
    MODELS,
    VerifierFixture,
    embedding_payload,
)


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("embedding_verifier_under_test", VERIFIER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load production verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_fixture(
    fixture: VerifierFixture, *, optimized: bool = False
) -> subprocess.CompletedProcess[str]:
    env = fixture.env()
    if optimized:
        env["PYTHONOPTIMIZE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(VERIFIER_PATH),
            "--test-only",
            str(fixture.config()),
            str(fixture.evidence),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


class StrictUnitParserTests(unittest.TestCase):
    def test_production_parser_rejects_complete_hostile_mutation_class(self) -> None:
        verifier = _load_verifier()
        canonical = UNIT.read_text()
        mutations = {
            "host short memory alias": canonical.replace(
                "  --memory 20g \\\n", "  --memory 20g -m 24g \\\n", 1
            ),
            "host flag after image": canonical.replace(
                "  /usr/local/bin/vllm serve",
                "  -m 24g \\\n  /usr/local/bin/vllm serve",
                1,
            ),
            "third model alias": canonical.replace(
                "qwen3-embedding-8b Qwen/Qwen3-Embedding-8B",
                "qwen3-embedding-8b Qwen/Qwen3-Embedding-8B hostile",
                1,
            ),
            "unexpected model option": canonical.replace(
                "    --enforce-eager", "    --truncate-dim 256 \\\n    --enforce-eager", 1
            ),
            "neighbor lifecycle": canonical
            + "\nExecStartPost=/usr/bin/systemctl --user restart "
            + "vllm-aeon-27b-dflash.service\n",
            "duplicate stop": canonical + "\nExecStop=/usr/bin/true\n",
            "malformed quoting": canonical.replace(
                "--dtype bfloat16", "--dtype 'bfloat16", 1
            ),
        }
        verifier.validate_unit(UNIT)
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "hostile.service"
                path.write_text(mutation)
                with self.assertRaises((RuntimeError, ValueError)):
                    verifier.validate_unit(path)

    def test_vector_validation_rejects_bool_and_scaled_cosine_is_finite(self) -> None:
        verifier = _load_verifier()
        payload = embedding_payload(MODELS[0])
        payload["data"][0]["embedding"][0] = True
        with self.assertRaises(RuntimeError):
            verifier.vectors(payload)

        large = [1e308] * 4096
        score = verifier.cosine(large, large)
        self.assertTrue(math.isfinite(score))
        self.assertAlmostEqual(score, 1.0, places=12)


class BoundedCommandTests(unittest.TestCase):
    def test_timeout_terminates_and_reaps_descendant_process_group(self) -> None:
        verifier = _load_verifier()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid_path = root / "child.pid"
            shim = root / "spawn-descendant"
            shim.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess, sys, time\n"
                "child = subprocess.Popen(['/usr/bin/sleep', '30'])\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "time.sleep(30)\n"
            )
            shim.chmod(0o700)
            try:
                with self.assertRaises(Exception):
                    verifier.command([str(shim), str(child_pid_path)], timeout=1)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not child_pid_path.exists():
                    time.sleep(0.01)
                self.assertTrue(child_pid_path.exists(), "shim did not publish child PID")
                child_pid = int(child_pid_path.read_text())
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if child_pid_path.exists():
                    child_pid = int(child_pid_path.read_text())
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and Path(f"/proc/{child_pid}").exists():
                        time.sleep(0.02)

    def test_output_and_input_bounds_are_enforced_while_process_runs(self) -> None:
        verifier = _load_verifier()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "output exceeded bound"):
            verifier.command(
                [
                    sys.executable,
                    "-c",
                    "import os,time; os.write(1, b'x' * (5 * 1024 * 1024)); time.sleep(30)",
                ],
                timeout=10,
            )
        self.assertLess(time.monotonic() - started, 5)
        with self.assertRaisesRegex(RuntimeError, "input exceeded bound"):
            verifier.command(
                [sys.executable, "-c", "pass"],
                input_text="x" * (5 * 1024 * 1024),
            )

    def test_nonzero_parent_cannot_leave_an_orphaned_process_group(self) -> None:
        verifier = _load_verifier()
        with tempfile.TemporaryDirectory() as temporary:
            child_pid_path = Path(temporary) / "child.pid"
            shim = Path(temporary) / "orphan-on-failure"
            shim.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess, sys\n"
                "child = subprocess.Popen([\n"
                "    '/usr/bin/sleep', '30'\n"
                "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "raise SystemExit(7)\n"
            )
            shim.chmod(0o700)
            try:
                with self.assertRaisesRegex(RuntimeError, "command failed"):
                    verifier.command([str(shim), str(child_pid_path)], timeout=4)
                child_pid = int(child_pid_path.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if child_pid_path.exists():
                    child_pid = int(child_pid_path.read_text())
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class CurrentGenerationVerifierTests(unittest.TestCase):
    def assert_fixture_rejected(self, fixture: VerifierFixture) -> None:
        fixture.save()
        result = _run_fixture(fixture)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_valid_fixture_passes_in_normal_and_optimized_python(self) -> None:
        for optimized in (False, True):
            with self.subTest(optimized=optimized), VerifierFixture() as fixture:
                result = _run_fixture(fixture, optimized=optimized)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                receipt = json.loads(
                    (fixture.evidence / "verification.receipt.json").read_text()
                )
                self.assertEqual(receipt["verification"], "passed")
                serialized = json.dumps(receipt, sort_keys=True)
                self.assertNotIn(CURRENT_INVOCATION, serialized)
                self.assertNotIn(CONTAINER_ID, serialized)
                self.assertNotIn('"202"', serialized)
                self.assertNotIn('"303"', serialized)

    def test_rejects_malformed_or_replaced_systemd_generation(self) -> None:
        with VerifierFixture() as fixture:
            fixture.state["systemd_outputs"][0] += "MainPID=202\n"
            self.assert_fixture_rejected(fixture)
        with VerifierFixture() as fixture:
            fixture.state["systemd_outputs"][1] = fixture.state[
                "systemd_outputs"
            ][1].replace(CURRENT_INVOCATION, "3" * 32)
            self.assert_fixture_rejected(fixture)
        with VerifierFixture() as fixture:
            fixture.state["systemd_outputs"][0] = fixture.state[
                "systemd_outputs"
            ][0].replace("DropInPaths=", "DropInPaths=/tmp/hostile.conf")
            self.assert_fixture_rejected(fixture)

    def test_rejects_hostile_effective_command_and_fragment(self) -> None:
        with VerifierFixture() as fixture:
            fixture.state["systemd_outputs"][0] = fixture.state[
                "systemd_outputs"
            ][0].replace(str(fixture.fragment), "/tmp/hostile.service")
            self.assert_fixture_rejected(fixture)
        with VerifierFixture() as fixture:
            fixture.state["systemd_outputs"][0] = fixture.state[
                "systemd_outputs"
            ][0].replace(" --enforce-eager ;", " --enforce-eager -m 24g ;")
            self.assert_fixture_rejected(fixture)

    def test_rejects_replaced_or_unpopulated_container_cgroup(self) -> None:
        with VerifierFixture() as fixture:
            replaced = json.loads(json.dumps(fixture.state["docker_outputs"][1]))
            replaced[0]["Id"] = "b" * 64
            fixture.state["docker_outputs"][1] = replaced
            self.assert_fixture_rejected(fixture)
        with VerifierFixture() as fixture:
            cgroup_events = (
                fixture.cgroup
                / "app.slice"
                / f"docker-{CONTAINER_ID}.scope"
                / "cgroup.events"
            )
            cgroup_events.write_text("populated 0\nfrozen 0\n")
            self.assert_fixture_rejected(fixture)

    def test_rejects_ambiguous_engine_capacity_metrics(self) -> None:
        with VerifierFixture() as fixture:
            fixture.state["journal"].append(dict(fixture.state["journal"][0]))
            self.assert_fixture_rejected(fixture)
        with VerifierFixture() as fixture:
            fixture.state["journal"][0]["MESSAGE"] = (
                "(EngineCore_DP1 pid=405) GPU KV cache size: 34,124 tokens"
            )
            self.assert_fixture_rejected(fixture)

    def test_rejects_alias_model_and_quality_ambiguity(self) -> None:
        with VerifierFixture() as fixture:
            fixture.state["models"]["data"].append({"id": "hostile-alias"})
            self.assert_fixture_rejected(fixture)
        with VerifierFixture() as fixture:
            fixture.state["embeddings"][MODELS[0]]["model"] = MODELS[1]
            self.assert_fixture_rejected(fixture)
        with VerifierFixture() as fixture:
            fixture.state["embeddings"][MODELS[0]]["data"][0]["embedding"] = [
                0.0
            ] * 4096
            self.assert_fixture_rejected(fixture)
        with VerifierFixture() as fixture:
            altered: dict[str, Any] = embedding_payload(MODELS[0])
            altered["data"][0]["embedding"] = altered["data"][2]["embedding"]
            fixture.state["embeddings"][MODELS[0]] = altered
            self.assert_fixture_rejected(fixture)

    def test_rejects_nofollow_evidence_target(self) -> None:
        with VerifierFixture() as fixture:
            target = fixture.root / "outside"
            target.write_text("unchanged\n")
            receipt = fixture.evidence / "verification.receipt.json"
            receipt.symlink_to(target)
            result = _run_fixture(fixture)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.read_text(), "unchanged\n")


if __name__ == "__main__":
    unittest.main()
