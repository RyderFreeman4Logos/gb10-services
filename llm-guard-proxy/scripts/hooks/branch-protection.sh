#!/usr/bin/env bash
set -euo pipefail

branch="$(git rev-parse --abbrev-ref HEAD)"

case "${branch}" in
    main | master | dev)
        echo "Refusing to commit or push directly on protected branch: ${branch}" >&2
        exit 1
        ;;
    HEAD)
        echo "Refusing to commit or push from detached HEAD" >&2
        exit 1
        ;;
esac
