"""Reddit hot-discussion ingest loader for the content platform.

Fetches the hottest discussions from configured subreddits via Reddit's
public JSON endpoints (or app-only OAuth when REDDIT_CLIENT_ID /
REDDIT_CLIENT_SECRET are set), quick-filters them for enterprise-AI
relevance, and returns materials in the same shape as the other ingest
loaders (rss / blogs / consulting).

Configured in sources.yaml under the `reddit` section:
  reddit:
    enabled: true
    sort: top            # top / hot / rising
    time_filter: day     # hour / day / week / month / year
    max_items_per_sub: 15
    min_score: 0
    subreddits:
      - name: "artificial"
        priority: high
        topics: ["trends", "enterprise-ai"]
"""

import base64
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

from lib import (
    fetch_url,
    get_date_str,
    guess_relevance_reason,
    is_duplicate,
    load_sources,
    load_url_registry,
    quick_relevance_check,
)
from lib.page_utils import extract_page_summary

REDDIT_OAUTH_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API_URL = "https://oauth.reddit.com"
REDDIT_PUBLIC_URL = "https://www.reddit.com"
USER_AGENT = (
    "content-hub-collector/1.0 (enterprise-AI research pipeline; "
    "contact: xuhouchang@zerone-tech.com)"
)

REQUEST_TIMEOUT_SECONDS = 15
CONTENT_FETCH_TIMEOUT_SECONDS = 5
CONTENT_SUMMARY_MAX_CHARS = 4000
SELFTEXT_MAX_CHARS = 4000
SUMMARY_MAX_CHARS = 2000
MAX_LOADER_SECONDS = 30

# priority -> per-subreddit post cap
MAX_ITEMS_BY_PRIORITY = {"core": 25, "high": 25, "medium": 15, "low": 10}
DEFAULT_MAX_ITEMS = 15


def get_reddit_config() -> dict:
    """Return the `reddit` section of sources.yaml (empty dict if absent)."""
    try:
        sources = load_sources()
        return sources.get("reddit", {}) or {}
    except Exception:
        return {}


def get_subreddits() -> list[dict]:
    """Return enabled subreddits with per-sub max_items applied."""
    cfg = get_reddit_config()
    if cfg.get("enabled") is False:
        return []
    subs = [s for s in cfg.get("subreddits", []) if s.get("enabled", True) is not False]
    for s in subs:
        if "max_items" not in s:
            s["max_items"] = MAX_ITEMS_BY_PRIORITY.get(s.get("priority", "medium"), DEFAULT_MAX_ITEMS)
    return subs


def get_reddit_token() -> str | None:
    """Acquire an app-only OAuth token when Reddit API credentials exist.

    Falls back to anonymous public JSON when credentials are missing or the
    token request fails.
    """
    client_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    try:
        payload = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "read"}).encode()
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            REDDIT_OAUTH_URL,
            data=payload,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Authorization": f"Basic {auth}",
            },
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            token = data.get("access_token", "")
            if token:
                print(f"  [reddit] OAuth token acquired (expires {data.get('expires_in', '?')}s)")
                return token
    except Exception as e:
        print(f"  [reddit] OAuth token request failed ({e}); falling back to public JSON")
    return None


def fetch_subreddit_posts(sub: str, sort: str, time_filter: str, limit: int, token: str | None) -> list[dict]:
    """Fetch posts from one subreddit, returning raw post data dicts.

    Uses the authenticated API when a token is available, otherwise the
    public JSON endpoints. Stickied posts are dropped.
    """
    base = REDDIT_API_URL if token else REDDIT_PUBLIC_URL
    path = f"/r/{sub}/{sort}.json"
    query = {"limit": min(max(limit, 1), 100)}
    if sort in ("top", "controversial"):
        query["t"] = time_filter
    url = f"{base}{path}?{urllib.parse.urlencode(query)}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  [reddit] fetch r/{sub} failed: {e}")
        return []
    posts = []
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {})
        if post.get("stickied"):
            continue
        posts.append(post)
    return posts


