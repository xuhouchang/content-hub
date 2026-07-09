"""Collector library — shared utils, source config loading, and URL registry."""

# Auto-load .env when any lib module is imported
from lib.env_loader import load_env; load_env()

import json
import os
import re
import datetime
from pathlib import Path
from typing import Optional

# URL normalization for dedup-aware comparisons. Guarded so root collectors
# that run without `content_platform` on sys.path never crash.
try:
    from content_platform.normalize.urls import normalize_url
except Exception:  # pragma: no cover - fallback only if package layout changes
    normalize_url = None

# ═══════════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════════

COLLECTOR_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE_DIR = COLLECTOR_DIR.parent
SOURCES_PATH = COLLECTOR_DIR / "sources.yaml"

REPORTS_DIR = WORKSPACE_DIR / "reports"
TMP_DIR = COLLECTOR_DIR / "tmp"

_sources_cache = None


# ═══════════════════════════════════════════════════
# Source config (sources.yaml)
# ═══════════════════════════════════════════════════

def load_sources(force_reload: bool = False) -> dict:
    """Load sources.yaml from collector root directory.

    Returns dict with keys: rss, blogs, consulting, thinktank, filtering.
    Cached after first load for performance.
    """
    import yaml
    global _sources_cache
    if _sources_cache is not None and not force_reload:
        return _sources_cache
    with open(SOURCES_PATH) as f:
        _sources_cache = yaml.safe_load(f)
    return _sources_cache


def get_sources(category: str) -> list[dict]:
    """Get source list for a specific category (rss, blogs, thinktank)."""
    data = load_sources()
    return data.get(category, [])


def get_filtering_rules() -> list[dict]:
    """Get filtering rule definitions from sources.yaml."""
    data = load_sources()
    return data.get("filtering", {}).get("rules", [])


# ═══════════════════════════════════════════════════
# Date & Calendar helpers
# ═══════════════════════════════════════════════════

def get_date_str() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.date.today().strftime("%Y-%m-%d")


def is_monday() -> bool:
    """Check if today is Monday (1=Monday in Python)."""
    return datetime.date.today().weekday() == 0


# ═══════════════════════════════════════════════════
# Slug
# ═══════════════════════════════════════════════════

def slugify(text: str, max_len: int = 60) -> str:
    """Convert text to a URL-friendly slug."""
    s = text.lower().strip()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s]+', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')[:max_len].rstrip('-')
    return s or "untitled"


# ═══════════════════════════════════════════════════
# URL Registry (dedup tracker)
# ═══════════════════════════════════════════════════

INDEX_DIR = REPORTS_DIR / "_index"
TSV_PATH = INDEX_DIR / "all_urls.tsv"


