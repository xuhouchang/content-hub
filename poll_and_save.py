#!/usr/bin/env python3
"""
Poll for agent-filtered JSON files, then save items to reports.

Used by run_all.sh at the end of pipeline. Runs as background process(es).
Polls every 20 seconds for up to MAX_WAIT seconds until filtered file appears,
then calls the appropriate save handler.

All logic is Python: no agent prompt, no shell decision-making.
"""

import sys
import json
import os
import time
import datetime
import re
import html as html_mod
import urllib.request
from pathlib import Path

# ── Config ──
MAX_WAIT = 900  # 15 minutes total wait
POLL_INTERVAL = 20  # check every 20 seconds

COLLECTOR_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = COLLECTOR_DIR / "tmp"
REPORTS_DIR = Path(os.environ.get(
    "REPORTS_DIR",
    "/home/ubuntu/.openclaw/workspace/reports"
))

ALL_URLS_FILE = REPORTS_DIR / "_index" / "all_urls.tsv"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
MIN_CONTENT_CHARS = 300


# ── Helpers (duplicated from lib.py to keep this file self-contained) ──

def slugify(text: str) -> str:
    s = re.sub(r'[^\w\s-]', '', text.lower())
    s = re.sub(r'[-\s]+', '-', s).strip('-')
    return s[:60]


def append_to_url_registry(urls: list[str]):
    """Register multiple URLs as collected (dedup-aware)."""
    if not urls:
        return
    from lib import append_to_url_registry as _append_single, load_url_registry
    registry = load_url_registry()
    for url in urls:
        url = url.strip()
        if not url:
            continue
        if url not in registry:
            _append_single(url, "collected")
            registry[url] = {"status": "collected", "date": ""}


def update_daily_summary(date: str, part: str, counts: dict):
    daily_dir = REPORTS_DIR / "_index" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    filepath = daily_dir / f"{date}.md"
    entry = f"## {part}\n"
    for k, v in counts.items():
        entry += f"- {k}: {v}\n"
    entry += "\n"
    if filepath.exists():
        with open(filepath, "a") as f:
            f.write(entry)
    else:
        with open(filepath, "w") as f:
            f.write(f"# Daily Report Collection — {date}\n\n")
            f.write(entry)


def fetch_full_content(url: str) -> str | None:
    """Fetch a URL and extract text content.

    For HTML pages: fetches and strips tags.
    For PDFs: uses pdftotext or Jina Reader API as fallback.

    Falls back to Jina.ai Reader API when direct fetch fails or returns 404.
    """
    if not url:
        return None

    if url.lower().endswith('.pdf'):
        return _fetch_pdf_content(url)

    # ── Try direct fetch first ──
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_content = resp.read().decode("utf-8", errors="replace")
        if len(html_content) > 500:  # plausible page
            body_match = re.search(
                r'<body[^>]*>(.*?)</body>', html_content, re.DOTALL | re.IGNORECASE
            )
            body = body_match.group(1) if body_match else html_content
            body = re.sub(
                r'<script[^>]*>.*?</script>', '', body, flags=re.DOTALL | re.IGNORECASE
            )
            body = re.sub(
                r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL | re.IGNORECASE
            )
            body = re.sub(
                r'</?(?:p|div|h[1-6]|li|br|tr|blockquote|article|section)[^>]*>',
                chr(10), body
            )
            text = html_mod.unescape(body)
            text = re.sub(r'\n\s*\n', chr(10)+chr(10), text)
            text = re.sub(r'[ \t]+', ' ', text)
            result = text[:50000].strip() if len(text) > 50000 else text.strip()
            if len(result) >= 300:
                return result
    except Exception:
        pass

    # ── Fallback: crawl4ai (local, free) ──
    try:
        from crawl4ai import AsyncWebCrawler
        import asyncio

        async def do_fetch():
            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(
                    url=url,
                    word_count_threshold=10,
                    bypass_cache=True,
                    verbose=False,
                )
                md = result.markdown if result else None
                if md and len(md.strip()) >= 300:
                    return md.strip()[:50000]
                return None

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            crawl_result = loop.run_until_complete(do_fetch())
        finally:
            loop.close()

        if crawl_result:
            return crawl_result
    except Exception:
        pass

    return None


