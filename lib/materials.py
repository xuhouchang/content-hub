#!/usr/bin/env python3
"""
Materials management for the collector pipeline.

Reading, filtering, deduplication, and sampling of collected materials.

Re-exports several utility functions from lib.py for convenience:
  quick_relevance_check, load_url_registry, is_duplicate, append_to_url_registry
"""

import datetime
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Optional

# ── Re-exports from lib.py ──
from lib import (
    quick_relevance_check,
    load_url_registry,
    is_duplicate,
    append_to_url_registry,
)
from content_platform.normalize.urls import normalized_url_key
from content_platform.recent_topics import recent_source_urls, recent_topics

# ── Paths ──
WORKSPACE_DIR = Path(__file__).resolve().parent.parent  # lib/ -> workspace/
REPORTS_DIR = WORKSPACE_DIR / "reports"
ALL_URLS_FILE = REPORTS_DIR / "_index" / "all_urls.tsv"


# ── Source quality categories (best first) ──
SOURCE_QUALITY_CATEGORIES = {
    "ranking_report": [
        "sloanreview.mit.edu", "knowledge.wharton.upenn.edu", "hbr.org",
        "rand.org", "oneusefulthing.org",
    ],
    "consulting_reports": [
        "mckinsey.com", "bcg.com", "bain.com", "deloitte.com",
        "gartner.com", "forrester.com", "idc.com",
    ],
    "quizzes": [],
    "blog": [
        "anthropic.com", "openai.com", "deepmind.google",
        "blog.google", "blog.samaltman.com", "stratechery.com",
        "salesforce.com", "microsoft.com",
    ],
    "newsletters": [
        "substack.com", "importai.substack.com",
    ],
}

_CATEGORY_ORDER = ["ranking_report", "consulting_reports", "quizzes", "blog", "newsletters"]


def _source_quality_score(domain: str) -> int:
    """Return a numeric quality score for a domain (5 = best, 0 = other)."""
    domain_lower = domain.lower()
    for i, category in enumerate(_CATEGORY_ORDER):
        keywords = SOURCE_QUALITY_CATEGORIES[category]
        for kw in keywords:
            if kw in domain_lower:
                return 5 - i  # ranking=5, consulting=4, quizzes=3, blog=2, newsletters=1
    return 0


def _fetch_curate_recent(days: int = 3) -> list[dict]:
    """Fetch recently collected materials from platform/curate/ (new architecture).

    Returns list of {url, line, status} with validated content.
    Filters for known high-value sources.
    """
    curate_dir = WORKSPACE_DIR / "platform" / "curate"
    if not curate_dir.exists():
        return []

    # Determine cut-off date
    today = datetime.date.today()
    cut_off = (today - datetime.timedelta(days=days)).isoformat()

    items = []
    seen_urls = set()
    for mat_file in sorted(curate_dir.rglob("materials.json"), reverse=True):
        dpart = mat_file.parent.name  # YYYY-MM-DD
        if dpart < cut_off:
            break
        try:
            with open(mat_file) as f:
                materials = json.load(f)
        except Exception:
            continue
        for item in materials:
            url = (item.get("canonical_url") or item.get("url") or "").rstrip("/")
            if not url or url in seen_urls:
                continue
            ct = item.get("content_text") or item.get("summary") or ""
            if len(ct) < MIN_CONTENT_CHARS:
                continue
            seen_urls.add(url)
            items.append({
                "url": url,
                "line": f"{url}\tcollected\t{dpart}",
                "status": "collected",
            })

    return items


