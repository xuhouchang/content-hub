"""M2 offline scenarios: evidence-led selection and qualified-final status.

No network, no LLM: the semantic/exec ordering is stubbed where needed.
"""

import json
import subprocess
from pathlib import Path

import pytest

import content_platform.runtime as runtime_module
from content_platform.business.daily_article import topic_planner as tp
from content_platform.business.daily_article.pipeline import run_daily_article_pipeline
from content_platform.datasets.article_pool import (
    build_article_pool,
    extract_candidate_evidence,
)


def _material(title, cluster, *, editorial=0.9, summary="", content="", content_chars=2000):
    return {
        "title": title,
        "url": f"https://example.com/{cluster}",
        "canonical_url": f"https://example.com/{cluster}",
        "summary": summary,
        "content_text": content,
        "editorial_fit_score": editorial,
        "novelty_score": 0.7,
        "quality": {"content_chars": content_chars},
        "dedup": {"cluster_id": cluster},
    }


def test_pure_opinion_is_rejected_even_with_score_and_length():
    opinion = _material(
        "AI will change everything",
        "c-op",
        editorial=0.99,
        summary="enterprises must embrace the future or be left behind",
        content=" ".join(["the future belongs to those who embrace change"] * 200),
    )
    pool = build_article_pool([opinion], topic_memory={"recent_outputs": []})
    assert pool["candidates"] == []
    assert any(r["reason"] == "no_checkable_evidence" for r in pool["rejected"])


def test_evidence_is_recorded_for_accepted_candidate():
    rich = _material(
        "Support org deployed an agent",
        "c1",
        summary="after go live the agent deflected 38% of tickets",
        content="the team measured cost savings and automation rate",
    )
    pool = build_article_pool([rich], topic_memory={"recent_outputs": []})
    assert len(pool["candidates"]) == 1
    evidence = pool["candidates"][0]["candidate_evidence"]
    assert evidence["original_source"]["url"]
    assert "38%" in evidence["checkable_facts"]
    assert evidence["implementation_details"]
    assert evidence["body_completeness"]["complete"] is True


def test_extract_evidence_marks_incomplete_body():
    thin = _material("t", "c", summary="38% growth", content="x", content_chars=100)
    evidence = extract_candidate_evidence(thin)
    assert evidence["body_completeness"]["complete"] is False


def test_ranking_prefers_more_checkable_evidence(monkeypatch):
    weak = _material("Weak evidence", "c1", editorial=0.9, summary="one 10% datapoint")
    strong = _material(
        "Strong evidence",
        "c2",
        editorial=0.7,
        summary="38% and 21% and 4x and $12 and 90%",
        content="deployed and measured after go live",
    )
    monkeypatch.setattr(tp, "_load_recent_topics", lambda days=7: [])
    monkeypatch.setattr(tp, "_load_recent_source_urls", lambda topics, days=7: set())
    monkeypatch.setattr(tp, "_llm_rank_executive_value", lambda c, model="m": {})
    result = tp.select_candidates([weak, strong], n=1)
    assert result["selected"][0]["title"] == "Strong evidence"


def test_substitute_scan_is_bounded_and_recorded(monkeypatch):
    recent_url = "https://example.com/c0"
    candidates = [
        _material("Recent source", "c0", summary="38% deployed"),
        _material("Substitute one", "c1", summary="21% measured after deployment"),
        _material("Substitute two", "c2", summary="4x cost savings measured"),
    ]
    monkeypatch.setattr(tp, "_load_recent_topics", lambda days=7: [])
    monkeypatch.setattr(
        tp, "_load_recent_source_urls", lambda topics, days=7: {recent_url}
    )
    monkeypatch.setattr(tp, "_llm_rank_executive_value", lambda c, model="m": {})
    result = tp.select_candidates(candidates, n=1, scan_limit=2)
    assert result["scanned"] <= 2
    assert [d["reason"] for d in result["decisions"] if d["decision"] == "rejected"] == [
        "recent_source_url"
    ]
    assert result["selected"][0]["title"] == "Substitute one"


def test_model_failure_keeps_deterministic_dedup_and_marks_unverified(monkeypatch):
    candidates = [
        _material("Fresh topic", "c1", summary="38% deployed and measured"),
        _material("Fresh topic two", "c2", summary="21% measured after deployment"),
    ]
    # Recent title is still deduped deterministically even when the model is down.
    monkeypatch.setattr(
        tp, "_load_recent_topics", lambda days=7: [{"title": "Fresh topic", "digest": "d"}]
    )
    monkeypatch.setattr(tp, "_load_recent_source_urls", lambda topics, days=7: set())
    monkeypatch.setattr(tp, "_llm_rank_executive_value", lambda c, model="m": {})
    monkeypatch.setattr(tp, "_llm_judge_topic_similar_verbose", lambda *a, **k: (False, False))
    result = tp.select_candidates(candidates, n=1)
    assert result["model_available"] is False
    reasons = [d["reason"] for d in result["decisions"]]
    assert "recent_title" in reasons  # deterministic dedup retained
    assert result["selected"][0]["title"] == "Fresh topic two"
    assert any(d["reason"] == "semantic_unverified" for d in result["decisions"])