def _fetch_pdf_content(url: str) -> str | None:
    """Extract text from a PDF URL."""
    import subprocess
    import tempfile
    import shutil

    if not shutil.which('pdftotext'):
        return None

    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    tmp_path = tmp.name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            tmp.write(resp.read())
        tmp.close()

        try:
            result = subprocess.run(
                ['pdftotext', tmp_path, '-', '-l', '10'],
                capture_output=True, timeout=30
            )
            text = result.stdout.decode('utf-8', errors='replace').strip()
            if len(text) > 200:
                return text[:50000]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        try:
            jina_url = "https://r.jina.ai/" + url
            req2 = urllib.request.Request(jina_url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/plain",
            })
            with urllib.request.urlopen(req2, timeout=30) as resp:
                jina_text = resp.read().decode('utf-8', errors='replace')
                if 'Markdown Content:' in jina_text:
                    jina_text = jina_text.split('Markdown Content:', 1)[1]
                return jina_text[:50000].strip() if len(jina_text) > 50000 else jina_text.strip()
        except Exception:
            pass

        return None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def merge_with_raw(filtered: list, raw: list) -> list:
    raw_by_index = {i: item for i, item in enumerate(raw)}
    merged = []
    for fitem in filtered:
        idx = fitem.get("index", -1)
        if idx < 0 or idx not in raw_by_index:
            continue
        if fitem.get("verdict") != "pass":
            continue
        item = dict(raw_by_index[idx])
        item["topics"] = fitem.get("topics", [])
        item["verdict"] = "pass"
        item["_filter_reason"] = fitem.get("reason", "")
        merged.append(item)
    return merged


def has_substantive_content(content: str) -> bool:
    """Return whether fetched or fallback content is worth saving/indexing."""
    return len(content.strip()) >= MIN_CONTENT_CHARS


# ── Save Handlers ──

def save_rss(items: list, date_str: str):
    """Save passed RSS items as markdown files."""
    today = datetime.date.today()
    saved = 0
    new_urls = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        source_name = (item.get("source_name") or "unknown").split("(")[0].strip()
        source_slug = slugify(source_name)
        item_slug = slugify((item.get("title") or "untitled")[:40])
        save_dir = REPORTS_DIR / "newsletters" / source_slug / today.strftime("%Y-%m")
        save_dir.mkdir(parents=True, exist_ok=True)
        full_content = fetch_full_content(url)
        content_text = full_content or (item.get("summary") or "No content available")
        if not has_substantive_content(content_text):
            print(f"  ⏭️  Content too short ({len(content_text.strip())} chars): {url[:60]}")
            continue
        topics = item.get("topics", [])
        topics_str = json.dumps(list(topics))
        md = f"""---
title: "{item.get('title', 'Untitled')}"
source: "{source_name}"
source_type: newsletter
date: {item.get('date', date_str)}
url: {url}
topics: {topics_str}
relevance: high
---

{content_text}

---

*Collected by research-report-collector on {date_str}*
"""
        filepath = save_dir / f"{date_str}_{source_slug}_{item_slug}.md"
        with open(filepath, "w") as f:
            f.write(md)
        saved += 1
        new_urls.append(url)
        print(f"  Saved: {filepath.name}")
    if new_urls:
        append_to_url_registry(new_urls)
    update_daily_summary(date_str, "Part 1: RSS Feeds", {"saved": saved, "registered": len(new_urls)})
    print(f"  Done: {saved} RSS items saved, {len(new_urls)} registered")


def save_blogs(items: list, date_str: str):
    """Save passed blog items as markdown files."""
    today = datetime.date.today()
    saved = 0
    new_urls = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        source_name = item.get("source_name") or "unknown"
        source_slug = slugify(source_name)
        item_slug = slugify((item.get("title") or "untitled")[:50])
        save_dir = REPORTS_DIR / "blog" / "ai-companies" / today.strftime("%Y") / today.strftime("%Y-%m")
        save_dir.mkdir(parents=True, exist_ok=True)
        full_content = fetch_full_content(url)
        content_text = full_content or (item.get("summary") or "No content available")
        if not has_substantive_content(content_text):
            print(f"  ⏭️  Content too short ({len(content_text.strip())} chars): {url[:60]}")
            continue
        topics = item.get("topics", [])
        topics_str = json.dumps(list(topics))
        md = f"""---
title: "{item.get('title', 'Untitled')}"
source: "{source_name}"
source_type: blog
date: {date_str}
url: {url}
topics: {topics_str}
relevance: high
---

{content_text}

---

*Collected by research-report-collector on {date_str}*
"""
        filepath = save_dir / f"{date_str}_{source_slug}_{item_slug}.md"
        with open(filepath, "w") as f:
            f.write(md)
        saved += 1
        new_urls.append(url)
        print(f"  Saved: {filepath.name}")
    if new_urls:
        append_to_url_registry(new_urls)
    update_daily_summary(date_str, "Part 2: AI Company Blogs", {"saved": saved, "registered": len(new_urls)})
    print(f"  Done: {saved} blog items saved, {len(new_urls)} registered")


