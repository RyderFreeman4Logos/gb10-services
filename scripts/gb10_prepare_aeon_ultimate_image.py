#!/usr/bin/env python3
"""Admit the pinned Ultimate derived local image before unit start.

Uses profile/aeon-ultimate-uncensored-nvfp4/image as the exact context with
`--network=none --pull=false --iidfile`. Rebuild image IDs are not assumed
deterministic: compare the iidfile to the configured override and, on
mismatch, load a previously verified artifact instead of starting the unit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


UNIT_NAME = "vllm-aeon-ultimate-uncensored-nvfp4.service"
EXPECTED_IMAGE_ID = (
    "sha256:26c62d60a7cce96b279d768eaafc20189f7a38e125f9183e11ba6d5f9d4a53e0"
)
IMAGE_DIR = Path("profile/aeon-ultimate-uncensored-nvfp4/image")
DOCKERFILE = IMAGE_DIR / "Dockerfile.aeon-v029-modelopt-54367"
CONFIG = Path("config/aeon-vllm-release.json")


def configured_override(root: Path) -> str:
    release = json.loads((root / CONFIG).read_text())
    digest = release["overrides"][UNIT_NAME]
    if digest != EXPECTED_IMAGE_ID:
        raise ValueError(
            f"configured Ultimate override {digest} is not {EXPECTED_IMAGE_ID}"
        )
    return digest


def normalize_iid(raw: str) -> str:
    token = raw.strip()
    if token.startswith("sha256:") is False:
        token = f"sha256:{token}"
    if len(token) != 71 or token[7:].strip("0123456789abcdef"):
        raise ValueError(f"iidfile is not a full sha256 image ID: {raw!r}")
    return token


def compare_iidfile(path: Path, expected: str) -> str:
    observed = normalize_iid(path.read_text())
    if observed != expected:
        raise ValueError(
            "iidfile "
            f"{observed} does not match configured override {expected}; "
            "rebuild image IDs are not assumed deterministic — load a "
            "verified artifact instead of starting the unit"
        )
    return observed


def _run_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/docker", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--iidfile", type=Path, required=True)
    parser.add_argument("--load", type=Path)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--compare-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        expected = configured_override(root)
        iidfile = args.iidfile if args.iidfile.is_absolute() else root / args.iidfile
        if args.compare_only:
            if args.load is not None or args.build:
                raise ValueError("compare-only cannot combine with --load or --build")
            observed = compare_iidfile(iidfile, expected)
            print(observed)
            return 0
        iidfile.parent.mkdir(parents=True, exist_ok=True)
        if args.load is not None and args.build:
            raise ValueError("choose either --load or --build")
        if args.load is not None:
            loaded = _run_docker(["load", "-i", str(args.load)])
            if loaded.returncode != 0:
                raise ValueError(loaded.stderr.strip() or "docker load failed")
            inspect = _run_docker(["image", "inspect", "--format", "{{.Id}}", expected])
            if inspect.returncode != 0:
                raise ValueError(
                    f"loaded artifact does not provide {expected}: {inspect.stderr}"
                )
            iidfile.write_text(normalize_iid(inspect.stdout) + "\n")
        elif args.build:
            context = root / IMAGE_DIR
            dockerfile = root / DOCKERFILE
            built = _run_docker(
                [
                    "build",
                    "--network=none",
                    "--pull=false",
                    "-f",
                    str(dockerfile),
                    "--iidfile",
                    str(iidfile),
                    str(context),
                ]
            )
            if built.returncode != 0:
                raise ValueError(built.stderr.strip() or "docker build failed")
        else:
            inspect = _run_docker(["image", "inspect", "--format", "{{.Id}}", expected])
            if inspect.returncode != 0:
                raise ValueError(
                    f"{expected} is not present locally; pass --load of a "
                    "verified artifact or --build with --network=none "
                    "--pull=false --iidfile against "
                    f"{IMAGE_DIR}"
                )
            iidfile.write_text(normalize_iid(inspect.stdout) + "\n")
        observed = compare_iidfile(iidfile, expected)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"gb10_prepare_aeon_ultimate_image: {error}", file=sys.stderr)
        return 1
    print(observed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
