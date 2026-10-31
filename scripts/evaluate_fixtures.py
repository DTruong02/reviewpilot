"""Validate the review fixture set or score recorded fixture predictions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "evaluation" / "fixtures"
MANIFEST = FIXTURE_DIR / "manifest.json"


def load_manifest() -> dict[str, Any]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("fixtures"), list):
        raise ValueError("unsupported or malformed fixture manifest")
    seen: set[str] = set()
    for fixture in data["fixtures"]:
        fixture_id = fixture.get("id")
        if not fixture_id or fixture_id in seen:
            raise ValueError(f"missing or duplicate fixture id: {fixture_id!r}")
        seen.add(fixture_id)
        diff_path = FIXTURE_DIR / fixture["diff"]
        if not diff_path.is_file() or "diff --git " not in diff_path.read_text(encoding="utf-8"):
            raise ValueError(f"missing or invalid unified diff for {fixture_id}")
        label_ids: set[str] = set()
        for label in fixture.get("labels", []):
            label_id = label.get("id")
            if not label_id or label_id in label_ids:
                raise ValueError(f"missing or duplicate label id in {fixture_id}")
            label_ids.add(label_id)
            if label.get("category") not in {"bug", "security"}:
                raise ValueError(f"invalid label category in {fixture_id}")
    if len(seen) < 10:
        raise ValueError("evaluation requires at least 10 fixtures")
    if not any(f["labels"] for f in data["fixtures"]) or not any(
        not f["labels"] for f in data["fixtures"]
    ):
        raise ValueError("fixture set must include both known-bug and clean changes")
    return data


def score(predictions_path: Path) -> dict[str, Any]:
    manifest = load_manifest()
    predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
    if not isinstance(predictions, list):
        raise ValueError("predictions must be a JSON array")
    fixture_ids = {f["id"] for f in manifest["fixtures"]}
    known = {
        (fixture["id"], label["id"])
        for fixture in manifest["fixtures"]
        for label in fixture["labels"]
    }
    observed: set[tuple[str, str]] = set()
    false_positive_count = 0
    for prediction in predictions:
        fixture_id = prediction.get("fixture_id")
        label_id = prediction.get("label_id")
        if fixture_id not in fixture_ids or not label_id:
            raise ValueError(f"invalid fixture prediction: {prediction!r}")
        key = (fixture_id, label_id)
        if key in observed:
            raise ValueError(f"duplicate prediction: {key}")
        observed.add(key)
        if key not in known:
            false_positive_count += 1
    true_positives = len(observed & known)
    false_negatives = len(known - observed)
    clean_fixture_ids = {f["id"] for f in manifest["fixtures"] if not f["labels"]}
    clean_false_positive_ids = {
        fixture_id for fixture_id, _ in observed - known if fixture_id in clean_fixture_ids
    }
    precision = true_positives / len(observed) if observed else None
    recall = true_positives / len(known) if known else None
    clean_count = len(clean_fixture_ids)
    return {
        "fixtures": len(manifest["fixtures"]),
        "ground_truth_findings": len(known),
        "true_positives": true_positives,
        "false_positives": false_positive_count,
        "clean_fixture_false_positives": sum(
            fixture_id in clean_fixture_ids for fixture_id, _ in observed - known
        ),
        "clean_fixtures_with_false_positives": len(clean_false_positive_ids),
        "false_negatives": false_negatives,
        "precision": precision,
        "recall": recall,
        "false_positive_rate_per_clean_fixture": len(clean_false_positive_ids) / clean_count,
        "average_review_duration_ms": average_observation(predictions, "duration_ms"),
        "average_estimated_cost_usd": average_observation(predictions, "estimated_cost_usd"),
    }


def average_observation(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [row[field] for row in rows if isinstance(row.get(field), (int, float))]
    return sum(values) / len(values) if values else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, help="observed predictions JSON to score")
    args = parser.parse_args()
    manifest = load_manifest()
    if args.predictions:
        print(json.dumps(score(args.predictions), indent=2))
    else:
        print(f"Validated {len(manifest['fixtures'])} fixtures.")


if __name__ == "__main__":
    main()