def save_consulting(items: list, date_str: str):
    """Save passed consulting items."""
    today = datetime.date.today()
    saved = 0
    new_urls = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        firm = item.get("firm") or item.get("source_name") or "consulting"
        firm_slug = slugify(firm)
        item_slug = slugify((item.get("title") or "untitled")[:40])
        save_dir = REPORTS_DIR / "consulting-reports" / firm_slug / today.strftime("%Y-%m")
        save_dir.mkdir(parents=True, exist_ok=True)
        full_content = fetch_full_content(url)
        content_text = full_content or item.get("summary") or "No content available"
        if not has_substantive_content(content_text):
            print(f"  ⏭️  Content too short ({len(content_text.strip())} chars): {url[:60]}")
            continue
        topics = item.get("topics", [])
        topics_str = json.dumps(list(topics))
        md = f"""---
title: "{item.get('title', 'Untitled')}"
source: "{firm}"
source_type: consulting
date: {item.get('date', date_str)}
url: {url}
topics: {topics_str}
relevance: high
---

{content_text}

---

*Collected by research-report-collector on {date_str}*
"""
        filepath = save_dir / f"{date_str}_{firm_slug}_{item_slug}.md"
        with open(filepath, "w") as f:
            f.write(md)
        saved += 1
        new_urls.append(url)
        print(f"  Saved: {filepath.name}")
    if new_urls:
        append_to_url_registry(new_urls)
    update_daily_summary(date_str, "Part 3: Consulting Reports", {"saved": saved, "registered": len(new_urls)})
    print(f"  Done: {saved} consulting items saved, {len(new_urls)} registered")


# ── Poll Logic ──

SOURCE_CONFIGS = {
    "rss": {
        "save_fn": save_rss,
        "filter_pattern": "rss_filtered_{date}.json",
        "raw_pattern": "rss_raw_{date}.json",
    },
    "blogs": {
        "save_fn": save_blogs,
        "filter_pattern": "blogs_filtered_{date}.json",
        "raw_pattern": "blogs_raw_{date}.json",
    },
    "consulting": {
        "save_fn": save_consulting,
        "filter_pattern": "consulting_filtered_{date}.json",
        "raw_pattern": "consulting_raw_{date}.json",
    },
    "thinktank": {
        "save_fn": save_consulting,
        "filter_pattern": "thinktank_filtered_{date}.json",
        "raw_pattern": "thinktank_raw_{date}.json",
    },
}


def poll_and_save(source: str, date_str: str):
    """Poll for filtered file and save when available."""
    config = SOURCE_CONFIGS.get(source)
    if not config:
        print(f"✗ Unknown source: {source}")
        return False

    tmp_dir_path = TMP_DIR / source
    if not tmp_dir_path.exists():
        print(f"  No tmp dir for {source}, nothing to do")
        return False

    filtered_pattern = config["filter_pattern"].format(date=date_str)
    raw_pattern = config["raw_pattern"].format(date=date_str)

    filtered_path = tmp_dir_path / filtered_pattern
    raw_path = tmp_dir_path / raw_pattern

    if not raw_path.exists():
        print(f"  No raw data for {source}, nothing to save")
        return False

    # Load raw data first (it should exist from Phase 1)
    with open(raw_path) as f:
        raw_data = json.load(f)

    if not raw_data:
        print(f"  Raw data for {source} is empty, nothing to do")
        return False

    # Quick-pass fallback: if filtered file won't come (no agent jobs),
    # use keyword-based passes
    # We check if agent jobs were even scheduled by looking at raw metadata
    # Agent filters exist if the raw file has entries without _relevance_quick set

    # Poll for filtered file
    waited = 0
    while waited < MAX_WAIT:
        if filtered_path.exists():
            print(f"\n  ✓ Filtered file appeared after {waited}s: {filtered_path.name}")
            try:
                with open(filtered_path) as f:
                    filtered_data = json.load(f)
            except (json.JSONDecodeError, Exception) as e:
                print(f"  ✗ Invalid filtered file, retrying in {POLL_INTERVAL}s: {e}")
                time.sleep(POLL_INTERVAL)
                waited += POLL_INTERVAL
                continue

            items = merge_with_raw(filtered_data, raw_data)
            if items:
                config["save_fn"](items, date_str)
            else:
                print(f"  No items passed filter for {source}")
            return True

        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    # Timed out: fall back to quick-pass items
    print(f"\n  ⏱  Timed out after {MAX_WAIT}s waiting for filtered file for {source}")
    quick_items = [
        i for i in raw_data if i.get("_relevance_quick") == "pass"
    ]
    if quick_items:
        print(f"  Using quick-pass fallback: {len(quick_items)} items")
        config["save_fn"](quick_items, date_str)
    else:
        print(f"  No quick-pass items and no agent verdict — nothing saved for {source}")
    return False


