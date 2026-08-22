#!/usr/bin/env python3
"""
book_research.py — 《企业AI落地》书稿增量研究执行器

将每日海外/AI 相关研究素材，按《OpenClaw 增量研究执行提示词 v1.0》
转化为结构化的「书稿增量素材卡」，以幂等方式写入唯一输出总库。

唯一允许写入的飞书文档（纯输出容器，禁止覆盖）:
  CFE8dplCmoXwTpxNpdwcLRtznCh

使用方式:
  python3 book_research.py                 # 今天，完整跑
  python3 book_research.py --date 2026-08-01
  python3 book_research.py --dry-run        # 只读校验，不写任何东西
  python3 book_research.py --max-cards 10
  python3 book_research.py --no-fetch       # 跳过飞书读取（本地调试）
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from lib.llm import call_model
from lib.materials import get_all_collected_urls, read_material_content

# ── 路径 ──
WORKSPACE_DIR = Path(__file__).resolve().parent          # .../workspace
BOOK_DIR = WORKSPACE_DIR / "research" / "book_incremental"
STATE_FILE = BOOK_DIR / "state.json"                     # 去重持久化
TMP_DIR = Path("/tmp")

CONFIG = {}
try:
    with open(BOOK_DIR / "CONFIG.json", encoding="utf-8") as f:
        CONFIG = json.load(f)
except Exception as e:
    print(f"⚠️ 无法加载 CONFIG.json: {e}")
    sys.exit(1)

LARK_CLI = CONFIG.get("lark_cli", "lark-cli")
API_VER = CONFIG.get("feishu_api_ver", "v1")
IDENTITY = CONFIG.get("identity", "user")
OUTPUT_DOC = CONFIG["output_doc"]
CTRL = CONFIG["control_docs"]
GAP_HEADINGS = CONFIG["evidence_gap_sheet_headings"]
MAX_CARDS = CONFIG.get("max_cards_per_day", 10)
MAX_PRIORITY = CONFIG.get("max_priority_read", 5)

# ── 核心命题（相关性最高标准）──
CORE_PROBLEM = "模型能力→任务能力→流程能力→组织能力→经营能力"

# 07 待补证据总表 doc token
GAP_DOC = CTRL["07_待补证据总表"]


# ════════════════════════════════════════════════════════════════
#  飞书读取（lark-cli，User Access Token 默认身份）
# ════════════════════════════════════════════════════════════════

def _run_lark(args: list, timeout: int = 60) -> str:
    """Run lark-cli with user identity; return stdout text."""
    cmd = args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"lark-cli error: {r.stderr[:300]}\nstdout: {r.stdout[:200]}")
    return r.stdout


def doc_outline(doc_token: str) -> list[dict]:
    """Fetch doc outline, return list of {id, heading, level, text}."""
    out = _run_lark([
        LARK_CLI, "docs", "+fetch",
        "--api-version", API_VER, "--as", IDENTITY,
        "--doc", doc_token, "--scope", "outline",
    ])
    data = json.loads(out)
    content = data["data"]["document"]["content"]
    tokens = re.findall(r'<(h[1-6])\s+id="([^"]+)"[^>]*>(.*?)</\1>', content)
    return [{"id": bid, "level": int(lv[1]), "text": re.sub(r"<[^>]+>", "", txt).strip()}
            for lv, bid, txt in tokens]


def doc_section(doc_token: str, block_id: str) -> str:
    """Fetch a single section (block subtree) as markdown-ish text."""
    out = _run_lark([
        LARK_CLI, "docs", "+fetch",
        "--api-version", API_VER, "--as", IDENTITY,
        "--doc", doc_token, "--scope", "section",
        "--start-block-id", block_id,
    ])
    data = json.loads(out)
    return data["data"]["document"]["content"]


def read_control(required_headings: list[tuple[str, str]]) -> dict[str, str]:
    """Read control docs by heading. Returns {key: text}."""
    result = {}
    for key, doc_token, heading in required_headings:
        try:
            outline = doc_outline(doc_token)
            match = next((o for o in outline if o["text"] == heading or heading in o["text"]), None)
            if not match:
                result[key] = f"(未找到 heading: {heading})"
                continue
            result[key] = doc_section(doc_token, match["id"])
        except Exception as e:
            result[key] = f"(读取失败: {e})"
    return result


def find_section_id(doc_token: str, heading_substr: str) -> Optional[str]:
    """Find block id whose heading text contains heading_substr."""
    outline = doc_outline(doc_token)
    for o in outline:
        if heading_substr in o["text"] or o["text"] in heading_substr:
            return o["id"]
    return None


def keyword_exists(doc_token: str, keyword: str, scope: str = "full") -> bool:
    """Check if keyword exists in the output doc (idempotency pre-check)."""
    try:
        out = _run_lark([
            LARK_CLI, "docs", "+fetch",
            "--api-version", API_VER, "--as", IDENTITY,
            "--doc", doc_token, "--scope", "keyword", "--keyword", keyword,
        ])
        data = json.loads(out)
        content = data["data"]["document"]["content"]
        return keyword in content
    except Exception:
        return False


def read_output_doc() -> str:
    """Read full output doc content (for post-write verification)."""
    doc_token = OUTPUT_DOC
    out = _run_lark([
        LARK_CLI, "docs", "+fetch",
        "--api-version", API_VER, "--as", IDENTITY,
        "--doc", doc_token, "--scope", "full",
    ])
    return json.loads(out)["data"]["document"]["content"]


# ════════════════════════════════════════════════════════════════
#  素材读取（复用现有采集体系）
# ════════════════════════════════════════════════════════════════

def load_new_materials() -> list[dict]:
    """Load collected materials, filter already-processed, pre-score."""
    all_materials = get_all_collected_urls()
    processed = _load_state()["processed_urls"]
    new = []
    for m in all_materials:
        url = m.get("url", "").strip().rstrip("/")
        if not url or url in processed:
            continue
        content = read_material_content(url)
        if not content:
            continue
        new.append({"url": url, "content": content[:8000]})
    return new


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed_urls": [], "runs": {}}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_url(url: str) -> str:
    """Normalize URL: strip tracking params & fragments."""
    url = re.sub(r"[?#](utm_[^&]*|gclid[^&]*|fbclid[^&]*|ref[^&]*).*?$", "", url)
    url = re.sub(r"[?#].*$", "", url)
    return url.strip().rstrip("/")


# ════════════════════════════════════════════════════════════════
#  LLM 化
# ════════════════════════════════════════════════════════════════

SYSTEM_BASE = (
    "你是《企业AI落地》一书的长期海外研究助手。你的职责是：\n"
    "1. 找到可靠的新素材；\n"
    "2. 判断素材与书哪一章最相关；\n"
    "3. 判断能否补充某个已编号证据缺口 G-xx-xx；\n"
    "4. 区分来源事实、来源观点与你推导的候选观点；\n"
    "5. 记录支持证据、挑战证据和适用边界。\n"
    "你只是研究助手，不是作者：不得把自己的推导写成作者观点，"
    "不得创建正式观点编号 V，不得创建正式证据编号 E。\n"
)


def llm_judge_chapter(url: str, content: str, chapter_map: str) -> str:
    """Ask LLM which chapter the material belongs to (primary + up to 2 related)."""
    prompt = (
        f"全书核心命题（相关性最高标准）：{CORE_PROBLEM}\n\n"
        f"章节地图：\n{chapter_map}\n\n"
        f"请判断以下素材最属于哪一章（主归属），以及是否需要关联章节（最多2个）。\n"
        f"只输出 JSON：{{\"primary\": \"08\", \"related\": [\"07\", \"09\"]}}，章号用两位数（00-18）。\n\n"
        f"URL: {url}\n\n素材内容：\n{content[:4000]}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_BASE},
        {"role": "user", "content": prompt},
    ]
    try:
        resp = call_model(messages, temperature=0.2, model="deepseek-v4-flash")
        m = re.search(r"\{.*\}", resp or "", re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        primary = str(data.get("primary", "00")).zfill(2)
        related = [str(x).zfill(2) for x in data.get("related", [])[:2]]
        return primary, related
    except Exception:
        return "00", []


def llm_generate_card(url: str, content: str, primary: str, related: list,
                      gap_text: str, control_ctx: str) -> str:
    """Generate one card per the prompt's section-9 markdown template."""
    prompt = f"""根据以下素材，输出一张书稿增量素材卡。严格按给定 Markdown 模板。

URL: {url}

素材内容：
{content[:8000]}

主归属章节：第{int(primary)}章
关联章节：{', '.join(f'第{int(x)}章' for x in related) if related else '无'}

该章待补证据缺口（只读参考，不要勾选）：
{gap_text[:3000]}

写作与编辑标准（证据等级/来源观点归属）：
{control_ctx[:2000]}

输出模板（严格遵照）：
### OC-{datetime.date.today().strftime('%Y%m%d')}-XX｜素材标题

- **原始链接：** {url}
- **来源机构：**
- **发布时间：**
- **来源类型：** A / B / C
- **样本量或研究对象：**
- **来源等级：** A / B / C
- **主归属章节：** 第{int(primary)}章
- **关联章节：**
- **命中证据缺口：** G-xx-xx / 无明确命中
- **与核心命题关系：** 支持 / 挑战 / 补充边界 / 新线索
- **优先级：** P1 / P2 / P3
- **人工审核：** 待审核

#### 核心发现

#### 关键数据

#### 来源观点

#### 与书稿的关系

#### 可用于书稿的位置

#### OpenClaw 候选观点

> AI 推导，未经作者确认，不属于正式作者观点。

#### 适用范围、局限与交叉验证
"""
    messages = [
        {"role": "system", "content": SYSTEM_BASE},
        {"role": "user", "content": prompt},
    ]
    return call_model(messages, temperature=0.5, model="deepseek-v4-flash")


