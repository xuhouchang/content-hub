#!/usr/bin/env bash
# Independently scheduled Stage B worker. Safe to run every five minutes:
# empty passes are silent and a non-blocking worker lock prevents overlap.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ -x "$DEFAULT_PYTHON" ]]; then
  PYTHON="${PYTHON:-$DEFAULT_PYTHON}"
else
  PYTHON="${PYTHON:-python3}"
fi
DATE="${DATE:-$(date +%Y-%m-%d)}"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/publish_${DATE}.log"

mkdir -p "$LOG_DIR"
"$PYTHON" "$SCRIPT_DIR/publish_worker.py" --once --quiet-empty >> "$LOG_FILE" 2>&1
