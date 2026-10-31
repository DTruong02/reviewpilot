# Review evaluation fixtures

This fixture set contains ten small, synthetic unified PR diffs: six labeled bug/security changes and four labeled clean changes. Labels identify expected finding IDs, category, file, and new-side line. The examples are intentionally compact and are not a substitute for a representative, independently annotated production corpus.

Validate the manifest and diff files from the repository root:

```powershell
python scripts/evaluate_fixtures.py
```

To score a recorded run, provide a JSON array with one row per finding the reviewer emitted. A row must contain `fixture_id` and `label_id`; optional `duration_ms` and `estimated_cost_usd` are included in descriptive averages when recorded. Predictions that do not match a known label count as false positives. To measure clean-fixture false positives, use a distinct emitted label ID for each finding on a clean fixture. Duplicate prediction rows are rejected.

Example:

```json
[
  {"fixture_id":"bug-null-user","label_id":"null-user","duration_ms":840,"estimated_cost_usd":0.002}
]
```

```powershell
python scripts/evaluate_fixtures.py --predictions run.json
```

The evaluator reports precision, recall, false positives, false negatives, and false-positive findings per clean fixture. Duration and estimated model cost are `null` unless actual observations are supplied. This repository does not claim benchmark results: the fixtures are hand-authored, small, and not yet executed against a live model. Human review should confirm both fixture semantics and any model finding adjudication before using results to compare prompts or models. Matching currently uses fixture and label IDs supplied by the evaluator operator; it does not infer semantic equivalence between free-text findings.
