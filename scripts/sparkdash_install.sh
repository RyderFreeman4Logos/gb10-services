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
TMPDIR="${TMPDIR:-${HOME}/tmp}"

fail() {
  echo "sparkdash: $*" >&2
  exit 1
}

if [[ -z "${PINNED_COMMIT}" || -z "${PINNED_REPO}" ]]; then
  fail "missing pin in ${PIN_FILE}"
fi

file_matches() {
  local path="$1" expected_hash="$2" mode actual_mode
  shift 2
  [[ -f "${path}" && ! -L "${path}" ]] || return 1
  [[ "$(sha256sum -- "${path}" | awk '{print $1}')" == "${expected_hash}" ]] || return 1
  actual_mode="$(stat -c '%a' -- "${path}")"
  for mode in "$@"; do
    [[ "${actual_mode}" == "${mode}" ]] && return 0
  done
  return 1
}

pin_hash() {
  git -C "${CHECKOUT}" show "${PINNED_COMMIT}:$1" | sha256sum | awk '{print $1}'
}

preflight_existing_checkout() {
  local head dirty line index_status path llm_base auth_base probe_base daily_base panel_base types_base store_base
  local llm_overlay auth_overlay probe_overlay daily_overlay panel_overlay types_overlay store_overlay
  local sparks_overlay sparks_legacy llm_backup auth_backup probe_backup daily_backup panel_backup types_backup store_backup sparks

  git -C "${CHECKOUT}" cat-file -e "${PINNED_COMMIT}^{commit}" 2>/dev/null ||
    fail "existing checkout lacks pinned commit ${PINNED_COMMIT}; refusing network or source writes"
  head="$(git -C "${CHECKOUT}" rev-parse HEAD)"
  [[ "${head}" == "${PINNED_COMMIT}" ]] ||
    fail "checkout HEAD ${head} != pin ${PINNED_COMMIT}; refusing checkout or fetch"

  llm_base="$(pin_hash server/collectors/llmHost.js)"
  auth_base="$(pin_hash server/auth.js)"
  probe_base="$(pin_hash server/collectors/LlmProbe.js)"
  daily_base="$(pin_hash server/collectors/LlmDaily.js)"
  panel_base="$(pin_hash src/components/SparkPage/LlmPanel.tsx)"
  types_base="$(pin_hash src/api/types.ts)"
  store_base="$(pin_hash src/hooks/metricsStore.ts)"
  llm_overlay="$(sha256sum -- "${PROFILE}/llmHost.js" | awk '{print $1}')"
  auth_overlay="$(sha256sum -- "${PROFILE}/auth.js" | awk '{print $1}')"
  probe_overlay="$(sha256sum -- "${PROFILE}/LlmProbe.js" | awk '{print $1}')"
  daily_overlay="$(sha256sum -- "${PROFILE}/LlmDaily.js" | awk '{print $1}')"
  panel_overlay="$(sha256sum -- "${PROFILE}/LlmPanel.tsx" | awk '{print $1}')"
  types_overlay="$(sha256sum -- "${PROFILE}/types.ts" | awk '{print $1}')"
  store_overlay="$(sha256sum -- "${PROFILE}/metricsStore.ts" | awk '{print $1}')"
  sparks_overlay="$(sha256sum -- "${PROFILE}/sparks.json" | awk '{print $1}')"
  sparks_legacy="$(sha256sum -- "${PROFILE}/sparks.legacy-bbec3bb.json" | awk '{print $1}')"

  # 0664 baseline and 0600 llmHost overlay are exact observed pre-upgrade states.
  # Every published managed file is normalized to 0644 below.
  if ! file_matches "${CHECKOUT}/server/collectors/llmHost.js" "${llm_base}" 644 664 &&
    ! file_matches "${CHECKOUT}/server/collectors/llmHost.js" "${llm_overlay}" 600 644; then
    fail "refusing unknown bytes or mode at server/collectors/llmHost.js"
  fi
  if ! file_matches "${CHECKOUT}/server/auth.js" "${auth_base}" 644 664 &&
    ! file_matches "${CHECKOUT}/server/auth.js" "${auth_overlay}" 644; then
    fail "refusing unknown bytes or mode at server/auth.js"
  fi
  if ! file_matches "${CHECKOUT}/server/collectors/LlmProbe.js" "${probe_base}" 644 664 &&
    ! file_matches "${CHECKOUT}/server/collectors/LlmProbe.js" "${probe_overlay}" 644; then
    fail "refusing unknown bytes or mode at server/collectors/LlmProbe.js"
  fi
  if ! file_matches "${CHECKOUT}/server/collectors/LlmDaily.js" "${daily_base}" 644 664 &&
    ! file_matches "${CHECKOUT}/server/collectors/LlmDaily.js" "${daily_overlay}" 644; then
    fail "refusing unknown bytes or mode at server/collectors/LlmDaily.js"
  fi
  if ! file_matches "${CHECKOUT}/src/components/SparkPage/LlmPanel.tsx" "${panel_base}" 644 664 &&
    ! file_matches "${CHECKOUT}/src/components/SparkPage/LlmPanel.tsx" "${panel_overlay}" 644; then
    fail "refusing unknown bytes or mode at src/components/SparkPage/LlmPanel.tsx"
  fi
  if ! file_matches "${CHECKOUT}/src/api/types.ts" "${types_base}" 644 664 &&
    ! file_matches "${CHECKOUT}/src/api/types.ts" "${types_overlay}" 644; then
    fail "refusing unknown bytes or mode at src/api/types.ts"
  fi
  if ! file_matches "${CHECKOUT}/src/hooks/metricsStore.ts" "${store_base}" 644 664 &&
    ! file_matches "${CHECKOUT}/src/hooks/metricsStore.ts" "${store_overlay}" 644; then
    fail "refusing unknown bytes or mode at src/hooks/metricsStore.ts"
  fi

  sparks="${CHECKOUT}/config/sparks.json"
  if [[ -e "${sparks}" || -L "${sparks}" ]]; then
    if ! file_matches "${sparks}" "${sparks_overlay}" 644 &&
      ! file_matches "${sparks}" "${sparks_legacy}" 644; then
      fail "refusing unknown bytes or mode at config/sparks.json"
    fi
  fi

  llm_backup="${CHECKOUT}/server/collectors/llmHost.js.upstream-bbec3bb"
  if [[ -e "${llm_backup}" || -L "${llm_backup}" ]]; then
    file_matches "${llm_backup}" "${llm_base}" 644 664 ||
      fail "refusing unknown bytes or mode at server/collectors/llmHost.js.upstream-bbec3bb"
  fi
  auth_backup="${CHECKOUT}/server/auth.js.upstream-bbec3bb"
  if [[ -e "${auth_backup}" || -L "${auth_backup}" ]]; then
    file_matches "${auth_backup}" "${auth_base}" 644 664 ||
      fail "refusing unknown bytes or mode at server/auth.js.upstream-bbec3bb"
  fi
  probe_backup="${CHECKOUT}/server/collectors/LlmProbe.js.upstream-bbec3bb"
  if [[ -e "${probe_backup}" || -L "${probe_backup}" ]]; then
    file_matches "${probe_backup}" "${probe_base}" 644 664 ||
      fail "refusing unknown bytes or mode at server/collectors/LlmProbe.js.upstream-bbec3bb"
  fi
  daily_backup="${CHECKOUT}/server/collectors/LlmDaily.js.upstream-bbec3bb"
  if [[ -e "${daily_backup}" || -L "${daily_backup}" ]]; then
    file_matches "${daily_backup}" "${daily_base}" 644 664 ||
      fail "refusing unknown bytes or mode at server/collectors/LlmDaily.js.upstream-bbec3bb"
  fi
  panel_backup="${CHECKOUT}/src/components/SparkPage/LlmPanel.tsx.upstream-bbec3bb"
  if [[ -e "${panel_backup}" || -L "${panel_backup}" ]]; then
    file_matches "${panel_backup}" "${panel_base}" 644 664 ||
      fail "refusing unknown bytes or mode at src/components/SparkPage/LlmPanel.tsx.upstream-bbec3bb"
  fi
  types_backup="${CHECKOUT}/src/api/types.ts.upstream-bbec3bb"
  if [[ -e "${types_backup}" || -L "${types_backup}" ]]; then
    file_matches "${types_backup}" "${types_base}" 644 664 ||
      fail "refusing unknown bytes or mode at src/api/types.ts.upstream-bbec3bb"
  fi
  store_backup="${CHECKOUT}/src/hooks/metricsStore.ts.upstream-bbec3bb"
  if [[ -e "${store_backup}" || -L "${store_backup}" ]]; then
    file_matches "${store_backup}" "${store_base}" 644 664 ||
      fail "refusing unknown bytes or mode at src/hooks/metricsStore.ts.upstream-bbec3bb"
  fi

  dirty="$(GIT_OPTIONAL_LOCKS=0 git --no-optional-locks -C "${CHECKOUT}" status --porcelain=v1 --untracked-files=no)"
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    index_status="${line:0:1}"
    path="${line:3}"
    [[ "${index_status}" == " " ]] || fail "refusing staged source change: ${path}"
    case "${path}" in
      server/collectors/llmHost.js|server/collectors/LlmProbe.js|server/collectors/LlmDaily.js|server/auth.js|src/components/SparkPage/LlmPanel.tsx|src/api/types.ts|src/hooks/metricsStore.ts) ;;
      *) fail "refusing unexpected tracked edit: ${path}" ;;
    esac
  done <<< "${dirty}"
}

