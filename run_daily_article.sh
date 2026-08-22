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
LOG_FILE="${LOG_DIR}/article_${DATE}.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "Content Platform Article Job — $DATE"
echo "=========================================="

"$PYTHON" "$SCRIPT_DIR/platform_cli.py" run article-daily --date "$DATE" --workspace-dir "$SCRIPT_DIR"

echo "=========================================="
echo "Article job complete"
echo "Log: $LOG_FILE"
echo "=========================================="
