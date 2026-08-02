set shell := ["bash", "-euo", "pipefail", "-c"]

# Fast, deterministic checks for every local commit.
quick-check:
    @for script in scripts/*.sh; do bash -n "$script"; done
    python3 -m unittest discover -s tests -p 'test_*.py' -v
    git diff --check
    git diff --cached --check

# Validate all tracked user units without sudo, installation, or reload.
systemd-check:
    python3 scripts/verify_systemd_units.py

# Exercise the deployable Guard binary against the tracked loop-recovery policy.
guard-loop-recovery-unit filter="GuardOfflineSelfTestTests" pattern="test_llm_guard_proxy_loop_recovery_smoke.py":
    python3 -m unittest discover -s tests -p '{{pattern}}' -v -k "{{filter}}"

guard-loop-recovery-smoke:
    @scratch_parent="${LLM_GUARD_PROXY_SMOKE_TMPDIR:-${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR is required}}"; mkdir -p "$scratch_parent"; scratch="$(mktemp -d "$scratch_parent/llm-guard-loop-recovery.XXXXXX")"; trap 'rm -rf "$scratch"' EXIT; python3 scripts/llm_guard_proxy_loop_recovery_smoke.py --candidate-config config/llm-guard-proxy/config.toml --binary "${LLM_GUARD_PROXY_BINARY:-$HOME/.local/bin/llm-guard-proxy}" --root "$scratch/run"

pre-push: quick-check systemd-check
