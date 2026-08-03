from __future__ import annotations

import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"
PROFILE_DIR = ROOT / "config" / "aeon-dflash-profiles"
UNIT = SYSTEMD / "vllm-aeon-27b-dflash.service"
ALIAS = SYSTEMD / "vllm-aeon-27b-dflash-hikv.service"
INSTALLED_ACTIVE_PROFILE = "/home/obj/.config/gb10/aeon-dflash-profiles/active.env"
PROFILE_VALUES = {
    "baseline.env": "0.355",
    "hikv.env": "0.45",
}


def _directive_argv(text: str, directive: str) -> list[str]:
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"{directive}="))
    command = [lines[start].removeprefix(f"{directive}=")]
    while command[-1].rstrip().endswith("\\"):
        start += 1
        command.append(lines[start].strip())
    return shlex.split(
        " ".join(line.rstrip().removesuffix("\\").rstrip() for line in command)
    )


def _profile(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not value or key in values:
            raise AssertionError(f"invalid profile line in {path}: {line!r}")
        values[key] = value
    return values


class DflashEnvironmentProfileContractTests(unittest.TestCase):
    def test_one_canonical_unit_selects_only_gpu_utilization_from_active_profile(self) -> None:
        profiles = {name: _profile(PROFILE_DIR / name) for name in PROFILE_VALUES}
        self.assertEqual(
            profiles,
            {
                name: {"AEON_GPU_MEMORY_UTILIZATION": value}
                for name, value in PROFILE_VALUES.items()
            },
        )
        self.assertTrue((PROFILE_DIR / "active.env").is_symlink())
        self.assertEqual((PROFILE_DIR / "active.env").readlink(), Path("hikv.env"))

        regular_dflash_units = [
            path
            for path in SYSTEMD.glob("vllm-aeon-27b-dflash*.service")
            if not path.is_symlink()
        ]
        self.assertEqual(regular_dflash_units, [UNIT])
        self.assertTrue(ALIAS.is_symlink())
        self.assertEqual(ALIAS.readlink(), Path("vllm-aeon-27b-dflash.service"))
        self.assertEqual(ALIAS.resolve(), UNIT.resolve())

        text = UNIT.read_text()
        self.assertIn(f"EnvironmentFile={INSTALLED_ACTIVE_PROFILE}", text)
        self.assertIn("TimeoutStartSec=3000", text)
        self.assertIn("--deadline 2800", text)
        argv = _directive_argv(text, "ExecStart")
        utilization = argv.index("--gpu-memory-utilization")
        self.assertEqual(argv[utilization + 1], "${AEON_GPU_MEMORY_UTILIZATION}")

        rendered = {
            name: [value.replace("${AEON_GPU_MEMORY_UTILIZATION}", profile["AEON_GPU_MEMORY_UTILIZATION"])
                   for value in argv]
            for name, profile in profiles.items()
        }
        for name, expected in PROFILE_VALUES.items():
            with self.subTest(profile=name):
                runtime = rendered[name]
                self.assertEqual(runtime[utilization + 1], expected)
                self.assertEqual(shlex.split(shlex.join(runtime)), runtime)
        baseline = rendered["baseline.env"]
        hikv = rendered["hikv.env"]
        self.assertEqual(
            baseline[:utilization] + baseline[utilization + 2 :],
            hikv[:utilization] + hikv[utilization + 2 :],
        )


if __name__ == "__main__":
    unittest.main()
