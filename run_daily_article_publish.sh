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
LOG_FILE="${LOG_DIR}/article_publish_${DATE}.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "Content Platform: Publish Articles — $DATE"
echo "=========================================="

PENDING_FILE="$SCRIPT_DIR/pending_articles.json"
if [[ ! -f "$PENDING_FILE" ]]; then
  echo "⚠️ No pending articles queue found"
  exit 0
fi

# Read the queue
ARTICLES=$(python3 -c "
import json
with open('$PENDING_FILE') as f:
    dirs = json.load(f)
for d in dirs:
    print(d)
" 2>/dev/null) || {
  echo "⚠️ Could not read pending_articles.json"
  exit 0
}

COUNTER=0
while IFS= read -r article_dir; do
  if [[ -z "$article_dir" ]]; then
    continue
  fi
  
  ARTICLE_PATH="$SCRIPT_DIR/wechat-articles/$article_dir"
  # article_dir might be full path already, handle both
  if [[ ! -d "$article_dir" ]] && [[ -d "$ARTICLE_PATH" ]]; then
    ARTICLE_PATH="$ARTICLE_PATH"
  elif [[ -d "$article_dir" ]]; then
    ARTICLE_PATH="$article_dir"
  else
    echo "⚠️ Directory not found: $article_dir"
    continue
  fi
  
  IMAGES_DIR="$ARTICLE_PATH/images"
  ARTICLE_FILE="$ARTICLE_PATH/article.md"
  META_FILE="$ARTICLE_PATH/meta.json"
  
  if [[ ! -f "$ARTICLE_FILE" ]]; then
    echo "⚠️ No article.md in $ARTICLE_PATH"
    continue
  fi
  
  echo ""
  echo "── Processing: $ARTICLE_PATH ──"
  
  # Step 1: Search for images (if images don't already exist)
  IMAGE_COUNT=$(ls "$IMAGES_DIR"/*.jpg "$IMAGES_DIR"/*.jpeg "$IMAGES_DIR"/*.png 2>/dev/null | wc -l || echo 0)
  if [[ "$IMAGE_COUNT" -le 2 ]]; then
    echo "🔍 Searching images..."
    "$PYTHON" "$SCRIPT_DIR/collector/image_search.py" --article "$ARTICLE_FILE" --output "$IMAGES_DIR" 2>/dev/null || true
  else
    echo "✅ Images already exist ($IMAGE_COUNT files), skipping search"
  fi
  
  # Step 2: Embed images into article
  if [[ -d "$IMAGES_DIR" ]]; then
    echo "🖼️ Embedding images..."
    "$PYTHON" "$SCRIPT_DIR/collector/embed_images.py" --article "$ARTICLE_FILE" --images-dir "$IMAGES_DIR" --dry-run 2>/dev/null || true
  fi
  
  # Step 3: Publish to WeChat draft
  echo "📤 Publishing to WeChat..."
  DIGEST=""
  if [[ -f "$META_FILE" ]]; then
    DIGEST=$(python3 -c "import json; print(json.load(open('$META_FILE')).get('digest',''))" 2>/dev/null || echo "")
  fi
  cd "$SCRIPT_DIR/collector"
  PUBLISH_CMD=("$PYTHON" "$SCRIPT_DIR/collector/wechat_publish.py" --article "$ARTICLE_FILE" --images-dir "$IMAGES_DIR")
  if [[ -n "$DIGEST" ]]; then
    PUBLISH_CMD+=(--digest "$DIGEST")
  fi
  "${PUBLISH_CMD[@]}" 2>&1 || echo "  ⚠️ Publish step had issues"
  cd "$SCRIPT_DIR"
  
  COUNTER=$((COUNTER + 1))
done <<< "$ARTICLES"

# Clean up the queue file (add .done so we can debug if needed)
if [[ "$COUNTER" -gt 0 ]]; then
  mv "$PENDING_FILE" "${PENDING_FILE}.done"
  echo "✅ Published $COUNTER article(s), queue cleared"
else
  echo "⚠️ No articles were processed"
fi

echo ""
echo "=========================================="
echo "Publish phase complete"
echo "Log: $LOG_FILE"
echo "=========================================="