def load_control_context() -> dict:
    """Load core problem, main line, chapter map, evidence rules, terms (cached per day)."""
    return read_control([
        ("core_problem", CTRL["00_全书总控大纲"], "全书核心命题"),
        ("main_line", CTRL["00_全书总控大纲"], "全书核心主线"),
        ("chapter_map", CTRL["00_全书总控大纲"], "二、全书章节地图"),
        ("evidence_grade", CTRL["04_写作与编辑标准"], "证据等级"),
        ("evidence_use", CTRL["04_写作与编辑标准"], "证据使用原则"),
        ("terms", CTRL["02_概念与术语表"], "四、需要作者统一口径的高频表述"),
    ])


def load_gap_section(chapter: str) -> str:
    """Read the evidence-gap section for a chapter from the 07 sheet."""
    heading = GAP_HEADINGS.get(chapter, GAP_HEADINGS["00"])
    bid = find_section_id(GAP_DOC, heading)
    if not bid:
        return "(未找到缺口区块)"
    return doc_section(GAP_DOC, bid)


# ════════════════════════════════════════════════════════════════
#  输出格式化 & 幂等写入
# ════════════════════════════════════════════════════════════════

def build_daily_block(date_str: str, cards: list[dict], stats: dict) -> str:
    """Assemble the full daily markdown block (section 9 template)."""
    run = "RUN-" + date_str.replace("-", "")
    lines = []
    lines.append(f"## {date_str}｜{run}｜每日增量")
    lines.append("")
    lines.append(f"- 运行时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"- 提纲版本：v1.0")
    lines.append(f"- 扫描素材数：{stats.get('scanned', 0)}")
    lines.append(f"- 去重淘汰数：{stats.get('dedup', 0)}")
    lines.append(f"- 低相关或低质量淘汰数：{stats.get('lowquality', 0)}")
    lines.append(f"- 最终保留数：{len(cards)}")
    lines.append(f"- 运行状态：{'成功' if cards else '成功（无合格新增）'}")
    lines.append("")
    lines.append("### 今日优先阅读")
    lines.append("")
    lines.append("| 编号 | 优先级 | 素材 | 主归属章节 | 命中缺口 | 关系 | 人工审核 |")
    lines.append("|---|---|---|---|---|---|---|")

    if not cards:
        lines.append("")
        lines.append("今日无合格新增。")
        lines.append("")
        lines.append("### 本次淘汰与异常说明")
        lines.append("")
        lines.append("- 重复素材：统计见上")
        lines.append("- 无原始来源：统计见上")
        lines.append("- 低相关素材：统计见上")
        lines.append("- 抓取失败或待复查：统计见上")
        lines.append("")
        return "\n".join(lines)

    for i, c in enumerate(cards[:MAX_PRIORITY], 1):
        lines.append(f"| {c['编号']} | {c['优先级']} | {c['标题']} | 第{int(c['章节'])}章 | {c['缺口']} | {c['关系']} | 待审核 |")

    lines.append("")
    for c in cards:
        lines.append("")
        lines.append(c["text"])

    lines.append("")
    lines.append("### 本次淘汰与异常说明")
    lines.append("")
    lines.append(f"- 重复素材：{stats.get('dedup', 0)}")
    lines.append(f"- 无原始来源：{stats.get('nourl', 0)}")
    lines.append(f"- 低相关素材：{stats.get('lowquality', 0)}")
    lines.append(f"- 抓取失败或待复查：{stats.get('fail', 0)}")
    lines.append("")
    return "\n".join(lines)


def write_to_output(block_md: str, date_str: str, dry_run: bool = False) -> bool:
    """Idempotent write: append if RUN absent, else replace_range by title."""
    run = "RUN-" + date_str.replace("-", "")
    # lark-cli --content @file 要求相对当前目录路径，故放到 workspace 内 runs/ 目录
    runs_dir = WORKSPACE_DIR / "research" / "book_incremental" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = runs_dir / f"openclaw-book-research-{date_str}.md"
    tmp_file.write_text(block_md, encoding="utf-8")
    rel_path = f"./research/book_incremental/runs/openclaw-book-research-{date_str}.md"

    if dry_run:
        print(f"  [dry-run] 写入文件: {tmp_file}")
        return True

    title = f"## {date_str}｜{run}｜每日增量"

    if keyword_exists(OUTPUT_DOC, run):
        # retry: str_replace by title
        print(f"  ⚠️ 当天运行编号已存在（{run}），执行 str_replace 替换当天区块")
        cmd = [
            LARK_CLI, "docs", "+update",
            "--api-version", API_VER, "--as", IDENTITY,
            "--doc", OUTPUT_DOC,
            "--command", "str_replace",
            "--pattern", title,
            "--content", f"@{rel_path}",
            "--doc-format", "markdown",
        ]
    else:
        print(f"  追加当天区块（{run}）")
        cmd = [
            LARK_CLI, "docs", "+update",
            "--api-version", API_VER, "--as", IDENTITY,
            "--doc", OUTPUT_DOC,
            "--command", "append",
            "--content", f"@{rel_path}",
            "--doc-format", "markdown",
        ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"  ❌ 写入失败: {r.stderr[:300]}")
        return False
    print(f"  ✅ 写入完成: {r.stdout[:200]}")
    return True


def verify_write(date_str: str) -> bool:
    """Post-write read-back: RUN block exists and only once."""
    run = "RUN-" + date_str.replace("-", "")
    content = read_output_doc()
    count = content.count(run)
    print(f"  回读确认: {run} 出现 {count} 次")
    return count == 1


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="《企业AI落地》每日增量研究")
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-cards", type=int, default=MAX_CARDS)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--no-fei-shu", action="store_true")
    args = parser.parse_args()

    date_str = args.date or datetime.date.today().isoformat()
    print(f"\n{'=' * 60}\n📚 书稿增量研究 — {date_str}\n{'=' * 60}")

    stats = {"scanned": 0, "dedup": 0, "lowquality": 0, "nourl": 0, "fail": 0}

    # 1. 读取控制文档
    print("\n1️⃣ 读取全书方向与证据规则...")
    ctx = {}
    if not args.no_fetch:
        ctx = load_control_context()
        for k, v in ctx.items():
            print(f"  ✓ {k}: {len(v)} chars")

    # 2. 素材
    print("\n2️⃣ 读取采集素材...")
    materials = load_new_materials() if not args.no_fetch else []
    stats["scanned"] = len(materials)
    print(f"  {len(materials)} 条未处理素材")

    # 3. 逐条：初判章节 → 读缺口 → 生成卡片
    print("\n3️⃣ 生成素材卡...")
    cards = []
    for i, m in enumerate(materials):
        if len(cards) >= args.max_cards:
            break
        url, content = m["url"], m["content"]
        stats["scanned"] = len(materials)
        try:
            primary, related = llm_judge_chapter(url, content, ctx.get("chapter_map", ""))
            gap_text = ""
            if not args.no_fetch:
                gap_text = load_gap_section(primary)
            card = llm_generate_card(url, content, primary, related, gap_text,
                                     (ctx.get("evidence_grade", "") + "\n" + ctx.get("evidence_use", "")))
            if not card or "### OC-" not in card:
                stats["lowquality"] += 1
                continue
            title_match = re.search(r"^\s*#+\s+(.+)$", card, re.M)
            title_match = re.search(r"^\s*#+\s*(.+)$", card, re.M)
            # 先整体替换卡体内陈旧 OC 编号为当天正确编号（含标题行）
            oc_num = f"OC-{date_str.replace('-','')}-{i+1:02d}"
            card_clean = re.sub(r"OC-\d{8}-\d{2}", oc_num, card)
            title_raw = title_match.group(1).strip() if title_match else f"素材{i+1}"
            # 若标题本身形如 "OC-xxx｜标题"，去掉编号前缀避免重复
            title = re.sub(r"^OC-\d{8}-\d{2}\s*[｜|]\s*", "", title_raw)
            prio_match = re.search(r"\*\*优先级：\*\*\s*([P0-9]+)", card)
            gap_match = re.search(r"\*\*命中证据缺口：\*\*\s*(\S+)", card)
            rel_match = re.search(r"\*\*与核心命题关系：\*\*\s*(\S+)", card)
            cards.append({
                "编号": f"OC-{date_str.replace('-','')}-{i+1:02d}",
                "标题": title,
                "章节": primary,
                "缺口": gap_match.group(1) if gap_match else "无明确命中",
                "关系": rel_match.group(1) if rel_match else "待定",
                "优先级": prio_match.group(1) if prio_match else "P3",
                "text": card_clean,
                "url": _norm_url(url),
            })
            print(f"  ✓ Card {len(cards)}: 第{int(primary)}章 | {url[:60]}")
        except Exception as e:
            stats["fail"] += 1
            print(f"  ⚠️ 处理失败: {e}")

    # 4. 组装日报 & 写入
    print("\n4️⃣ 组装日报区块...")
    block = build_daily_block(date_str, cards, stats)

    if not args.no_fei_shu:
        ok = write_to_output(block, date_str, dry_run=args.dry_run)
        if ok and not args.dry_run:
            print("\n5️⃣ 回读确认...")
            verify_write(date_str)

    # 5. 持久化去重状态
    if cards and not args.dry_run and not args.no_fei_shu:
        state = _load_state()
        for c in cards:
            state["processed_urls"].append(c["url"])
        state.setdefault("runs", {})[date_str] = {
            "count": len(cards), "urls": [c["url"] for c in cards]
        }
        _save_state(state)
        print(f"  💾 已记录 {len(cards)} 条已处理 URL")

    print(f"\n{'=' * 60}\n✅ 完成：{len(cards)} 张卡片\n{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
