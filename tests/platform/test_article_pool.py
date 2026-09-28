from content_platform.datasets.article_pool import build_article_pool


def test_article_pool_excludes_low_editorial_fit_primary_candidates():
    materials = [
        {
            "title": "GitHub pricing tiers",
            "editorial_fit_score": 0.2,
            "summary": "pricing change",
            "dedup": {"cluster_id": "c1"},
            "quality": {"content_chars": 5000},
        },
        {
            "title": "AI workflow redesign in finance ops",
            "editorial_fit_score": 0.9,
            "summary": "after deployment the agent deflected 38% of tickets",
            "dedup": {"cluster_id": "c2"},
            "quality": {"content_chars": 5000},
        },
    ]

    pool = build_article_pool(materials, topic_memory={"recent_outputs": []})

    assert [item["title"] for item in pool["candidates"]] == [
        "AI workflow redesign in finance ops"
    ]


def test_article_pool_blocks_recent_cluster_reuse():
    materials = [
        {
            "title": "AI workflow redesign",
            "editorial_fit_score": 0.9,
            "dedup": {"cluster_id": "cluster_1"},
            "quality": {"content_chars": 5000},
        },
    ]

    topic_memory = {"recent_outputs": [{"cluster_ids": ["cluster_1"]}]}
    pool = build_article_pool(materials, topic_memory=topic_memory)

    assert pool["candidates"] == []
