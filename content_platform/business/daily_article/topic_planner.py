"""Topic planner: pick N distinct clusters, avoiding semantic topic repeat."""

import json
import re
import sys
from pathlib import Path
from typing import Optional
from content_platform.recent_topics import recent_source_urls, recent_topics

WORKSPACE_DIR = Path(__file__).resolve().parents[3]


def _load_recent_topics(days: int = 7) -> list[dict]:
    """Load article-only topic history; case topics live in their own ledger."""
    return recent_topics(WORKSPACE_DIR, "article", days=days)


def _llm_judge_topic_similar(
    new_title: str,
    new_digest: Optional[str],
    recent_topics: list[dict],
    model: str = "deepseek-flash",
) -> bool:
    """Ask LLM whether new_title+new_digest is too similar to any recent topic.

    Returns True if similar (should be filtered out), False otherwise.
    """
    if not recent_topics:
        return False

    # Build recent topics summary
    recent_lines = []
    for i, t in enumerate(recent_topics, 1):
        recent_lines.append(f'{i}. 标题: {t.get("title", "")}')
        digest = t.get("digest", "")
        if digest:
            recent_lines.append(f'   摘要: {digest}')
    recent_text = "\n".join(recent_lines)

    prompt = f"""你是一个内容选题判断助手。判断下面的新选题是否与近期的已发布文章主题高度相似。

=== 近期文章主题（7天内） ===
{recent_text}

=== 新选题 ===
标题: {new_title}
摘要: {new_digest or ""}

请比较新选题相对每篇近期文章带来的独立增量。
只有在同一素材/核心论点被换标题复述，且没有新增证据、实施机制、决策含义或适用边界时，才判为 REPEAT。
主题属于同一大方向本身不构成重复；只要至少有一项实质独立增量，就判为 INCREMENT。

如果没有独立增量，回复「REPEAT」。
如果有独立增量或主题不同，回复「INCREMENT」。

只回复 REPEAT 或 INCREMENT。"""

    # Call LLM via lib.llm.call_model
    sys.path.insert(0, str(WORKSPACE_DIR))
    from lib.llm import call_model

    messages = [
        {"role": "user", "content": prompt},
    ]
    resp = call_model(messages, model=model, temperature=0.1, max_tokens=50)
    # Defensive: call_model may return None on empty content / retry exhaustion.
    # Never call .strip() on a None — return a conservative verdict instead.
    if resp is None or not resp.strip():
        print("  ⚠️ LLM topic check returned empty/None; treating as DISTINCT "
              "(URL/title dedup already applied)")
        return False
    cleaned = re.sub(r"[^A-Z]", "", resp.strip().upper())
    return cleaned == "REPEAT"


def _load_recent_source_urls(recent_topics: list[dict], days: int = 7) -> set[str]:
    """Collect cross-format source URLs while keeping topic histories separate."""
    recent_urls = recent_source_urls(WORKSPACE_DIR, days=days)
    for t in recent_topics:
        for url in t.get("source_urls", []):
            recent_urls.add(url.rstrip("/"))
    return recent_urls


