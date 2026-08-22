#!/usr/bin/env python3
"""
Framework Article Writer — 每周一篇框架/方法论文章.

区别于案例拆解（decompose_case_study.py）：
- 案例拆解 = 深度拆一个案例
- 框架文章 = 输出一个可复用的框架/模型/判断，用多个案例做论据支撑

区别于公众号文章（write_article.py）：
- 公众号文章 = 推理链驱动的传播性文章
- 框架文章 = 方法论输出的专业内容

启动时机：案例库积累至少 10 篇案例拆解后启用（预计 2-3 周内）。
当前只写 prompt 骨架，不跑 cron。

Usage (future):
  python3 framework_article.py --topic "流程简化审计框架"
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.llm import call_model
from lib.models import get_model

COLLECTOR_DIR = Path(__file__).parent
WORKSPACE_DIR = COLLECTOR_DIR.parent
CASES_DIR = WORKSPACE_DIR / "wechat-drafts" / "case-studies"
OUTPUT_DIR = WORKSPACE_DIR / "wechat-drafts" / "framework-articles"
FRAMEWORK_MODEL = get_model("framework")


# ════════════════════════════════════════════════════════════════
#  FRAMEWORK PROMPT
# ════════════════════════════════════════════════════════════════

FRAMEWORK_PROMPT = """你是一个企业AI咨询顾问。你的任务是写一篇框架/方法论文章，面向企业AI转型决策者。

## 文章定位

这类文章每周一篇，目标是输出**可复用的框架、模型、判断**，让读者看完能自己用。

不追求阅读量传播。追求的是：决策者看完觉得"这套方法论有道理，我想试试"。

## 文章结构

### 开头段（100-150字）
- 提出一个具体的场景问题（管理者每天都在面对的）
- 说清楚为什么现有方法解决不了
- 引出你的框架

### 核心框架（主段落）
- 框架名称 + 一句话定义
- 框架拆解（3-5个组成部分），每个部分包含：
  - 它是什么
  - 它解决什么问题
  - 操作指引（读者现在能做什么）
- 最好画一个简单的框架图示（文字描述就行，不需真图）

### 案例穿插（框架文章的独特之处）
- 从收到的案例素材中，每个框架环节搭配一个具体案例作为论据
- 案例引用要简短（200字以内一个），不要完整复述案例
- 和案例拆解文章的关系：案例拆解是"深潜"，框架文章是"俯瞰"
- 一条规则：如果没有具体案例佐证，这个框架就不写

### 使用场景
- 什么情况下用这个框架
- 谁应该用它
- 用完后能得到的产出是什么

### 注意事项
- 全文 2000-3000 字
- 禁止出现来源名称（Stanford、McKinsey、BCG等）
- 案例引用时用"一家物流公司""某金融机构"等脱敏表述
- 不要出现"在这个变革的时代""随着AI技术发展"等套话
- 句式简洁，一段不超过5句
- 可以用"我们"（代表咨询团队立场）

## 输出格式

```markdown
# 标题：一句话吸引目标读者

摘要: 一句话总结（不超过80字）

（正文，纯 Markdown）
```

标题格式示例：
- 「先用这个方法给你的企业做一次'流程混乱度'审计」
- 「AI项目失败不是技术问题，是这个框架你没填对」
- 「你们花三个月选模型，不如花三天填一张这张表」

## 框架选题库（逐步扩充）

