from __future__ import annotations

import hashlib
import py_compile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE_DIR = ROOT / "profile/aeon-ultimate-uncensored-nvfp4/image"
DOCKERFILE = IMAGE_DIR / "Dockerfile.aeon-v029-modelopt-54367"
PATCH = IMAGE_DIR / "modelopt-54367-v029-adapted.patch"
CPU_REGRESSION = IMAGE_DIR / "cpu_dispatcher_regression.py"
BASE = "sha256:2421bb1228a85370c1c50adb31f605c4361acf4d48d65282fcb919e74f34fae7"
PROVENANCE_SHA256 = {
    DOCKERFILE: "479c5518df6b6be8d24e8f6691f0cb94698c3a4e5e8d0ab618e82e585b998f9a",
    PATCH: "7d2ff70d56dc0910197749c8e42b5ed95fc598bffb1fad01f2fa94eeab1ab4cb",
    CPU_REGRESSION: "c6a55f872bf96560642974bed4f149199d109a0497bec634bb75c1edcf8ca62a",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AeonUltimateDerivedImageProvenanceTests(unittest.TestCase):
    def test_tracked_dockerfile_patch_and_cpu_regression_match_build_receipt(self) -> None:
        for path, digest in PROVENANCE_SHA256.items():
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(path.is_file(), f"missing {path}")
                self.assertEqual(_sha256(path), digest)

    def test_dockerfile_derives_from_central_2421_base_and_names_the_patch(self) -> None:
        text = DOCKERFILE.read_text()
        self.assertIn(f"FROM ghcr.io/aeon-7/aeon-vllm-ultimate@{BASE}", text)
        self.assertIn(PATCH.name, text)
        self.assertIn(PROVENANCE_SHA256[PATCH], text)
        self.assertIn("aeon.layer=\"modelopt-54367-v029-adapted\"", text)

    def test_cpu_dispatcher_regression_is_the_existing_runnable_script(self) -> None:
        py_compile.compile(str(CPU_REGRESSION), doraise=True)
        source = CPU_REGRESSION.read_text()
        self.assertIn("EXPECT_PCPT_DISPATCH", source)
        self.assertIn("ModelOptFp8PcPtLinearMethod", source)
        self.assertIn("in_proj_qkvz", source)


if __name__ == "__main__":
    unittest.main()