def pick_top_n_clusters(
    candidates: list[dict],
    n: int = 2,
    topic_diversity_days: int = 7,
    model: str = "deepseek-flash",
) -> list[dict]:
    """Pick the top N cluster leaders, filtering out topics similar to recent articles.

    Applies three layers of dedup:
      1. URL-level: skip candidates whose canonical_url appeared in recent articles.
      2. Title-level: skip exact title matches.
      3. Semantic-level: LLM judges whether the topic is substantially the same.

    Args:
        candidates: Full list of candidate materials (from article_pool).
        n: Number of clusters to select.
        topic_diversity_days: Days of history to check for topic similarity.
        model: LLM model for similarity judgment.

    Returns:
        List of selected materials (1 per cluster), ordered by editorial_fit_score desc.
    """
    if not candidates:
        return []

    recent_topics = _load_recent_topics(days=topic_diversity_days)
    recent_titles = {t.get("title", "") for t in recent_topics}
    recent_source_url_set = _load_recent_source_urls(recent_topics, days=topic_diversity_days)

    # Sort candidates by editorial_fit_score DESC
    sorted_candidates = sorted(
        candidates,
        key=lambda m: (m.get("editorial_fit_score", 0.0), m.get("novelty_score", 0.0)),
        reverse=True,
    )

    # ── 段位重排：对 top-K 候选做决策者洞察排序 ──
    # 素材分高（执行细节丰富）≠ 段位高（面向老板的普适洞察）。
    # 先按 editorial_fit 取 top-K 个不同 cluster，再用 LLM 按 executive
    # value 重排，让"能升维"的选题优先于"细节最丰富"的选题。
    top_k = 8
    top_candidates: list[dict] = []
    seen: set[str] = set()
    for m in sorted_candidates:
        cluster_id = m.get("dedup", {}).get("cluster_id", "")
        key = cluster_id or (m.get("canonical_url", "") or m.get("url", ""))
        if key and key in seen:
            continue
        seen.add(key)
        top_candidates.append(m)
        if len(top_candidates) >= top_k:
            break

    if len(top_candidates) > n:
        exec_scores = _llm_rank_executive_value(top_candidates, model=model)
        if exec_scores:
            top_candidates.sort(
                key=lambda m: exec_scores.get(m.get("canonical_url", "") or m.get("url", ""), -1.0),
                reverse=True,
            )
            print(f"  🎯 Executive-value ranking applied to {len(top_candidates)} candidates")
            for m in top_candidates:
                url = m.get("canonical_url", "") or m.get("url", "")
                print(f"    • {exec_scores.get(url, 0.0):.1f} — {m.get('title', '')[:55]}")
        else:
            print("  ⚠️ Executive-value ranking unavailable; using editorial_fit order")

    # Semantic rejections in the first shortlist should not end selection when
    # good candidates remain. Keep the LLM check bounded to avoid a large burst
    # of per-candidate calls on the cross-day pool.
    shortlisted_ids = {id(m) for m in top_candidates}
    remaining = [m for m in sorted_candidates if id(m) not in shortlisted_ids]
    candidates_to_check = (top_candidates + remaining)[:max(top_k + n * 2, n)]

    selected: list[dict] = []
    seen_clusters: set[str] = set()

    for m in candidates_to_check:
        if len(selected) >= n:
            break

        cluster_id = m.get("dedup", {}).get("cluster_id", "")
        if cluster_id and cluster_id in seen_clusters:
            continue
        seen_clusters.add(cluster_id)

        title = m.get("title", "")
        digest = m.get("summary", "")
        candidate_url = (m.get("canonical_url", "") or m.get("url", "")).rstrip("/")

        # ── Layer 1: URL-level dedup ──
        if recent_source_url_set and candidate_url.lower() in recent_source_url_set:
            print(f"  ⏭️  Skipping (same source URL used recently): {title[:50]}")
            continue

        # ── Layer 2: exact title match ──
        if title in recent_titles:
            continue

        # ── Layer 3: semantic similarity check ──
        if recent_topics:
            try:
                similar = _llm_judge_topic_similar(title, digest, recent_topics, model=model)
                if similar:
                    print(f"  ⏭️  Skipping (topic too similar to recent): {title[:50]}")
                    continue
            except Exception as e:
                print(f"  ⚠️ LLM topic check failed for '{title[:30]}': {e}")
                # On failure, still include if URL-level dedup passed

        selected.append(m)

    print(f"  Selected {len(selected)}/{n} clusters from {len(candidates)} candidates")
    for s in selected:
        cluster = s.get("dedup", {}).get("cluster_id", "?")
        print(f"    - [{cluster}] {s.get('title', '')[:60]}")

    return selected


