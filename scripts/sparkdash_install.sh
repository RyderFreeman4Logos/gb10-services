#!/usr/bin/env bash
# Install or refresh sparkDash host-process dashboard from this repository.
# Does not start/stop vLLM or Guard. Does not use Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${ROOT}/profile/sparkdash"
PIN_FILE="${PROFILE}/PIN"
CHECKOUT="${SPARKDASH_CHECKOUT:-/home/obj/src/sparkDash}"
PINNED_COMMIT="$(awk -F= '/^commit=/{print $2}' "${PIN_FILE}")"
PINNED_REPO="$(awk -F= '/^repo=/{print $2}' "${PIN_FILE}")"
NODE_BIN="$(awk -F= '/^node_bin=/{print $2}' "${PIN_FILE}")"

if [[ -z "${PINNED_COMMIT}" || -z "${PINNED_REPO}" ]]; then
  echo "sparkdash: missing pin in ${PIN_FILE}" >&2
  exit 1
fi

mkdir -p "${CHECKOUT%/*}"
export TMPDIR="${TMPDIR:-${HOME}/tmp}"
mkdir -p "${TMPDIR}"

if [[ ! -d "${CHECKOUT}/.git" ]]; then
  git clone --filter=blob:none "${PINNED_REPO}" "${CHECKOUT}"
fi
if git -C "${CHECKOUT}" remote get-url origin >/dev/null 2>&1; then
  git -C "${CHECKOUT}" fetch --filter=blob:none origin
fi
git -C "${CHECKOUT}" checkout --detach "${PINNED_COMMIT}"
git -C "${CHECKOUT}" reset --hard "${PINNED_COMMIT}"
HEAD="$(git -C "${CHECKOUT}" rev-parse HEAD)"
if [[ "${HEAD}" != "${PINNED_COMMIT}" ]]; then
  echo "sparkdash: checkout HEAD ${HEAD} != pin ${PINNED_COMMIT}" >&2
  exit 1
fi
DIRTY="$(git -C "${CHECKOUT}" status --porcelain --untracked-files=no)"
if [[ -n "${DIRTY}" ]]; then
  echo "sparkdash: tracked source is dirty after reset --hard ${PINNED_COMMIT}" >&2
  echo "${DIRTY}" >&2
  exit 1
fi

if [[ ! -x "${NODE_BIN}" ]]; then
  echo "sparkdash: node binary missing at ${NODE_BIN}; install mise node@22.23.2 first" >&2
  exit 1
fi

(
  cd "${CHECKOUT}"
  PATH="$(dirname "${NODE_BIN}"):${PATH}"
  npm ci --no-audit --no-fund
  npm run build
)

install -d -m 0755 "${HOME}/.config/sparkdash" "${HOME}/.config/systemd/user"
if [[ ! -f "${CHECKOUT}/server/collectors/llmHost.js.upstream-bbec3bb" ]]; then
  cp -a "${CHECKOUT}/server/collectors/llmHost.js" "${CHECKOUT}/server/collectors/llmHost.js.upstream-bbec3bb"
fi
if [[ ! -f "${CHECKOUT}/server/auth.js.upstream-bbec3bb" ]]; then
  cp -a "${CHECKOUT}/server/auth.js" "${CHECKOUT}/server/auth.js.upstream-bbec3bb"
fi
install -m 0644 "${PROFILE}/llmHost.js" "${CHECKOUT}/server/collectors/llmHost.js"
install -m 0644 "${PROFILE}/auth.js" "${CHECKOUT}/server/auth.js"
install -m 0644 "${PROFILE}/sparks.json" "${CHECKOUT}/config/sparks.json"
install -m 0644 "${PROFILE}/sparkdash.env" "${HOME}/.config/sparkdash/sparkdash.env"
install -m 0644 "${PROFILE}/sparkdash.service" "${HOME}/.config/systemd/user/sparkdash.service"

echo "sparkdash: installed pin ${PINNED_COMMIT} at ${CHECKOUT}"
echo "sparkdash: unit ${HOME}/.config/systemd/user/sparkdash.service"
echo "sparkdash: enable with: systemctl --user daemon-reload && systemctl --user enable --now sparkdash.service"
echo "sparkdash: never Requires= model units; stop/start this unit only"
