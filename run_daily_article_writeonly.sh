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
LOG_FILE="${LOG_DIR}/article_writeonly_${DATE}.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "Content Platform: Write Articles — $DATE"
echo "=========================================="

# Step 1: Run the WeChat article pipeline (write + image matching, no publish)
"$PYTHON" "$SCRIPT_DIR/write_wechat_article.py" --date "$DATE"

# Note: write_wechat_article.py handles enqueueing to pending_articles.json automatically.
# The 07:30 publish cron will read from that queue and publish to WeChat drafts.
echo "📝 Articles enqueued for async publish (handled by write_wechat_article.py)"

echo "=========================================="
echo "Write-only phase complete"
echo "Log: $LOG_FILE"
echo "=========================================="