初期选题（案例积累 2-3 周后按顺序出）：
1. 流程简化审计框架——从哪个流程入手做AI最划算
2. 人机分工原则模型——什么该给AI，什么该给人
3. AI落地 ROI 测算框架——算清楚收益账，不用拍脑袋
4. 知识转移四步法——怎么让内部团队接得住AI
5. 高管参与度评估模型——你的项目死在哪个层级
6. 流程压缩 vs 技术替代判断表——什么情况先做流程清理
"""


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════

def load_case_studies() -> list[dict]:
    """Load all written case study articles for reference."""
    cases = []
    if not CASES_DIR.exists():
        return cases
    for d in sorted(CASES_DIR.iterdir()):
        if not d.is_dir():
            continue
        article_path = d / "article.md"
        meta_path = d / "meta.json"
        if article_path.exists():
            meta = {}
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            cases.append({
                "title": d.name,
                "article": article_path.read_text(encoding="utf-8")[:3000],
                "digest": meta.get("digest", ""),
                "date": meta.get("date", ""),
                "materials_count": meta.get("materials_count", 0),
            })
    return cases


def extract_digest(text: str) -> str:
    m = re.search(r"^摘要[：:]\s*(.+)", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def strip_digest(text: str) -> str:
    return re.sub(r"^摘要[：:].*\n?", "", text, flags=re.MULTILINE).strip()


# ════════════════════════════════════════════════════════════════
#  Write
# ════════════════════════════════════════════════════════════════

def write_framework(
    topic: str,
    case_studies: list[dict],
    dry_run: bool = False,
) -> Optional[str]:
    """Write a framework article."""
    print(f"\n{'='*60}")
    print(f"📐 Framework Article — '{topic}'")
    print(f"{'='*60}")

    # Build case context
    case_context = "\n\n".join(
        f"=== 案例 {i}: {c['title']} ===\n{c['article']}"
        for i, c in enumerate(case_studies, 1)
    )

    if not case_context:
        print("  ⚠️ No case studies available for reference")

    now = __import__("datetime").datetime.now().strftime("%Y-%m-%d")

    user_prompt = f"""请写一篇框架文章，主题是：{topic}

当前日期：{now}

可参考的案例素材（已在之前的案例拆解中完整写过，这里只提供概要）：

{case_context or "（暂无案例素材，仅基于分析方法论本身输出）"}

请按 FRAMEWORK_PROMPT 要求输出完整文章。"""

    print(f"\n5️⃣  Calling LLM for framework writing ({FRAMEWORK_MODEL})...")
    response = call_model(
        [{"role": "system", "content": FRAMEWORK_PROMPT},
         {"role": "user", "content": user_prompt}],
        temperature=0.7, max_tokens=None, model=FRAMEWORK_MODEL,
    )
    if not response:
        print("  ❌ Model returned no response")
        return None

    digest = extract_digest(response)
    article_text = strip_digest(response)

    if len(article_text) < 500:
        print(f"  ❌ Article too short ({len(article_text)} chars)")
        return None

    print(f"  ✓ Article generated: {len(article_text)} chars")

    title_match = re.search(r'^#\s+(.+)$', article_text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else topic
    title_slug = re.sub(r'[^\w\u4e00-\u9fff-]', '-', title)[:60].strip('-').lower()
    output_dir = OUTPUT_DIR / f"{now}-{title_slug}"

    if dry_run:
        print(f"\n  [Dry run] Would write to: {output_dir}")
        print(f"\n  Preview:\n{article_text[:800]}...")
        return str(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    article_path = output_dir / "article.md"
    article_path.write_text(article_text, encoding="utf-8")

    if digest:
        meta = {"digest": digest, "topic": topic, "date": now, "cases_referenced": len(case_studies)}
        (output_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    print(f"\n  ✅ Output: {output_dir}/")
    print(f"     📄 article.md")
    print(f"     📋 References: {len(case_studies)} case studies")

    return str(output_dir)


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Framework article writer")
    parser.add_argument("--topic", required=True, help="Framework topic")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    topic = args.topic.strip()

    print(f"\n📐 Framework Article Writer")
    print(f"   Topic: {topic}")

    cases = load_case_studies()
    print(f"   Available case studies: {len(cases)}")

    out = write_framework(topic, cases, dry_run=args.dry_run)
    if not out:
        print("  ❌ Failed")
        return 1

    print(f"\n{'='*60}")
    print("✅ Done!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
