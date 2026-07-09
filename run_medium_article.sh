#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ -x "$DEFAULT_PYTHON" ]]; then
  PYTHON="${PYTHON:-$DEFAULT_PYTHON}"
else
  PYTHON="${PYTHON:-python3}"
fi
DATE="${DATE:-$(date +%Y-%m-%d)}"
LOG_DIR="${SCRIPT_DIR}/logs"
LOG_FILE="${LOG_DIR}/medium_${DATE}.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "Medium Article Pipeline — $DATE"
echo "=========================================="

"$PYTHON" "$SCRIPT_DIR/write_medium_article.py" --date "$DATE"

echo "=========================================="
echo "Complete"
echo "Log: $LOG_FILE"
echo "=========================================="
