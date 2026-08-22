#!/usr/bin/env python3
"""
Part 2: AI Company Blog Scanner.
Fetches blog list pages → extracts recent posts → saves raw JSON for agent filtering.
"""

import sys
import json
import os
import re
import datetime
import html
import urllib.request
import urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import (
    load_url_registry, is_duplicate, save_raw, get_date_str,
    quick_relevance_check, guess_relevance_reason,
    get_sources, fetch_url,
)

# Blog sources — now loaded from sources.yaml
BLOG_SOURCES = get_sources("blogs")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def extract_page_summary(html: str, max_chars: int = 2000) -> str:
    """Extract readable summary text from HTML page content.
    If content is already plain text (Jina reader output), returns as-is.
    Otherwise strips tags and boilerplate.
    """
    # Check if already plain text (no HTML tags)
    if "<" not in html[:500] or ">" not in html[:500]:
        return html.strip()[:max_chars]
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<nav[^>]*>.*?</nav>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<header[^>]*>.*?</header>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<footer[^>]*>.*?</footer>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    cut_points = []
    for marker in ["Skip to main content", "Skip to content", "Here's what you need",
                   "Latest news", "Featured", "Read time:", "minutes ago"]:
        idx = text.find(marker)
        if 200 < idx < 3000:
            cut_points.append(idx)
    if cut_points:
        text = text[max(cut_points):]
    return text[:max_chars]


def extract_links_from_html(html_text: str, base_url: str, source_name: str) -> list[dict]:
    """
    Extract article links from a blog listing page.
    Looks for <a> tags and tries to find title + URL.
    """
    items = []
    seen_urls = set()

    # Pattern: <a href="...">title</a> — loose match around article cards
    # Also look for structured patterns
    # Pattern 1: Typical blog list — look for <a> with href containing /blog/ /news/ /research/ etc
    article_patterns = [
        r'<a\s+(?:[^>]*?\s+)?href="([^"]*?(?:/blog/|/news/|/research/|/index/)[^"]*)"[^>]*>([^<]{10,150})</a>',
        r'<a\s+(?:[^>]*?\s+)?href="([^"]*?(?:post|article|entry|item)/[^"]*)"[^>]*>([^<]{10,150})</a>',
        r'<a\s+(?:[^>]*?\s+)?href="(/[a-z0-9][a-z0-9/-]+)"[^>]*>([^<]{15,200})</a>',
    ]

    for pat in article_patterns:
        for m in re.finditer(pat, html_text, re.IGNORECASE):
            href = m.group(1).strip()
            title = html.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip()

            # Normalize URL
            if href.startswith("/"):
                parsed = urllib.parse.urlparse(base_url)
                href = f"{parsed.scheme}://{parsed.netloc}{href}"
            elif not href.startswith("http"):
                href = base_url.rstrip("/") + "/" + href.lstrip("/")

            # De-duplicate within this page
            if href in seen_urls:
                continue
            seen_urls.add(href)

            # Skip non-article links
            if any(x in href.lower() for x in ["#", "javascript", "mailto:", "tag/", "category/", "page/"]):
                continue

            # Skip generic pages
            title_lower = title.lower()
            if any(x in title_lower for x in ["sign up", "log in", "subscribe", "contact us", "about us"]):
                continue

            # Skip known category/index landing pages (no publication date, just link lists)
            # These are static nav pages that get scraped identically every day
            skip_url_patterns = [
                "/news/ai-adoption", "/news/company-announcements", "/news/research",
                "/news/product-releases", "/news/safety", "/news/engineering",
                "/news/security", "/news/global-affairs",
                "/safety/", "/security-and-privacy/", "/trust-and-transparency/",
                "/product/security",
                "/innovation-and-ai/infrastructure-and-cloud/",
                "/innovation-and-ai/technology/safety-security/",
                "/categories/", "/services/",
                "/label/hardware", "/label/security-privacy",
                "/grok/business",
            ]
            href_lower = href.lower()
            if any(p in href_lower for p in skip_url_patterns):
                continue

            # Skip very short titles (likely nav items)
            if len(title) < 10:
                continue

            items.append({
                "title": title.strip(),
                "url": href,
                "source_name": source_name,
                "source_url": base_url,
                "date": "",
            })

    return items


def main():
    date_str = get_date_str()
    registry = load_url_registry()
    from datetime import date
    two_days_ago = date.today().isoformat()

    all_raw_items = []
    skipped_dupes = 0
    skipped_filter = 0
    maybe_count = 0
    fetch_errors = 0

    print(f"Part 2: AI Company Blog Scanning — {date_str}")

    for source in BLOG_SOURCES:
        name = source["name"]
        url = source["url"]
        fetch_mode = source.get("fetch_mode", "direct")

        print(f"  Scanning: {name} ({url})")
        # List pages (blog index) fetch via direct HTTP only — need HTML for link extraction
        html_text = fetch_url(url, prefer="direct")

        if not html_text:
            # Last resort: try crawl4ai for link extraction
            html_text = fetch_url(url, prefer="crawl4ai")
            fetch_errors += 1
            continue

        entries = extract_links_from_html(html_text, url, name)

        # Filter: only last 2 days where possible
        # Most blog list pages don't show dates, so we apply post-hoc date resolution later
        print(f"    Found ~{len(entries)} potential article links")

        for entry in entries:
            entry_url = entry["url"].strip()

            if is_duplicate(entry_url, registry):
                skipped_dupes += 1
                continue

            check = quick_relevance_check(entry["title"], "")
            if check == "skip":
                skipped_filter += 1
                continue

            entry["_relevance_quick"] = check
            entry["_relevance_reason"] = guess_relevance_reason(entry["title"], "")
            entry["_source"] = "blog"

            if check == "maybe":
                maybe_count += 1

            # Fetch article content for summary (will be used by filter)
            article_fetch_mode = source.get("fetch_mode", "direct")
            try:
                article_text = fetch_url(entry_url, prefer=article_fetch_mode)
                if article_text:
                    entry["summary"] = extract_page_summary(article_text, max_chars=2000)
                    if entry["summary"]:
                        print(f"    📄 Fetched content: {len(entry['summary'])}c")
            except Exception as e:
                print(f"    ⚠️ Content fetch failed: {e}")

            all_raw_items.append(entry)

    print(f"\n  Results: {len(all_raw_items)} new items (pass/maybe)")
    print(f"  Filtered out: {skipped_dupes} dupes, {skipped_filter} by filter, {fetch_errors} fetch errors")
    print(f"  Needs agent review: {maybe_count}")

    if all_raw_items:
        filename = f"blogs_raw_{date_str}.json"
        path = save_raw("blogs", filename, all_raw_items)
        print(f"  Saved: {path}")
    else:
        print("  No new items to process.")

    summary = {
        "date": date_str,
        "source": "blogs",
        "total_new": len(all_raw_items),
        "skipped_dupes": skipped_dupes,
        "skipped_filter": skipped_filter,
        "fetch_errors": fetch_errors,
        "needs_agent": maybe_count,
    }
    print(f"\nSUMMARY:{json.dumps(summary)}")


if __name__ == "__main__":
    main()
