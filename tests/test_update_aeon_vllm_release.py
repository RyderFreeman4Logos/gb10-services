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


class UpdateAeonVllmReleaseTests(unittest.TestCase):
    def test_quick_check_rejects_stale_generated_release_consumers(self) -> None:
        self.assertIn(
            "python3 scripts/update_aeon_vllm_release.py --check",
            (ROOT / "justfile").read_text(),
        )

    def test_one_release_file_regenerates_every_active_consumer_and_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            target = Path(raw_tmp)
            for relative in (CONFIG, UPDATER, *DERIVED):
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)

            old = json.loads((target / CONFIG).read_text())
            new = {
                **old,
                "tag": "2026-10-01-v0.30.1-omni",
                "repository_digest": "sha256:" + "a" * 64,
                "arm64_digest": "sha256:" + "b" * 64,
                "runtime_version": "v0.30.1-omni",
            }
            (target / CONFIG).write_text(json.dumps(new, indent=2) + "\n")
            command = ["python3", str(target / UPDATER), "--root", str(target)]
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            check = subprocess.run(
                [*command, "--check"], text=True, capture_output=True, check=False
            )
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


if __name__ == "__main__":
    unittest.main()