def save_from_strategist(candidates_path: str, date_str: str):
    """Save items from consulting-report-strategist agent output.

    The agent produces a JSON list of {url, firm, title, priority, discovery, notes}.
    This function fetches content and saves them as consulting reports.
    """
    path = Path(candidates_path)
    if not path.exists():
        print(f"✗ Candidate file not found: {candidates_path}")
        return False

    with open(path) as f:
        try:
            candidates = json.load(f)
        except json.JSONDecodeError as e:
            print(f"✗ Invalid candidate JSON: {e}")
            return False

    if not candidates:
        print("  No candidates, nothing to save")
        return False

    print(f"  Loading {len(candidates)} candidates from strategist...")

    # Filter to P0 and P1 only by default (configurable via env)
    min_priority = os.environ.get("STRATEGIST_MIN_PRIORITY", "P1")
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    min_level = priority_order.get(min_priority, 1)

    selected = [c for c in candidates
                if priority_order.get(c.get("priority", "P3"), 99) <= min_level
                and c.get("url", "").strip()]

    print(f"  {len(selected)} items pass priority filter (≥{min_priority})")

    if not selected:
        print("  Nothing to save after priority filter")
        return False

    # Wrap each item with metadata needed by save_consulting
    wrapped = []
    for c in selected:
        wrapped.append({
            "title": c.get("title", "Untitled"),
            "url": c["url"].strip(),
            "firm": c.get("firm", "Unknown"),
            "source_name": f"Consulting: {c.get('firm', 'Unknown')}",
            "summary": c.get("notes", ""),
            "topics": [],
            "_strategist_priority": c.get("priority", "P1"),
            "_strategist_discovery": c.get("discovery", ""),
        })

    save_consulting(wrapped, date_str)
    return True


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Poll for agent-filtered items and save")
    parser.add_argument("sources", nargs="*", choices=list(SOURCE_CONFIGS.keys()) + ["all"],
                        help="Source(s) to poll and save")
    parser.add_argument("--from-strategist", default=None,
                        help="Path to consulting-report-strategist candidates JSON")
    parser.add_argument("--date", default=None, help="Date string (YYYY-MM-DD), defaults to today")
    args = parser.parse_args()

    date_str = args.date or datetime.date.today().isoformat()

    # If --from-strategist mode, only consume candidates file
    if args.from_strategist:
        print(f"── strategist candidates ──")
        ok = save_from_strategist(args.from_strategist, date_str)
        sys.exit(0 if ok else 1)

    if not args.sources:
        parser.print_help()
        sys.exit(1)

    sources = list(SOURCE_CONFIGS.keys()) if "all" in args.sources else args.sources

    print(f"Polling for agent-filtered items: {', '.join(sources)}")
    print(f"Max wait: {MAX_WAIT}s, poll interval: {POLL_INTERVAL}s")
    print()

    all_ok = True
    for source in sources:
        print(f"── {source} ──")
        ok = poll_and_save(source, date_str)
        if not ok:
            all_ok = False
        print()

    if all_ok:
        print("All sources processed")
    else:
        print("Some sources had issues (timed out or no data)")
        sys.exit(1)


if __name__ == "__main__":
    main()
