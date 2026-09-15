#!/usr/bin/env python3
"""Regenerate immutable AEON vLLM release consumers from one pin file."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


CONFIG = Path("config/aeon-vllm-release.json")
README = Path("README.md")
GUIDE = Path("docs/deployment/AGENTS.md")
QWEN36_UNIT = Path(
    "profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service"
)
EMBEDDING_UNIT = Path("profile/qwen3-embedding-8b/vllm-embedding.service")
ULTIMATE_UNIT = Path(
    "profile/aeon-ultimate-uncensored-nvfp4/vllm-aeon-ultimate-uncensored-nvfp4.service"
)
UNIT_PATHS = (
    ULTIMATE_UNIT,
    Path("profile/querit-4b-reranker/vllm-querit-4b-reranker.service"),
    EMBEDDING_UNIT,
    Path("profile/qwen3-reranker-8b/vllm-qwen3-reranker-8b.service"),
    QWEN36_UNIT,
    Path("profile/qwen3.8-27b-nvfp4-vllm/vllm-aeon-qwen38-dflash.service"),
)
BARE_IMAGE = re.compile(r"(?m)^  sha256:[0-9a-f]{64} \\$")
ALIASES = {
    Path("profile/abliterated-qwen-latest-27b"): Path("aeon-ultimate-uncensored-nvfp4"),
    Path("profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash-hikv.service"): Path(
        "vllm-aeon-27b-dflash.service"
    ),
}
CONTRACT = Path("scripts/gb10_embedding_profile_contract.py")
STORAGE = Path("scripts/gb10_embedding_activation_storage.py")
ACTIVATION = Path("scripts/gb10_embedding_activation.py")
WRAPPER = Path("scripts/gb10_activate_embedding_profile.sh")
NO_SWAP_CORE = Path("scripts/gb10_verify_vllm_no_swap_core.py")
NO_SWAP_WRAPPER = Path("scripts/gb10_verify_vllm_no_swap.sh")
CURRENT_SURFACES = (
    README,
    GUIDE,
    Path("profile/qwen3.8-27b-nvfp4-sglang/README.md"),
    CONTRACT,
    Path("scripts/querit_replay_trust.py"),
    Path("tests/embedding_profile_fixtures.py"),
    Path("tests/test_aeon_ultimate_uncensored_profile.py"),
    Path("tests/test_embedding_service_contracts.py"),
    Path("tests/test_querit_service_contracts.py"),
    Path("tests/test_querit_vllm_production_contracts.py"),
    Path("tests/test_vllm_image_identity_contracts.py"),
)
ULTIMATE_OVERRIDE_SURFACES = (
    Path("scripts/gb10_prepare_aeon_ultimate_image.py"),
    Path("scripts/gb10_verify_vllm_no_swap_core.py"),
    Path("tests/test_aeon_ultimate_derived_image.py"),
    Path("tests/test_update_aeon_vllm_release.py"),
    Path("tests/test_vllm_no_swap_verifier.py"),
)
ANNOTATION = re.compile(
    r"^# AEON image release: (?P<tag>\d{4}-\d{2}-\d{2}-(?P<version>v[^;]+)); "
    r"immutable digest: (?P<digest>sha256:[0-9a-f]{64})$",
    re.MULTILINE,
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE = re.compile(r"[0-9A-Za-z./:_-]+@sha256:[0-9a-f]{64}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _replace(text: str, old: str, new: str, label: str) -> str:
    if old == new:
        return text
    if old not in text:
        raise ValueError(f"{label}: expected current value is missing")
    return text.replace(old, new)


def _replace_regex(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"{label}: expected exactly one authority field, found {count}")
    return updated


def _cache_key(version: str) -> str:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.]+)?", version)
    if match is None:
        raise ValueError("runtime_version must start with a semantic vLLM version")
    major, minor, patch = (int(value) for value in match.groups())
    return f"v{major}{minor:02d}{patch}"


def _load_release(root: Path) -> dict:
    release = json.loads((root / CONFIG).read_text())
    expected = {
        "repository",
        "tag",
        "repository_digest",
        "arm64_digest",
        "runtime_version",
        "overrides",
    }
    if set(release) != expected:
        raise ValueError(f"{CONFIG}: expected exactly {sorted(expected)}")
    for key in ("repository", "tag", "repository_digest", "arm64_digest", "runtime_version"):
        if not isinstance(release[key], str) or not release[key]:
            raise ValueError(f"{CONFIG}: expected exactly {sorted(expected)}")
    overrides = release["overrides"]
    if not isinstance(overrides, dict) or set(overrides) != {ULTIMATE_UNIT.name}:
        raise ValueError("overrides must pin only the Ultimate unit")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}-v[^\s]+", release["tag"]) is None:
        raise ValueError("tag must be a dated AEON release tag")
    if not release["tag"].endswith("-" + release["runtime_version"]):
        raise ValueError("tag and runtime_version disagree")
    for key in ("repository_digest", "arm64_digest"):
        if DIGEST.fullmatch(release[key]) is None:
            raise ValueError(f"{key} must be a full sha256 digest")
    override_digest = overrides[ULTIMATE_UNIT.name]
    if DIGEST.fullmatch(override_digest) is None:
        raise ValueError("Ultimate override must be a full sha256 digest")
    if override_digest == release["repository_digest"]:
        raise ValueError("Ultimate override must differ from the central digest")
    derived_image = (
        "sha256:0652d5b5641f673c43455523ceb981e8ddd4df04ad862ad86edb0d59a517672e"
    )
    derived_base = (
        "sha256:2421bb1228a85370c1c50adb31f605c4361acf4d48d65282fcb919e74f34fae7"
    )
    if override_digest == derived_image and release["repository_digest"] != derived_base:
        raise ValueError(
            "Ultimate derived override is still bound to "
            f"{derived_base}; rebuild or retarget the override before moving "
            "the central release"
        )
    _cache_key(release["runtime_version"])
    return release


def _append_rollback(readme: str, guide: str, identity_test: str, old: dict[str, str]) -> tuple[str, str, str]:
    old_pair = f"{old['tag']} @ {old['repository_digest']}"
    rollback_line = next(
        line for line in readme.splitlines() if line.startswith("rollback/superseded:")
    )
    if old["tag"] not in rollback_line:
        readme = readme.replace(
            rollback_line,
            rollback_line.replace("rollback/superseded: ", f"rollback/superseded: {old_pair}; "),
        )

    guide_line = next(
        line
        for line in guide.splitlines()
        if line.startswith("* Rollback/superseded images remain retained:")
    )
    if old["tag"] not in guide_line:
        old_entry = f"`{old['tag']}` (`{old['repository_digest']}`), "
        guide = guide.replace(
            guide_line,
            guide_line.replace("remain retained: ", "remain retained: " + old_entry),
        )

    marker_block = identity_test.split("SUPERSEDED_MARKERS = (", 1)[1].split(")", 1)[0]
    if old["tag"] not in marker_block:
        identity_test = identity_test.replace(
            "SUPERSEDED_MARKERS = (\n",
            "SUPERSEDED_MARKERS = (\n"
            f'    "{old["tag"]}",\n'
            f'    "{old["repository_digest"].removeprefix("sha256:")}",\n',
            1,
        )
    return readme, guide, identity_test


def _render(root: Path) -> dict[Path, str]:
    release = _load_release(root)
    for alias, target in ALIASES.items():
        path = root / alias
        if not path.is_symlink() or path.readlink() != target:
            raise ValueError(f"{alias}: expected symlink target {target}")
    for path in UNIT_PATHS:
        candidate = root / path
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"{path}: expected canonical AEON unit is missing")
    discovered = {
        path.resolve().relative_to(root)
        for path in (root / "profile").glob("*/*.service")
        if "# AEON image release:" in path.read_text()
    }
    if discovered != set(UNIT_PATHS):
        missing = sorted(set(UNIT_PATHS) - discovered)
        extra = sorted(discovered - set(UNIT_PATHS))
        raise ValueError(f"AEON unit inventory mismatch: missing={missing}, extra={extra}")

    paths = set(
        (
            *UNIT_PATHS,
            *CURRENT_SURFACES,
            *ULTIMATE_OVERRIDE_SURFACES,
            STORAGE,
            ACTIVATION,
            WRAPPER,
            NO_SWAP_WRAPPER,
        )
    )
    texts = {path: (root / path).read_text() for path in paths}
    canonical = texts[QWEN36_UNIT]
    match = ANNOTATION.search(canonical)
    if match is None:
        raise ValueError(f"{QWEN36_UNIT}: release annotation is missing")
    image_match = re.search(
        r"(?P<repository>[^\s]+)@(?P<digest>sha256:[0-9a-f]{64})", canonical
    )
    if image_match is None or image_match.group("digest") != match.group("digest"):
        raise ValueError(f"{QWEN36_UNIT}: image and annotation disagree")
    old = {
        "repository": image_match.group("repository"),
        "tag": match.group("tag"),
        "runtime_version": match.group("version"),
        "repository_digest": match.group("digest"),
    }
    old["date"] = old["tag"][:10]
    old_cache = _cache_key(old["runtime_version"])
    new_cache = _cache_key(release["runtime_version"])
    old_host_cache = f"aeon-qwen36-{old_cache}-{old['repository_digest'][7:13]}"
    new_host_cache = f"aeon-qwen36-{new_cache}-{release['repository_digest'][7:13]}"
    old_container_cache = f"aeon-qwen36-{old_cache}"
    new_container_cache = f"aeon-qwen36-{new_cache}"

    expected_annotation = (
        f"# AEON image release: {release['tag']}; immutable digest: "
        f"{release['repository_digest']}"
    )
    expected_image = f"{release['repository']}@{release['repository_digest']}"
    override_digest = release["overrides"][ULTIMATE_UNIT.name]
    old_override_match = BARE_IMAGE.search(texts[ULTIMATE_UNIT])
    if old_override_match is None:
        raise ValueError(f"{ULTIMATE_UNIT}: current Ultimate override is missing")
    old_override = old_override_match.group(0).strip().rstrip("\\").strip()
    if DIGEST.fullmatch(old_override) is None:
        raise ValueError(f"{ULTIMATE_UNIT}: current Ultimate override is not a digest")
    if old_override != override_digest:
        for path in (*CURRENT_SURFACES, *ULTIMATE_OVERRIDE_SURFACES):
            texts[path] = texts[path].replace(old_override, override_digest)
    expected_ultimate_annotation = (
        f"# AEON image release: {release['tag']}; immutable digest: {override_digest}"
    )
    for path in UNIT_PATHS:
        text = texts[path]
        if path == ULTIMATE_UNIT:
            text = _replace(text, old["tag"], release["tag"], str(path))
            text = _replace(
                text, old["runtime_version"], release["runtime_version"], str(path)
            )
            repo_images = IMAGE.findall(text)
            bare_images = BARE_IMAGE.findall(text)
            if repo_images and not bare_images:
                if len(repo_images) != 1:
                    raise ValueError(f"{path}: generated unit release identity is ambiguous")
                text = text.replace(repo_images[0], override_digest)
            elif bare_images and not repo_images:
                if len(bare_images) != 1:
                    raise ValueError(f"{path}: generated unit release identity is ambiguous")
                text = text.replace(bare_images[0], f"  {override_digest} \\", 1)
            else:
                raise ValueError(f"{path}: generated unit release identity is ambiguous")
            annotations = [match.group(0) for match in ANNOTATION.finditer(text)]
            if len(annotations) != 1:
                raise ValueError(f"{path}: generated unit release identity is ambiguous")
            text = text.replace(annotations[0], expected_ultimate_annotation)
            annotations = [match.group(0) for match in ANNOTATION.finditer(text)]
            images = IMAGE.findall(text)
            bares = BARE_IMAGE.findall(text)
            descriptions = [line for line in text.splitlines() if line.startswith("Description=")]
            if (
                annotations != [expected_ultimate_annotation]
                or images
                or bares != [f"  {override_digest} \\"]
                or len(descriptions) != 1
            ):
                raise ValueError(f"{path}: generated unit release identity is ambiguous")
            versions = re.findall(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?", descriptions[0])
            if versions != [release["runtime_version"]]:
                raise ValueError(f"{path}: Description does not identify the generated release")
            texts[path] = text
            continue
        for key in ("repository", "tag", "repository_digest", "runtime_version"):
            text = _replace(text, old[key], release[key], str(path))
        if path == QWEN36_UNIT:
            text = _replace(text, old_host_cache, new_host_cache, str(path))
            text = _replace(text, old_container_cache, new_container_cache, str(path))
        annotations = [match.group(0) for match in ANNOTATION.finditer(text)]
        images = IMAGE.findall(text)
        descriptions = [line for line in text.splitlines() if line.startswith("Description=")]
        if annotations != [expected_annotation] or images != [expected_image] or len(descriptions) != 1:
            raise ValueError(f"{path}: generated unit release identity is ambiguous")
        versions = re.findall(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?", descriptions[0])
        if versions != [release["runtime_version"]]:
            raise ValueError(f"{path}: Description does not identify the generated release")
        texts[path] = text

    for path in CURRENT_SURFACES:
        text = texts[path]
        for key in ("repository", "tag", "repository_digest", "runtime_version"):
            if old[key] in text:
                text = text.replace(old[key], release[key])
        texts[path] = text

    identity_test_path = Path("tests/test_vllm_image_identity_contracts.py")
    for path in (identity_test_path, Path("tests/test_querit_service_contracts.py")):
        texts[path] = texts[path].replace(old["date"], release["tag"][:10])

    readme = texts[README]
    arm_match = re.search(
        r"selects the Linux ARM64 manifest\n`(?P<digest>sha256:[0-9a-f]{64})`", readme
    )
    if arm_match is None:
        raise ValueError(f"{README}: ARM64 manifest receipt is missing")
    old_arm = arm_match.group("digest")
    texts[README] = readme.replace(old_arm, release["arm64_digest"])
    texts[GUIDE] = texts[GUIDE].replace(old_arm, release["arm64_digest"])

    guide_lines = texts[GUIDE].splitlines(keepends=True)
    for index, line in enumerate(guide_lines):
        if line.startswith("* The ") and "AEON text unit rotates compiled artifacts" in line:
            if old["tag"] != release["tag"] and old_host_cache not in line:
                raise ValueError(f"{GUIDE}: current compile-cache row is stale")
            if old["tag"] != release["tag"]:
                suffix = (
                    f"The {old['runtime_version']} namespace `/{'home/obj/.cache/vllm-compile/' + old_host_cache}` "
                    f"(mounted as `/var/cache/vllm/{old_container_cache}`) is retained for rollback. "
                )
                pivot = "). The "
                line = line.replace(pivot, "). " + suffix + "The ", 1)
            line = line.replace(old_host_cache, new_host_cache, 1)
            line = line.replace(old_container_cache, new_container_cache, 1)
            guide_lines[index] = line
            break
    else:
        raise ValueError(f"{GUIDE}: compile-cache row is missing")
    texts[GUIDE] = "".join(guide_lines)

    start = texts[GUIDE].find("The tracked text units retain")
    end = texts[GUIDE].find("\n\n", start)
    if start < 0 or end < 0:
        raise ValueError(f"{GUIDE}: compile-cache troubleshooting guidance is missing")
    guidance = texts[GUIDE][start:end]
    if old["tag"] != release["tag"]:
        guidance = _replace(guidance, old_host_cache, new_host_cache, str(GUIDE))
        guidance = _replace(guidance, old_container_cache, new_container_cache, str(GUIDE))
    if guidance.count(new_host_cache) != 1 or guidance.count(new_container_cache) != 2:
        raise ValueError(f"{GUIDE}: compile-cache troubleshooting guidance is stale")
    texts[GUIDE] = texts[GUIDE][:start] + guidance + texts[GUIDE][end:]

    command_note = (
        "For the next release, edit only `config/aeon-vllm-release.json`, then run\n"
        "`python3 scripts/update_aeon_vllm_release.py`; it regenerates every literal\n"
        "unit/contract pin and the embedding activation authority-hash chain.\n"
    )
    if command_note not in texts[README]:
        anchor = "and deployment evidence remain in their dated research records.\n"
        texts[README] = _replace(
            texts[README], anchor, anchor + "\n" + command_note, str(README)
        )

    if old["tag"] != release["tag"]:
        texts[README], texts[GUIDE], texts[identity_test_path] = _append_rollback(
            texts[README], texts[GUIDE], texts[identity_test_path], old
        )

    contract = texts[CONTRACT]
    contract = _replace_regex(
        contract,
        r'^EXPECTED_UNIT_SHA256 = "[0-9a-f]{64}"$',
        f'EXPECTED_UNIT_SHA256 = "{_sha256(texts[EMBEDDING_UNIT])}"',
        str(CONTRACT),
    )
    texts[CONTRACT] = contract
    contract_sha = _sha256(contract)
    no_swap_core_sha = _sha256(texts[NO_SWAP_CORE])
    no_swap_wrapper = _replace_regex(
        texts[NO_SWAP_WRAPPER],
        r"^EXPECTED_CORE_SHA256=[0-9a-f]{64}$",
        f"EXPECTED_CORE_SHA256={no_swap_core_sha}",
        str(NO_SWAP_WRAPPER),
    )
    texts[NO_SWAP_WRAPPER] = no_swap_wrapper
    storage = texts[STORAGE]
    storage = _replace_regex(
        storage,
        r'^(\s*"gb10_embedding_profile_contract\.py": ")[0-9a-f]{64}(".*)$',
        rf"\g<1>{contract_sha}\g<2>",
        str(STORAGE),
    )
    storage = _replace_regex(
        storage,
        r'^(\s*"core": ")[0-9a-f]{64}(".*)$',
        rf"\g<1>{no_swap_core_sha}\g<2>",
        str(STORAGE),
    )
    storage = _replace_regex(
        storage,
        r'^(\s*"wrapper": ")[0-9a-f]{64}(".*)$',
        rf"\g<1>{_sha256(no_swap_wrapper)}\g<2>",
        str(STORAGE),
    )
    texts[STORAGE] = storage
    activation = texts[ACTIVATION]
    for name, digest in (
        ("gb10_embedding_profile_contract.py", contract_sha),
        ("gb10_embedding_activation_storage.py", _sha256(storage)),
    ):
        activation = _replace_regex(
            activation,
            rf'^(\s*"{re.escape(name)}": ")[0-9a-f]{{64}}(".*)$',
            rf'\g<1>{digest}\g<2>',
            str(ACTIVATION),
        )
    texts[ACTIVATION] = activation
    texts[WRAPPER] = _replace_regex(
        texts[WRAPPER],
        r'^expected_engine_sha256="[0-9a-f]{64}"$',
        f'expected_engine_sha256="{_sha256(activation)}"',
        str(WRAPPER),
    )
    return texts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        rendered = _render(root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 2
    changed = [path for path, text in rendered.items() if (root / path).read_text() != text]
    if args.check:
        if changed:
            print("stale generated AEON release consumers:", file=sys.stderr)
            for path in sorted(changed):
                print(path, file=sys.stderr)
            return 1
        print("AEON release consumers are current")
        return 0
    for path in sorted(changed):
        (root / path).write_text(rendered[path])
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