def get_all_collected_urls(validate_content: bool = True) -> list[dict]:
    """Read all_urls.tsv and return list of {url, line} for items with 'collected' status.

    When validate_content=True, only includes URLs whose saved Markdown file
    or platform/curate/ entry has substantive content (≥MIN_CONTENT_CHARS).

    Falls back to platform/curate/ (new architecture) for recent materials
    not yet recorded in all_urls.tsv.

    Results are sorted by source quality (descending):
    ranking_report > consulting_reports > quizzes > blog > newsletters > other.
    """
    items = []
    seen_urls = set()

    # ── Phase 1: from all_urls.tsv (traditional source) ──
    if ALL_URLS_FILE.exists():
        with open(ALL_URLS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1] in ("collected", "unknown"):
                    url = parts[0].rstrip("/")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    # ── Content validation guard ──
                    if validate_content:
                        content = read_material_content(url)
                        if not content:
                            continue

                    items.append({
                        "url": url,
                        "line": line,
                        "status": "collected",
                    })

    # ── Phase 2: supplement from platform/curate/ (always include) ──
    # Ensure recent high-quality materials are included even if not yet in all_urls.tsv
    curate_items = _fetch_curate_recent(days=7)
    for ci in curate_items:
        if ci["url"] not in seen_urls:
            seen_urls.add(ci["url"])
            items.append(ci)

    # Sort by source quality score (descending)
    for m in items:
        domain = urllib.parse.urlparse(m["url"]).netloc.lower()
        m["_quality_score"] = _source_quality_score(domain)

    items.sort(key=lambda x: x["_quality_score"], reverse=True)
    return items


def find_report_file(url: str) -> Optional[Path]:
    """Search reports/ for a Markdown file containing this URL."""
    url_stripped = url.strip().rstrip("/")

    # Search in reports directory recursively
    for md_file in REPORTS_DIR.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            if url_stripped in content or url in content:
                return md_file
        except Exception:
            continue
    return None


# ── Minimum content threshold ──
# URLs whose saved Markdown content falls below this are considered empty
# and skipped. Prevents writing articles from placeholder/no-content items.
MIN_CONTENT_CHARS = 300


def _read_curate_content(url: str, url_stripped: str, min_chars: int = MIN_CONTENT_CHARS) -> Optional[str]:
    """Fallback: search platform/curate/ for content (new architecture)."""
    curate_dir = WORKSPACE_DIR / "platform" / "curate"
    if not curate_dir.exists():
        return None
    for mat_file in sorted(curate_dir.rglob("materials.json"), reverse=True):
        try:
            with open(mat_file) as f:
                materials = json.load(f)
            for item in materials:
                u = item.get("canonical_url") or item.get("url") or ""
                if u.rstrip("/") == url_stripped or u.rstrip("/") == url.rstrip("/"):
                    ct = item.get("content_text") or item.get("summary") or ""
                    if len(ct) >= min_chars:
                        return ct
                    break
        except Exception:
            continue
    return None


def read_material_content(url: str, min_chars: int = MIN_CONTENT_CHARS) -> Optional[str]:
    """Read the content of a material from its saved Markdown file.

    First tries reports/ (old architecture), then falls back to
    platform/curate/ (new architecture).

    Returns None if: file not found, content is below min_chars threshold,
    or content looks like an empty stub ("No content available").
    """
    url_stripped = url.strip().rstrip("/")
    md_file = find_report_file(url)
    if md_file:
        content = md_file.read_text(encoding="utf-8", errors="replace")
        # Remove front matter (--- block)
        content = re.sub(r'^---\n.*?\n---\n', '', content, flags=re.DOTALL)
        # Remove trailing "Collected by" line
        content = re.sub(r'\n---\n\n\*Collected by.*?\*$', '', content, flags=re.DOTALL)
        content = content.strip()

        # ── Content size guard ──
        if len(content) < min_chars:
            return None

        # ── Stub guard: content that is just a placeholder string ──
        stub_patterns = ["No content available", "No summary available"]
        if content.strip() in stub_patterns or any(p == content.strip() for p in stub_patterns):
            return None

        return content

    # -- Fallback: platform/curate/ (new architecture) --
    return _read_curate_content(url, url_stripped, min_chars)