def _llm_rank_executive_value(candidates: list[dict], model: str = "deepseek-flash") -> dict[str, float]:
    """Ask the LLM to score the executive-insight (段位) value of candidates.

    Returns {url: executive_score(0-10)}. A high score means the candidate can
    support a high-caliber article for enterprise-AI decision makers — one
    that extracts a transferable mechanism/judgment (not just case storytelling).

    Used to re-rank the top candidates so the strongest editorial_fit material
    doesn't automatically win: 段位 beats 细节.
    """
    if not candidates:
        return {}

    lines = []
    for i, m in enumerate(candidates, 1):
        url = m.get("canonical_url", "") or m.get("url", "")
        lines.append(
            f"{i}. 标题: {m.get('title', '')}\n"
            f"   来源: {m.get('source_name', '')} ({m.get('source_type', '')})\n"
            f"   摘要: {(m.get('summary', '') or '')[:300]}\n"
            f"   URL: {url}"
        )
    candidates_text = "\n\n".join(lines)

    prompt = f"""你是一位面向「企业AI落地决策者」（老板/高管/业务负责人）的内容主编。下面的候选素材将用来写公众号文章，请评估每个候选的「段位」——即它能支撑的这篇文章的高度。

**本号选题主轴（唯一）：企业AI怎么落地。** 老板关心的是"我的企业怎么把AI落下去"：该不该投、先投哪个环节、业务流程怎么变、组织/绩效/人才怎么配套、ROI怎么度量、有什么代价和坑。**AI行业的技术/产业趋势（合成数据、模型能力、算力、评测基准）不是本号主轴，除非直接落到"企业怎么落地"。**

高段位文章的判断标准：
1. 能提炼出**可迁移的落地机制/规律**：脱离具体案例、对同类企业普遍成立的落地判断（例如"AI项目失败的主因是组织而非技术""业务流程不重构，提效只会局部化""绩效制度必须跟着业务流程变"）
2. 有明确的**落地决策含义**：能回答决策者的问题——该不该投、先投哪、组织怎么配、ROI怎么量、什么环节必须留人
3. 案例/数据只是**证据**，不是文章主角
4. 低段位文章：停留在"某个case讲了什么""某工具怎么搭""某公司做了什么"；**或主题是AI技术/产业趋势（非落地主轴）**

=== 候选素材 ===
{candidates_text}

=== 任务 ===
对每个候选输出 executive_score（0-10，一位小数）和一句话理由。
- 9-10：落地机制+决策含义都清晰，可直接写高段位落地文章
- 7-8：有可提炼的落地洞察，但需要写作时升维
- 4-6：有一定价值，但更偏执行细节/具体case，或主题是AI技术趋势
- 0-3：纯技术/纯新闻/纯case描述，或AI行业趋势（非落地主轴），无落地决策含义

只输出 JSON 数组，按候选顺序：
```json
[
  {{"url": "候选URL", "executive_score": 8.5, "reason": "一句话理由"}}
]
```"""

    sys.path.insert(0, str(WORKSPACE_DIR))
    from lib.llm import call_model

    messages = [{"role": "user", "content": prompt}]
    try:
        resp = call_model(messages, model=model, temperature=0.2, max_tokens=2048)
    except Exception as e:
        print(f"  ⚠️ Executive-value ranking failed: {e}")
        return {}
    if not resp or not resp.strip():
        return {}

    json_match = re.search(r"```(?:json)?\n(.*?)\n```", resp, re.DOTALL)
    json_str = json_match.group(1) if json_match else resp.strip()
    try:
        results = json.loads(json_str)
        if not isinstance(results, list):
            return {}
    except json.JSONDecodeError:
        return {}

    scores = {}
    for r in results:
        url = r.get("url", "")
        if url:
            try:
                scores[url] = float(r.get("executive_score", 0.0))
            except (TypeError, ValueError):
                scores[url] = 0.0
    return scores


# ── Legacy compatibility wrapper ──
def pick_primary_cluster(candidates: list[dict]) -> dict:
    """Legacy: returns the single top pick."""
    result = pick_top_n_clusters(candidates, n=1)
    return result[0] if result else candidates[0] if candidates else {}
