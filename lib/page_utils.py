"""Shared page-extraction helpers for content-platform ingest loaders.

Extracted from the legacy collectors (collect_blogs.py / collect_consulting.py)
so the new content_platform chain has no dependency on the old collector
scripts. Everything here is pure text/HTTP handling, no pipeline logic.
"""

import html
import json
import re
import urllib.parse

from lib import fetch_url


def extract_page_summary(html_text: str, max_chars: int = 2000) -> str:
    """Extract readable summary text from HTML page content.

    If content is already plain text (Jina reader output), returns as-is.
    Otherwise strips tags and boilerplate.
    """
    # Check if already plain text (no HTML tags)
    if "<" not in html_text[:500] or ">" not in html_text[:500]:
        return html_text.strip()[:max_chars]
    text = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
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
    """Extract article links from a blog listing page.

    Looks for <a> tags and tries to find title + URL.
    Returns list of {title, url, source_name, source_url, date}.
    """
    items = []
    seen_urls = set()

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


def fetch_page(url: str) -> str | None:
    """Fetch a webpage using unified fetch_url."""
    return fetch_url(url, timeout=20)


def extract_article_links(html_text: str, base_url: str, source_name: str) -> list[dict]:
    """Extract article links from a list page HTML.

    Returns list of {title, url, source_name}.
    This is intentionally broad — filtering happens downstream.
    """
    items = []
    seen_urls = set()

    # Match <a href="...">title</a> patterns
    for m in re.finditer(r'href="([^"]*)"[^>]*>\s*([^<]{15,200}?)\s*<', html_text, re.IGNORECASE):
        href = m.group(1)
        title = html.unescape(m.group(2).strip())

        # Normalize URL
        if href.startswith("/"):
            parsed = urllib.parse.urlparse(base_url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        elif href.startswith(("#", "javascript:")) or href in seen_urls:
            continue

        # Filter out non-article links
        if len(title) < 15 or len(href) < 20:
            continue
        if any(x in href.lower() for x in ["/tag/", "/category/", "/author/", "/page/", "#"]):
            continue

        seen_urls.add(href)
        items.append({"title": title, "url": href, "source_name": source_name})

    return items


def serper_search(queries: list[str], api_key: str, num_days: str = "m") -> list[dict]:
    """Search via Serper.dev Google Search API.

    Args:
        queries: List of search query strings.
        api_key: Serper.dev API key.
        num_days: Time range suffix for tbs param (m=month, w=week, d=day).

    Returns:
        List of {title, link, snippet, date} from organic results.
    """
    import http.client

    payload = [{"q": q, "tbs": f"qdr:{num_days}"} for q in queries]
    body = json.dumps(payload)

    conn = http.client.HTTPSConnection("google.serper.dev")
    conn.request("POST", "/search", body, headers={
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    })
    res = conn.getresponse()
    raw = res.read().decode("utf-8")
    results = json.loads(raw)

    items = []
    for r in results:
        for organic in r.get("organic", []):
            items.append({
                "title": organic.get("title", ""),
                "url": organic.get("link", ""),
                "snippet": organic.get("snippet", ""),
                "date": organic.get("date", ""),
            })
    return items