new_checkout=0
if [[ -d "${CHECKOUT}/.git" ]]; then
  preflight_existing_checkout
else
  [[ ! -e "${CHECKOUT}" && ! -L "${CHECKOUT}" ]] ||
    fail "${CHECKOUT} exists and is not a git checkout; refusing to overwrite"
  new_checkout=1
fi

existing_env="${HOME}/.config/sparkdash/sparkdash.env"
if [[ -e "${existing_env}" || -L "${existing_env}" ]]; then
  [[ -f "${existing_env}" && ! -L "${existing_env}" ]] ||
    fail "existing sparkdash.env is not a regular file; refusing to overwrite"
fi

mkdir -p "${CHECKOUT%/*}" "${TMPDIR}"
if (( new_checkout )); then
  git clone --filter=blob:none "${PINNED_REPO}" "${CHECKOUT}"
  git -C "${CHECKOUT}" checkout --detach "${PINNED_COMMIT}"
  preflight_existing_checkout
fi

[[ -x "${NODE_BIN}" ]] ||
  fail "node binary missing at ${NODE_BIN}; install mise node@22.23.2 first"

install -d -m 0755 "${HOME}/.config/sparkdash" "${HOME}/.config/systemd/user"
llm_tmp="$(mktemp "${TMPDIR}/sparkdash-llm-upstream.XXXXXX")"
auth_tmp="$(mktemp "${TMPDIR}/sparkdash-auth-upstream.XXXXXX")"
probe_tmp="$(mktemp "${TMPDIR}/sparkdash-probe-upstream.XXXXXX")"
daily_tmp="$(mktemp "${TMPDIR}/sparkdash-daily-upstream.XXXXXX")"
panel_tmp="$(mktemp "${TMPDIR}/sparkdash-panel-upstream.XXXXXX")"
types_tmp="$(mktemp "${TMPDIR}/sparkdash-types-upstream.XXXXXX")"
store_tmp="$(mktemp "${TMPDIR}/sparkdash-store-upstream.XXXXXX")"
trap 'rm -f "${llm_tmp}" "${auth_tmp}" "${probe_tmp}" "${daily_tmp}" "${panel_tmp}" "${types_tmp}" "${store_tmp}"' EXIT
git -C "${CHECKOUT}" show "${PINNED_COMMIT}:server/collectors/llmHost.js" >"${llm_tmp}"
git -C "${CHECKOUT}" show "${PINNED_COMMIT}:server/auth.js" >"${auth_tmp}"
git -C "${CHECKOUT}" show "${PINNED_COMMIT}:server/collectors/LlmProbe.js" >"${probe_tmp}"
git -C "${CHECKOUT}" show "${PINNED_COMMIT}:server/collectors/LlmDaily.js" >"${daily_tmp}"
git -C "${CHECKOUT}" show "${PINNED_COMMIT}:src/components/SparkPage/LlmPanel.tsx" >"${panel_tmp}"
git -C "${CHECKOUT}" show "${PINNED_COMMIT}:src/api/types.ts" >"${types_tmp}"
git -C "${CHECKOUT}" show "${PINNED_COMMIT}:src/hooks/metricsStore.ts" >"${store_tmp}"
install -m 0644 "${llm_tmp}" "${CHECKOUT}/server/collectors/llmHost.js.upstream-bbec3bb"
install -m 0644 "${auth_tmp}" "${CHECKOUT}/server/auth.js.upstream-bbec3bb"
install -m 0644 "${probe_tmp}" "${CHECKOUT}/server/collectors/LlmProbe.js.upstream-bbec3bb"
install -m 0644 "${daily_tmp}" "${CHECKOUT}/server/collectors/LlmDaily.js.upstream-bbec3bb"
install -m 0644 "${panel_tmp}" "${CHECKOUT}/src/components/SparkPage/LlmPanel.tsx.upstream-bbec3bb"
install -m 0644 "${types_tmp}" "${CHECKOUT}/src/api/types.ts.upstream-bbec3bb"
install -m 0644 "${store_tmp}" "${CHECKOUT}/src/hooks/metricsStore.ts.upstream-bbec3bb"
install -m 0644 "${PROFILE}/llmHost.js" "${CHECKOUT}/server/collectors/llmHost.js"
install -m 0644 "${PROFILE}/auth.js" "${CHECKOUT}/server/auth.js"
install -m 0644 "${PROFILE}/LlmProbe.js" "${CHECKOUT}/server/collectors/LlmProbe.js"
install -m 0644 "${PROFILE}/LlmDaily.js" "${CHECKOUT}/server/collectors/LlmDaily.js"
install -m 0644 "${PROFILE}/LlmPanel.tsx" "${CHECKOUT}/src/components/SparkPage/LlmPanel.tsx"
install -m 0644 "${PROFILE}/types.ts" "${CHECKOUT}/src/api/types.ts"
install -m 0644 "${PROFILE}/metricsStore.ts" "${CHECKOUT}/src/hooks/metricsStore.ts"
install -m 0644 "${PROFILE}/sparks.json" "${CHECKOUT}/config/sparks.json"

