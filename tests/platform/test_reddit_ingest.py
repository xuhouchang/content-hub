import content_platform.ingest.reddit as reddit_ingest


def _fake_post(post_id, title, selftext="", score=100, num_comments=42,
               upvote_ratio=0.9, permalink="/r/artificial/comments/abc/enterprise_ai_deployment/",
               is_self=True, stickied=False, external_url=""):
    return {
        "id": post_id,
        "title": title,
        "selftext": selftext,
        "score": score,
        "num_comments": num_comments,
        "upvote_ratio": upvote_ratio,
        "permalink": permalink,
        "is_self": is_self,
        "stickied": stickied,
        "created_utc": 1780000000,
        "author": "some_user",
        "url": external_url or f"https://www.reddit.com{permalink}",
    }


def test_post_to_material_normalizes_shape_and_skips_irrelevant(monkeypatch):
    monkeypatch.setattr(reddit_ingest, "get_reddit_config", lambda: {"min_score": 0})

    relevant = _fake_post(
        "abc",
        "How enterprises are deploying AI agents in production",
        selftext="enterprise deployment workflow adoption governance metrics" * 5,
    )
    material = reddit_ingest.post_to_material(relevant, "artificial", "2026-08-22", "top", "day")

    assert material is not None
    assert material["url"] == "https://www.reddit.com/r/artificial/comments/abc/enterprise_ai_deployment/"
    assert material["source_type"] == "reddit"
    assert material["source_name"] == "r/artificial"
    assert material["relevance_hint"] in ("pass", "maybe")
    assert material["reddit_score"] == 100
    assert material["reddit_num_comments"] == 42
    assert material["external_url"] == ""

    celebrity = _fake_post("def", "Celebrity gossip about AI influencer")
    assert reddit_ingest.post_to_material(celebrity, "artificial", "2026-08-22", "top", "day") is None


def test_post_to_material_drops_low_score_and_keeps_external_url(monkeypatch):
    monkeypatch.setattr(reddit_ingest, "get_reddit_config", lambda: {"min_score": 50})

    low_score = _fake_post("ghi", "Enterprise AI adoption question", score=10)
    assert reddit_ingest.post_to_material(low_score, "artificial", "2026-08-22", "top", "day") is None

    link_post = _fake_post(
        "jkl",
        "Enterprise AI adoption guide",
        selftext="",
        score=100,
        is_self=False,
        external_url="https://example.com/enterprise-ai-guide",
    )
    material = reddit_ingest.post_to_material(link_post, "artificial", "2026-08-22", "top", "day")
    assert material is not None
    assert material["external_url"] == "https://example.com/enterprise-ai-guide"
    assert material["url"].startswith("https://www.reddit.com/r/artificial/comments/")


def test_load_reddit_materials_dedups_filters_and_limits(monkeypatch):
    monkeypatch.setattr(
        reddit_ingest,
        "get_reddit_config",
        lambda: {"enabled": True, "sort": "top", "time_filter": "day",
                 "max_items_per_sub": 15, "min_score": 0},
    )
    monkeypatch.setattr(
        reddit_ingest,
        "get_subreddits",
        lambda: [{"name": "artificial", "max_items": 15}],
    )
    monkeypatch.setattr(reddit_ingest, "load_url_registry", lambda: {"https://www.reddit.com/r/artificial/comments/dupe/x/": {}})
    monkeypatch.setattr(reddit_ingest, "get_reddit_token", lambda: None)

    def fake_fetch(sub, sort, time_filter, limit, token):
        return [
            _fake_post("pass", "Enterprise AI deployment workflow guide",
                       selftext="enterprise deployment workflow adoption governance metrics" * 5),
            _fake_post("dupe", "Duplicate thread", permalink="/r/artificial/comments/dupe/x/"),
            _fake_post("skip", "Movie review thread", score=200),
        ]

    monkeypatch.setattr(reddit_ingest, "fetch_subreddit_posts", fake_fetch)

    materials = reddit_ingest.load_reddit_materials("2026-08-22")

    assert [m["url"] for m in materials] == [
        "https://www.reddit.com/r/artificial/comments/abc/enterprise_ai_deployment/"
    ]
    assert materials[0]["source_type"] == "reddit"


def test_load_reddit_materials_disabled_when_configured_off(monkeypatch):
    monkeypatch.setattr(reddit_ingest, "get_reddit_config", lambda: {"enabled": False})
    monkeypatch.setattr(reddit_ingest, "get_subreddits", lambda: [{"name": "artificial"}])

    assert reddit_ingest.load_reddit_materials("2026-08-22") == []


def test_load_reddit_materials_enriches_link_posts(monkeypatch):
    monkeypatch.setattr(
        reddit_ingest,
        "get_reddit_config",
        lambda: {"enabled": True, "sort": "top", "time_filter": "day",
                 "max_items_per_sub": 15, "min_score": 0},
    )
    monkeypatch.setattr(reddit_ingest, "get_subreddits", lambda: [{"name": "artificial", "max_items": 15}])
    monkeypatch.setattr(reddit_ingest, "load_url_registry", dict)
    monkeypatch.setattr(reddit_ingest, "get_reddit_token", lambda: None)

    def fake_fetch(sub, sort, time_filter, limit, token):
        return [
            _fake_post("link", "Enterprise AI deployment guide",
                       selftext="", is_self=False,
                       external_url="https://example.com/enterprise-ai-guide"),
        ]

    monkeypatch.setattr(reddit_ingest, "fetch_subreddit_posts", fake_fetch)
    monkeypatch.setattr(
        reddit_ingest,
        "fetch_url",
        lambda url, prefer="direct", timeout=5: "enterprise deployment workflow adoption " * 80,
    )
    monkeypatch.setattr(
        reddit_ingest,
        "extract_page_summary",
        lambda html, max_chars=4000: html[:max_chars],
    )

    materials = reddit_ingest.load_reddit_materials("2026-08-22")

    assert len(materials) == 1
    assert materials[0]["content_text"].startswith("enterprise deployment workflow")
    assert len(materials[0]["content_text"]) >= 300