def score_material(m: dict) -> int:
    """Score a material for selection priority. Higher = better."""
    url = m.get("url", "")
    content = m.get("content", "")
    source_domain = urllib.parse.urlparse(url).netloc.lower()
    content_len = len(content or "")

    score = 0

    # 🏆 High-value sources (research/thought leadership)
    high_value = [
        "sloanreview.mit.edu", "knowledge.wharton.upenn.edu", "hbr.org",
        "oneusefulthing.org", "rand.org", "importai.substack.com",
        "anthropic.com", "deepmind.google", "openai.com",
    ]
    medium_value = [
        "blog.google", "blog.samaltman.com", "stratechery.com",
        "salesforce.com", "microsoft.com", "techcrunch.com",
    ]

    for domain in high_value:
        if domain in source_domain:
            score += 18
            break
    for domain in medium_value:
        if domain in source_domain:
            score += 10
            break

    # Content depth: longer articles tend to have more substance
    if content_len > 10000:
        score += 20  # Deep analysis
    elif content_len > 3000:
        score += 10  # Medium depth
    elif content_len < 500:
        score -= 10  # Too short

    # Penalize purely promotional content
    promo_keywords = ["announcing", "launch", "release", "product update", "upgrade"]
    content_lower = content.lower()
    promo_count = sum(1 for kw in promo_keywords if kw in content_lower[:500])
    if promo_count >= 2:
        score -= 20

    return score


# ── Recent article tracking (diversity boost) ──
# Path to recent article log (written by write_article.py on each run)
RECENT_ARTICLES_FILE = WORKSPACE_DIR / "wechat-articles" / "_article_topics.json"

# Keywords that have been heavily used in recent articles —
# materials whose titles/content overlap these will be penalized in scoring
_RECENT_THEMES = [
    # "why not use / task audit / pick the right model" cluster
    "任务", "审计", "落地", "选型", "模型", "验收",
    "用不起来", "不用", "拆解", "部署",
    # "how to choose / which one" cluster
    "哪个AI", "选工具", "选模型", "哪个模型", "选哪个",
    "怎么选", "怎么用", "用它",
    # "tools not used" cluster
    "买了", "团队不用", "没用上",
]


def load_recent_themes() -> set:
    """Load recently used topic keywords from article log."""
    themes = set()
    for item in recent_topics(WORKSPACE_DIR, "article", days=30)[-10:]:
        title = (item.get("title", "") or "").lower()
        digest = (item.get("digest", "") or "").lower()
        for kw in _RECENT_THEMES:
            if kw in title or kw in digest:
                themes.add(kw)
    return themes


def filter_used_materials(materials: list[dict]) -> list[dict]:
    """Filter out materials whose URLs are marked 'used' in all_urls.tsv.
    
    This is a code-level, non-negotiable filter — runs BEFORE any scoring or
    sampling. Any URL with status 'used' is excluded.
    """
    if not materials:
        return []
    
    used_urls = set()
    if ALL_URLS_FILE.exists():
        with open(ALL_URLS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1].strip() == "used":
                    used_urls.add(parts[0].strip().rstrip("/"))
    
    if not used_urls:
        return materials
    
    before = len(materials)
    filtered = []
    for m in materials:
        url = (m.get("url", "") or "").strip().rstrip("/")
        if url in used_urls:
            print(f"  🚫 Excluded (used): {url[:80]}")
        else:
            filtered.append(m)
    
    after = len(filtered)
    if before != after:
        print(f"  Filtered {before - after} already-used material(s)")
    return filtered


# ── Tag-based clustering ──

