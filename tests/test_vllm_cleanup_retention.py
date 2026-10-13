"""Controlled stop must retain the exited generation before Docker destroys it."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[1] / 'scripts/gb10_verify_vllm_no_swap_core.py'

class CleanupRetentionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('cleanup_retention_core', CORE)
        self.c = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.c
        with patch.object(sys, 'argv', [str(CORE), '/usr/bin/docker', '/usr/bin/systemctl', '/proc', '/sys/fs/cgroup', '90', '15', '0']):
            exec(compile(CORE.read_text().rsplit("\ntry:\n    main()", 1)[0], str(CORE), "exec"), self.c.__dict__)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cid = 'a' * 64
        self.file = Path(self.tmp.name) / 'model.cid'
        self.file.write_text(self.cid + '\n'); self.file.chmod(0o600)
        self.output = str(Path(self.tmp.name) / 'last.json')
        self.events = []; self.removed = False
        self.payload = {'Id': self.cid, 'Name': '/vllm-test', 'State': {'Running': True, 'Status': 'running'}}
        self.retain_failure = False
        self.replace = False

    def inspect(self, reference, **kwargs):
        return None if self.removed else self.payload

    def command(self, argv, **kwargs):
        if 'stop' in argv:
            self.events.append('stop'); self.payload['State'].update(Running=False, Status='exited')
            if self.replace:
                self.file.write_text('b' * 64 + '\n')
        elif 'rm' in argv:
            self.events.append('remove'); self.removed = True
        else:
            self.events.append('retain')
            self.assertTrue(self.file.exists()); self.assertFalse(self.removed)
            self.assertEqual(self.payload['State']['Status'], 'exited')
            self.assertIn(self.output, argv)
            self.assertEqual(argv[argv.index('--expected-cid') + 1], self.cid)
            if self.retain_failure:
                raise self.c.ContractError('bounded command timed out')
        return self.c.CommandResult(0, b'', b'')

    def cleanup(self):
        with patch.object(self.c, 'inspect_payload', self.inspect), patch.object(self.c, 'run_command', self.command):
            self.c.cleanup(['vllm-test'], str(self.file), self.output)

    def test_stop_retain_remove_order(self):
        self.cleanup()
        self.assertEqual(self.events, ['stop', 'retain', 'remove']); self.assertFalse(self.file.exists())

    def test_natural_exit_uses_same_retention_boundary(self):
        self.payload['State'].update(Running=False, Status='exited')
        self.cleanup(); self.assertEqual(self.events, ['stop', 'retain', 'remove'])

    def test_retainer_timeout_does_not_prevent_cleanup(self):
        self.retain_failure = True
        self.cleanup(); self.assertEqual(self.events, ['stop', 'retain', 'remove'])

    def test_changed_cid_authority_stops_before_retention_or_remove(self):
        self.replace = True
        with self.assertRaises(self.c.ContractError): self.cleanup()
        self.assertEqual(self.events, ['stop']); self.assertTrue(self.file.exists())

    def test_expired_deadline_never_launches_child(self):
        with patch.object(self.c.time, 'monotonic', return_value=50), patch.object(self.c.subprocess, 'Popen') as spawn:
            with self.assertRaises(self.c.ContractError):
                self.c.run_command(['/usr/bin/true'], deadline=50)
            spawn.assert_not_called()

    def test_shared_deadline_caps_each_command_with_reap_reserve(self):
        with patch.object(self.c.time, 'monotonic', return_value=40), patch.object(self.c.subprocess, 'Popen') as spawn:
            spawn.return_value.communicate.return_value = (b'', b'')
            spawn.return_value.returncode = 0
            self.c.run_command(['/usr/bin/true'], timeout=35, deadline=50)
            spawn.return_value.communicate.assert_called_once_with(timeout=6)

    def test_cli_retention_is_cleanup_only(self):
        with self.assertRaises(self.c.ContractError):
            self.c.parse_cli(['--unit', '/a.service', '--retain-inspect-output', self.output])

    def test_stop_failure_preserves_cid_and_never_retains_or_removes(self):
        with patch.object(self.c, 'inspect_payload', self.inspect), patch.object(self.c, 'run_command', return_value=self.c.CommandResult(1, b'', b'')):
            with self.assertRaises(self.c.ContractError):
                self.c.cleanup(['vllm-test'], str(self.file), self.output)
        self.assertTrue(self.file.exists())

    def test_all_cleanup_commands_share_one_deadline(self):
        deadlines = []
        def command(argv, **kwargs):
            deadlines.append(kwargs['deadline'])
            return self.command(argv, **kwargs)
        def inspect(reference, **kwargs):
            deadlines.append(kwargs['deadline'])
            return self.inspect(reference, **kwargs)
        with patch.object(self.c, 'inspect_payload', inspect), patch.object(self.c, 'run_command', command):
            self.c.cleanup(['vllm-test'], str(self.file), self.output)
        self.assertGreater(len(deadlines), 4)
        self.assertEqual(len(set(deadlines)), 1)

    def test_cumulative_budget_expiry_prevents_next_command(self):
        with patch.object(self.c.time, 'monotonic', side_effect=[40, 47]), patch.object(self.c.subprocess, 'Popen') as spawn:
            spawn.return_value.communicate.return_value = (b'', b'')
            spawn.return_value.returncode = 0
            self.c.run_command(['/usr/bin/true'], deadline=50)
            with self.assertRaises(self.c.ContractError):
                self.c.run_command(['/usr/bin/true'], deadline=50)
            self.assertEqual(spawn.call_count, 1)

    def test_real_retainer_archives_before_remove(self):
        import json
        import os
        import subprocess
        from test_gb10_retain_container_inspect import _exited_payload, _fake_docker
        root = Path(self.tmp.name)
        bin_dir = root / 'bin'; bin_dir.mkdir()
        payload = json.loads(_exited_payload())
        payload[0]['Id'] = self.cid; payload[0]['Name'] = '/vllm-test'
        _fake_docker(bin_dir, json.dumps(payload), 0)
        env = dict(os.environ, HOME=str(root), PATH=str(bin_dir) + ':/usr/bin:/bin')
        def command(argv, **kwargs):
            if 'gb10_retain_container_inspect.sh' in ' '.join(argv):
                self.events.append('retain')
                actual = list(argv)
                actual[3] = str(CORE.parent / 'gb10_retain_container_inspect.sh')
                result = subprocess.run(actual, env=env, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b'retained cid=', result.stdout)
                return self.c.CommandResult(result.returncode, result.stdout, result.stderr)
            if 'rm' in argv:
                receipts = list((root / '.local/state/gb10-vllm-cids').glob('*.exited.json'))
                self.assertEqual(len(receipts), 1)
                receipt = json.loads(receipts[0].read_text())
                self.assertEqual(receipt['container_id'], self.cid)
                self.assertEqual(receipt['state']['Status'], 'exited')
            return self.command(argv, **kwargs)
        with patch.object(self.c, 'inspect_payload', self.inspect), patch.object(self.c, 'run_command', command):
            self.c.cleanup(['vllm-test'], str(self.file), self.output)
        self.assertEqual(self.events, ['stop', 'retain', 'remove'])
