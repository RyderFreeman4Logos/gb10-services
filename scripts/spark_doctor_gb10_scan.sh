#!/usr/bin/env bash
# Low-interference spark-doctor scan wrapper for GB10.
set -Eeuo pipefail

export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/$(id -u)/docker.sock}"
export PATH="$HOME/.local/bin:$HOME/.local/share/spark-doctor/venv/bin:$PATH"

SPARK_DOCTOR_BIN="${SPARK_DOCTOR_BIN:-$HOME/.local/bin/spark-doctor}"
SPARK_DOCTOR_OUT_DIR="${SPARK_DOCTOR_OUT_DIR:-$HOME/log/spark-doctor}"
SPARK_DOCTOR_SAMPLE_SECONDS="${SPARK_DOCTOR_SAMPLE_SECONDS:-3}"
SPARK_DOCTOR_INCLUDE_LOGS="${SPARK_DOCTOR_INCLUDE_LOGS:-0}"
SPARK_DOCTOR_STRICT_EXIT="${SPARK_DOCTOR_STRICT_EXIT:-0}"

mkdir -p "$SPARK_DOCTOR_OUT_DIR"
stamp="$(date +%Y%m%dT%H%M%S%z)"
json_out="$SPARK_DOCTOR_OUT_DIR/spark-doctor-${stamp}.json"
markdown_out="$SPARK_DOCTOR_OUT_DIR/spark-doctor-${stamp}.md"
log_out="$SPARK_DOCTOR_OUT_DIR/spark-doctor-${stamp}.log"

if [[ ! -x "$SPARK_DOCTOR_BIN" ]]; then
  echo "spark-doctor binary not found or not executable: $SPARK_DOCTOR_BIN" | tee "$log_out" >&2
  exit 127
fi

args=(scan --sample-seconds "$SPARK_DOCTOR_SAMPLE_SECONDS" --json "$json_out" --markdown "$markdown_out" --no-save)
if [[ "$SPARK_DOCTOR_INCLUDE_LOGS" == "1" || "$SPARK_DOCTOR_INCLUDE_LOGS" == "true" ]]; then
  args+=(--include-logs)
else
  args+=(--no-logs)
fi

set +e
"$SPARK_DOCTOR_BIN" "${args[@]}" 2>&1 | tee "$log_out"
rc=${PIPESTATUS[0]}
set -e

ln -sfn "$(basename "$json_out")" "$SPARK_DOCTOR_OUT_DIR/latest.json"
ln -sfn "$(basename "$markdown_out")" "$SPARK_DOCTOR_OUT_DIR/latest.md"
ln -sfn "$(basename "$log_out")" "$SPARK_DOCTOR_OUT_DIR/latest.log"

printf 'spark-doctor scan complete rc=%s json=%s markdown=%s log=%s\n' "$rc" "$json_out" "$markdown_out" "$log_out"

if [[ "$SPARK_DOCTOR_STRICT_EXIT" == "1" || "$SPARK_DOCTOR_STRICT_EXIT" == "true" ]]; then
  exit "$rc"
fi
exit 0
