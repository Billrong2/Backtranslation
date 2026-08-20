from __future__ import annotations

from pathlib import Path

import pytest

from backtranslation.complete_case_120 import (
    CompleteCaseError,
    EXPECTED_MISSING_METHODS,
    aggregate_method_scores,
    fixed_cohort,
)


PROJECT = Path(__file__).resolve().parents[1]


def test_fixed_real_cohort_is_exactly_120_cells_and_49_methods() -> None:
    cohort = fixed_cohort(
        project_directory=PROJECT,
        inventory_path=PROJECT / "artifacts" / "provenance" / "legacy-attempt-inventory-v0.5.json",
    )
    assert cohort["valid_cells"] == 120
    assert cohort["methods_with_valid_runs"] == 49
    assert cohort["run_counts"] == {"0": 38, "1": 42, "2": 40}
    assert cohort["valid_runs_per_method_distribution"] == {"1": 6, "2": 15, "3": 28}
    assert cohort["missing_method_ids"] == EXPECTED_MISSING_METHODS


def _synthetic_bundle() -> dict:
    records = []
    methods = [f"tse-{index:03d}" for index in range(1, 50)]
    # Exact frozen distribution: first 6 have one, next 15 have two, last 28 have three.
    for position, method in enumerate(methods):
        count = 1 if position < 6 else 2 if position < 21 else 3
        for run in range(count):
            value = float(position + run)
            records.append(
                {
                    "method_id": method,
                    "run_index": run,
                    "scores": {
                        "ruby": value,
                        "codebert": value,
                        "rouge_1": value,
                        "rouge_2": value,
                        "rouge_l": value,
                        "bleu": value,
                    },
                }
            )
    return {"schema_version": "backtranslation.complete-case-120-scores.v1", "records": records}


def test_one_run_is_used_and_three_runs_are_averaged() -> None:
    rows = aggregate_method_scores(_synthetic_bundle())
    assert len(rows) == 49
    assert rows[0]["valid_run_count"] == 1
    assert rows[0]["ruby"] == 0.0
    assert rows[-1]["valid_run_count"] == 3
    assert rows[-1]["ruby"] == 49.0


def test_duplicate_cell_is_rejected() -> None:
    bundle = _synthetic_bundle()
    bundle["records"][1] = dict(bundle["records"][0])
    with pytest.raises(CompleteCaseError, match="complete_case_score_record_invalid"):
        aggregate_method_scores(bundle)

