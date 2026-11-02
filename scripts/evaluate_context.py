"""Score recorded context retrieval and report observed operational metrics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "evaluation" / "context_manifest.json"
VALIDATION_STATUSES = {
    "not_attempted",
    "queued",
    "running",
    "reproduced",
    "not_reproduced",
    "inconclusive",
    "blocked",
    "error",
}


def load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(
        manifest.get("queries"), list
    ):
        raise ValueError("unsupported or malformed context manifest")
    seen: set[str] = set()
    for query in manifest["queries"]:
        query_id = query.get("query_id")
        relevant = query.get("relevant_symbol_ids")
        if not isinstance(query_id, str) or not query_id or query_id in seen:
            raise ValueError(f"missing or duplicate query id: {query_id!r}")
        if (
            not isinstance(relevant, list)
            or not relevant
            or any(not isinstance(item, str) or not item for item in relevant)
            or len(set(relevant)) != len(relevant)
        ):
            raise ValueError(f"invalid relevance labels for {query_id}")
        seen.add(query_id)
    if len(seen) < 3:
        raise ValueError("context evaluation requires at least three query fixtures")
    return manifest


def _average(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [
        row[field]
        for row in rows
        if isinstance(row.get(field), (int, float)) and not isinstance(row[field], bool)
    ]
    return sum(values) / len(values) if values else None


def score(observations_path: Path) -> dict[str, Any]:
    manifest = load_manifest()
    rows = json.loads(observations_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise TypeError("observations must be a JSON array")
    expected = {
        row["query_id"]: set(row["relevant_symbol_ids"]) for row in manifest["queries"]
    }
    seen_queries: set[str] = set()
    recall_by_k: dict[str, list[float]] = {"1": [], "3": [], "5": []}
    reciprocal_ranks: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("each observation must be an object")
        query_id = row.get("query_id")
        ranked = row.get("retrieved_symbol_ids")
        if query_id not in expected or query_id in seen_queries:
            raise ValueError(f"unknown or duplicate query observation: {query_id!r}")
        if (
            not isinstance(ranked, list)
            or any(not isinstance(item, str) for item in ranked)
            or len(set(ranked)) != len(ranked)
        ):
            raise ValueError(f"invalid retrieved symbol IDs for {query_id}")
        seen_queries.add(query_id)
        relevant = expected[query_id]
        for k in (1, 3, 5):
            recall_by_k[str(k)].append(len(set(ranked[:k]) & relevant) / len(relevant))
        rank = next(
            (index + 1 for index, symbol in enumerate(ranked) if symbol in relevant),
            None,
        )
        reciprocal_ranks.append(1 / rank if rank else 0.0)

    validation_rows = [row for row in rows if "validation_status" in row]
    status_counts: dict[str, int] = {}
    for row in validation_rows:
        status = row["validation_status"]
        if status not in VALIDATION_STATUSES:
            raise ValueError(f"invalid validation status: {status!r}")
        status_counts[status] = status_counts.get(status, 0) + 1

    embedding_requests = sum(
        row["embedding_requests"]
        for row in rows
        if isinstance(row.get("embedding_requests"), int)
        and row["embedding_requests"] >= 0
    )
    embedding_cache_hits = sum(
        row["embedding_cache_hits"]
        for row in rows
        if isinstance(row.get("embedding_cache_hits"), int)
        and row["embedding_cache_hits"] >= 0
    )
    embedding_total = embedding_requests + embedding_cache_hits
    return {
        "fixture_queries": len(expected),
        "observed_queries": len(seen_queries),
        "recall_at_k": {
            f"recall@{k}": sum(values) / len(values) if values else None
            for k, values in recall_by_k.items()
        },
        "mean_reciprocal_rank": (
            sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else None
        ),
        "average_indexing_duration_ms": _average(rows, "indexing_duration_ms"),
        "average_review_duration_ms": _average(rows, "review_duration_ms"),
        "average_estimated_cost_usd": _average(rows, "estimated_cost_usd"),
        "embedding_requests": embedding_requests,
        "embedding_cache_hits": embedding_cache_hits,
        "embedding_cache_hit_rate": (
            embedding_cache_hits / embedding_total if embedding_total else None
        ),
        "validation_outcomes": status_counts,
        "average_sandbox_startup_ms": _average(rows, "sandbox_startup_ms"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observations", type=Path, help="recorded retrieval observations JSON"
    )
    args = parser.parse_args()
    manifest = load_manifest()
    if args.observations:
        print(json.dumps(score(args.observations), indent=2))
    else:
        print(f"Validated {len(manifest['queries'])} synthetic context query fixtures.")


if __name__ == "__main__":
    main()
