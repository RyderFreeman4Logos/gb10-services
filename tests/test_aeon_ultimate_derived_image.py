from __future__ import annotations

import hashlib
import importlib.util
import os
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
IMAGE_DIR = ROOT / "profile/aeon-ultimate-uncensored-nvfp4/image"
DOCKERFILE = IMAGE_DIR / "Dockerfile.aeon-v029-modelopt-54367"
PATCH = IMAGE_DIR / "modelopt-54367-v029-adapted.patch"
DFLASH2_PATCH = IMAGE_DIR / "qwen3-dflash2-layer-type.patch"
CPU_REGRESSION = IMAGE_DIR / "cpu_dispatcher_regression.py"
BASE = "sha256:2421bb1228a85370c1c50adb31f605c4361acf4d48d65282fcb919e74f34fae7"
EXPECTED_IMAGE = "sha256:112e96dae5543672afcfe2511193db4e095c222b5bea0dab9e109b84e9626b11"
PROVENANCE_SHA256 = {
    DOCKERFILE: "6cbbd94d6cb7778da866cb22db411c489d5ee660ff6da4b204fa0d66325547c1",
    PATCH: "7d2ff70d56dc0910197749c8e42b5ed95fc598bffb1fad01f2fa94eeab1ab4cb",
    DFLASH2_PATCH: "8b2f477f1dc1edfdefb22daf1d1075bc282ab5bd39bb6c875685ec453f94370b",
    CPU_REGRESSION: "50361d638face3f29440556889851debded4e4473c661ed7650ee6e8c9ef808f",
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
        self.assertIn(DFLASH2_PATCH.name, text)
        self.assertIn(CPU_REGRESSION.name, text)
        self.assertIn("aeon.dflash2.patch", text)

    def test_modelopt_patch_retains_exactly_two_hunks(self) -> None:
        text = PATCH.read_text()
        self.assertEqual(text.count("@@ -"), 2)
        self.assertIn("in_proj_qkvz", text)
        self.assertIn("FP8_PER_CHANNEL_PER_TOKEN", text)
        self.assertNotIn("layer_type", text)

    def test_dflash2_patch_forwards_explicit_layer_type_not_kwargs(self) -> None:
        text = DFLASH2_PATCH.read_text()
        self.assertIn('layer_type: str = "full_attention"', text)
        self.assertIn("layer_type=layer_type", text)
        self.assertNotIn("**kwargs", text)
        self.assertNotIn("** kw", text)
        self.assertIn("qwen3_dflash2.py", text)
        self.assertIn("DFlash2Qwen3DecoderLayer", text)

    def test_cpu_dispatcher_regression_is_the_existing_runnable_script(self) -> None:
        py_compile.compile(str(CPU_REGRESSION), doraise=True)
        source = CPU_REGRESSION.read_text()
        self.assertIn("EXPECT_PCPT_DISPATCH", source)
        self.assertIn("ModelOptFp8PcPtLinearMethod", source)
        self.assertIn("in_proj_qkvz", source)
        self.assertIn("DFlash2Qwen3DecoderLayer", source)
        self.assertIn("sliding_attention", source)
        self.assertIn("layer_type", source)
        self.assertIn("captured", source)
        self.assertIn("dflash2_no_var_kw", source)
        self.assertIn("dflash2_forwards_sliding_attention", source)

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
        self.assertIn("112e96dae5543672afcfe2511193db4e095c222b5bea0dab9e109b84e9626b11", source)
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

    def test_run_docker_uses_scrubbed_production_daemon_under_hostile_ambient_selectors(
        self,
    ) -> None:
        helper = ROOT / "scripts" / "gb10_prepare_aeon_ultimate_image.py"
        spec = importlib.util.spec_from_file_location(
            "gb10_prepare_aeon_ultimate_image_under_test", helper
        )
        if spec is None or spec.loader is None:
            self.fail("could not load Ultimate image admission helper")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        production_env = {
            "HOME": "/home/obj",
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "DOCKER_HOST": "unix:///run/user/1001/docker.sock",
        }
        hostile = {
            "DOCKER_HOST": "tcp://127.0.0.1:2375",
            "DOCKER_CONTEXT": "hostile-context",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": "/hostile/certs",
            "BUILDX_BUILDER": "hostile-builder",
            "BUILDX_CONFIG": "/hostile/buildx",
            "DOCKER_BUILDKIT": "1",
        }
        calls: list[tuple[list[str], dict[str, str] | None]] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            env = kwargs.get("env")
            recorded_env = dict(env) if isinstance(env, dict) else None
            calls.append((list(argv), recorded_env))
            args = list(argv)
            stdout = ""
            if len(args) >= 2 and args[0] == "/usr/bin/docker" and args[1] == "build":
                iidfile = Path(args[args.index("--iidfile") + 1])
                iidfile.write_text(EXPECTED_IMAGE + "\n")
            elif (
                len(args) >= 4
                and args[0] == "/usr/bin/docker"
                and args[1] == "image"
                and args[2] == "inspect"
            ):
                stdout = EXPECTED_IMAGE + "\n"
            return subprocess.CompletedProcess(args, 0, stdout, "")

        def run_main(extra: list[str]) -> int:
            argv = ["gb10_prepare_aeon_ultimate_image.py", "--root", str(ROOT), *extra]
            with mock.patch.object(sys, "argv", argv):
                return module.main()

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            load_tar = tmp / "verified.tar"
            load_tar.write_bytes(b"")
            with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
                with mock.patch.dict(os.environ, hostile, clear=False):
                    self.assertEqual(
                        run_main(["--iidfile", str(tmp / "build.iid"), "--build"]), 0
                    )
                    self.assertEqual(
                        run_main(
                            [
                                "--iidfile",
                                str(tmp / "load.iid"),
                                "--load",
                                str(load_tar),
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(
                        run_main(["--iidfile", str(tmp / "inspect.iid")]), 0
                    )

        self.assertEqual(len(calls), 4)
        shapes = [tuple(argv[1:3] if argv[1] == "image" else argv[1:2]) for argv, _env in calls]
        self.assertEqual(
            shapes, [("build",), ("load",), ("image", "inspect"), ("image", "inspect")]
        )
        build_argv = calls[0][0]
        self.assertEqual(build_argv[0], "/usr/bin/docker")
        self.assertIn("--network=none", build_argv)
        self.assertIn("--pull=false", build_argv)
        self.assertIn("--iidfile", build_argv)
        self.assertIn(str(ROOT / IMAGE_DIR.relative_to(ROOT)), build_argv)
        self.assertIn(str(ROOT / DOCKERFILE.relative_to(ROOT)), build_argv)
        for argv, env in calls:
            self.assertEqual(argv[0], "/usr/bin/docker")
            self.assertEqual(env, production_env)
            self.assertNotIn("DOCKER_CONTEXT", env or {})
            self.assertNotIn("DOCKER_TLS_VERIFY", env or {})
            self.assertNotIn("DOCKER_CERT_PATH", env or {})
            self.assertNotIn("BUILDX_BUILDER", env or {})
            self.assertNotIn("BUILDX_CONFIG", env or {})
            self.assertNotIn("DOCKER_BUILDKIT", env or {})
            self.assertNotEqual((env or {}).get("DOCKER_HOST"), hostile["DOCKER_HOST"])


if __name__ == "__main__":
    unittest.main()
