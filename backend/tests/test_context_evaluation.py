import json

import pytest
from scripts.evaluate_context import load_manifest, score


def test_context_evaluation_manifest_is_synthetic_and_valid():
    manifest = load_manifest()
    assert len(manifest["queries"]) == 3
    assert "Synthetic" in manifest["description"]


def test_context_evaluator_scores_recall_and_observed_metrics(tmp_path):
    observations = [
        {
            "query_id": "auth-token-change",
            "retrieved_symbol_ids": ["unrelated", "auth.validate_token"],
            "indexing_duration_ms": 200,
            "embedding_requests": 1,
            "embedding_cache_hits": 3,
            "review_duration_ms": 800,
            "estimated_cost_usd": 0.002,
            "validation_status": "blocked",
            "sandbox_startup_ms": None,
        },
        {
            "query_id": "cache-invalidation-change",
            "retrieved_symbol_ids": ["cache.invalidate_entry"],
            "embedding_requests": 0,
            "embedding_cache_hits": 2,
        },
    ]
    path = tmp_path / "observations.json"
    path.write_text(json.dumps(observations), encoding="utf-8")

    report = score(path)

    assert report["observed_queries"] == 2
    assert report["recall_at_k"]["recall@1"] == pytest.approx(0.25)
    assert report["recall_at_k"]["recall@3"] == pytest.approx(0.5)
    assert report["validation_outcomes"] == {"blocked": 1}
    assert report["embedding_requests"] == 1
    assert report["embedding_cache_hits"] == 5
    assert report["embedding_cache_hit_rate"] == pytest.approx(5 / 6)
    assert report["average_sandbox_startup_ms"] is None


def test_context_evaluator_rejects_unknown_query(tmp_path):
    path = tmp_path / "observations.json"
    path.write_text(
        json.dumps([{"query_id": "unknown", "retrieved_symbol_ids": []}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown or duplicate"):
        score(path)
