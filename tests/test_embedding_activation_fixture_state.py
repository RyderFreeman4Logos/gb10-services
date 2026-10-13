"""A killed fake command must not corrupt the rollback's shared input state."""
import json
import os
from pathlib import Path
import runpy
import select
import signal
import subprocess
import sys
import unittest

from embedding_activation_fixtures import ActivationFixture, UNIT


class ActivationFixtureStateTests(unittest.TestCase):
    def test_interrupted_state_publication_preserves_prior_json_for_every_writer(self):
        for writer in ("tool", "core-success", "core-failure", "fixture"):
            with self.subTest(writer=writer), ActivationFixture() as fixture:
                if writer == "core-failure":
                    fixture.state["swap_zero"] = []
                fixture.save()
                before = fixture.state_path.read_bytes()
                reader, notifier = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(reader)
                    original_open = Path.open
                    def paused_open(path, mode="r", *args, **kwargs):
                        stream = original_open(path, mode, *args, **kwargs)
                        if "w" in mode and path.parent == fixture.state_path.parent and (
                                path == fixture.state_path or path.suffix == ".tmp"):
                            os.write(notifier, b"opened")
                            signal.pause()
                        return stream
                    Path.open = paused_open
                    os.environ.update(fixture.env())
                    try:
                        if writer == "fixture":
                            fixture.state["generation"] += 1
                            fixture.save()
                        elif writer == "tool":
                            sys.argv = [str(fixture.tool), "stop", UNIT]
                            runpy.run_path(str(fixture.tool), run_name="__main__")
                        else:
                            sys.argv = [str(fixture.source_core), "--test-only", "--unit",
                                        str(fixture.source_unit), "--container", "vllm-embedding"]
                            runpy.run_path(str(fixture.source_core), run_name="__main__")
                    finally:
                        os._exit(3)
                os.close(notifier)
                try:
                    self.assertTrue(select.select([reader], [], [], 5)[0], "writer never opened its output")
                    self.assertEqual(os.read(reader, 6), b"opened")
                finally:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    os.close(reader)
                self.assertEqual(fixture.state_path.read_bytes(), before,
                                 "interrupted fake writer truncated the rollback state")
                self.assertEqual(json.loads(fixture.state_path.read_text()), fixture.state)

    def test_completed_tool_publication_is_readable_by_next_command(self):
        with ActivationFixture() as fixture:
            result = subprocess.run([str(fixture.tool), "stop", UNIT], env=fixture.env(),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(fixture.state_path.read_text())["active"])
            next_result = subprocess.run([str(fixture.tool), "info", "--format", "{{.CgroupVersion}}"],
                                         env=fixture.env(), capture_output=True, text=True, timeout=5)
            self.assertEqual(next_result.returncode, 0, next_result.stderr)
            self.assertEqual(next_result.stdout.strip(), "2")
