#!/usr/bin/env bash
set -euo pipefail

# Compatibility entry point for the retired pending_articles.json pipeline.
# The canonical writer now queues JSONL records and publish_worker.py consumes
# them idempotently. Keep this filename so an old cron entry remains safe.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ -x "$DEFAULT_PYTHON" ]]; then
  PYTHON="${PYTHON:-$DEFAULT_PYTHON}"
else
  PYTHON="${PYTHON:-python3}"
fi

exec "$PYTHON" "$SCRIPT_DIR/publish_worker.py" --once
