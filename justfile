set shell := ["bash", "-euo", "pipefail", "-c"]

gate_jobs := env_var_or_default("GB10_LOCAL_GATE_JOBS", "2")
test_threads := env_var_or_default("GB10_LOCAL_TEST_THREADS", "2")

# Fast, deterministic checks for every local commit.
quick-check:
    @for script in scripts/*.sh; do bash -n "$script"; done
    python3 -m unittest discover -s tests -p 'test_*.py' -v
    cargo fmt --all -- --check
    git diff --check
    git diff --cached --check

# Validate all tracked user units without sudo, installation, or reload.
systemd-check:
    python3 scripts/verify_systemd_units.py

# Full local equivalent of the former remote quality gate. Raise the two
# GB10_LOCAL_* variables only when the host has measured memory headroom.
rust-check:
    cargo clippy --workspace --all-targets --all-features --jobs {{gate_jobs}} -- -D warnings
    RUST_TEST_THREADS={{test_threads}} cargo test --workspace --all-features --jobs {{gate_jobs}}

pre-push: quick-check systemd-check rust-check
