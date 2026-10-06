from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import errno
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


STORE = Path(__file__).resolve().parents[1] / "scripts/hooks/receipt-store.py"
SPEC = importlib.util.spec_from_file_location("receipt_store", STORE)
assert SPEC is not None and SPEC.loader is not None
store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(store)


class ReceiptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.directory = self.root / "store"
        self.candidate = self.root / "candidate"
        self.candidate.write_bytes(b"fixture receipt\n")
        self.candidate.chmod(0o600)
        self.digest = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        self.leaf = self.directory / (self.digest + ".receipt")

    def command(self, command: str, digest: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(STORE), command, str(self.directory),
             str(self.candidate), self.digest if digest is None else digest],
            capture_output=True, text=True, timeout=5, check=False,
        )

    def test_interrupted_actual_publication_remains_single_link_and_retryable(self) -> None:
        # Both barriers perform the real syscall before exiting without cleanup.
        # The link barrier witnesses the old defect; rename witnesses the repair.
        child = subprocess.run(
            [sys.executable, "-c", '''
import ctypes, importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("receipt_store_child", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
real_link = os.link
real_lib = ctypes.CDLL(None, use_errno=True)
def interrupted_link(*args, **kwargs):
    real_link(*args, **kwargs)
    os._exit(77)
class Library:
    def renameat2(self, *args):
        rename = real_lib.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(*args)
        if result == 0:
            os._exit(77)
        return result
library = Library()
# A callable attribute permits the production ABI declarations on the barrier.
def rename(*args):
    return Library.renameat2(library, *args)
library.renameat2 = rename
os.link = interrupted_link
ctypes.CDLL = lambda *args, **kwargs: library
m.run("publish", Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
''', str(STORE), str(self.directory), str(self.candidate), self.digest],
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertEqual(child.returncode, 77, child.stderr)
        self.assertEqual(self.leaf.read_bytes(), self.candidate.read_bytes())
        print(f"actual publication child_exit={child.returncode} nlink={self.leaf.stat().st_nlink}", flush=True)
        before = self.leaf.stat()
        results = [(command, self.command(command)) for command in ("verify", "publish")]
        for command, result in results:
            print(f"fresh {command} rc={result.returncode} {result.stderr.strip()}", flush=True)
        for command, result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.leaf.stat().st_nlink, 1)
        self.assertEqual(self.leaf.stat().st_ino, before.st_ino)
        self.assertEqual(list(self.directory.glob(".receipt-*")), [])

    def test_unsafe_existing_entries_are_refused_without_overwrite(self) -> None:
        for kind in ("foreign-hardlink", "symlink", "tamper", "mode"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as scratch:
                self.directory = Path(scratch) / "store"
                self.directory.mkdir(mode=0o700)
                self.leaf = self.directory / (self.digest + ".receipt")
                target = Path(scratch) / "target"
                target.write_bytes(b"untouched foreign bytes\n")
                target.chmod(0o600)
                if kind == "foreign-hardlink":
                    os.link(target, self.leaf)
                elif kind == "symlink":
                    self.leaf.symlink_to(target)
                else:
                    self.leaf.write_bytes(b"tampered\n" if kind == "tamper" else self.candidate.read_bytes())
                    self.leaf.chmod(0o644 if kind == "mode" else 0o600)
                before = self.leaf.lstat()
                original = self.leaf.read_bytes()
                for command in ("verify", "publish"):
                    result = self.command(command)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.leaf.lstat(), before)
                    self.assertEqual(self.leaf.read_bytes(), original)
                    self.assertEqual(target.read_bytes(), b"untouched foreign bytes\n")

    def test_wrong_digest_refused_before_publication(self) -> None:
        for command in ("verify", "publish"):
            result = self.command(command, "0" * 64)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("digest differs", result.stderr)
        self.assertFalse(self.directory.exists())

    def test_concurrent_publish_preserves_first_inode(self) -> None:
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.command("publish"), range(6)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        before = self.leaf.stat()
        self.assertEqual(before.st_nlink, 1)
        again = self.command("publish")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.leaf.stat(), before)
        self.assertEqual(self.leaf.read_bytes(), self.candidate.read_bytes())

    def test_unavailable_no_replace_fails_closed_without_link_fallback(self) -> None:
        for failure in ("missing-symbol", errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as scratch:
                directory = Path(scratch) / "store"
                library = object()
                if failure != "missing-symbol":
                    def failed_rename(*args):
                        assert isinstance(failure, int)
                        ctypes.set_errno(failure)
                        return -1
                    library = mock.Mock(renameat2=failed_rename)
                with mock.patch.object(ctypes, "CDLL", return_value=library), mock.patch.object(
                    os, "link", side_effect=AssertionError("unsafe hardlink fallback")
                ):
                    with self.assertRaises((store.StoreError, OSError)):
                        store.run("publish", directory, self.candidate, self.digest)
                self.assertEqual(sorted(path.name for path in directory.iterdir()), [".lock"])

    def test_foreign_owner_refused_before_publication(self) -> None:
        with mock.patch.object(store.os, "getuid", return_value=os.getuid() + 1):
            for command in ("verify", "publish"):
                with self.assertRaisesRegex(store.StoreError, "metadata is unsafe"):
                    store.run(command, self.directory, self.candidate, self.digest)
        self.assertFalse(self.directory.exists())

    def test_no_replace_race_preserves_uncooperative_existing_leaf(self) -> None:
        real_read = store.read_existing
        calls = 0
        def raced_read(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.leaf.write_bytes(b"foreign competing receipt\n")
                self.leaf.chmod(0o600)
                return None
            return real_read(*args)
        with mock.patch.object(store, "read_existing", side_effect=raced_read):
            with self.assertRaisesRegex(store.StoreError, "stale or tampered"):
                store.run("publish", self.directory, self.candidate, self.digest)
        self.assertEqual(self.leaf.read_bytes(), b"foreign competing receipt\n")
        self.assertEqual(self.leaf.stat().st_nlink, 1)
        self.assertEqual(list(self.directory.glob(".receipt-*")), [])


if __name__ == "__main__":
    unittest.main()