def load_url_registry() -> dict:
    """Load the URL dedup registry from _index/all_urls.tsv.

    Returns dict: {url: {status, date}}
    """
    registry = {}
    if TSV_PATH.exists():
        with open(TSV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    url = parts[0].strip()
                    registry[url] = {"status": parts[1].strip(), "date": parts[2].strip() if len(parts) > 2 else "unknown"}
    return registry


def save_url_registry(registry: dict):
    """Write the URL registry to TSV."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    with open(TSV_PATH, "w") as f:
        f.write("# url\tstatus\tdate\n")
        for url, info in sorted(registry.items()):
            status = info.get("status", "collected")
            date = info.get("date", get_date_str())
            f.write(f"{url}\t{status}\t{date}\n")


def is_duplicate(url: str, registry: dict) -> bool:
    """Check if URL already exists in the registry (normalization-aware).

    Two URLs that differ only by tracking params (?utm_source=...), scheme
    (http/https) or a trailing slash are treated as the same article.
    """
    if normalize_url is not None:
        norm = normalize_url(url)
        return any(normalize_url(k) == norm for k in registry)
    return url.strip() in registry


def append_to_url_registry(url: str, status: str = "collected", date_str: str = None):
    """Append a single URL to the TSV file (dedup by URL).

    Checks in-memory if URL already exists; if so, updates status at the TSV level
    by rewriting the whole file (only when needed — avoids duplicates entirely).
    The raw URL is preserved in the TSV (downstream content lookups rely on it);
    only the *comparison* is normalization-aware.
    """
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    if not TSV_PATH.exists():
        with open(TSV_PATH, "w") as f:
            f.write("# url\tstatus\tdate\n")

    date = date_str or get_date_str()
    url = url.strip()
    if not url:
        return

    registry = load_url_registry()
    if is_duplicate(url, registry):
        # Already exists (normalization-aware). Locate the original raw key
        # and bump its status/date if changed.
        existing_key = url
        if normalize_url is not None:
            norm = normalize_url(url)
            for k in registry:
                if normalize_url(k) == norm:
                    existing_key = k
                    break
        if registry[existing_key]["status"] != status:
            registry[existing_key]["status"] = status
            registry[existing_key]["date"] = date
            save_url_registry(registry)
    else:
        with open(TSV_PATH, "a") as f:
            f.write(f"{url}\t{status}\t{date}\n")


# ═══════════════════════════════════════════════════
# Content-hash Registry (cross-run content-level dedup)
# ═══════════════════════════════════════════════════
# Catches syndicated reprints: the same article arriving from a *different*
# URL. Keyed by sha256 of the (substantive) article body.
# Path is workspace-relative so tests stay hermetic and production dedup still
# spans days within the same workspace.

def _content_hash_tsv(workspace_dir=None):
    base = Path(workspace_dir) / "reports" / "_index" if workspace_dir else INDEX_DIR
    return base / "content_hashes.tsv"


def load_content_hashes(workspace_dir=None) -> dict:
    """Load {content_hash: original_url} map from content_hashes.tsv."""
    tsv = _content_hash_tsv(workspace_dir)
    registry = {}
    if tsv.exists():
        with open(tsv) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    registry[parts[0].strip()] = parts[1].strip()
    return registry


def is_content_duplicate(content_hash: str, workspace_dir=None) -> str | None:
    """Return the original source URL if this content hash was seen before.

    Returns None when the hash is empty or unknown.
    """
    if not content_hash:
        return None
    return load_content_hashes(workspace_dir).get(content_hash)


def mark_content_seen(content_hash: str, url: str, workspace_dir=None):
    """Persist a content hash -> source url mapping for future dedup."""
    if not content_hash:
        return
    tsv = _content_hash_tsv(workspace_dir)
    tsv.parent.mkdir(parents=True, exist_ok=True)
    with open(tsv, "a") as f:
        f.write(f"{content_hash}\t{url}\n")


# ═══════════════════════════════════════════════════
# Save raw JSON
# ═══════════════════════════════════════════════════

def save_raw(source: str, filename: str, items: list) -> Path:
    """Save raw items to tmp/<source>/<filename>."""
    dest_dir = TMP_DIR / source
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / filename
    with open(path, "w") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return path


# ═══════════════════════════════════════════════════
# Quick keyword-based relevance check
# ═══════════════════════════════════════════════════

TOPIC_KW_MAP = {
    "enterprise-ai-adoption": {
        "label": "AI在企业中实际落地",
        "keywords": [
            "adopt", "deploy", "implement", "rollout", "enterprise", "workflow",
            "ROI", "transformation", "automation", "productivity", "efficiency",
            "legacy", "migration", "integration", "pilot", "production",
        ],
    },
    "org-talent": {
        "label": "组织/人才/绩效",
        "keywords": [
            "workforce", "reskilling", "upskill", "organization", "culture",
            "leadership", "talent", "hiring", "team structure", "hybrid work",
            "remote", "collaboration", "change management", "performance",
        ],
    },
    "ai-governance": {
        "label": "AI治理/风险/合规",
        "keywords": [
            "risk", "compliance", "safety", "alignment", "regulation",
            "governance", "bias", "fairness", "transparency", "explainability",
            "audit", "security", "privacy", "hallucination", "jailbreak",
        ],
    },
    "technical-architecture": {
        "label": "AI系统建设/架构",
        "keywords": [
            "infrastructure", "architecture", "LLM", "training", "fine-tuning",
            "RAG", "agent", "pipeline", "vector", "embedding", "scaling",
            "inference", "latency", "cost", "GPU", "cloud", "MCP",
        ],
    },
    "case-study": {
        "label": "企业AI落地案例",
        "keywords": [
            "case study", "real-world", "production", "customer story",
            "implementation", "industry", "vertical", "domain",
        ],
    },
    "surprising-research": {
        "label": "反常识/有趣AI研究发现",
        "keywords": [
            "unexpected", "surprising", "counterintuitive", "interesting",
            "breakthrough", "discovery", "emergent", "capability",
            "insight", "reveal", "surprising finding",
        ],
    },
}

# Keywords that suggest a topic is NOT relevant
SKIP_KEYWORDS = [
    "stock", "price", "investor", "fundraising", "valuation", "IPO",
    "earning", "quarterly", "dividend", "market cap",
    "game review", "movie review", "sports", "celebrity",
    "recipe", "travel guide", "fashion", "crypto price",
]


def quick_relevance_check(title: str, summary: str = "") -> str:
    """Quick keyword-based relevance check.

    Returns 'pass' (directly relevant), 'maybe' (borderline, needs LLM), or 'skip'.
    """
    text = (title + " " + summary).lower()

    # Skip if contains finance/game keywords
    for kw in SKIP_KEYWORDS:
        if kw in text:
            return "skip"

    # Check each topic
    max_score = 0
    for topic_id, topic_info in TOPIC_KW_MAP.items():
        score = sum(1 for kw in topic_info["keywords"] if kw in text)
        if score > max_score:
            max_score = score

    if max_score >= 3:
        return "pass"
    elif max_score >= 1:
        return "maybe"
    return "skip"


def guess_relevance_reason(title: str, summary: str = "") -> str:
    """Return a short string identifying which topic(s) this matches."""
    text = (title + " " + summary).lower()
    matches = []
    for topic_id, topic_info in TOPIC_KW_MAP.items():
        hits = [kw for kw in topic_info["keywords"] if kw in text]
        if hits:
            matches.append(f"{topic_info['label']}({len(hits)} hits)")
    return "; ".join(matches[:3]) if matches else "no match"


# ═══════════════════════════════════════════════════
# Unified content fetcher (with Jina AI fallback)
# ═══════════════════════════════════════════════════
# Controlled via sources.yaml -> reader config.
# Priority: direct HTTP → Jina.ai reader (if enabled)


_FETCH_CONFIG_CACHE = None


def _load_fetch_config() -> dict:
    """Load reader config from sources.yaml, cached."""
    global _FETCH_CONFIG_CACHE
    if _FETCH_CONFIG_CACHE is None:
        sources = load_sources()
        _FETCH_CONFIG_CACHE = sources.get("reader", {})
    return _FETCH_CONFIG_CACHE


def _fetch_direct(url: str, timeout: int = 20, prefer_markdown: bool = True) -> str | None:
    """Direct HTTP fetch, return page content.

    When prefer_markdown=True, sends Accept: text/markdown header so that
    Cloudflare sites with "Markdown for Agents" enabled return Markdown directly.
    Falls back to text/html if markdown not supported by the origin.
    """
    try:
        import urllib.request
        accept = "text/markdown, text/html, application/xhtml+xml" if prefer_markdown else "text/html,application/xhtml+xml"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return raw
    except Exception as e:
        return None


def _fetch_via_jina(url: str, timeout: int = 30) -> str | None:
    """Fetch via Jina.ai reader API."""
    config = _load_fetch_config()
    jina = config.get("jina", {})
    if not jina.get("enabled", False):
        return None
    api_key = jina.get("api_key", "")
    if not api_key or api_key == "your_jina_api_key_here":
        api_key = os.environ.get("JINA_API_KEY", "")
    base_url = jina.get("base_url", "https://r.jina.ai")
    if not api_key:
        return None
    try:
        import requests
        resp = requests.get(
            f"{base_url}/{url}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  [WARN] Jina fetch failed for {url}: {e}")
        return None


def update_daily_summary(date_str: str, source_name: str, stats: dict) -> None:
    """Stub: record daily summary stats."""
    pass


def fetch_url(url: str, timeout: int = 30, prefer: str = "direct") -> str | None:
    """Fetch a URL, returning text content.

    Priority:
      1. direct HTTP (unless prefer="jina")
      2. Jina.ai reader (if enabled in sources.yaml)

    Args:
        url: The URL to fetch.
        timeout: Max wait time in seconds.
        prefer: "direct" (default, try direct first) or "jina" (Jina only).

    Returns:
        Response text, or None if all methods fail.
    """
    result = None

    if prefer == "jina":
        # Skip direct, go straight to Jina
        return _fetch_via_jina(url, timeout=timeout)

    # Try direct first
    result = _fetch_direct(url, timeout=timeout)
    if result:
        return result

    # Fallback to Jina
    print(f"  ↪ Direct fetch failed for {url[:60]}..., falling back to Jina.ai")
    return _fetch_via_jina(url, timeout=timeout)
