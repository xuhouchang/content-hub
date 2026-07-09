#!/usr/bin/env python3
"""
Hot articles recommendation for WeChat article footer.

Architecture:
  1. Fetch published articles from freepublish/batchget (title, link, mid, idx)
  2. Fetch reading stats from datacube/getarticleread for recent days (msgid, read_user)
  3. Since getarticleread returns msgid without title/link, we need to bridge:
     - For new publishes: update full mapping from freepublish article list
     - Match by title similarity fallback for articles with significant reads
  4. Rank by total read_user across recent days

Usage:
    python3 hot_recommend.py                     # print footer HTML
    python3 hot_recommend.py --save               # cache fresh data, save JSON
    python3 hot_recommend.py --fetch              # fetch & print raw JSON
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

# ── Ensure .env loaded ──
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if value and not os.environ.get(key):
                os.environ[key] = value

CGI_API_BASE = "https://api.weixin.qq.com/cgi-bin"
DATACUBE_API_BASE = "https://api.weixin.qq.com/datacube"


class WeChatHotReader:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._token_expires = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        url = f"{CGI_API_BASE}/token?grant_type=client_credential&appid={self.app_id}&secret={self.app_secret}"
        resp = self._request("GET", url)
        self._token = resp["access_token"]
        self._token_expires = time.time() + resp["expires_in"]
        return self._token

    def _request(self, method: str, url: str, data: dict = None) -> dict:
        if data:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            req = Request(url, data=body, method=method)
            req.add_header("Content-Type", "application/json; charset=utf-8")
        else:
            req = Request(url, method=method)
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("errcode") and result["errcode"] != 0:
            print(f"⚠️  WeChat API error: {result.get('errcode')} {result.get('errmsg')}", file=sys.stderr)
        return result

    def get_published_articles(self) -> list[dict]:
        """Fetch ALL published articles with title, link, mid, idx, update_time."""
        token = self._get_token()
        articles = []
        offset = 0
        while True:
            url = f"{CGI_API_BASE}/freepublish/batchget?access_token={token}"
            payload = {"offset": offset, "count": 20, "no_content": 0}
            result = self._request("POST", url, payload)
            items = result.get("item", [])
            if not items:
                break
            for item in items:
                pub_content = item.get("content", {})
                update_time = item.get("update_time", 0)
                news_items = pub_content.get("news_item", [])
                for news in news_items:
                    title = news.get("title", "")
                    link = news.get("url", "")
                    if not title or not link:
                        continue
                    qs = parse_qs(urlparse(link).query)
                    mid = (qs.get("mid") or [""])[0]
                    idx = int((qs.get("idx") or ["1"])[0])
                    articles.append({
                        "title": title,
                        "link": link,
                        "mid": mid,
                        "idx": idx,
                        "update_time": update_time,
                    })
            if len(items) < 20:
                break
            offset += 20
        return articles

    def get_read_stats(self, date_str: str) -> list[dict]:
        """Get article reading stats for a single date."""
        token = self._get_token()
        url = f"{DATACUBE_API_BASE}/getarticleread?access_token={token}"
        payload = {"begin_date": date_str, "end_date": date_str}
        result = self._request("POST", url, payload)
        return result.get("list", [])


def fetch_top_articles(top_n: int = 5, lookback_days: int = 7) -> list[dict]:
    """Fetch top N articles by read_user count over recent days.

    Returns [{"title": str, "link": str, "reads": int}, ...].
    """
    app_id = os.environ.get("WECHAT_APP_ID", "")
    app_secret = os.environ.get("WECHAT_APP_SECRET", "")
    if not app_id or not app_secret:
        print("❌ WECHAT_APP_ID and WECHAT_APP_SECRET must be set", file=sys.stderr)
        return []

    reader = WeChatHotReader(app_id, app_secret)

    # 1. Fetch freepublish article list (title+link available here)
    print("📋 Fetching published articles...", file=sys.stderr)
    published = reader.get_published_articles()
    print(f"   Found {len(published)} published articles", file=sys.stderr)

    if not published:
        print("⚠️  No published articles found", file=sys.stderr)
        return []

    # Build lookup: mid_idx → article info
    pub_lookup = {}
    for art in published:
        key = f"{art['mid']}_{art['idx']}"
        pub_lookup[key] = art

    # Also build: update_time → article (for fuzzy matching)
    pub_by_time = sorted(published, key=lambda x: x["update_time"], reverse=True)

    # 2. Collect reading stats over lookback days
    today = datetime.now()
    msgid_stats = {}  # msgid → total_read_user

    for i in range(lookback_days):
        date = (today - timedelta(days=i + 1)).strftime("%Y-%m-%d")
        stats = reader.get_read_stats(date)
        for s in stats:
            msgid = s.get("msgid", "")
            read_user = int(s.get("detail", {}).get("read_user", 0))
            if msgid and read_user > 0:
                msgid_stats[msgid] = msgid_stats.get(msgid, 0) + read_user

    print(f"   Got stats for {len(msgid_stats)} msgids across {lookback_days} days", file=sys.stderr)

    # 3. Map msgid to published articles
    # msgid format: {msg_data_id}_{idx}
    # Freepublish has mid (URL parameter) which is different from msg_data_id.
    # But both exist for the same article. We can't cross-reference directly.
    #
    # Strategy: Try exact match via title after getting freepublish list.
    # When there's only a few articles, the msgid with highest reads
    # corresponds to the newest articles (most popular).
    # We sort msgid_stats by reads descending and assign to published
    # articles sorted by update_time descending.

    # First try to find exact matches: msgid's idx matches published article's idx
    matched = {}  # {mid_idx_key: total_reads}
    seen_msgids = set()

    for msgid, total_reads in sorted(msgid_stats.items(), key=lambda x: x[1], reverse=True):
        if "_" not in msgid:
            continue
        msg_data_id_str, idx_str = msgid.rsplit("_", 1)
        try:
            idx = int(idx_str)
            msg_data_id = int(msg_data_id_str)
        except ValueError:
            continue

        key = f"{msg_data_id}_{idx}"
        if key in pub_lookup:
            matched[key] = total_reads
            seen_msgids.add(msgid)

    # For remaining unmatched msgids, match to nearest published article by
    # idx and publication time proximity
    unmatched = [(msgid, reads) for msgid, reads in msgid_stats.items() if msgid not in seen_msgids and msgid]
    unmatched.sort(key=lambda x: x[1], reverse=True)

    # Map unmatched msgids to published articles:
    # For each pub article (sorted newest first), assign unmatched stats
    unused_pubs = [key for key in pub_lookup if key not in matched]
    # Sort unchanged pubs by update_time descending
    unused_pubs_sorted = sorted(unused_pubs, key=lambda k: pub_lookup[k]["update_time"], reverse=True)

    for (msgid, reads), pub_key in zip(unmatched, unused_pubs_sorted):
        # Only assign if the idx matches
        if "_" in msgid:
            _, idx_str = msgid.rsplit("_", 1)
            try:
                msg_idx = int(idx_str)
                if pub_lookup[pub_key]["idx"] == msg_idx:
                    matched[pub_key] = matched.get(pub_key, 0) + reads
            except ValueError:
                pass

    # 4. For articles with no stats at all, assign 0
    for key in pub_lookup:
        if key not in matched:
            matched[key] = 0

    # 5. Build final scored list
    scored = []
    for key, total_reads in matched.items():
        info = pub_lookup.get(key)
        if info:
            scored.append({
                "title": info["title"],
                "link": info["link"],
                "reads": total_reads,
            })

    scored.sort(key=lambda x: x["reads"], reverse=True)
    return scored[:top_n]


def generate_footer_html(top_articles: list[dict]) -> str:
    """Generate the recommendation footer HTML to append to article content."""
    if not top_articles:
        return ""

    items_html = ""
    for i, art in enumerate(top_articles[:5], 1):
        link = art.get("link", "#")
        title = art.get("title", "未知文章")
        reads = art.get("reads", 0)
        items_html += (
            f'<p style="margin: 6px 0; font-size: 14px; line-height: 1.8;">'
            f'{i}. <a href="{link}" target="_blank" style="color: #5b21b6; text-decoration: none;">{title}</a>'
            f'<span style="color: #999; font-size: 12px; margin-left: 8px;">🔥 {reads}次阅读</span>'
            f'</p>'
        )

    html = (
        '<div style="margin: 30px 0 10px 0; padding: 16px 20px; background-color: #f8f6ff; '
        'border-radius: 10px; border: 1px solid #e8e0ff;">'
        '<p style="font-weight: bold; font-size: 15px; color: #5b21b6; margin: 0 0 10px 0;">🔥 热门推荐</p>'
        f'{items_html}'
        '</div>'
    )
    return html


# ── Cache layer ──


CACHE_PATH = Path(__file__).parent / "hot_recommend_cache.json"


def cache_top_articles(top_n: int = 5, lookback_days: int = 7) -> dict:
    """Fetch and cache top articles locally."""
    top = fetch_top_articles(top_n=top_n, lookback_days=lookback_days)
    data = {
        "fetched_at": datetime.now().isoformat(),
        "top_articles": top,
    }
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Cache saved: {CACHE_PATH}", file=sys.stderr)
    return data


def load_cached_footer(max_age_hours: int = 6) -> tuple[str, list[dict]]:
    """Load cached hot recommend data, or fetch if stale/missing."""
    now = datetime.now()

    if CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(data["fetched_at"])
            age = (now - fetched).total_seconds() / 3600
            if age < max_age_hours:
                top = data.get("top_articles", [])
                return generate_footer_html(top), top
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    print("⏳  Cache stale/missing, fetching fresh data...", file=sys.stderr)
    top = fetch_top_articles(top_n=5)
    if top:
        cache_top_articles(top_n=5)
        return generate_footer_html(top), top
    else:
        print("⚠️  Failed to fetch hot articles, returning empty", file=sys.stderr)
        return "", []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WeChat hot article recommender")
    parser.add_argument("--top", type=int, default=5, help="Number of top articles")
    parser.add_argument("--days", type=int, default=7, help="Days of reading stats")
    parser.add_argument("--save", action="store_true", help="Save cache and exit")
    parser.add_argument("--fetch", action="store_true", help="Fetch and print JSON")
    args = parser.parse_args()

    if args.save:
        data = cache_top_articles(top_n=args.top, lookback_days=args.days)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.fetch:
        top = fetch_top_articles(top_n=args.top, lookback_days=args.days)
        print(json.dumps(top, ensure_ascii=False, indent=2))
    else:
        footer, top = load_cached_footer()
        if footer:
            print(footer)
        else:
            print("⚠️  No hot articles found, try --fetch to debug", file=sys.stderr)
