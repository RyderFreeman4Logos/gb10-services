#!/usr/bin/env bash
# Generation-bound no-swap verifier and generation-safe vLLM cleanup helper.
set -euo pipefail
umask 077

fail() {
    printf 'gb10_vllm_no_swap: %s\n' "$*" >&2
    exit 1
}

TEST_SELECTORS=(
    BASH_ENV
    ENV
    PYTHONHOME
    PYTHONPATH
    GB10_VLLM_NO_SWAP_CGROUP_ROOT
    GB10_VLLM_NO_SWAP_COMMAND_TIMEOUT_SECONDS
    GB10_VLLM_NO_SWAP_DOCKER_BIN
    GB10_VLLM_NO_SWAP_PROC_ROOT
    GB10_VLLM_NO_SWAP_SYSTEMCTL_BIN
    GB10_VLLM_NO_SWAP_WAIT_SECONDS
)

test_only=0
if [[ "${1-}" == "--test-only" ]]; then
    test_only=1
    shift
fi

if (( test_only == 0 )); then
    for selector in "${TEST_SELECTORS[@]}"; do
        [[ ! -v "$selector" ]] || fail "test-only selector is forbidden in production mode: $selector"
    done
    [[ "${DOCKER_HOST-}" == "unix:///run/user/1001/docker.sock" ]] \
        || fail "DOCKER_HOST is not the fixed rootless production socket"
    DOCKER_BIN=/usr/bin/docker
    SYSTEMCTL_BIN=/usr/bin/systemctl
    PROC_ROOT=/proc
    CGROUP_ROOT=/sys/fs/cgroup
    WAIT_SECONDS=90
    COMMAND_TIMEOUT_SECONDS=15
else
    DOCKER_BIN="${GB10_VLLM_NO_SWAP_DOCKER_BIN-}"
    SYSTEMCTL_BIN="${GB10_VLLM_NO_SWAP_SYSTEMCTL_BIN-}"
    PROC_ROOT="${GB10_VLLM_NO_SWAP_PROC_ROOT-}"
    CGROUP_ROOT="${GB10_VLLM_NO_SWAP_CGROUP_ROOT-}"
    WAIT_SECONDS="${GB10_VLLM_NO_SWAP_WAIT_SECONDS-}"
    COMMAND_TIMEOUT_SECONDS="${GB10_VLLM_NO_SWAP_COMMAND_TIMEOUT_SECONDS-}"
    [[ "${DOCKER_HOST-}" == "unix:///run/user/$(/usr/bin/id -u)/docker.sock" ]] \
        || fail "test-only DOCKER_HOST is not the current rootless socket"
    [[ "$DOCKER_BIN" == /* && -x "$DOCKER_BIN" ]] \
        || fail "test-only Docker executable is invalid"
    [[ "$SYSTEMCTL_BIN" == /* && -x "$SYSTEMCTL_BIN" ]] \
        || fail "test-only systemctl executable is invalid"
    [[ "$PROC_ROOT" == /* && -d "$PROC_ROOT" ]] \
        || fail "test-only proc root is invalid"
    [[ "$CGROUP_ROOT" == /* && -d "$CGROUP_ROOT" ]] \
        || fail "test-only cgroup root is invalid"
    [[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ && "$COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
        || fail "test-only timeout is invalid"
    (( WAIT_SECONDS <= 5 && COMMAND_TIMEOUT_SECONDS <= 5 )) \
        || fail "test-only timeout exceeds its bound"
fi


CORE_BASENAME=gb10_verify_vllm_no_swap_core.py
EXPECTED_CORE_SHA256=e137b5262dfab8719199531ed5a1b2eed59b2cd5937c6891ce2e7e5a15754b2e

/usr/bin/python3 -I - \
    "${BASH_SOURCE[0]}" \
    "$CORE_BASENAME" \
    "$EXPECTED_CORE_SHA256" \
    "$DOCKER_BIN" \
    "$SYSTEMCTL_BIN" \
    "$PROC_ROOT" \
    "$CGROUP_ROOT" \
    "$WAIT_SECONDS" \
    "$COMMAND_TIMEOUT_SECONDS" \
    "$test_only" \
    "$@" <<'PY'
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import NoReturn


def fail(message: str) -> NoReturn:
    print(f"gb10_vllm_no_swap: {message}", file=sys.stderr)
    raise SystemExit(1)


def identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


try:
    wrapper_path = Path(sys.argv[1]).resolve(strict=True)
    core_path = wrapper_path.parent / sys.argv[2]
    expected_digest = sys.argv[3]
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(core_path, flags)
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if not stat.S_ISREG(before.st_mode):
            fail("verifier core is not a regular file")
        if before.st_uid != os.geteuid():
            fail("verifier core has an unsafe owner")
        if before.st_nlink != 1:
            fail("verifier core has an unsafe link count")
        if mode not in (0o600, 0o644):
            fail("verifier core has an unsafe mode")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(core_path, follow_symlinks=False)
        if identity(before) != identity(after) or identity(after) != identity(current):
            fail("verifier core changed while loading")
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
except (IndexError, OSError, RuntimeError, ValueError) as error:
    fail(f"verifier core cannot be loaded: {error}")

if hashlib.sha256(payload).hexdigest() != expected_digest:
    fail("verifier core digest mismatch")

sys.argv = [str(core_path), *sys.argv[4:]]
exec(
    compile(payload, str(core_path), "exec"),
    {"__name__": "__main__", "__file__": str(core_path)},
)
PY
