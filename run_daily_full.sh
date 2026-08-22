#!/usr/bin/env bash
set -euo pipefail

# ==========================================
# 每日全管线：公众号文章 + Medium 英文文章（同一数据源）
# ==========================================
# 逻辑：
#   1. 跑公众号文章全流程 →
#      platform_cli.py run article-daily 选材 → write_article.py 写文
#   2. 用同一批选材素材，write_medium_article.py 写英文版 Medium 观点
#      （非中译英，而是同一素材源的英文独立观点）
# ==========================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ -x "$DEFAULT_PYTHON" ]]; then
  PYTHON="${PYTHON:-$DEFAULT_PYTHON}"
else
  PYTHON="${PYTHON:-python3}"
fi
DATE="${DATE:-$(date +%Y-%m-%d)}"
LOG_DIR="${SCRIPT_DIR}/logs"
LOG_FILE="${LOG_DIR}/daily_full_${DATE}.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "📰 每日全管线 — $DATE"
echo "=========================================="

# ─── Step 1: 公众号文章 ───
echo ""
echo "═══ Step 1: 公众号文章（中文）═══"
echo ""

"$PYTHON" "$SCRIPT_DIR/platform_cli.py" run article-daily --date "$DATE" --workspace-dir "$SCRIPT_DIR"

echo ""
echo "  ✅ 公众号文章完成"
echo ""

# ─── Step 2: Medium 英文文章（同一数据源）───
echo ""
echo "═══ Step 2: Medium 文章（英文，同一数据源）═══"
echo ""

# 找出 article-daily pipeline 选择的素材文件，合并两篇文章的素材
PLATFORM_DIR="$SCRIPT_DIR/platform/datasets/$DATE"
MERGED_MATERIALS="$PLATFORM_DIR/medium_materials.json"
MATERIALS_FILE=""

# 合并 article_materials_1.json + article_materials_2.json（如果存在）
if [[ -f "$PLATFORM_DIR/article_materials_1.json" ]]; then
  "$PYTHON" -c "
import json
merged = []
for f in ['article_materials_1.json', 'article_materials_2.json']:
    fp = '$PLATFORM_DIR/' + f
    try:
        data = json.load(open(fp))
        if isinstance(data, list):
            merged.extend(data)
            print(f'  ✓ {f}: {len(data)} materials')
    except: pass
# 去重（按 url）
seen = set()
deduped = []
for m in merged:
    u = (m.get('url','') or '').rstrip('/')
    if u and u not in seen:
        seen.add(u)
        deduped.append(m)
print(f'  ✅ 合并后共 {len(deduped)} 条素材（去重后）')
json.dump(deduped, open('$MERGED_MATERIALS', 'w'), ensure_ascii=False)
"
  MATERIALS_FILE="$MERGED_MATERIALS"
elif [[ -f "$PLATFORM_DIR/article_materials.json" ]]; then
  MATERIALS_FILE="$PLATFORM_DIR/article_materials.json"
  echo "  使用素材: article_materials.json（fallback）"
fi

if [[ -n "$MATERIALS_FILE" ]]; then
  echo "  素材文件: $MATERIALS_FILE"
  echo ""
  if "$PYTHON" "$SCRIPT_DIR/write_medium_article.py" --date "$DATE" --materials "$MATERIALS_FILE"; then
    echo ""
    echo "  ✅ Medium 文章完成"
  else
    MEDIUM_EXIT=$?
    echo ""
    echo "  ⚠️ Medium 文章失败 (exit=$MEDIUM_EXIT)"
  fi
else
  echo "  ⚠️ 未找到 article-daily 的素材文件，跳过 Medium 文章"
  echo "  查找路径: $PLATFORM_DIR/"
  ls -la "$PLATFORM_DIR/" 2>/dev/null || echo "  (目录不存在)"
fi

echo ""
echo "=========================================="
echo "✅ 每日全管线完成 — $DATE"
echo "  日志: $LOG_FILE"
echo "=========================================="
