import content_platform.ingest.blogs as blogs_ingest
import content_platform.ingest.cases as cases_ingest
import content_platform.ingest.consulting as consulting_ingest
import content_platform.ingest.rss as rss_ingest


def test_load_rss_materials_filters_duplicates_and_normalizes_shape(monkeypatch):
    monkeypatch.setattr(
        rss_ingest,
        "RSS_FEEDS",
        [{"name": "Example Feed", "url": "https://example.com/feed"}],
    )
    monkeypatch.setattr(rss_ingest, "load_url_registry", lambda: {"https://example.com/dupe": {}})
    monkeypatch.setattr(
        rss_ingest,
        "fetch_rss",
        lambda url: [
            {
                "title": "Enterprise workflow redesign guide",
                "url": "https://example.com/pass",
                "summary": "enterprise workflow redesign adoption governance",
                "source_name": "Example Feed",
            },
            {
                "title": "Duplicate workflow redesign guide",
                "url": "https://example.com/dupe",
                "summary": "enterprise workflow redesign adoption governance",
                "source_name": "Example Feed",
            },
            {
                "title": "Celebrity gossip",
                "url": "https://example.com/skip",
                "summary": "celebrity gossip",
                "source_name": "Example Feed",
            },
        ],
    )

    materials = rss_ingest.load_rss_materials("2026-06-03")

    assert [item["url"] for item in materials] == ["https://example.com/pass"]
    assert materials[0]["source_type"] == "rss"
    assert materials[0]["source_name"] == "Example Feed"
    assert materials[0]["content_text"] == materials[0]["summary"]


def test_load_rss_materials_prefers_fetched_article_content(monkeypatch):
    monkeypatch.setattr(
        rss_ingest,
        "RSS_FEEDS",
        [{"name": "Example Feed", "url": "https://example.com/feed"}],
    )
    monkeypatch.setattr(rss_ingest, "load_url_registry", lambda: {})
    monkeypatch.setattr(
        rss_ingest,
        "fetch_rss",
        lambda url: [
            {
                "title": "Enterprise workflow redesign guide",
                "url": "https://example.com/pass",
                "summary": "short summary",
                "source_name": "Example Feed",
            }
        ],
    )
    monkeypatch.setattr(
        rss_ingest,
        "fetch_url",
        lambda url, prefer="direct", timeout=30: "enterprise workflow redesign governance " * 80,
    )
    monkeypatch.setattr(
        rss_ingest,
        "extract_page_summary",
        lambda html, max_chars=4000: html[:max_chars],
    )

    materials = rss_ingest.load_rss_materials("2026-06-03")

    assert len(materials[0]["content_text"]) > len(materials[0]["summary"])
    assert materials[0]["content_text"].startswith("enterprise workflow redesign")


def test_load_rss_materials_limits_items_per_feed(monkeypatch):
    monkeypatch.setattr(
        rss_ingest,
        "RSS_FEEDS",
        [{"name": "Example Feed", "url": "https://example.com/feed"}],
    )
    monkeypatch.setattr(rss_ingest, "load_url_registry", lambda: {})
    monkeypatch.setattr(
        rss_ingest,
        "fetch_rss",
        lambda url: [
            {
                "title": f"Enterprise workflow redesign guide {i}",
                "url": f"https://example.com/pass/{i}",
                "summary": "enterprise workflow redesign adoption governance",
                "source_name": "Example Feed",
            }
            for i in range(8)
        ],
    )
    monkeypatch.setattr(
        rss_ingest,
        "fetch_url",
        lambda url, prefer="direct", timeout=30: "enterprise workflow redesign governance " * 40,
    )
    monkeypatch.setattr(
        rss_ingest,
        "extract_page_summary",
        lambda html, max_chars=4000: html[:max_chars],
    )

    materials = rss_ingest.load_rss_materials("2026-06-03")

    assert len(materials) == 3


def test_load_blog_materials_fetches_article_content_and_normalizes_shape(monkeypatch):
    monkeypatch.setattr(
        blogs_ingest,
        "BLOG_SOURCES",
        [{"name": "Example Blog", "url": "https://example.com/blog"}],
    )
    monkeypatch.setattr(blogs_ingest, "load_url_registry", lambda: {})

    def fake_fetch_url(url, prefer="direct", timeout=30):
        if url == "https://example.com/blog":
            return "<html></html>"
        if url == "https://example.com/blog/adoption-playbook":
            return "enterprise workflow redesign governance " * 80
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(blogs_ingest, "fetch_url", fake_fetch_url)
    monkeypatch.setattr(
        blogs_ingest,
        "extract_links_from_html",
        lambda html_text, base_url, source_name: [
            {
                "title": "Enterprise AI adoption playbook",
                "url": "https://example.com/blog/adoption-playbook",
                "source_name": source_name,
            }
        ],
    )
    monkeypatch.setattr(
        blogs_ingest,
        "extract_page_summary",
        lambda html, max_chars=2000: html[:max_chars],
    )

    materials = blogs_ingest.load_blog_materials("2026-06-03")

    assert len(materials) == 1
    assert materials[0]["url"] == "https://example.com/blog/adoption-playbook"
    assert materials[0]["source_type"] == "blogs"
    assert materials[0]["source_name"] == "Example Blog"
    assert materials[0]["content_text"].startswith("enterprise workflow redesign")