def _load_material_tags() -> dict:
    """Load LLM-tag data from _material_tags.json.
    Returns dict: url -> {title, tags: {dim: tag}, key_signal, tagged_at}
    """
    tags_file = WORKSPACE_DIR / "wechat-articles" / "_material_tags.json"
    if tags_file.exists():
        try:
            return json.loads(tags_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return {}
    return {}


def _compute_tag_similarity(tags_a: dict, tags_b: dict) -> float:
    """Compute tag similarity between two materials (0.0 - 1.0).

    Counts shared dimensions (same tag in same dimension) divided
    by total number of dimensions.

    Compatibility: old entries have 'evidence_type' (single dim),
    new entries have 'evidence_source' + 'evidence_depth' (split dims).
    When comparing old↔new, maps evidence_type → (evidence_source, evidence_depth)
    using approximate mapping, so evidence dim contributes 0.5 instead of 0.
    Missing dimensions count as mismatch.
    """
    if not tags_a or not tags_b:
        return 0.0

    def _normalize(tags):
        """Normalize old evidence_type to new schema for comparison."""
        t = dict(tags)
        if "evidence_type" in t and "evidence_source" not in t and "evidence_depth" not in t:
            # Old entry: try to infer evidence_source + evidence_depth
            et = t.pop("evidence_type", "")
            # Approximate mapping
            source_map = {
                "定量数据/调研数据": "一手数据/实验",
                "实验/基准测试": "公开数据集/指标",
                "定性案例/深度访谈": "一手案例/采访",
                "引用/综述": "引用他人研究",
                "推理/逻辑论证": "逻辑推演/框架",
                "个人经验/感悟": "个人经验/观察",
                "政策/法规原文": "法规/政策原文",
            }
            depth_map = {
                "定量数据/调研数据": "有数据/仅描述",
                "实验/基准测试": "有数据/仅描述",
                "定性案例/深度访谈": "有案例/有细节",
                "引用/综述": "引用综述/合成",
                "推理/逻辑论证": "纯观点/无支撑",
                "个人经验/感悟": "纯观点/无支撑",
                "政策/法规原文": "引用综述/合成",
            }
            t["evidence_source"] = source_map.get(et, "逻辑推演/框架")
            t["evidence_depth"] = depth_map.get(et, "纯观点/无支撑")
        return t

    na = _normalize(tags_a)
    nb = _normalize(tags_b)

    shared = 0
    all_dims = set(na.keys()) | set(nb.keys())
    for dim in all_dims:
        if dim in na and dim in nb:
            if na[dim] == nb[dim]:
                shared += 1
    return shared / max(len(all_dims), 1)


def _tag_vector(tags: dict) -> str:
    if not tags:
        return "(untagged)"
    return " | ".join(f"{dim}={tag}" for dim, tag in sorted(tags.items()))


def _cluster_materials_by_topic(materials: list[dict]) -> dict[str, list[dict]]:
    """Cluster materials by LLM tag similarity. Falls back to keyword clustering
    when no LLM tag data is available.

    Strategy:
    1. Load LLM tag database
    2. Match materials to their tags by URL
    3. Greedy clustering: pairwise tag similarity >= 0.3
    4. Label each cluster by its anchor article's topic_focus tag
    5. Untagged / unmatched materials go to "其他"
    """
    tags_db = _load_material_tags()

    if not tags_db:
        return _cluster_materials_by_topic_keyword(materials)

    # Separate tagged and untagged materials
    tagged: list[tuple[dict, dict]] = []  # (material, tags)
    untagged: list[dict] = []
    for m in materials:
        url = (m.get("url", "") or "").rstrip("/")
        tag_entry = tags_db.get(url)
        if tag_entry and tag_entry.get("tags"):
            tagged.append((m, tag_entry["tags"]))
        else:
            untagged.append(m)

    # Greedy clustering by tag similarity
    clusters_raw: list[list[tuple[dict, dict]]] = []
    assigned = set()

    for i, (m_i, tags_i) in enumerate(tagged):
        if i in assigned:
            continue
        cluster = [(m_i, tags_i)]
        assigned.add(i)
        for j, (m_j, tags_j) in enumerate(tagged):
            if j in assigned:
                continue
            if _compute_tag_similarity(tags_i, tags_j) >= 0.4:
                cluster.append((m_j, tags_j))
                assigned.add(j)
        clusters_raw.append(cluster)

    # Build result dict with readable labels
    result: dict[str, list[dict]] = {}
    for cluster in clusters_raw:
        anchor_tags = cluster[0][1]
        # Use topic_focus as primary label (may be list or string)
        raw_label = anchor_tags.get("topic_focus", "其他")
        if isinstance(raw_label, list):
            label = raw_label[0] if raw_label else "其他"
        else:
            label = str(raw_label) if raw_label else "其他"
        # Deduplicate labels
        candidate = label
        disambig_idx = 1
        while candidate in result:
            suffix = anchor_tags.get("content_form", "")
            if isinstance(suffix, list):
                suffix = suffix[0] if suffix else ""
            if suffix:
                candidate = f"{label} / {suffix}"
            else:
                candidate = f"{label} ({disambig_idx})"
            disambig_idx += 1
        result[candidate] = [m for m, _ in cluster]

    if untagged:
        result["其他"] = untagged

    return result


def _cluster_materials_by_topic_keyword(materials: list[dict]) -> dict[str, list[dict]]:
    """Fallback: keyword-based clustering (no LLM tags available)."""
    TOPIC_CLUSTERS = {
        "自动驾驶/具身智能/机器人": ["waymo", "cruise", "autonomous", "self-driving", "robot", "robotics", "embodied", "physical ai", "humanoid"],
        "AI Agent/多智能体编排": ["agent", "multi-agent", "agentic", "orchestrat", "multiagent", "智能体"],
        "企业AI落地/采纳障碍": ["adoption", "rollout", "implement", "deploy", "pilot", "deployment", "落地", "采纳"],
        "组织变革/人机协作": ["organiz", "workforce", "human-ai", "managers", "employee", "talent", "culture", "collaborat", "leader", "组织", "变革"],
        "AI技术/模型/基础设施": ["model", "llm", "foundation", "inference", "training", "infra", "gpt", "deepseek", "claude", "gemini"],
        "商业战略/竞争/投资": ["startup", "vc", "invest", "market", "compete", "strategy", "funding", "盈利", "融资"],
        "AI治理/安全/信任": ["governance", "safety", "alignment", "trust", "ethical", "transparen", "risk", "regulat", "治理", "安全"],
        "开发工具/工程实践": ["prompt", "rag", "langchain", "pipeline", "mlops", "coding", "cursor", "dev", "开发", "工程"],
        "AI与社会/经济影响": ["economic", "gdp", "productivity", "jobs", "labor", "wage", "社会", "经济"],
        "垂直行业应用": ["healthcare", "legal", "financ", "retail", "manufactur", "education", "sales", "customer service"],
        "AI研究/反常识发现": ["research", "study", "experiment", "paper", "arxiv", "findings", "研究"],
        "客户案例/实战记录": ["case study", "customer", "how we", "lessons learned", "实战", "案例"],
    }
    clusters: dict[str, list[dict]] = {k: [] for k in TOPIC_CLUSTERS}
    clusters["其他"] = []
    for m in materials:
        text = ((m.get("title", "") or "") + " " + (m.get("url", "") or "")).lower()[:500]
        best, best_score = "其他", 0
        for label, keywords in TOPIC_CLUSTERS.items():
            score = sum(len(kw) for kw in keywords if kw in text)
            url_lower = (m.get("url", "") or "").lower()
            score += sum(len(kw) * 2 for kw in keywords if kw in url_lower)
            if score > best_score:
                best_score, best = score, label
        clusters[best if best_score > 0 and best != "其他" else "其他"].append(m)
    return {k: v for k, v in clusters.items() if v}


def _load_key_signals() -> dict:
    """Load key_signals from tag database.
    Returns dict: url -> key_signal string (or empty)
    """
    tags_db = _load_material_tags()
    signals = {}
    for url, entry in tags_db.items():
        ks = (entry.get("key_signal") or "").strip()
        if len(ks) >= 15:  # minimum meaningful signal
            signals[url.rstrip("/")] = ks
    return signals


def _count_signals_in_cluster(items: list[dict], signals_db: dict) -> int:
    """Count how many materials in a cluster have a meaningful key_signal."""
    count = 0
    for m in items:
        url = (m.get("url", "") or "").rstrip("/")
        if url in signals_db:
            count += 1
    return count


def _pick_best_cluster(clusters: dict[str, list[dict]], max_count: int = 5) -> list[dict]:
    """Pick the best cluster: prioritize clusters with the most key_signal (观点信号).

    Core logic:
    1. Exclude "其他" from ranking
    2. Sort clusters by: signal_count DESC, then avg_score DESC
       — signal_count = how many materials have a meaningful key_signal
       — avg_score = average material score (source quality + content depth)
    3. If best cluster has < 2 signal items, attempt merge with next best cluster's signals
    4. Deduplicate by domain (max 2 per domain), return top-scoring
    """
    signals_db = _load_key_signals()

    ranked_clusters = [(label, items) for label, items in clusters.items() if label != "其他"]

    if not ranked_clusters:
        print("  ⚠️  All materials fell into '其他' cluster. Using it anyway.")
        ranked_clusters = [("其他", clusters.get("其他", []))]

    if not ranked_clusters:
        return []

    # ── Rank by (signal_count desc, avg_score desc) ──
    cluster_rankings = []
    for label, items in ranked_clusters:
        signal_count = _count_signals_in_cluster(items, signals_db)
        avg_score = sum(m.get("score", 0) for m in items) / max(len(items), 1)
        cluster_rankings.append((signal_count, avg_score, label, items))

    # Primary sort: signal_count desc, secondary: avg_score desc
    cluster_rankings.sort(key=lambda x: (-x[0], -x[1]))

    best_label, best_items = cluster_rankings[0][2], cluster_rankings[0][3]
    best_signal_count = cluster_rankings[0][0]
    best_avg_score = cluster_rankings[0][1]

    print(f"  🎯 Selected cluster: {best_label} ({len(best_items)} materials, "
          f"{best_signal_count} with key_signal, avg_score: {best_avg_score:.1f})")

    if len(cluster_rankings) > 1:
        print("     Other clusters:")
        for signal_count, avg_score, label, items in cluster_rankings[1:4]:
            sig_count = _count_signals_in_cluster(items, signals_db)
            print(f"       • {label}: {len(items)} materials, {sig_count} signals (avg: {avg_score:.1f})")
        if len(cluster_rankings) > 4:
            print(f"       ... and {len(cluster_rankings)-4} more clusters")

    # ── Start with best cluster ──
    selected = list(best_items)
    used_urls = {m.get("url", "") for m in selected}

    # ── Cross-cluster signal-borrow: DISABLED (公众号文章质量修复方案 4.2) ──
    # One article == one core thesis. We no longer merge in signal materials from
    # other clusters just because the best cluster has few key_signals — that
    # produced cross-topic concatenation. If the best cluster is too thin to
    # support depth, we keep it as-is below the coherence threshold and let the
    # caller / quality gate flag "证据不足 / 不建议成文".
    best_signal_count = _count_signals_in_cluster(selected, signals_db)
    if best_signal_count < 2 and len(selected) >= 1:
        print(f"  ⚠️ Best cluster has only {best_signal_count} key_signal — "
              f"NOT borrowing from other clusters (single-thesis rule). "
              f"May be flagged as 证据不足 later.")

    # ── Deduplicate by domain ──
    seen_domains: dict[str, int] = {}
    deduped = []
    for m in sorted(selected, key=lambda x: x.get("score", 0), reverse=True):
        domain = _registrable_domain(m.get("url", ""))
        if domain not in seen_domains:
            seen_domains[domain] = 0
        if seen_domains[domain] < 2:
            deduped.append(m)
            seen_domains[domain] += 1

    return deduped[:max_count]


# Multi-level public suffixes that must be skipped when extracting the
# registrable domain (otherwise e.g. bbc.co.uk would collapse to "co").
_MULTI_LEVEL_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "net.uk",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn", "com.tw",
    "com.au", "net.au", "org.au", "gov.au", "edu.au",
    "co.nz", "org.nz", "govt.nz",
    "co.jp", "or.jp", "ne.jp", "go.jp", "com.jp",
    "com.br", "com.mx", "co.in", "com.hk",
}


