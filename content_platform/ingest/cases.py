"""Case-study / Podcast materials — daily instead of weekly, with real RSS fetching."""
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote

import requests

from lib import guess_relevance_reason
from lib import is_duplicate
from lib import load_sources
from lib import load_url_registry
from lib import quick_relevance_check


# ── Podcast sources loaded from sources.yaml ──
def _get_podcast_sources():
    """Load podcast channel/feed config from sources.yaml."""
    from lib import load_sources as _load_sources
    all_src = _load_sources()
    cfg = all_src.get("podcast", {})
    return cfg

YT_TIMEOUT = 10
YT_RSS_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def _fetch_youtube_rss(channel_name: str, channel_id: str, max_items: int = 3) -> list[dict]:
    """Fetch recent videos from a YouTube channel via public RSS feed."""
    url = YT_RSS_TEMPLATE.format(channel_id)
    try:
        resp = requests.get(url, timeout=YT_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []
    for entry in list(root.findall("atom:entry", ns))[:max_items]:
        title = entry.findtext("atom:title", "", ns).strip()
        video_url = entry.findtext("atom:link", "", ns)
        # atom:link has href as attribute
        link_el = entry.find("atom:link", ns)
        if link_el is not None:
            video_url = link_el.get("href", "")
        published = entry.findtext("atom:published", "", ns)
        summary = entry.findtext("atom:media:group/atom:media:description", "", ns) or ""

        if not title:
            continue

        items.append({
            "url": video_url,
            "title": title,
            "summary": summary[:2000],
            "content_text": summary[:4000] or f"[YouTube Video] {title}",
            "source_type": "podcasts",
            "source_name": channel_name,
            "date": published[:10],
        })
    return items


def _fetch_podcast_rss(feed_name: str, feed_url: str, max_items: int = 3) -> list[dict]:
    """Fetch recent episodes from a podcast RSS feed."""
    try:
        import feedparser as fp
        parsed = fp.parse(feed_url)
    except Exception:
        return []

    items = []
    for entry in parsed.entries[:max_items]:
        title = entry.get("title", "").strip()
        link = entry.get("link", "")
        if not title:
            continue

        summary = entry.get("summary", "")[:2000]
        published = entry.get("published", entry.get("updated", ""))
        content_text = entry.get("content", [{}])[0].get("value", "") if entry.get("content") else summary

        items.append({
            "url": link,
            "title": title,
            "summary": summary,
            "content_text": content_text[:4000] or summary,
            "source_type": "podcasts",
            "source_name": feed_name,
            "date": published[:10],
        })
    return items


def load_case_materials(date_str: str, force: bool = False) -> list[dict]:
    """Load case-study/podcast materials — runs daily."""
    registry = load_url_registry()
    materials = []
    cfg = _get_podcast_sources()

    # 1. YouTube channels
    for ch in cfg.get("youtube_channels", []):
        max_items = ch.get("max_items", 3)
        for item in _fetch_youtube_rss(ch["name"], ch["channel_id"], max_items):
            if not item["url"] or is_duplicate(item["url"], registry):
                continue
            relevance = quick_relevance_check(item["title"], item["summary"])
            # Podcast/broadcast titles are often short and don't match enterprise AI keywords;
            # downgrade 'skip' to 'maybe' so LLM filter gets a chance
            if relevance == "skip":
                relevance = "maybe"
            item["relevance_hint"] = relevance
            item["relevance_reason"] = guess_relevance_reason(item["title"], item["summary"])
            materials.append(item)

    # 2. Podcast RSS feeds
    for feed in cfg.get("rss_feeds", []):
        max_items = feed.get("max_items", 3)
        for item in _fetch_podcast_rss(feed["name"], feed["url"], max_items):
            if not item["url"] or is_duplicate(item["url"], registry):
                continue
            relevance = quick_relevance_check(item["title"], item["summary"])
            if relevance == "skip":
                relevance = "maybe"
            item["relevance_hint"] = relevance
            item["relevance_reason"] = guess_relevance_reason(item["title"], item["summary"])
            materials.append(item)

    return materials
