from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path("config/aeon-vllm-release.json")
UPDATER = Path("scripts/update_aeon_vllm_release.py")
UNITS = (
    Path("profile/aeon-ultimate-uncensored-nvfp4/vllm-aeon-ultimate-uncensored-nvfp4.service"),
    Path("profile/querit-4b-reranker/vllm-querit-4b-reranker.service"),
    Path("profile/qwen3-embedding-8b/vllm-embedding.service"),
    Path("profile/qwen3-reranker-8b/vllm-qwen3-reranker-8b.service"),
    Path("profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service"),
    Path("profile/qwen3.8-27b-nvfp4-vllm/vllm-aeon-qwen38-dflash.service"),
)
ALIASES = {
    Path("profile/abliterated-qwen-latest-27b"): Path("aeon-ultimate-uncensored-nvfp4"),
    Path("profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash-hikv.service"): Path(
        "vllm-aeon-27b-dflash.service"
    ),
}
DERIVED = UNITS + (
    Path("README.md"),
    Path("docs/deployment/AGENTS.md"),
    Path("profile/qwen3.8-27b-nvfp4-sglang/README.md"),
    Path("scripts/gb10_activate_embedding_profile.sh"),
    Path("scripts/gb10_embedding_activation.py"),
    Path("scripts/gb10_embedding_activation_storage.py"),
    Path("scripts/gb10_embedding_profile_contract.py"),
    Path("scripts/querit_replay_trust.py"),
    Path("tests/embedding_profile_fixtures.py"),
    Path("tests/test_aeon_ultimate_uncensored_profile.py"),
    Path("tests/test_embedding_service_contracts.py"),
    Path("tests/test_querit_service_contracts.py"),
    Path("tests/test_querit_vllm_production_contracts.py"),
    Path("tests/test_vllm_image_identity_contracts.py"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_fixture(target: Path) -> None:
    for relative in (CONFIG, UPDATER, *DERIVED):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    for relative, link_target in ALIASES.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(link_target, target_is_directory=(ROOT / relative).is_dir())


def _run_updater(target: Path, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    command = ["python3", str(target / UPDATER), "--root", str(target)]
    if check:
        command.append("--check")
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _troubleshooting_cache_paragraph(guide: str) -> str:
    return guide.split("The tracked text units retain", 1)[1].split("\n\n", 1)[0]


class UpdateAeonVllmReleaseTests(unittest.TestCase):
    def test_quick_check_rejects_stale_generated_release_consumers(self) -> None:
        self.assertIn(
            "python3 scripts/update_aeon_vllm_release.py --check",
            (ROOT / "justfile").read_text(),
        )

    def test_one_release_file_regenerates_every_active_consumer_and_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            target = Path(raw_tmp)
            _copy_fixture(target)

            old = json.loads((target / CONFIG).read_text())
            new = {
                **old,
                "tag": "2026-10-01-v0.30.1-omni",
                "repository_digest": "sha256:" + "a" * 64,
                "arm64_digest": "sha256:" + "b" * 64,
                "runtime_version": "v0.30.1-omni",
            }
            (target / CONFIG).write_text(json.dumps(new, indent=2) + "\n")
            result = _run_updater(target)
            self.assertEqual(result.returncode, 0, result.stderr)
            first_hashes = {relative: _sha256(target / relative) for relative in DERIVED}
            second = _run_updater(target)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout, "")
            self.assertEqual(
                {relative: _sha256(target / relative) for relative in DERIVED}, first_hashes
            )
            check = _run_updater(target, check=True)
            self.assertEqual(check.returncode, 0, check.stderr)

            image = f"{new['repository']}@{new['repository_digest']}"
            for relative in UNITS:
                text = (target / relative).read_text()
                with self.subTest(unit=relative):
                    self.assertEqual(text.count(image), 1)
                    self.assertIn(
                        "# AEON image release: "
                        f"{new['tag']}; immutable digest: {new['repository_digest']}",
                        text,
                    )
                    description = next(
                        line for line in text.splitlines() if line.startswith("Description=")
                    )
                    self.assertIn(new["runtime_version"], description)
                    self.assertNotIn(old["repository_digest"], text)

            qwen36 = (target / UNITS[4]).read_text()
            self.assertIn("aeon-qwen36-v0301-aaaaaa", qwen36)
            self.assertIn("/var/cache/vllm/aeon-qwen36-v0301", qwen36)

            readme = (target / "README.md").read_text()
            guide = (target / "docs/deployment/AGENTS.md").read_text()
            for document in (readme, guide):
                self.assertIn(new["tag"], document)
                self.assertIn(new["repository_digest"], document)
                self.assertIn(new["arm64_digest"], document)
                self.assertRegex(document, rf"(?i)rollback[^\n]*{re.escape(old['tag'])}")
                self.assertRegex(
                    document, rf"(?i)rollback[^\n]*{re.escape(old['repository_digest'])}"
                )
            self.assertIn(
                "python3 scripts/update_aeon_vllm_release.py", readme
            )
            self.assertIn("aeon-qwen36-v0290-2421bb", guide)
            cache_guidance = _troubleshooting_cache_paragraph(guide)
            self.assertIn("aeon-qwen36-v0301-aaaaaa", cache_guidance)
            self.assertIn("/var/cache/vllm/aeon-qwen36-v0301", cache_guidance)
            self.assertNotIn("aeon-qwen36-v0290-2421bb", cache_guidance)

            contract = target / "scripts/gb10_embedding_profile_contract.py"
            storage = target / "scripts/gb10_embedding_activation_storage.py"
            activation = target / "scripts/gb10_embedding_activation.py"
            wrapper = target / "scripts/gb10_activate_embedding_profile.sh"
            self.assertIn(new["repository_digest"], contract.read_text())
            self.assertIn(
                f'EXPECTED_UNIT_SHA256 = "{_sha256(target / UNITS[2])}"',
                contract.read_text(),
            )
            self.assertIn(
                f'"gb10_embedding_profile_contract.py": "{_sha256(contract)}"',
                storage.read_text(),
            )
            activation_text = activation.read_text()
            self.assertIn(
                f'"gb10_embedding_profile_contract.py": "{_sha256(contract)}"',
                activation_text,
            )
            self.assertIn(
                f'"gb10_embedding_activation_storage.py": "{_sha256(storage)}"',
                activation_text,
            )
            self.assertIn(
                f'expected_engine_sha256="{_sha256(activation)}"', wrapper.read_text()
            )

            identity_test = (target / "tests/test_vllm_image_identity_contracts.py").read_text()
            markers = identity_test.split("SUPERSEDED_MARKERS = (", 1)[1].split(")", 1)[0]
            self.assertIn(old["tag"], markers)
            self.assertIn(old["repository_digest"].removeprefix("sha256:"), markers)

    def test_current_cache_guidance_matches_release_namespace(self) -> None:
        release = json.loads((ROOT / CONFIG).read_text())
        paragraph = _troubleshooting_cache_paragraph((ROOT / "docs/deployment/AGENTS.md").read_text())
        version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.]+)?", release["runtime_version"])
        self.assertIsNotNone(version)
        assert version is not None
        cache = "v{}{:02d}{}".format(*(int(value) for value in version.groups()))
        self.assertIn(
            f"aeon-qwen36-{cache}-{release['repository_digest'][7:13]}", paragraph
        )
        self.assertIn(f"/var/cache/vllm/aeon-qwen36-{cache}", paragraph)

    def test_malformed_or_incomplete_consumers_are_rejected_in_both_modes(self) -> None:
        def missing_image(target: Path) -> None:
            release = json.loads((target / CONFIG).read_text())
            unit = target / UNITS[0]
            unit.write_text(
                unit.read_text().replace(
                    f"{release['repository']}@{release['repository_digest']}",
                    "",
                )
            )

        def stale_image(target: Path) -> None:
            release = json.loads((target / CONFIG).read_text())
            unit = target / UNITS[0]
            unit.write_text(
                unit.read_text().replace(
                    f"{release['repository']}@{release['repository_digest']}",
                    "stale-image.invalid@sha256:" + "c" * 64,
                )
            )

        def missing_unit(target: Path) -> None:
            (target / UNITS[1]).unlink()

        def wrong_unit(target: Path) -> None:
            (target / UNITS[1]).write_text("[Unit]\nDescription=Wrong service\n")

        def stale_annotation(target: Path) -> None:
            release = json.loads((target / CONFIG).read_text())
            unit = target / UNITS[2]
            annotation = (
                f"# AEON image release: {release['tag']}; immutable digest: "
                f"{release['repository_digest']}"
            )
            unit.write_text(unit.read_text().replace(annotation, annotation[:-64] + "d" * 64))

        def stale_version(target: Path) -> None:
            release = json.loads((target / CONFIG).read_text())
            unit = target / UNITS[3]
            lines = unit.read_text().splitlines(keepends=True)
            lines = [
                line.replace(release["runtime_version"], "v0.28.0-omni")
                if line.startswith("Description=")
                else line
                for line in lines
            ]
            unit.write_text("".join(lines))

        def extra_unit(target: Path) -> None:
            destination = target / "profile/unexpected-aeon/vllm-unexpected.service"
            destination.parent.mkdir(parents=True)
            shutil.copy2(target / UNITS[0], destination)

        for name, corrupt in (
            ("missing-image", missing_image),
            ("stale-image", stale_image),
            ("missing-unit", missing_unit),
            ("wrong-unit", wrong_unit),
            ("stale-annotation", stale_annotation),
            ("stale-version", stale_version),
            ("unexpected-extra-unit", extra_unit),
        ):
            for check in (False, True):
                with self.subTest(case=name, check=check), tempfile.TemporaryDirectory() as raw_tmp:
                    target = Path(raw_tmp)
                    _copy_fixture(target)
                    corrupt(target)
                    result = _run_updater(target, check=check)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wrong_alias_targets_are_rejected_in_both_modes(self) -> None:
        for alias, _expected in ALIASES.items():
            for check in (False, True):
                with self.subTest(alias=alias, check=check), tempfile.TemporaryDirectory() as raw_tmp:
                    target = Path(raw_tmp)
                    _copy_fixture(target)
                    (target / alias).unlink()
                    (target / alias).symlink_to("wrong-target")
                    result = _run_updater(target, check=check)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