def _registrable_domain(url: str) -> str:
    """Return the registrable domain (e.g. 'bbc' for 'www.bbc.co.uk').

    Handles multi-level public suffixes (co.uk, com.cn, ...); falls back to the
    last two labels for unknown TLDs, and 'unknown' for empty URLs.
    """
    if not url:
        return "unknown"
    netloc = urllib.parse.urlparse(url).netloc.lower()
    if not netloc:
        return "unknown"
    # strip any userinfo and port
    netloc = netloc.split("@")[-1].split(":")[0]
    labels = netloc.split(".")
    if len(labels) <= 2:
        return netloc
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LEVEL_TLDS:
        return ".".join(labels[-3:])
    return last_two


def sample_materials(materials: list[dict], max_count: int = 5, skip_used_filter: bool = False) -> list[dict]:
    """Sample materials for LLM context: cluster by tags, pick best cluster.

    Strategy:
    1. Filter out materials marked 'used' in all_urls.tsv (unless ``skip_used_filter``)
    2. Filter out recently-used URLs (past 3 days)
    3. Load LLM tag database for tag-based clustering
    4. Cluster materials by tag similarity (or fallback keyword clustering)
    5. Pick the best cluster

    ``skip_used_filter``: True for the ``--materials`` override path. When the
    daily selector already curated today's candidates (and content-hash dedup in
    runtime.py governs cross-day reprint dedup), re-filtering them against the
    legacy ``all_urls.tsv`` 'used' status is redundant and can wrongly empty a
    fresh selection. The past-3-day recent-URL filter below is still applied, so
    cross-day dedup on recently published URLs is preserved.

    Returns top-scoring materials from the best cluster.
    """
    if not materials:
        return []

    if not skip_used_filter:
        # ── CODE-LEVEL FILTER: Exclude materials marked 'used' in all_urls.tsv ──
        materials = filter_used_materials(materials)
        if not materials:
            return []

    # ── Exclude recently used URLs (past 3 days) ──
    recently_used_urls = recent_source_urls(WORKSPACE_DIR, days=3)

    if recently_used_urls:
        print(f"  🚫 Excluding {len(recently_used_urls)} recently used URLs")
        before = len(materials)
        materials = [m for m in materials if normalized_url_key(m.get("url", "")) not in recently_used_urls]
        after = len(materials)
        if before != after:
            print(f"  Filtered out {before - after} materials (used in last 3 days)")

    recent_themes = load_recent_themes()
    if recent_themes:
        print(f"  📋 Avoiding recently used themes: {', '.join(sorted(recent_themes))}")

    if not materials:
        return []

    # Score all materials
    for m in materials:
        if "score" not in m:
            m["score"] = score_material(m)
        url_lower = (m.get("url", "") or "").lower()
        for theme in recent_themes:
            if theme in url_lower:
                m["score"] -= 30

    # ── Cluster by tag, pick best cluster ──
    print("\n  📊 Clustering materials by tag similarity...")
    clusters = _cluster_materials_by_topic(materials)
    print(f"     Found {len(clusters)} clusters:")
    for label, items in sorted(clusters.items(), key=lambda x: len(x[1]), reverse=True):
        total_score = sum(m.get("score", 0) for m in items)
        print(f"       • {label}: {len(items)} materials (score: {total_score})")

    selected = _pick_best_cluster(clusters, max_count)

    if selected:
        # Find its cluster label
        cluster_label = "?"
        for label, items in clusters.items():
            if selected[0] in items:
                cluster_label = label
                break
        print(f"     Selected {len(selected)} materials from cluster '{cluster_label}'")
        # Attach key_signal to each selected material
        tags_db = _load_material_tags()
        for s in selected:
            url = (s.get("url", "") or "").rstrip("/")
            entry = tags_db.get(url, {})
            ks = (entry.get("key_signal") or "").strip()
            if len(ks) >= 15:
                s["key_signal"] = ks
            # Print selection info
            if entry and entry.get("tags"):
                print(f"       • {s.get('url','')[:70]}")
                print(f"          tags: {_tag_vector(entry['tags'])[:80]}")
                if s.get("key_signal"):
                    print(f"          signal: {s['key_signal'][:80]}")
                continue
            print(f"       • {s.get('url','')[:80]}")
    else:
        # 公众号文章质量修复方案 4.2：不跨主题拼接。
        # 单一聚类不足以支撑深度时，输出"不建议成文/需要补充素材"，
        # 而不是用不同主题的高分素材强行捏合成一篇。
        print("  ❌ Could not build a coherent themed cluster (single-thesis rule).")
        print("     ⛔ Proceeding WITHOUT cross-theme concatenation.")
        print("     ⛔ Result: 不建议成文 / 需要补充同一论点的素材。")
        print("     ⛔ Returning empty sample so the caller aborts the write "
              "(no fabricated or stitched article).")
        selected = []

    return selected[:max_count]