def test_load_blog_materials_limits_article_fetches_per_source(monkeypatch):
    monkeypatch.setattr(
        blogs_ingest,
        "BLOG_SOURCES",
        [{"name": "Example Blog", "url": "https://example.com/blog"}],
    )
    monkeypatch.setattr(blogs_ingest, "load_url_registry", lambda: {})
    monkeypatch.setattr(blogs_ingest, "MAX_ARTICLES_PER_SOURCE", 3)
    monkeypatch.setattr(blogs_ingest, "extract_page_summary", lambda html, max_chars=2000: html[:max_chars])
    monkeypatch.setattr(
        blogs_ingest,
        "extract_links_from_html",
        lambda html_text, base_url, source_name: [
            {"title": f"Enterprise AI adoption playbook {i}", "url": f"https://example.com/blog/{i}"}
            for i in range(5)
        ],
    )

    fetched_urls = []

    def fake_fetch_url(url, prefer="direct", timeout=30):
        fetched_urls.append(url)
        if url == "https://example.com/blog":
            return "<html></html>"
        return "enterprise workflow redesign governance " * 80

    monkeypatch.setattr(blogs_ingest, "fetch_url", fake_fetch_url)

    materials = blogs_ingest.load_blog_materials("2026-06-03")

    assert len(materials) == 3
    assert fetched_urls == [
        "https://example.com/blog",
        "https://example.com/blog/0",
        "https://example.com/blog/1",
        "https://example.com/blog/2",
    ]


def test_load_blog_materials_stops_after_time_budget(monkeypatch):
    monkeypatch.setattr(
        blogs_ingest,
        "BLOG_SOURCES",
        [
            {"name": "Example Blog 1", "url": "https://example.com/blog-1"},
            {"name": "Example Blog 2", "url": "https://example.com/blog-2"},
        ],
    )
    monkeypatch.setattr(blogs_ingest, "load_url_registry", lambda: {})
    monkeypatch.setattr(blogs_ingest, "MAX_BLOG_LOADER_SECONDS", 5)
    monkeypatch.setattr(
        blogs_ingest,
        "extract_links_from_html",
        lambda html_text, base_url, source_name: [],
    )

    clock = iter([0, 0, 6, 6, 6, 6])
    monkeypatch.setattr(blogs_ingest.time, "monotonic", lambda: next(clock))

    fetched_urls = []

    def fake_fetch_url(url, prefer="direct", timeout=30):
        fetched_urls.append(url)
        return "<html></html>"

    monkeypatch.setattr(blogs_ingest, "fetch_url", fake_fetch_url)

    materials = blogs_ingest.load_blog_materials("2026-06-03")

    assert materials == []
    assert fetched_urls == ["https://example.com/blog-1"]


def test_load_consulting_materials_returns_empty_when_not_scheduled(monkeypatch):
    # Offline: no source config and not Monday -> nothing is fetched.
    monkeypatch.setattr(consulting_ingest, "load_sources", lambda: {})
    monkeypatch.setattr(consulting_ingest, "is_monday", lambda: False)

    assert consulting_ingest.load_consulting_materials("2026-06-03") == []


def test_load_case_materials_returns_podcast_specs_when_forced(monkeypatch):
    # Offline: case materials load daily from podcast RSS without network.
    monkeypatch.setattr(
        cases_ingest,
        "_get_podcast_sources",
        lambda: {
            "youtube_channels": [],
            "rss_feeds": [
                {"name": "Example Podcast", "url": "https://example.com/podcast", "max_items": 3}
            ],
        },
    )
    monkeypatch.setattr(cases_ingest, "load_url_registry", lambda: {})
    monkeypatch.setattr(
        cases_ingest,
        "_fetch_podcast_rss",
        lambda name, feed_url, max_items=3: [
            {
                "title": "Example Podcast",
                "url": "https://example.com/podcast/episode-1",
                "summary": "enterprise AI rollout",
                "source_name": name,
                "source_type": "podcasts",
            }
        ],
    )

    materials = cases_ingest.load_case_materials("2026-06-03", force=True)

    assert len(materials) == 1
    assert materials[0]["title"] == "Example Podcast"
    assert materials[0]["source_type"] == "podcasts"
    assert materials[0]["url"] == "https://example.com/podcast/episode-1"
