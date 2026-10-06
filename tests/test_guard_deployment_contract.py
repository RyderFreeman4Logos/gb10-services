"""Offline Guard rollback admission/readiness regressions; no live commands."""
import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import gb10_guard_deployment as contract
import gb10_bounded_process as bounded

OLD = "a" * 64
NEW = "b" * 64
BEFORE = {"ActiveState": "active", "SubState": "running", "MainPID": "12",
          "InvocationID": "a" * 32, "ExecMainStartTimestampMonotonic": "100"}
AFTER = dict(BEFORE, MainPID="13", InvocationID="b" * 32,
             ExecMainStartTimestampMonotonic="200")


class GuardDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(contract, "missing executable Guard deployment contract")
        self.evidence = {
            "old": {"sha256": OLD, "supported_schemas": [4], "source_commit": "a" * 40,
                    "evidence_sha256": "c" * 64},
            "candidate": {"sha256": NEW, "supported_schemas": [4, 5], "migration_target": 5,
                          "source_commit": "b" * 40, "evidence_sha256": "d" * 64}}

    def admitted_operations(self, evidence, current=4):
        operations = []
        # The documented cutover must gate its FIRST write on this check.
        try:
            contract.admit(evidence, OLD, NEW, current)
        except contract.Hold:
            return "HOLD", operations
        operations.extend(["stop", "install", "start", "database migration"])
        return "ADMIT", operations

    def test_schema_4_to_5_holds_before_all_writes(self):
        self.assertEqual(self.admitted_operations(self.evidence), ("HOLD", []))

    def test_unknown_bindings_hold_before_all_writes(self):
        for role, key in [("old", "sha256"), ("candidate", "source_commit"),
                          ("old", "supported_schemas"), ("candidate", "evidence_sha256")]:
            evidence = copy.deepcopy(self.evidence)
            evidence[role].pop(key)
            with self.subTest(role=role, key=key):
                self.assertEqual(self.admitted_operations(evidence), ("HOLD", []))
        self.evidence["candidate"]["sha256"] = OLD
        self.assertEqual(self.admitted_operations(self.evidence), ("HOLD", []))

    def test_explicit_compatible_schemas_admit(self):
        self.evidence["old"]["supported_schemas"] = [4, 5]
        self.assertEqual(self.admitted_operations(self.evidence)[0], "ADMIT")
        self.assertEqual(self.admitted_operations(self.evidence, None), ("HOLD", []))
        self.assertEqual(self.admitted_operations(self.evidence, True), ("HOLD", []))
        self.evidence["candidate"]["supported_schemas"] = [5]
        self.assertEqual(self.admitted_operations(self.evidence), ("HOLD", []))

    def wait(self, probes, generations=None):
        now = [0.0]
        timeouts = []
        operations = []
        def sleep(seconds):
            now[0] += seconds
        def health(deadline):
            cap = min(5.0, deadline - now[0])
            timeouts.append(cap)
            probe = probes.pop(0) if len(probes) > 1 else probes[0]
            if isinstance(probe, Exception):
                raise probe
            return probe
        rows = generations or [AFTER]
        def generation(deadline):
            return rows.pop(0) if len(rows) > 1 else rows[0]
        with patch.object(contract.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(contract.time, "sleep", side_effect=sleep), \
             patch.object(contract, "query_generation", side_effect=generation), \
             patch.object(contract, "health_status", side_effect=health), \
             patch.object(contract, "held_digest", return_value=NEW):
            try:
                result = contract.wait_ready(BEFORE, NEW)
            except contract.Hold:
                result = "HOLD"
        return result, now[0], timeouts, operations

    def test_refused_then_healthy_never_restores_old(self):
        result, elapsed, timeouts, operations = self.wait([ConnectionRefusedError(), 200])
        self.assertEqual(result, AFTER)
        self.assertGreater(elapsed, 0)
        self.assertLess(elapsed, 60)
        self.assertTrue(all(0 < value <= 5 for value in timeouts))
        self.assertEqual(operations, [])

    def test_process_tree_containment_error_is_terminal(self):
        failure = contract.ProcessTreeContainmentError("controlled containment failure")
        result, _, timeouts, _ = self.wait([failure, 200])
        self.assertEqual(result, "HOLD")
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(self.wait([RuntimeError("ordinary command failure"), 200])[0], AFTER)

    def test_real_command_containment_remains_terminal_through_cleanup(self):
        # Actual child cleanup precedes the synthetic census: no live survivor claim.
        real_reap, real_capture = bounded._bounded_reap, bounded._Capture
        real_close = bounded._ProcessTree.close
        for fault in (None, "reap", "finalize"):
            with self.subTest(fault=fault):
                primaries, observed, children, cleanups, queries = [], [], [], [], []
                secondary = OSError("controlled secondary cleanup fault")

                def reap(process, tree, selector, streams, captures, deadline):
                    real_reap(process, tree, selector, streams, captures, deadline)
                    children.append(process)
                    cleanups.append("reap")
                    if len(cleanups) == 1:
                        with patch.object(tree, "survivors", return_value=[999999999]):
                            try:
                                real_reap(process, tree, selector, streams, captures,
                                          bounded.time.monotonic() - 1)
                            except bounded.ProcessTreeContainmentError as error:
                                primaries.append(error)
                                raise
                    elif fault == "reap":
                        raise secondary

                def close(tree):
                    real_close(tree)
                    if fault == "finalize":
                        raise secondary

                def query(deadline):
                    queries.append("query")
                    if len(queries) == 1:
                        try:
                            with patch.object(bounded, "_bounded_reap", side_effect=reap), \
                                 patch.object(bounded, "_Capture", side_effect=lambda data: real_capture(data, limit=0)), \
                                 patch.object(bounded._ProcessTree, "close", close):
                                bounded.command([sys.executable, "-B", "-c", 'print("offline")'], timeout=2)
                        except BaseException as error:
                            observed.append(error)
                            raise
                    return AFTER

                with patch.object(contract, "query_generation", side_effect=query), \
                     patch.object(contract, "health_status", return_value=200) as health, \
                     patch.object(contract, "held_digest", return_value=NEW), \
                     patch.object(contract.time, "sleep") as sleep:
                    try:
                        contract.wait_ready(BEFORE, NEW)
                        result = "READY"
                    except contract.Hold:
                        result = "HOLD"
                self.assertEqual(result, "HOLD")
                self.assertEqual(queries, ["query"])
                health.assert_not_called()
                sleep.assert_not_called()
                self.assertEqual(len(cleanups), 2)
                self.assertIs(observed[0], primaries[0])
                self.assertIsInstance(observed[0], bounded.ProcessTreeContainmentError)
                if fault is not None:
                    self.assertIs(observed[0].__cause__, secondary)
                    self.assertIn("controlled secondary cleanup fault", str(observed[0].__cause__))
                for child in children:
                    self.assertIsNotNone(child.returncode)
                    self.assertFalse(Path(f"/proc/{child.pid}").exists())
                    self.assertTrue(child.stdout.closed and child.stderr.closed)

    def test_ordinary_runtime_and_refusal_still_retry(self):
        for error in (RuntimeError("ordinary command failure"), ConnectionRefusedError()):
            with self.subTest(error=error):
                self.assertEqual(self.wait([error, 200])[0], AFTER)

    def test_expiry_and_permanent_schema_failure_are_bounded(self):
        result, elapsed, _, operations = self.wait([ConnectionRefusedError()])
        self.assertEqual(result, "HOLD")
        self.assertEqual(elapsed, 60)
        self.assertEqual(operations, [])
        failed = dict(AFTER, ActiveState="failed", SubState="failed", MainPID="0")
        result, elapsed, _, operations = self.wait([200], [failed])
        self.assertEqual(result, "HOLD")
        self.assertLess(elapsed, 60)
        self.assertEqual(operations, [])

    def test_http_200_requires_fresh_unchanged_generation_and_digest(self):
        for row in [BEFORE, dict(AFTER, InvocationID=""), dict(AFTER, ActiveState="inactive")]:
            with self.subTest(row=row):
                self.assertEqual(self.wait([200], [row])[0], "HOLD")
        with patch.object(contract, "held_digest", return_value=OLD):
            # Keep this test's inner mock from replacing the mismatched digest.
            with patch.object(contract, "query_generation", return_value=AFTER), \
                 patch.object(contract, "health_status", return_value=200):
                with self.assertRaises(contract.Hold):
                    contract.wait_ready(BEFORE, NEW)

    def test_deadline_crossed_during_final_digest_cannot_publish_ready(self):
        now = [0.0]
        hashes = [0]
        def held(row):
            hashes[0] += 1
            if hashes[0] == 2:
                now[0] = 60.01
            return NEW
        with patch.object(contract.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(contract, "query_generation", return_value=AFTER), \
             patch.object(contract, "health_status", return_value=200), \
             patch.object(contract, "held_digest", side_effect=held):
            with self.assertRaises(contract.Hold):
                contract.wait_ready(BEFORE, NEW)

    def run_ready_cli(self, generations, health, digests):
        with tempfile.TemporaryDirectory(dir=Path.home() / "tmp") as folder:
            before = Path(folder) / "before.json"
            before.write_text(json.dumps(BEFORE))
            argv = ["guard-check", "ready", "--before", str(before), "--expected-sha256", NEW]
            calls = []
            generation_rows, health_rows, hashes = iter(generations), iter(health), iter(digests)
            def command(args, **kwargs):
                if args[0] == "/usr/bin/systemctl":
                    calls.append("query")
                    row = next(generation_rows)
                    return "\n".join(f"{field}={row[field]}" for field in contract.FIELDS)
                calls.append("health")
                result = next(health_rows)
                if isinstance(result, Exception):
                    raise result
                return str(result)
            output = io.StringIO()
            with patch.object(sys, "argv", argv), patch.object(contract, "command", side_effect=command), \
                 patch.object(contract, "digest", side_effect=lambda _: next(hashes)), \
                 patch.object(contract.time, "monotonic", return_value=0), \
                 patch.object(contract.time, "sleep"), contextlib.redirect_stdout(output):
                rc = contract.main()
        return rc, json.loads(output.getvalue()), calls

    def test_cli_readiness_holds_terminal_final_observations_without_retry(self):
        failed = dict(AFTER, ActiveState="failed", SubState="failed", MainPID="0")
        cases = [([AFTER, failed, AFTER, AFTER], [200, 200], [NEW, NEW, NEW]),
                 ([AFTER, AFTER, AFTER, AFTER], [200, 200], [NEW, OLD, NEW, NEW])]
        for generations, health, digests in cases:
            with self.subTest(generations=generations[1], digests=digests):
                rc, receipt, calls = self.run_ready_cli(generations, health, digests)
                self.assertEqual((rc, receipt["status"]), (1, "HOLD"))
                self.assertEqual(calls, ["query", "health", "query"])

    def test_cli_readiness_retries_refusal_and_generation_change(self):
        changed = dict(AFTER, MainPID="14", InvocationID="c" * 32,
                       ExecMainStartTimestampMonotonic="300")
        cases = [([AFTER, changed, changed, changed], [200, 200], [NEW] * 4),
                 ([AFTER, AFTER, AFTER], [ConnectionRefusedError(), 200], [NEW] * 3)]
        for generations, health, digests in cases:
            with self.subTest(generations=generations, health=health):
                rc, receipt, calls = self.run_ready_cli(generations, health, digests)
                self.assertEqual((rc, receipt["status"]), (0, "READY"))
                self.assertGreater(calls.count("query"), 2)

    def test_cli_rechecks_real_offline_binary_and_report_bytes(self):
        with tempfile.TemporaryDirectory(dir=Path.home() / "tmp") as folder:
            root = Path(folder)
            for name in ("old", "candidate", "report"):
                (root / name).write_text("offline fixture " + name)
            evidence = copy.deepcopy(self.evidence)
            for role in ("old", "candidate"):
                evidence[role].update(sha256=contract.digest(root / role),
                                      evidence_file="report",
                                      evidence_sha256=contract.digest(root / "report"))
            argv = ["guard-check", "admit", "--old-binary", str(root / "old"),
                    "--candidate-binary", str(root / "candidate"), "--current-schema", "4",
                    "--evidence", str(root / "evidence.json")]
            def run():
                (root / "evidence.json").write_text(json.dumps(evidence))
                output = io.StringIO()
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), \
                     patch.object(contract, "command") as commands:
                    rc = contract.main()
                self.assertEqual(commands.call_args_list, [])  # ZERO lifecycle/DB commands
                return rc, json.loads(output.getvalue())["status"]
            self.assertEqual(run(), (1, "HOLD"))  # real 4→5 admission call
            evidence["old"]["supported_schemas"] = [4, 5]
            self.assertEqual(run(), (0, "ADMIT"))
            parsed_manifest = root / "manifest.parsed"
            parsed_manifest.write_bytes((root / "evidence.json").read_bytes())
            parsed_sha256 = contract.digest(parsed_manifest)
            read_metadata = contract.read_metadata
            def rewrite_after_parse(path):
                snapshot = read_metadata(path)
                if path == root / "evidence.json":
                    path.write_bytes(path.read_bytes() + bytes([10]))
                return snapshot
            output = io.StringIO()
            with (
                patch.object(sys, "argv", argv),
                patch.object(contract, "read_metadata", side_effect=rewrite_after_parse),
                patch.object(contract, "command") as commands,
                contextlib.redirect_stdout(output),
            ):
                rc = contract.main()
            receipt = json.loads(output.getvalue())
            self.assertEqual(commands.call_args_list, [])
            self.assertEqual(rc, 0)
            self.assertEqual(receipt["evidence_sha256"], parsed_sha256)
            self.assertNotEqual(receipt["evidence_sha256"], contract.digest(root / "evidence.json"))
            for role in ("old", "candidate"):
                evidence[role]["evidence_file"] = str(root / "report")
            self.assertEqual(run(), (1, "HOLD"))
            for role in ("old", "candidate"):
                evidence[role]["evidence_file"] = "report"
            (root / "candidate").write_text("changed offline candidate")
            self.assertEqual(run(), (1, "HOLD"))
            evidence["candidate"]["sha256"] = contract.digest(root / "candidate")
            (root / "report").write_text("changed supporting report")
            self.assertEqual(run(), (1, "HOLD"))
            evidence["old"].pop("supported_schemas")
            self.assertEqual(run(), (1, "HOLD"))

    def test_real_cli_metadata_refuses_fifo_and_invalid_regular_inputs(self):
        with tempfile.TemporaryDirectory(dir=Path.home() / "tmp") as folder:
            root = Path(folder)
            fifo = root / "metadata.fifo"
            os.mkfifo(fifo, 0o600)
            malformed = root / "malformed.json"
            malformed.write_text("{")
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"old": {}, "old": {}}')
            unknown = root / "unknown.json"
            unknown.write_text("{}")
            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (contract.MAX_METADATA_BYTES + 1))
            for action in ("admit", "ready"):
                for metadata in (fifo, malformed, duplicate, unknown, oversized, root):
                    with self.subTest(action=action, metadata=metadata.name):
                        argv = [sys.executable, "-B", str(SCRIPTS / "gb10_guard_deployment.py"), action]
                        if action == "admit":
                            argv += ["--old-binary", str(malformed), "--candidate-binary", str(malformed),
                                     "--evidence", str(metadata), "--current-schema", "4"]
                        else:
                            # Only the unknown regular object uses an invalid digest.
                            argv += ["--before", str(metadata), "--expected-sha256",
                                     "unknown" if metadata == unknown else NEW]
                        try:
                            result = subprocess.run(argv, capture_output=True, text=True, timeout=2)
                        except subprocess.TimeoutExpired:
                            self.fail("real CLI metadata open blocked before HOLD")
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(json.loads(result.stdout)["status"], "HOLD")

    def test_metadata_reader_rejects_bom_encoded_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path.home() / "tmp") as folder:
            metadata = Path(folder) / "metadata.json"
            metadata.write_bytes(bytes.fromhex("efbbbf7b7d"))
            with self.assertRaises(json.JSONDecodeError):
                contract.read_metadata(metadata)

    def test_metadata_reader_checks_held_descriptor_and_preserves_directory_symlink(self):
        with tempfile.TemporaryDirectory(dir=Path.home() / "tmp") as folder:
            root = Path(folder)
            metadata = root / "metadata.json"
            metadata.write_text('{"offline": true}')
            alias = root / "alias"
            alias.symlink_to(root, target_is_directory=True)
            self.assertEqual(contract.read_metadata(alias / metadata.name)[0], {"offline": True})
            self.assertTrue(alias.is_symlink())
            fifo = root / "metadata.fifo"
            os.mkfifo(fifo, 0o600)
            fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            with patch.object(contract.os, "open", return_value=fd), \
                 patch.object(contract.json, "loads", side_effect=AssertionError("read before fstat")):
                with self.assertRaises(contract.Hold):
                    contract.read_metadata(metadata)  # Path is regular; opened descriptor is not.
            with self.assertRaises(OSError):
                os.fstat(fd)  # Refusal closes the owned descriptor.

    def test_readme_first_install_is_guarded_and_matches_unit_entrypoint(self):
        readme = (SCRIPTS.parent / "README.md").read_text()
        first_install = readme.split("#### Guard binary/schema admission", 1)[0]
        blocks = re.findall(r"```bash\n(.*?)```", first_install, re.S)
        blocks = [block for block in blocks if "install -Dm755" in block and "llm-guard-proxy" in block]
        self.assertEqual(len(blocks), 1, "missing guarded FIRST INSTALL binary publication")
        unit = (SCRIPTS.parent / "profile/llm-guard-proxy/llm-guard-proxy.service").read_text()
        self.assertIn("ExecStart=/home/obj/.local/bin/llm-guard-proxy", unit)
        with tempfile.TemporaryDirectory(dir=Path.home() / "tmp") as folder:
            root = Path(folder)
            candidate = root / "candidate"
            candidate.write_bytes(b"offline candidate")
            tools = root / "tools"
            tools.mkdir()
            mise = tools / "mise"
            mise.write_text('#!/bin/sh\nif [ "$1" = which ]; then printf "%s\\n" "$CANDIDATE"; fi\n')
            mise.chmod(0o755)
            env = dict(os.environ, HOME=str(root), CANDIDATE=str(candidate),
                       PATH=str(tools) + os.pathsep + os.environ["PATH"])
            destination = root / ".local/bin/llm-guard-proxy"
            result = subprocess.run(["bash", "-eu", "-c", blocks[0]], env=env,
                                    capture_output=True, text=True, timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(destination.read_bytes(), candidate.read_bytes())
            destination.write_bytes(b"retained existing installation")
            result = subprocess.run(["bash", "-eu", "-c", blocks[0]], env=env,
                                    capture_output=True, text=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(destination.read_bytes(), b"retained existing installation")
            destination.unlink()
            destination.symlink_to(root / "missing")
            result = subprocess.run(["bash", "-eu", "-c", blocks[0]], env=env,
                                    capture_output=True, text=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(destination.is_symlink())
            self.assertFalse((root / "missing").exists())

    def test_nonregular_evidence_is_rejected_before_open(self):
        with patch.object(Path, "stat", return_value=type("Stat", (), {"st_mode": 0o010600})()), \
             patch.object(Path, "open", side_effect=AssertionError("must not open FIFO")):
            with self.assertRaises(contract.Hold):
                contract.digest("offline-fifo")

    def test_generation_query_is_bounded_and_malformed_output_holds(self):
        with patch.object(contract.time, "monotonic", return_value=59.75), \
             patch.object(contract, "command", return_value="MainPID=13\n") as run:
            with self.assertRaises(contract.Hold):
                contract.query_generation(60)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 0.25)
        self.assertEqual(run.call_args.kwargs["deadline"], 60)

    def test_health_command_is_capped_by_remaining_deadline(self):
        with patch.object(contract.time, "monotonic", return_value=59.75), \
             patch.object(contract, "command", return_value="200") as run:
            self.assertEqual(contract.health_status(60), 200)
        kwargs = run.call_args.kwargs
        self.assertLessEqual(kwargs["timeout"], 0.25)
        self.assertEqual(kwargs["deadline"], 60)
        args = run.call_args.args[0]
        self.assertLessEqual(float(args[args.index("--max-time") + 1]), 0.25)


if __name__ == "__main__":
    unittest.main()
