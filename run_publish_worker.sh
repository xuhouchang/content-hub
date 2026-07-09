#!/usr/bin/env bash
# Publish worker safety-net wrapper (Stage B of the decoupled pipeline).
#
# write_article.py auto-launches publish_worker.py in the background after each
# successful write, but a separate cron entry here guarantees delivery even if
# that background process dies. Schedule it ~90 min after the article job:
#
#   30 7 * * *  cd /path/to/content-hub && ./run_publish_worker.sh >> logs/publish_$(date +\%Y-\%m-\%d).log 2>&1
#
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
LOG_FILE="${LOG_DIR}/publish_${DATE}.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "Publish Worker (safety net) — $DATE"
echo "=========================================="

"$PYTHON" "$SCRIPT_DIR/publish_worker.py" --once

echo "=========================================="
echo "Publish worker pass complete"
echo "Log: $LOG_FILE"
echo "=========================================="
