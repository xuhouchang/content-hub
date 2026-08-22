import os

from lib import guess_relevance_reason
from lib import is_duplicate
from lib import is_monday
from lib import load_sources
from lib import load_url_registry
from lib import quick_relevance_check


MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


def fetch_page(url: str) -> str | None:
    try:
        from collect_consulting import fetch_page as collect_consulting_page
    except ModuleNotFoundError:
        return None

    return collect_consulting_page(url)


def extract_article_links(html_text: str, base_url: str, source_name: str) -> list[dict]:
    try:
        from collect_consulting import extract_article_links as collect_article_links
    except ModuleNotFoundError:
        return []

    return collect_article_links(html_text, base_url, source_name)


def serper_search(queries: list[str], api_key: str, num_days: str = "m") -> list[dict]:
    try:
        from collect_consulting import serper_search as collect_consulting_search
    except ModuleNotFoundError:
        return []

    return collect_consulting_search(queries, api_key, num_days=num_days)


def load_consulting_materials(date_str: str, force: bool = False) -> list[dict]:
    try:
        sources_config = load_sources()
    except ModuleNotFoundError:
        sources_config = {}
    run_daily = sources_config.get("consulting", {}).get("run_daily", False) or force
    if not is_monday() and not run_daily:
        return []

    registry = load_url_registry()
    sources = sources_config or {}
    materials = []

    serper_key = os.environ.get("SERPER_API_KEY", "")
    consulting_cfg = sources.get("consulting", {})
    consulting_firms = consulting_cfg.get("firms", [])
    query_templates = consulting_cfg.get("search_queries", [])
    month_name = MONTH_NAMES[int(date_str.split("-")[1])]
    year = date_str.split("-")[0]

    if serper_key:
        for firm in consulting_firms:
            queries = [
                query.replace("{firm}", firm).replace("{month}", month_name).replace("{year}", year)
                for query in query_templates
            ]
            for result in serper_search(queries, serper_key, num_days="m"):
                url = result.get("url", "").strip()
                title = result.get("title", "")
                snippet = result.get("snippet", "")
                if not url or is_duplicate(url, registry):
                    continue

                relevance = quick_relevance_check(title, snippet)
                if relevance == "skip":
                    continue

                materials.append(
                    {
                        "url": url,
                        "title": title,
                        "summary": snippet,
                        "content_text": snippet,
                        "source_type": "consulting",
                        "source_name": f"Consulting: {firm}",
                        "date": result.get("date", date_str),
                        "relevance_hint": relevance,
                        "relevance_reason": guess_relevance_reason(title, snippet),
                    }
                )

    thinktank_cfg = sources.get("thinktank", [])
    if isinstance(thinktank_cfg, dict):
        thinktank_cfg = thinktank_cfg.get("sources", [])
    for source in thinktank_cfg:
        source_name = source["name"]
        source_url = source["url"]
        html_text = fetch_page(source_url)
        if not html_text:
            continue

        for entry in extract_article_links(html_text, source_url, source_name):
            entry_url = entry.get("url", "").strip()
            title = entry.get("title", "")
            if not entry_url or is_duplicate(entry_url, registry):
                continue

            relevance = quick_relevance_check(title, "")
            if relevance == "skip":
                continue

            materials.append(
                {
                    "url": entry_url,
                    "title": title,
                    "summary": "",
                    "content_text": "",
                    "source_type": "consulting",
                    "source_name": source_name,
                    "source_url": source_url,
                    "date": date_str,
                    "relevance_hint": relevance,
                    "relevance_reason": guess_relevance_reason(title, ""),
                }
            )

    return materials
