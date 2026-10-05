"""Contract checks for the isolated native-dimension canary (no model download)."""
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class Native1024CanaryTests(unittest.TestCase):
    def test_startup_consumes_the_same_lifetime_budget(self):
        import subprocess
        code = '''import sys,types,tempfile,pathlib,json,hashlib,time
import native1024_canary as c
with tempfile.TemporaryDirectory() as root:
 p=pathlib.Path(root)/c.REVISION;p.mkdir();(p/"config.json").write_text(json.dumps({"hidden_size":1024,"architectures":["XLMRobertaModel"]}));(p/"model.safetensors").write_bytes(b"")
 c.WEIGHTS_SHA256=hashlib.sha256(b"").hexdigest();c._available=lambda:12*1024**3
 def late(*args,**kwargs):raise AssertionError("startup escaped the one-second lifetime")
 sys.modules["torch"]=types.SimpleNamespace(set_num_threads=lambda n:time.sleep(3),float32=None)
 sys.modules["transformers"]=types.SimpleNamespace(AutoModel=types.SimpleNamespace(from_pretrained=late),AutoTokenizer=types.SimpleNamespace(from_pretrained=late))
 sys.argv=["canary","--model-path",str(p),"--seconds","1"]
 try:c.main()
 except TimeoutError:print("STARTUP_DEADLINE_ENFORCED")
 else:raise AssertionError("startup never expired")
'''
        result = subprocess.run([sys.executable, "-c", "import sys;sys.path.insert(0," + repr(str(Path(__file__).resolve().parents[1] / "scripts")) + ");" + code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STARTUP_DEADLINE_ENFORCED", result.stdout)

    def test_accepted_request_cannot_outlive_canary_deadline(self):
        import signal
        import socket
        import time
        canary = importlib.import_module("native1024_canary")
        self.assertTrue(hasattr(canary, "_BoundedHTTPServer"), "accepted request has no absolute lifetime bound")
        server = object.__new__(canary._BoundedHTTPServer)
        server.deadline = time.monotonic() + 0.05
        server.RequestHandlerClass = lambda request, address, owner: request.recv(1)
        previous = signal.signal(signal.SIGALRM, canary._request_timeout)
        try:
            # Keep the peer open so the incomplete header remains blocked.
            request, peer = socket.socketpair()
            with request, peer, self.assertRaises(TimeoutError):
                server.finish_request(request, ("local", 0))
            self.assertLess(time.monotonic(), server.deadline + 0.5)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_rootless_loopback_publish_is_explicit(self):
        import subprocess
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/native1024_canary.py"), "--help"], capture_output=True, text=True, check=True)
        self.assertIn("--container-publish-loopback", result.stdout)

    def test_request_contract(self):
        try:
            canary = importlib.import_module("native1024_canary")
        except ModuleNotFoundError:
            self.fail("native 1024 canary request contract is absent")
        valid = {"model": canary.MODEL, "input": "synthetic", "dimensions": 1024}
        self.assertEqual(canary.parse_request(valid), ["synthetic"])
        self.assertEqual(canary.parse_request({"model": canary.MODEL, "input": ["a", "b"]}), ["a", "b"])
        for changed in (
            {"dimensions": 4096}, {"dimensions": True}, {"dimensions": 1024.0},
            {"model": "qwen3-embedding-8b"}, {"input": []}, {"input": ["a"] * 3},
            {"input": [1]}, {"input": ""}, {"encoding_format": "base64"},
            {"unknown": "ignored?"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                canary.parse_request({**valid, **changed})

    def test_vector_contract(self):
        try:
            canary = importlib.import_module("native1024_canary")
        except ModuleNotFoundError:
            self.fail("native 1024 canary output contract is absent")
        canary.validate_vectors([[1.0] + [0.0] * 1023], 1)
        for vectors in ([], [[1.0] * 4096], [[0.0] * 1024], [[float("nan")] * 1024]):
            with self.subTest(length=len(vectors)), self.assertRaises(ValueError):
                canary.validate_vectors(vectors, 1)


if __name__ == "__main__":
    unittest.main()