def post_to_material(post: dict, sub: str, date_str: str, sort: str, time_filter: str) -> dict | None:
    """Convert a raw Reddit post into a material dict (None when skipped).

    The canonical URL is the discussion permalink; link posts keep the
    linked article in `external_url` for downstream content fetch.
    """
    post_id = post.get("id", "")
    permalink = post.get("permalink", "")
    if not post_id or not permalink:
        return None
    title = post.get("title", "")
    selftext = (post.get("selftext") or "").strip()
    score = int(post.get("score", 0) or 0)
    min_score = int(get_reddit_config().get("min_score", 0) or 0)
    if score < min_score:
        return None

    url = f"https://www.reddit.com{permalink}"
    created_utc = post.get("created_utc", 0) or 0
    if created_utc:
        post_date = datetime.datetime.fromtimestamp(created_utc, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    else:
        post_date = date_str
    summary = selftext[:SUMMARY_MAX_CHARS]

    check = quick_relevance_check(title, summary)
    if check == "skip":
        return None

    is_self = bool(post.get("is_self"))
    return {
        "url": url,
        "title": title,
        "summary": summary,
        "content_text": selftext[:SELFTEXT_MAX_CHARS],
        "source_type": "reddit",
        "source_name": f"r/{sub}",
        "source_url": f"https://www.reddit.com/r/{sub}/",
        "date": post_date,
        "relevance_hint": check,
        "relevance_reason": guess_relevance_reason(title, summary),
        "reddit_score": score,
        "reddit_num_comments": int(post.get("num_comments", 0) or 0),
        "reddit_upvote_ratio": post.get("upvote_ratio", 0.0),
        "reddit_author": post.get("author", ""),
        "reddit_sort": sort,
        "reddit_time_filter": time_filter,
        "external_url": (post.get("url", "") or "") if not is_self else "",
    }


def _enrich_link_post_content(material: dict) -> dict:
    """For link posts with thin selftext, try fetching the linked article.

    Falls back to the original selftext when the fetch fails or the page has
    no extractable content. Best-effort only — never raises.
    """
    if material.get("content_text") and len(material["content_text"]) >= 300:
        return material
    external_url = material.get("external_url", "")
    if not external_url or "reddit.com" in external_url:
        return material
    try:
        page = fetch_url(external_url, prefer="direct", timeout=CONTENT_FETCH_TIMEOUT_SECONDS) or ""
        content = extract_page_summary(page, max_chars=CONTENT_SUMMARY_MAX_CHARS) if page else ""
        if len(content.strip()) >= 300:
            material["content_text"] = content
    except Exception:
        pass
    return material


def load_reddit_materials(date_str: str) -> list[dict]:
    """Collect hot Reddit discussions as platform materials.

    Mirrors load_rss_materials: dedup via the URL registry, quick relevance
    filter, opportunistic content enrichment, capped by a global time budget.
    """
    cfg = get_reddit_config()
    if cfg.get("enabled") is False:
        print("  [reddit] disabled in sources.yaml, skipping")
        return []
    subs = get_subreddits()
    if not subs:
        print("  [reddit] no enabled subreddits in sources.yaml, skipping")
        return []

    sort = cfg.get("sort", "top")
    time_filter = cfg.get("time_filter", "day")
    max_items = int(cfg.get("max_items_per_sub", DEFAULT_MAX_ITEMS) or DEFAULT_MAX_ITEMS)

    registry = load_url_registry()
    token = get_reddit_token()
    materials = []
    started_at = time.monotonic()

    for sub in subs:
        if time.monotonic() - started_at >= MAX_LOADER_SECONDS:
            break
        sub_name = sub["name"]
        sub_max = min(int(sub.get("max_items", max_items)), max_items)
        print(f"  [reddit] fetching r/{sub_name} ({sort}/{time_filter}, limit={sub_max})")
        for post in fetch_subreddit_posts(sub_name, sort, time_filter, sub_max, token)[:sub_max]:
            if time.monotonic() - started_at >= MAX_LOADER_SECONDS:
                break
            material = post_to_material(post, sub_name, date_str, sort, time_filter)
            if material is None:
                continue
            if is_duplicate(material["url"], registry):
                continue
            material = _enrich_link_post_content(material)
            materials.append(material)

    print(f"  [reddit] {len(materials)} new materials from {len(subs)} subreddits")
    return materials


if __name__ == "__main__":
    date_str = get_date_str()
    for m in load_reddit_materials(date_str):
        print(f"  {m['reddit_score']:>5} ⬆ {m['reddit_num_comments']:>4} 💬  {m['title'][:70]}  ({m['source_name']})")