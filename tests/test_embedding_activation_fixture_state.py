"""A killed fake command must not corrupt the rollback's shared input state."""

import json
import os
from pathlib import Path
import runpy
import select
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from embedding_activation_fixtures import ActivationFixture, UNIT


def _kill_and_reap(pid: int, timeout: float = 2) -> tuple[int, int]:
    """Settle only our unreaped direct child, with a bounded nonblocking wait."""
    waited, status = os.waitpid(pid, os.WNOHANG)
    if waited == pid:
        return waited, status
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + timeout
    while True:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return waited, status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"writer child {pid} did not settle after SIGKILL")
        time.sleep(0.01)


class ActivationFixtureStateTests(unittest.TestCase):
    def _interrupt_writer(self, fixture: ActivationFixture, writer: str) -> tuple[int, int]:
        reader, notifier = os.pipe()
        pid = None
        settled = None
        primary = None
        cleanup_errors = []
        try:
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(reader)
                    original_open = Path.open

                    def paused_open(path, mode="r", *args, **kwargs):
                        stream = original_open(path, mode, *args, **kwargs)
                        if "w" in mode and path.parent == fixture.state_path.parent and (
                            path == fixture.state_path or path.suffix == ".tmp"
                        ):
                            os.write(notifier, b"opened")
                            signal.pause()
                        return stream

                    Path.open = paused_open
                    os.environ.update(fixture.env())
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
            notifier = None
            self.assertTrue(select.select([reader], [], [], 5)[0], "writer never opened its output")
            self.assertEqual(os.read(reader, 6), b"opened", "writer exited before opening output")
        except BaseException as error:
            primary = error
        finally:
            if pid is not None and pid > 0:
                try:
                    settled = _kill_and_reap(pid)
                except BaseException as error:
                    cleanup_errors.append(error)
            for descriptor in (reader, notifier):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError as error:
                        cleanup_errors.append(error)
        if primary is not None:
            if cleanup_errors:
                raise primary from BaseExceptionGroup("writer cleanup failed", cleanup_errors)
            raise primary
        if cleanup_errors:
            raise BaseExceptionGroup("writer cleanup failed", cleanup_errors)
        self.assertIsNotNone(settled)
        waited, status = settled
        self.assertEqual(waited, pid)
        self.assertTrue(os.WIFSIGNALED(status), f"writer exited without SIGKILL: {status}")
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        return waited, status

    def test_interrupted_state_publication_preserves_prior_json_for_every_writer(self):
        for writer in ("tool", "core-success", "core-failure", "fixture"):
            with self.subTest(writer=writer), ActivationFixture() as fixture:
                if writer == "core-failure":
                    fixture.state["swap_zero"] = []
                fixture.save()
                before = fixture.state_path.read_bytes()
                descriptors = set(os.listdir("/proc/self/fd"))
                pid, status = self._interrupt_writer(fixture, writer)
                after = fixture.state_path.read_bytes()
                print(f"writer={writer} handshake=opened pid={pid} wait_status={status} "
                      f"before_bytes={len(before)} after_bytes={len(after)}", flush=True)
                self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors)
                self.assertEqual(after, before, "interrupted fake writer truncated the rollback state")
                self.assertEqual(json.loads(after), fixture.state)

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

    def test_reaper_preserves_early_child_exit_status_without_signalling(self):
        with patch("os.waitpid", return_value=(12345, 7 << 8)) as wait, patch("os.kill") as kill:
            self.assertEqual(_kill_and_reap(12345), (12345, 7 << 8))
        wait.assert_called_once_with(12345, os.WNOHANG)
        kill.assert_not_called()

    def test_reaper_expiry_is_bounded_without_a_real_survivor(self):
        with patch("os.waitpid", return_value=(0, 0)) as wait, patch("os.kill") as kill, \
                patch("time.monotonic", side_effect=[0, 2]), patch("time.sleep") as sleep:
            with self.assertRaisesRegex(TimeoutError, "did not settle"):
                _kill_and_reap(12345)
        self.assertTrue(all(call.args == (12345, os.WNOHANG) for call in wait.call_args_list))
        kill.assert_called_once_with(12345, signal.SIGKILL)
        sleep.assert_not_called()

    def test_fork_failure_closes_both_pipe_ends(self):
        with ActivationFixture() as fixture:
            descriptors = set(os.listdir("/proc/self/fd"))
            with patch("os.fork", side_effect=OSError("fork setup failed")):
                with self.assertRaisesRegex(OSError, "fork setup failed"):
                    self._interrupt_writer(fixture, "fixture")
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors)

    def test_cleanup_failure_preserves_setup_error_and_closes_pipe(self):
        with ActivationFixture() as fixture:
            descriptors = set(os.listdir("/proc/self/fd"))
            with patch("os.fork", return_value=12345), \
                    patch("select.select", return_value=([], [], [])), \
                    patch(__name__ + "._kill_and_reap", side_effect=OSError("cleanup failed")):
                with self.assertRaisesRegex(AssertionError, "writer never opened") as caught:
                    self._interrupt_writer(fixture, "fixture")
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors)
            self.assertIsInstance(caught.exception.__cause__, ExceptionGroup)
            self.assertRegex(str(caught.exception.__cause__.exceptions[0]), "cleanup failed")
