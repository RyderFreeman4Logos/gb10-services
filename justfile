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

pre-push: quick-check systemd-check
