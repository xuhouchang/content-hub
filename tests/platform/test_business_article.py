from content_platform.business.daily_article import topic_planner as tp
from content_platform.business.daily_article.topic_planner import pick_primary_cluster


def test_pick_primary_cluster_prefers_high_fit_high_novelty_candidate(monkeypatch):
    # Offline: no recent-topic history and no LLM executive re-ranking.
    monkeypatch.setattr(tp, "_load_recent_topics", lambda days=7: [])
    monkeypatch.setattr(tp, "_llm_rank_executive_value", lambda candidates, model="m": {})
    candidates = [
        {"title": "Pricing update", "editorial_fit_score": 0.2, "novelty_score": 0.9},
        {"title": "Workflow redesign", "editorial_fit_score": 0.9, "novelty_score": 0.7},
    ]

    picked = pick_primary_cluster(candidates)

    assert picked["title"] == "Workflow redesign"