def test_selection_records_referenced_history(monkeypatch):
    history = [
        {
            "title": "Fresh topic",
            "date": "2026-06-01",
            "source_urls": ["https://example.com/c1/"],
            "cluster_ids": ["c1"],
        }
    ]
    candidates = [
        _material("Fresh topic", "c1", summary="38% deployed"),
        _material("Fresh topic two", "c2", summary="21% measured after deployment"),
    ]
    monkeypatch.setattr(tp, "_load_recent_topics", lambda days=7: history)
    monkeypatch.setattr(tp, "_load_recent_source_urls", lambda topics, days=7: set())
    monkeypatch.setattr(tp, "_llm_rank_executive_value", lambda c, model="m": {})
    monkeypatch.setattr(tp, "_llm_judge_topic_similar_verbose", lambda *a, **k: (False, True))
    result = tp.select_candidates(candidates, n=1)
    # The consulted history is persisted (title + date + normalized source url).
    assert result["referenced_history"] == [
        {"title": "Fresh topic", "date": "2026-06-01", "source_urls": ["https://example.com/c1"]}
    ]
    rejection = next(d for d in result["decisions"] if d["reason"] == "recent_title")
    assert rejection["history_ref"] == {"match": "title", "title": "Fresh topic"}


def test_pipeline_persists_decisions_and_rejection_counts(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tp, "_load_recent_topics", lambda days=7: [])
    monkeypatch.setattr(tp, "_load_recent_source_urls", lambda topics, days=7: set())
    monkeypatch.setattr(tp, "_llm_rank_executive_value", lambda c, model="m": {})
    materials = [
        _material("Evidence A", "c1", summary="38% deployed and measured"),
        _material("Evidence B", "c2", summary="21% measured after deployment"),
        _material("Opinion", "c3", editorial=0.95, summary="embrace the future", content="opinion"),
    ]
    result = run_daily_article_pipeline(
        date_str="2026-06-03", workspace_dir=tmp_path, materials=materials, article_count=2
    )
    assert result["status"] == "success"
    assert len(result["selections"]) == 2
    assert result["rejection_counts"].get("no_checkable_evidence") == 1
    assert result["decisions"]
    persisted = json.loads(Path(result["selection_file"]).read_text(encoding="utf-8"))
    assert persisted["rejection_counts"] == result["rejection_counts"]
    assert "referenced_history" in persisted
    assert persisted["model_available"] is True


def _writer_with_codes(monkeypatch, codes):
    seq = iter(codes)
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda cmd, capture_output, text, timeout, **kw: subprocess.CompletedProcess(
            cmd, next(seq), stdout="ok", stderr=""
        ),
    )


def _two_evidence_materials():
    return [
        _material("Evidence A", "c1", summary="38% deployed and measured"),
        _material("Evidence B", "c2", summary="21% measured after deployment"),
    ]


@pytest.mark.parametrize(
    "codes,expected_status,expected_qualified",
    [
        ([0, 0], "success", 2),
        ([0, 2], "partial", 1),
        ([2, 2], "failed", 0),
    ],
)
def test_daily_status_from_qualified_finals(tmp_path, monkeypatch, codes, expected_status, expected_qualified):
    monkeypatch.setattr(tp, "_load_recent_topics", lambda days=7: [])
    monkeypatch.setattr(tp, "_load_recent_source_urls", lambda topics, days=7: set())
    monkeypatch.setattr(tp, "_llm_rank_executive_value", lambda c, model="m": {})
    _writer_with_codes(monkeypatch, codes)
    job = runtime_module.run_article_daily(
        date_str="2026-06-03", workspace_dir=tmp_path, materials=_two_evidence_materials()
    )
    assert job["status"] == expected_status
    assert job["artifacts"]["qualified_count"] == expected_qualified
    assert job["artifacts"]["stage_reason"] or expected_status == "success"


def test_material_shortage_reports_reasons(tmp_path):
    job = runtime_module.run_article_daily(
        date_str="2026-06-03", workspace_dir=tmp_path, materials=[]
    )
    assert job["status"] == "failed"
    assert "no article candidates" in job["artifacts"]["stage_reason"]
