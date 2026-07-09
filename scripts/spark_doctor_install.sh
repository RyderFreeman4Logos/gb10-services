#!/usr/bin/env bash
# Install or upgrade spark-doctor into a user-local virtualenv on GB10.
set -Eeuo pipefail

SPARK_DOCTOR_REPO="${SPARK_DOCTOR_REPO:-https://github.com/joeynyc/spark-doctor.git}"
SPARK_DOCTOR_REF="${SPARK_DOCTOR_REF:-418f47b}"
SPARK_DOCTOR_HOME="${SPARK_DOCTOR_HOME:-$HOME/.local/share/spark-doctor}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${SPARK_DOCTOR_VENV:-$SPARK_DOCTOR_HOME/venv}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

mkdir -p "$SPARK_DOCTOR_HOME" "$BIN_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install --upgrade "git+${SPARK_DOCTOR_REPO}@${SPARK_DOCTOR_REF}"

ln -sfn "$VENV_DIR/bin/spark-doctor" "$BIN_DIR/spark-doctor"

"$BIN_DIR/spark-doctor" version
"$BIN_DIR/spark-doctor" self-test

cat > "$SPARK_DOCTOR_HOME/install.env" <<EOF
SPARK_DOCTOR_REPO=${SPARK_DOCTOR_REPO}
SPARK_DOCTOR_REF=${SPARK_DOCTOR_REF}
SPARK_DOCTOR_HOME=${SPARK_DOCTOR_HOME}
SPARK_DOCTOR_VENV=${VENV_DIR}
SPARK_DOCTOR_BIN=${BIN_DIR}/spark-doctor
INSTALLED_AT=$(date -Is)
EOF

printf 'installed spark-doctor ref=%s bin=%s\n' "$SPARK_DOCTOR_REF" "$BIN_DIR/spark-doctor"
