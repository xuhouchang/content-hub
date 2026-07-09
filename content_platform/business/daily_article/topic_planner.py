"""Topic planner: pick N distinct clusters, avoiding semantic topic repeat."""

import datetime
import json
import re
import sys
from pathlib import Path
from typing import Optional

# ── Recent topics file ──
RECENT_TOPICS_FILE = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "wechat-articles"
    / "_recent_topics.json"
)


def _load_recent_topics(days: int = 7) -> list[dict]:
    """Load recent article topics from _recent_topics.json (last `days` days)."""
    if not RECENT_TOPICS_FILE.exists():
        return []
    try:
        entries = json.loads(RECENT_TOPICS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    return [
        e
        for e in entries
        if datetime.date.fromisoformat(e.get("date", "2000-01-01")) >= cutoff
    ]


def _llm_judge_topic_similar(
    new_title: str,
    new_digest: Optional[str],
    recent_topics: list[dict],
    model: str = "deepseek-chat",
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

=== 近期已发布文章（7天内） ===
{recent_text}

=== 新选题 ===
标题: {new_title}
摘要: {new_digest or ""}

请判断：新选题与上述任何一篇近期文章的主题是否**本质上是一个核心议题**。
特别注意以下情况也视为重复（即应回答 SIMILAR）：
- 同一个原始 Source 文章，只是换了不同的中文标题和案例来写
- 讨论的是同一个商业趋势/现象

只有确认核心议题完全不同才回答 DISTINCT。

如果「是同一个核心议题」，回复「SIMILAR」
如果「是不同的核心议题」，回复「DISTINCT」

只回复 SIMILAR 或 DISTINCT。"""

    # Call LLM via lib.llm.call_model
    sys.path.insert(0, str(RECENT_TOPICS_FILE.parent.parent))
    from lib.llm import call_model

    messages = [
        {"role": "user", "content": prompt},
    ]
    resp = call_model(messages, model=model, temperature=0.1, max_tokens=50)
    cleaned = re.sub(r"[^A-Z]", "", resp.strip().upper())
    return "SIMILAR" in cleaned


def _load_recent_source_urls(recent_topics: list[dict]) -> set[str]:
    """Collect all source URLs from recent topics (normalized)."""
    recent_urls: set[str] = set()
    for t in recent_topics:
        for url in t.get("source_urls", []):
            recent_urls.add(url.rstrip("/"))
    return recent_urls


def pick_top_n_clusters(
    candidates: list[dict],
    n: int = 2,
    topic_diversity_days: int = 7,
    model: str = "deepseek-chat",
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
    recent_source_urls = _load_recent_source_urls(recent_topics)

    # Sort candidates by editorial_fit_score DESC
    sorted_candidates = sorted(
        candidates,
        key=lambda m: (m.get("editorial_fit_score", 0.0), m.get("novelty_score", 0.0)),
        reverse=True,
    )

    selected: list[dict] = []
    seen_clusters: set[str] = set()

    for m in sorted_candidates:
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
        if recent_source_urls and candidate_url in recent_source_urls:
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


# ── Legacy compatibility wrapper ──
def pick_primary_cluster(candidates: list[dict]) -> dict:
    """Legacy: returns the single top pick."""
    result = pick_top_n_clusters(candidates, n=1)
    return result[0] if result else candidates[0] if candidates else {}
