#!/usr/bin/env python3
"""
┌──────────────────────────────────────────────────────────────┐
│ Unified Content Collector                                   │
│                                                              │
│ 分类采集 + 统一去重 + 统一沉淀                                 │
│                                                              │
│ 所有采集入口统一集成到一个脚本，按 mode 分发：                   │
│   mode=collect   — 采集（对应 collect_rss/blogs/consulting）  │
│   mode=save      — 保存（对应 save_rss/blogs/consulting）     │
│   mode=filter    — 过滤（对应 filter_rss/filter_items）       │
│   mode=case      — 案例拆解发现（对应 decompose 的外部发现）   │
│   mode=search    — AnySearch 搜索（替代 collect_podcasts）    │
│   mode=stats     — 每日统计                                  │
│                                                              │
│ 用法示例：                                                    │
│   python3 collect.py collect rss                             │
│   python3 collect.py collect blogs                           │
│   python3 collect.py collect consulting                      │
│   python3 collect.py save rss                                │
│   python3 collect.py filter rss                              │
│   python3 collect.py filter blogs                            │
│   python3 collect.py case --dry-run                          │
│   python3 collect.py search "enterprise AI case study"       │
│   python3 collect.py stats                                   │
│                                                              │
│ 兼容：直接调用原脚本仍然可以，collect.py 只是统一调度层。        │
└──────────────────────────────────────────────────────────────┘
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

COLLECTOR_DIR = Path(__file__).parent
WORKSPACE_DIR = COLLECTOR_DIR.parent

# ── Auto-load .env ──
dotenv_path = COLLECTOR_DIR / ".env"
if dotenv_path.exists():
    with open(dotenv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if value and not os.environ.get(key):
                os.environ[key] = value

sys.path.insert(0, str(COLLECTOR_DIR))

# ── External source definitions ──

EXTERNAL_SOURCES = [
    {
        "name": "case-studies.ai",
        "url": "https://case-studies.ai/",
        "type": "collection",
        "language": "en",
        "description": "Human-curated enterprise AI case study library. 8 categories.",
        "topics": ["enterprise-ai", "case-study", "production"],
    },
    {
        "name": "ninetwothree blog",
        "url": "https://www.ninetwothree.co/blog/",
        "type": "blog",
        "language": "en",
        "description": "AI adoption case studies with measurable business results",
        "topics": ["ai-adoption", "case-study", "enterprise"],
    },
    {
        "name": "Applied (theapplied.co)",
        "url": "https://theapplied.co/use-cases",
        "type": "collection",
        "language": "en",
        "description": "Real AI use cases with verified metrics.",
        "topics": ["use-case", "ai-agent", "enterprise"],
    },
    {
        "name": "Intercom Blog",
        "url": "https://www.intercom.com/blog",
        "type": "blog",
        "language": "en",
        "description": "Customer service AI case studies, AI agent deployment stories",
        "topics": ["customer-service", "ai-agent", "case-study"],
    },
    {
        "name": "Retool Blog",
        "url": "https://retool.com/blog",
        "type": "blog",
        "language": "en",
        "description": "Enterprise AI deployment stories, internal tools at scale.",
        "topics": ["enterprise", "internal-tools", "case-study"],
    },
    {
        "name": "LangChain Blog (customers)",
        "url": "https://blog.langchain.dev/",
        "type": "blog",
        "language": "en",
        "description": "Customer case studies of AI agent deployments.",
        "topics": ["ai-agent", "case-study", "llm"],
    },
    {
        "name": "AWS ML Blog",
        "url": "https://aws.amazon.com/blogs/machine-learning/",
        "type": "blog",
        "language": "en",
        "description": "Production AI deployment case studies on AWS.",
        "topics": ["mlops", "deployment", "engineering"],
    },
    {
        "name": "Google Cloud Blog (AI)",
        "url": "https://cloud.google.com/blog/products/ai-machine-learning",
        "type": "blog",
        "language": "en",
        "description": "Enterprise AI/ML deployment case studies on GCP.",
        "topics": ["mlops", "deployment", "cloud"],
    },
    {
        "name": "Moveworks Customer Stories",
        "url": "https://www.moveworks.com/us/en/customers",
        "type": "blog",
        "language": "en",
        "description": "Enterprise AI agent customer stories with quantified results.",
        "topics": ["ai-agent", "enterprise", "case-study"],
    },
    {
        "name": "Converteo Blog",
        "url": "https://converteo.com/en/blog/",
        "type": "blog",
        "language": "en",
        "description": "AI agent deployment case studies with consulting frameworks.",
        "topics": ["ai-agent", "retail", "case-study"],
    },
]

# Pre-configured search queries for case discovery
CASE_SEARCH_QUERIES = [
    "enterprise AI agent production deployment results automation rate ROI case study",
    "AI implementation case study company results metrics deployment 2025",
    "how company implemented AI agent customer service automation results",
    "enterprise AI copilot deployment production metrics lessons learned",
    "AI transforming business process case study quantified results",
    "company built AI assistant replaced manual process efficiency gains",
    "enterprise AI adoption real world story with numbers and timeline",
]


# ════════════════════════════════════════════════════════════════
#  Mode: collect — 采集（分发到原 collect_*.py）
# ════════════════════════════════════════════════════════════════

def cmd_collect(source: str, args) -> int:
    """Run collection for the given source type."""
    script_map = {
        "rss": "collect_rss.py",
        "blogs": "collect_blogs.py",
        "consulting": "collect_consulting.py",
        "podcasts": "collect_podcasts.py",
    }
    script = script_map.get(source)
    if not script:
        print(f"❌ Unknown collection source: {source}")
        print(f"   Supported: {', '.join(script_map.keys())}")
        return 1
    
    script_path = COLLECTOR_DIR / script
    if not script_path.exists():
        print(f"❌ Script not found: {script_path}")
        return 1
    
    cmd = [sys.executable, str(script_path)]
    env = os.environ.copy()
    if args.force:
        env["FORCE"] = "1"
    
    print(f"▶  collect {source}...")
    result = subprocess.run(cmd, env=env)
    return result.returncode


# ════════════════════════════════════════════════════════════════
#  Mode: save — 保存（分发到原 save_*.py）
# ════════════════════════════════════════════════════════════════

def cmd_save(source: str, args) -> int:
    """Save collected items to reports directory."""
    script_map = {
        "rss": "save_rss.py",
        "blogs": "save_blogs.py",
        "consulting": "save_consulting.py",
    }
    script = script_map.get(source)
    if not script:
        print(f"❌ Unknown save source: {source}")
        return 1
    
    script_path = COLLECTOR_DIR / script
    if not script_path.exists():
        print(f"❌ Script not found: {script_path}")
        return 1
    
    cmd = [sys.executable, str(script_path)]
    if args.date:
        cmd.extend(["--date", args.date])
    
    print(f"▶  save {source}...")
    return subprocess.run(cmd).returncode


# ════════════════════════════════════════════════════════════════
#  Mode: filter — 过滤（分发到原 filter_items.py）
# ════════════════════════════════════════════════════════════════

def cmd_filter(source: str, args) -> int:
    """Filter collected items using LLM."""
    filter_py = COLLECTOR_DIR / "filter_items.py"
    if not filter_py.exists():
        print(f"❌ filter_items.py not found")
        return 1
    
    cmd = [sys.executable, str(filter_py), source]
    if args.date:
        cmd.extend(["--date", args.date])
    if args.dry_run:
        cmd.append("--dry-run")
    if args.batch_size:
        cmd.extend(["--batch-size", str(args.batch_size)])
    if args.model:
        env = os.environ.copy()
        env["FILTER_MODEL"] = args.model
        print(f"▶  filter {source} (model={args.model})...")
        return subprocess.run(cmd, env=env).returncode
    
    print(f"▶  filter {source}...")
    return subprocess.run(cmd).returncode


# ════════════════════════════════════════════════════════════════
#  Mode: search — AnySearch 案例搜索
# ════════════════════════════════════════════════════════════════

def cmd_search(query: str, args) -> int:
    """Search for case studies using AnySearch API."""
    from lib import fetch_url
    
    url = "https://api.anysearch.com/v1/search"
    api_key = os.environ.get("ANYSEARCH_API_KEY", "as_sk_688a742d1ce960add6350d87f2adf1ac")
    
    payload = json.dumps({
        "query": query,
        "domains": ["business", "tech"],
        "content_types": ["web", "news"],
        "max_results": args.max_results or 10,
    })
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    import urllib.request
    req = urllib.request.Request(url, data=payload.encode(), headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"❌ Search error: {e}")
        return 1
    
    results = data.get("data", {}).get("results", [])
    
    # Filter: require meaningful content
    filtered = []
    for r in results:
        content = (r.get("content", "") or r.get("snippet", "") or "")
        url_str = r.get("url", "").strip()
        title = r.get("title", "").strip()
        
        if not url_str or not content or len(content) < 200:
            continue
        
        # Quality scoring
        score = 0
        for kw in ["production", "results", "deployment", "%", "metrics", "implemented", 
                    "enterprise", "revenue", "automated", "cost savings", "accuracy"]:
            if kw in content.lower():
                score += 1
        
        filtered.append({
            "title": title,
            "url": url_str,
            "content": content[:3000],
            "score": score,
            "content_length": len(content),
        })
    
    filtered.sort(key=lambda x: x["score"], reverse=True)
    
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps({"query": query, "results": filtered}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"✓ Saved {len(filtered)} results to {output_path}")
    else:
        print(f"\n=== Search: {query} ===")
        for i, r in enumerate(filtered[:args.max_results or 10], 1):
            print(f"\n[{i}] {r['title']}")
            print(f"    {r['url']}")
            print(f"    Score: {r['score']} | {r['content_length']} chars")
        print(f"\nTotal: {len(filtered)} matches")
    
    return 0


# ════════════════════════════════════════════════════════════════
#  Mode: case — 案例发现（外部来源）
# ════════════════════════════════════════════════════════════════

def _load_used_case_urls() -> set:
    used_file = COLLECTOR_DIR / ".state" / "external_used_urls.txt"
    if not used_file.exists():
        return set()
    return {line.strip() for line in used_file.read_text().split("\n") if line.strip()}


def _mark_case_url_used(url: str):
    used_file = COLLECTOR_DIR / ".state" / "external_used_urls.txt"
    used_file.parent.mkdir(parents=True, exist_ok=True)
    with open(used_file, "a") as f:
        f.write(url.strip().rstrip("/") + "\n")


def cmd_case_discover(args) -> int:
    """Discover a case study from external sources.
    
    Strategy:
      1. If --search provided, use AnySearch
      2. Try fetching from known collection sites
      3. Fall back to pre-configured search queries
    """
    used_urls = _load_used_case_urls()
    
    # Strategy 0: Direct URL fetch if --from-url provided
    if args.from_url:
        url = args.from_url.strip()
        print(f"  📡 Fetching: {url}")
        content = _fetch_url_as_text(url)
        if content and len(content) > 500:
            print(f"  ✓ Got {len(content)} chars")
            result = {
                "url": url,
                "title": args.title or url.split("/")[-1].replace("-", " ").title(),
                "content": content[:10000],
                "source": "direct",
            }
            if args.output:
                _save_case_result(result, args.output)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2)[:500])
            _mark_case_url_used(url)
            return 0
        print("  ❌ Failed to fetch content")
        return 1
    
    # Strategy 1: AnySearch
    if args.search:
        print(f"  🔍 Searching: {args.search}")
        results = _search_cases(args.search, max_results=args.max_results or 5)
        for r in results:
            if r["url"] in used_urls:
                continue
            content = r.get("full_content") or f"Title: {r['title']}\n\n{r['content']}"
            result = {
                "url": r["url"],
                "title": r["title"],
                "content": content[:10000],
                "source": "anysearch",
            }
            _mark_case_url_used(r["url"])
            if args.output:
                _save_case_result(result, args.output)
            else:
                print(f"  ✓ Found: {r['title'][:80]}")
                print(json.dumps(result, ensure_ascii=False, indent=2)[:500])
            return 0
    
    # Strategy 2: Collection sites
    for src in EXTERNAL_SOURCES:
        url = src["url"]
        if url in used_urls:
            continue
        print(f"  📡 Checking {src['name']}...")
        content = _fetch_url_as_text(url, timeout=10)
        if content and len(content) > 500:
            print(f"  ✓ Got {len(content)} chars from {src['name']}")
            result = {
                "url": url,
                "title": f"Case Study from {src['name']}",
                "content": content[:10000],
                "source": src["name"],
            }
            _mark_case_url_used(url)
            if args.output:
                _save_case_result(result, args.output)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2)[:500])
            return 0
        print(f"    (empty or failed)")
    
    # Strategy 3: Pre-configured queries
    for q in CASE_SEARCH_QUERIES:
        print(f"  🔍 Searching: {q[:60]}...")
        results = _search_cases(q, max_results=5)
        for r in results:
            if r["url"] in used_urls:
                continue
            content = r.get("full_content") or f"Title: {r['title']}\n\n{r['content']}"
            result = {
                "url": r["url"],
                "title": r["title"],
                "content": content[:10000],
                "source": "anysearch",
            }
            _mark_case_url_used(r["url"])
            if args.output:
                _save_case_result(result, args.output)
            else:
                print(f"  ✓ Found: {r['title'][:80]}")
                print(json.dumps(result, ensure_ascii=False, indent=2)[:500])
            return 0
    
    print("  ❌ No case found")
    return 1


def _fetch_url_as_text(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch a URL and extract readable text."""
    try:
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        text = resp.text
        # Strip HTML tags
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:15000]
    except Exception as e:
        print(f"  ⚠️ Fetch error: {e}")
        return None


