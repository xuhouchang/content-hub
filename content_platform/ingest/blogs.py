import time

from lib import fetch_url
from lib import guess_relevance_reason
from lib import is_duplicate
from lib import load_url_registry
from lib import quick_relevance_check


BLOG_SOURCES = None
LIST_PAGE_TIMEOUT_SECONDS = 10
ARTICLE_PAGE_TIMEOUT_SECONDS = 10
MAX_ARTICLES_PER_SOURCE = 5
MAX_BLOG_LOADER_SECONDS = 25


def _get_blog_sources() -> list[dict]:
    if BLOG_SOURCES is not None:
        return BLOG_SOURCES
    try:
        from collect_blogs import BLOG_SOURCES as configured_sources
    except ModuleNotFoundError:
        return []

    return configured_sources


def extract_links_from_html(html_text: str, base_url: str, source_name: str) -> list[dict]:
    try:
        from collect_blogs import extract_links_from_html as collect_blog_links
    except ModuleNotFoundError:
        return []

    return collect_blog_links(html_text, base_url, source_name)


def extract_page_summary(html: str, max_chars: int = 2000) -> str:
    try:
        from collect_blogs import extract_page_summary as collect_page_summary
    except ModuleNotFoundError:
        return html[:max_chars]

    return collect_page_summary(html, max_chars=max_chars)


def load_blog_materials(date_str: str) -> list[dict]:
    registry = load_url_registry()
    materials = []
    started_at = time.monotonic()

    for source in _get_blog_sources():
        if time.monotonic() - started_at >= MAX_BLOG_LOADER_SECONDS:
            break
        source_name = source["name"]
        source_url = source["url"]
        html_text = fetch_url(source_url, prefer="direct", timeout=LIST_PAGE_TIMEOUT_SECONDS)
        if not html_text:
            continue

        entries = extract_links_from_html(html_text, source_url, source_name)
        for entry in entries[:MAX_ARTICLES_PER_SOURCE]:
            if time.monotonic() - started_at >= MAX_BLOG_LOADER_SECONDS:
                break
            entry_url = entry.get("url", "").strip()
            if not entry_url or is_duplicate(entry_url, registry):
                continue

            title = entry.get("title", "")
            relevance = quick_relevance_check(title, "")
            if relevance == "skip":
                continue

            article_text = (
                fetch_url(
                    entry_url,
                    prefer=source.get("fetch_mode", "direct"),
                    timeout=ARTICLE_PAGE_TIMEOUT_SECONDS,
                )
                or ""
            )
            summary = extract_page_summary(article_text, max_chars=2000) if article_text else ""
            materials.append(
                {
                    "url": entry_url,
                    "title": title,
                    "summary": summary,
                    "content_text": summary,
                    "source_type": "blogs",
                    "source_name": source_name,
                    "source_url": source_url,
                    "date": entry.get("date", date_str),
                    "relevance_hint": relevance,
                    "relevance_reason": guess_relevance_reason(title, summary),
                }
            )

    return materials
