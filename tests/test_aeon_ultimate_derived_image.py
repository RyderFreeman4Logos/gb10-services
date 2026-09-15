from __future__ import annotations

import hashlib
import py_compile
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE_DIR = ROOT / "profile/aeon-ultimate-uncensored-nvfp4/image"
DOCKERFILE = IMAGE_DIR / "Dockerfile.aeon-v029-modelopt-54367"
PATCH = IMAGE_DIR / "modelopt-54367-v029-adapted.patch"
CPU_REGRESSION = IMAGE_DIR / "cpu_dispatcher_regression.py"
BASE = "sha256:2421bb1228a85370c1c50adb31f605c4361acf4d48d65282fcb919e74f34fae7"
EXPECTED_IMAGE = "sha256:26c62d60a7cce96b279d768eaafc20189f7a38e125f9183e11ba6d5f9d4a53e0"
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

    def test_canonical_offline_build_compares_iidfile_before_unit_start(self) -> None:
        helper = ROOT / "scripts" / "gb10_prepare_aeon_ultimate_image.py"
        guide = (ROOT / "docs" / "deployment" / "AGENTS.md").read_text()
        self.assertTrue(helper.is_file(), "missing tracked Ultimate image admission helper")
        source = helper.read_text()
        self.assertIn("--network=none", source)
        self.assertIn("--pull=false", source)
        self.assertIn("--iidfile", source)
        self.assertIn(str(IMAGE_DIR.relative_to(ROOT)), source)
        self.assertIn(DOCKERFILE.name, source)
        self.assertIn("26c62d60a7cce96b279d768eaafc20189f7a38e125f9183e11ba6d5f9d4a53e0", source)
        self.assertIn("not assumed deterministic", source)
        prepare_at = guide.find("gb10_prepare_aeon_ultimate_image.py")
        install_at = guide.find("### 5. Systemd User Services Installation")
        enable_at = guide.find("### 6. Enable and Start Services")
        self.assertGreater(prepare_at, 0)
        self.assertGreater(install_at, prepare_at)
        self.assertGreater(enable_at, install_at)
        self.assertIn("--network=none", guide)
        self.assertIn("--pull=false", guide)
        self.assertIn("--iidfile", guide)

        helper_cmd = ["python3", str(helper), "--root", str(ROOT)]
        with tempfile.TemporaryDirectory() as raw_tmp:
            iidfile = Path(raw_tmp) / "aeon-ultimate.iid"
            iidfile.write_text(EXPECTED_IMAGE + "\n")
            matched = subprocess.run(
                [*helper_cmd, "--iidfile", str(iidfile), "--compare-only"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(matched.returncode, 0, matched.stdout + matched.stderr)
            self.assertEqual(matched.stdout.strip(), EXPECTED_IMAGE)
            iidfile.write_text("sha256:" + "a" * 64 + "\n")
            mismatched = subprocess.run(
                [*helper_cmd, "--iidfile", str(iidfile), "--compare-only"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(mismatched.returncode, 0, mismatched.stdout)
            self.assertIn("does not match configured override", mismatched.stderr)
            self.assertIn("not assumed deterministic", mismatched.stderr)


if __name__ == "__main__":
    unittest.main()