def _search_cases(query: str, max_results: int = 10) -> list[dict]:
    """Search for case studies using AnySearch."""
    url = "https://api.anysearch.com/v1/search"
    api_key = os.environ.get("ANYSEARCH_API_KEY", "as_sk_688a742d1ce960add6350d87f2adf1ac")
    
    payload = json.dumps({
        "query": query,
        "domains": ["business", "tech"],
        "content_types": ["web", "news"],
        "max_results": max_results,
    })
    
    import urllib.request
    req = urllib.request.Request(
        url, data=payload.encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  ⚠️ AnySearch error: {e}")
        return []
    
    results = []
    for r in data.get("data", {}).get("results", []):
        url_str = r.get("url", "").strip()
        content = (r.get("content", "") or r.get("snippet", "") or "")
        title = r.get("title", "").strip()
        
        if not url_str or not content or len(content) < 200:
            continue
        
        # Score by keyword density
        score = sum(1 for kw in [
            "production", "results", "deployment", "%", "metrics",
            "implemented", "enterprise", "revenue", "automated",
            "cost savings", "accuracy", "company",
        ] if kw in content.lower())
        
        results.append({
            "title": title,
            "url": url_str,
            "content": content[:3000],
            "full_content": f"Title: {title}\n\n{content[:8000]}",
            "score": score,
            "content_length": len(content),
        })
    
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def _save_case_result(result: dict, output: str):
    """Save case discovery result to file."""
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✓ Saved to {output_path}")


# ════════════════════════════════════════════════════════════════
#  Mode: stats — 每日统计
# ════════════════════════════════════════════════════════════════

def cmd_stats(args) -> int:
    """Run daily statistics."""
    stats_py = COLLECTOR_DIR / "stats_daily.py"
    if not stats_py.exists():
        print("❌ stats_daily.py not found")
        return 1
    
    cmd = [sys.executable, str(stats_py)]
    if args.save:
        cmd.append("--save")
    
    print("▶  Running daily stats...")
    return subprocess.run(cmd).returncode


# ════════════════════════════════════════════════════════════════
#  Mode: list-sources — 列出所有可用的采集源
# ════════════════════════════════════════════════════════════════

def cmd_list_sources() -> int:
    """List all configured sources with categories."""
    
    print("\n┌────────────────────────────────────────────────────────────┐")
    print("│ Content Collector — Source Inventory                      │")
    print("└────────────────────────────────────────────────────────────┘")
    
    # RSS sources
    print("\n📡 RSS Feeds:")
    from lib import get_sources
    rss_sources = get_sources("rss")
    for s in rss_sources:
        print(f"   • {s.get('name', '?')}: {s.get('url', '?')}")
    
    # Blog sources
    print("\n📝 Blog Sources:")
    blog_sources = get_sources("blogs")
    for s in blog_sources:
        print(f"   • {s.get('name', '?')}: {s.get('url', '?')}")
    
    # External case sources
    print("\n📋 External Case Sources:")
    for s in EXTERNAL_SOURCES:
        print(f"   • [{s['type']}] {s['name']}: {s['url']}")
    
    print()
    return 0


# ════════════════════════════════════════════════════════════════
#  main()
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Unified Content Collector — 统一采集、搜索、案例发现入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
分类:
  collect       采集原始内容（RSS/Blogs/Consulting）
  save          保存采集内容到 reports 目录
  filter        用 LLM 过滤采集内容
  search        用 AnySearch 搜索案例
  case          发现外部案例来源
  stats         每日统计
  list-sources  列出所有采集源

示例:
  python3 collect.py collect rss
  python3 collect.py filter blogs
  python3 collect.py search "enterprise AI agent results"
  python3 collect.py case --search "retail AI deployment results"
  python3 collect.py case --from-url https://retool.com/blog/example
  python3 collect.py stats --save
  python3 collect.py list-sources
        """,
    )
    
    parser.add_argument("mode", nargs="?",
                        choices=["collect", "save", "filter", "search", "case", "stats", "list-sources"],
                        help="Operation mode")
    parser.add_argument("source", nargs="?",
                        help="Source name (rss/blogs/consulting/podcasts)")
    
    # Common flags
    parser.add_argument("--date", type=str, default=None,
                        help="Date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Dry run (no side effects)")
    parser.add_argument("--force", action="store_true",
                        help="Force run (skip day-of-week checks)")
    
    # Search / Case flags
    parser.add_argument("--search", type=str, default=None,
                        help="Search query")
    parser.add_argument("--from-url", type=str, default=None,
                        help="Direct URL to fetch as case source")
    parser.add_argument("--title", type=str, default=None,
                        help="Title for --from-url")
    parser.add_argument("--output", type=str, default=None,
                        help="Output file path (JSON)")
    parser.add_argument("--max-results", type=int, default=10,
                        help="Max search results")
    
    # Filter flags
    parser.add_argument("--batch-size", type=int, default=None,
                        help="Batch size for LLM filtering")
    parser.add_argument("--model", type=str, default=None,
                        help="Model override for filtering")
    
    # Stats flags
    parser.add_argument("--save", action="store_true",
                        help="Save stats to file")
    
    args = parser.parse_args()
    
    if not args.mode:
        parser.print_help()
        return 0
    
    if args.mode == "list-sources":
        return cmd_list_sources()
    
    if args.mode == "stats":
        return cmd_stats(args)
    
    if args.mode in ("collect", "save", "filter"):
        if not args.source:
            print(f"❌ Missing source for mode '{args.mode}'")
            print(f"   Usage: collect.py {args.mode} <source>")
            print(f"   Sources: rss, blogs, consulting, podcasts")
            return 1
    
    mode_map = {
        "collect": cmd_collect,
        "save": cmd_save,
        "filter": cmd_filter,
        "search": lambda _, a: cmd_search(a.search or "", a),
        "case": lambda _, a: cmd_case_discover(a),
    }
    
    handler = mode_map.get(args.mode)
    if handler:
        return handler(args.source if args.source else "", args)
    
    print(f"❌ Unknown mode: {args.mode}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