(
  cd "${CHECKOUT}"
  PATH="$(dirname "${NODE_BIN}"):${PATH}"
  npm ci --include=dev --no-audit --no-fund
  npm run build
)

[[ -f "${CHECKOUT}/dist/index.html" && ! -L "${CHECKOUT}/dist/index.html" ]] ||
  fail "built dist missing ${CHECKOUT}/dist/index.html"
dist_bytes="$(cat -- "${CHECKOUT}/dist/index.html" "${CHECKOUT}/dist"/assets/*.js 2>/dev/null || true)"
[[ "${dist_bytes}" == *generationTpsState* && "${dist_bytes}" == *stale* ]] ||
  fail "built dist missing generationTpsState"

# Build only after overlays, then verify neither source nor modes widened.
preflight_existing_checkout

if [[ ! -e "${existing_env}" ]]; then
  install -m 0644 "${PROFILE}/sparkdash.env" "${existing_env}"
fi
install -m 0644 "${PROFILE}/sparkdash.service" "${HOME}/.config/systemd/user/sparkdash.service"

# Read back the published source contract; user env bytes/mode are intentionally untouched.
preflight_existing_checkout
file_matches "${HOME}/.config/systemd/user/sparkdash.service" \
  "$(sha256sum -- "${PROFILE}/sparkdash.service" | awk '{print $1}')" 644 ||
  fail "installed unit failed byte/mode verification"

rm -f "${llm_tmp}" "${auth_tmp}" "${probe_tmp}" "${daily_tmp}" "${panel_tmp}" "${types_tmp}" "${store_tmp}"
trap - EXIT

echo "sparkdash: installed pin ${PINNED_COMMIT} at ${CHECKOUT}"
echo "sparkdash: preserved existing env when present; unit forces SPARKDASH_READ_ONLY=1"
echo "sparkdash: unit ${HOME}/.config/systemd/user/sparkdash.service"
echo "sparkdash: enable with: systemctl --user daemon-reload && systemctl --user enable --now sparkdash.service"
echo "sparkdash: never Requires= model units; stop/start this unit only"
