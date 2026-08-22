#!/usr/bin/env bash
# run_wechat_stats.sh — 微信数据周报 Pipeline
# 每周日 08:10 由 cron 触发
# 拉取过去7天的阅读数据 + 汇总成报告

set -euo pipefail

cd "$(dirname "$0")"

REPORT_DIR="../wechat-drafts/weekly-stats"
mkdir -p "$REPORT_DIR"

REPORT_FILE="${REPORT_DIR}/weekly-wechat-stats-$(date +%Y-%m-%d).md"
TOPN=10
LOOKBACK=7

echo "📊 微信数据周报 Pipeline — $(date '+%Y-%m-%d %H:%M')"
echo "═══════════════════════════════════════════"
echo ""

# Step 1: 拉取阅读数据
echo "📋 Step 1/3: 拉取已发布文章 + 阅读数据..."
RAW_JSON=$(python3 hot_recommend.py --fetch --top "${TOPN}" --days "${LOOKBACK}" 2>/dev/null)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "❌ 拉取数据失败 (exit code: ${EXIT_CODE})"
    python3 hot_recommend.py --fetch --top "${TOPN}" --days "${LOOKBACK}" 2>&1 > "${REPORT_DIR}/error_$(date +%Y%m%d_%H%M%S).json"
    exit 1
fi

echo "${RAW_JSON}"
echo ""
echo "✅ Step 1/3 完成"
echo ""

# Step 2: 用 Python 生成 Markdown 周报
echo "📝 Step 2/3: 生成周报 Markdown..."

# Write JSON to temp file for Python processing
echo "${RAW_JSON}" > /tmp/wechat_stats_raw.json

# Write report via Python — pass REPORT_FILE as arg to avoid heredoc env issue
python3 /dev/stdin "${REPORT_FILE}" << 'PYEOF'
import json, sys
from datetime import datetime, timedelta

report_file = sys.argv[1] if len(sys.argv) > 1 else ""

with open("/tmp/wechat_stats_raw.json") as f:
    raw = f.read().strip()

articles = json.loads(raw)
if not articles:
    print("❌ 无文章数据")
    sys.exit(1)

today = datetime.now()
end_date = today.strftime("%Y-%m-%d")
start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")

lines_out = []
lines_out.append(f"# 📊 微信文章周报 ({start_date} ~ {end_date})")
lines_out.append("")
lines_out.append(f"> 生成时间：{today.strftime('%Y-%m-%d %H:%M')}")
lines_out.append("")
lines_out.append("---")
lines_out.append("")
lines_out.append("## 📈 本周阅读排行 Top 10")
lines_out.append("")
lines_out.append("| # | 文章标题 | 阅读量 |")
lines_out.append("|---|---------|-------|")

for i, art in enumerate(articles, 1):
    title = art.get("title", "未知")
    reads = art.get("reads", 0)
    lines_out.append(f"| {i} | {title} | {reads} |")

lines_out.append("")
lines_out.append("---")
lines_out.append("")

total_reads = sum(a.get("reads", 0) for a in articles)
avg_reads = total_reads / len(articles) if articles else 0
max_art = articles[0] if articles else {}
lines_out.append("## 📊 本周概览")
lines_out.append("")
lines_out.append(f"- **活跃文章数**: {len(articles)} 篇（近7天有阅读数据）")
lines_out.append(f"- **总阅读量**: {total_reads} 次")
lines_out.append(f"- **平均阅读**: {avg_reads:.0f} 次/篇")
lines_out.append(f"- **最高阅读**: [{max_art.get('title', '')}]({max_art.get('link', '#')}) — {max_art.get('reads', 0)} 次")
lines_out.append("")

lines_out.append("## 🔗 本周热门文章链接")
lines_out.append("")
for i, art in enumerate(articles[:5], 1):
    title = art.get("title", "未知")
    link = art.get("link", "#")
    reads = art.get("reads", 0)
    lines_out.append(f"{i}. [{title}]({link}) — {reads} 次阅读")
lines_out.append("")
lines_out.append("---")
lines_out.append("")
lines_out.append("> 🤖 由 Content Hub 自动生成")

report = "\n".join(lines_out)
print(report)

if report_file:
    with open(report_file, "w") as f:
        f.write(report)
        print(f"\n✅ 已写入文件: {report_file}", file=sys.stderr)
PYEOF

echo ""
echo "✅ Step 2/3 完成"
echo ""

# Step 3: 确认报告文件
echo "📁 Step 3/3: 检查报告文件..."
if [ -f "${REPORT_FILE}" ]; then
    echo "✅ 报告已生成: ${REPORT_FILE}"
    wc -l "${REPORT_FILE}"
    echo ""
    echo "📄 报告内容预览:"
    head -5 "${REPORT_FILE}"
else
    echo "❌ 报告文件未生成"
    exit 1
fi

echo ""
echo "📊 周报 Pipeline 完成 ✅"
