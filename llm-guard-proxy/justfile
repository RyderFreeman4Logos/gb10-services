# Justfile for the llm-guard-proxy Rust workspace.
# Keep this file small; issue #1 only needs local quality gates and hook wiring.

set shell := ["bash", "-c"]
set tempdir := "."
set dotenv-load := true

_repo_root := `git rev-parse --show-superproject-working-tree 2>/dev/null | grep . || git rev-parse --show-toplevel`

default: pre-commit

check-branch:
    scripts/hooks/branch-protection.sh

check-generated-artifacts:
    #!/usr/bin/env bash
    set -euo pipefail
    blocked_paths="$(
        git diff --cached --name-only --diff-filter=ACMR \
            | grep -E '^(target/|\.tmp/|\.test-target/)|(\.log$|_output\.)' || true
    )"
    if [ -n "${blocked_paths}" ]; then
        echo "Generated or scratch artifacts are staged:"
        printf '%s\n' "${blocked_paths}"
        exit 1
    fi

fmt:
    cargo fmt --all

fmt-check:
    cargo fmt --all -- --check

clippy:
    cargo clippy --workspace --all-targets --all-features -- -D warnings

test:
    cargo test --workspace --all-features

smoke-gb10:
    scripts/smoke-gb10.sh

pre-commit-fast:
    just check-branch
    just check-generated-artifacts
    just fmt-check
    just clippy

pre-commit:
    just pre-commit-fast
    just test
