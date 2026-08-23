import time
import urllib.request

import feedparser

from lib import fetch_url
from lib import guess_relevance_reason
from lib import is_duplicate
from lib import load_url_registry
from lib import get_sources
from lib import quick_relevance_check
from lib.page_utils import extract_page_summary


RSS_FEEDS = None
RSS_FEED_TIMEOUT_SECONDS = 5
ARTICLE_PAGE_TIMEOUT_SECONDS = 5
ARTICLE_SUMMARY_MAX_CHARS = 4000
MAX_ITEMS_PER_FEED = 3  # default; per-source feeds override via priority
MAX_RSS_LOADER_SECONDS = 20


def _get_rss_feeds() -> list[dict]:
    if RSS_FEEDS is not None:
        return RSS_FEEDS
    try:
        feeds = get_sources("rss")
        # Apply priority-based max_items
        for feed in feeds:
            if "max_items" not in feed:
                p = feed.get("priority", "medium")
                feed["max_items"] = {"core": 5, "high": 5, "medium": 3}.get(p, 3)
        return feeds
    except (ModuleNotFoundError, FileNotFoundError):
        return []


def fetch_rss(url: str, max_items: int = 20) -> list[dict]:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=RSS_FEED_TIMEOUT_SECONDS) as resp:
            parsed = feedparser.parse(resp.read())
    except Exception:
        return []

    source_name = parsed.feed.get("title", url)
    items = []
    for entry in parsed.entries[:max_items]:
        items.append(
            {
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "date": entry.get("published", entry.get("updated", "")),
                "summary": entry.get("summary", "")[:2000],
                "source_name": source_name,
                "source_url": url,
            }
        )
    return items


def load_rss_materials(date_str: str) -> list[dict]:
    registry = load_url_registry()
    materials = []
    started_at = time.monotonic()

    for feed in _get_rss_feeds():
        if time.monotonic() - started_at >= MAX_RSS_LOADER_SECONDS:
            break
        source_name = feed["name"]
        source_url = feed["url"]
        feed_max = feed.get("max_items", MAX_ITEMS_PER_FEED)
        for item in fetch_rss(source_url)[:feed_max]:
            if time.monotonic() - started_at >= MAX_RSS_LOADER_SECONDS:
                break
            item_url = item.get("url", "").strip()
            if not item_url or is_duplicate(item_url, registry):
                continue

            summary = item.get("summary", "")
            article_text = fetch_url(item_url, prefer="direct", timeout=ARTICLE_PAGE_TIMEOUT_SECONDS) or ""
            content_text = (
                extract_page_summary(article_text, max_chars=ARTICLE_SUMMARY_MAX_CHARS)
                if article_text
                else summary
            )
            relevance = quick_relevance_check(item.get("title", ""), summary)
            if relevance == "skip":
                continue

            materials.append(
                {
                    "url": item_url,
                    "title": item.get("title", ""),
                    "summary": summary,
                    "content_text": content_text,
                    "source_type": "rss",
                    "source_name": item.get("source_name", source_name),
                    "source_url": source_url,
                    "date": item.get("date", date_str),
                    "relevance_hint": relevance,
                    "relevance_reason": guess_relevance_reason(item.get("title", ""), summary),
                }
            )

    return materials
