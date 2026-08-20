"""Frozen complete-case analysis of the 120 valid v0.5 round trips.

Generation validity is fixed by the outcome-blind legacy inventory.  This
module never retries or calls a provider.  It computes one score per retained
cell, averages available runs within method, and only then loads the fixed TSE
outcome file for method-level associations.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, canonical_json_bytes, write_bytes_once
from .cases import StudyCase, load_study_cases
from .directions import SchemaValidationError, validate_regenerated_code
from .java_validation import analyze_java_method
from .quota import (
    validate_attempt_eligibility,
    validate_legacy_attempt_inventory,
)
from .quota_execution import (
    snapshot_selection_evidence_tree,
    verify_legacy_inventory_physical,
)
from .ruby_scoring import RUBY_DEFINITION, ruby_similarity
from .scoring import (
    BLEU_DEFINITION,
    bleu_score,
    codebert_similarity,
    load_pinned_codebert,
    rouge_scores,
)
from .statistics import (
    TSE_EXPANDED_COHORT,
    aggregate_method_outcomes,
    analyze_association,
    holm_adjust,
)


INVENTORY_SHA256 = "4172483486daabe839e7d74b1efa7def98d037099e6a398936ff5c287729ad4a"
V05_DIGEST = "b0cf3c04fdf53ef0df2d233637b98ee17086f0c8ce6314b3d9b10a7cb1d16996"
COHORT_SCHEMA = "backtranslation.complete-case-120-cohort.v1"
SCORE_SCHEMA = "backtranslation.complete-case-120-scores.v1"
ANALYSIS_SCHEMA = "backtranslation.complete-case-120-analysis.v1"
EXPECTED_RUN_COUNTS = {0: 38, 1: 42, 2: 40}
EXPECTED_VALID_RUN_DISTRIBUTION = {1: 6, 2: 15, 3: 28}
EXPECTED_MISSING_METHODS = ["tse-020"]
METRICS = ("ruby", "codebert", "rouge_1", "rouge_2", "rouge_l", "bleu")


class CompleteCaseError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _association_task(
    arguments: tuple[list[dict[str, Any]], str, str, int, int]
) -> tuple[str, str, dict[str, Any]]:
    joined, metric, outcome, bootstrap_replicates, permutation_replicates = arguments
    result = analyze_association(
        joined,
        predictor_key=metric,
        outcome_key="au_mean" if outcome == "au" else "pbu_mean",
        metric_name=f"complete_case_{metric}",
        primary_gate=False,
        bootstrap_replicates=bootstrap_replicates,
        permutation_replicates=permutation_replicates,
    )
    return outcome, metric, result


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes(path: Path, code: str) -> bytes:
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise CompleteCaseError(code) from exc
    if (
        not path.is_file()
        or path.is_symlink()
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(payload) != after.st_size
    ):
        raise CompleteCaseError(code)
    return payload


def _canonical_object(path: Path, code: str) -> dict[str, Any]:
    payload = _read_bytes(path, code)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompleteCaseError(code) from exc
    if type(value) is not dict or payload != canonical_json_bytes(value):
        raise CompleteCaseError(code)
    return value


def _write_or_verify(path: Path, value: Mapping[str, Any], code: str) -> None:
    payload = canonical_json_bytes(dict(value))
    if path.exists():
        if _read_bytes(path, code) != payload:
            raise CompleteCaseError(code)
        return
    try:
        write_bytes_once(path, payload)
    except ArtifactError as exc:
        raise CompleteCaseError(code) from exc


def fixed_cohort(
    *, project_directory: Path, inventory_path: Path
) -> dict[str, Any]:
    """Validate and return the exact 120-cell outcome-blind cohort."""

    payload = _read_bytes(inventory_path, "complete_case_inventory_read_failed")
    if _sha256(payload) != INVENTORY_SHA256:
        raise CompleteCaseError("complete_case_inventory_hash_mismatch")
    try:
        raw = json.loads(payload)
        inventory = validate_legacy_attempt_inventory(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CompleteCaseError("complete_case_inventory_invalid") from exc
    if payload != canonical_json_bytes(inventory):
        raise CompleteCaseError("complete_case_inventory_not_canonical")
    try:
        verify_legacy_inventory_physical(
            project_directory=project_directory,
            inventory=inventory,
        )
    except Exception as exc:
        raise CompleteCaseError("complete_case_inventory_physical_mismatch") from exc

    cells: list[dict[str, Any]] = []
    run_counts: Counter[int] = Counter()
    method_counts: Counter[str] = Counter()
    seen: set[tuple[int, str]] = set()
    for item in inventory["cells"]:
        eligibility = validate_attempt_eligibility(item["eligibility"])
        if not eligibility["eligible"]:
            continue
        run = eligibility["cell"]["run_index"]
        method = eligibility["cell"]["method_id"]
        key = (run, method)
        if (
            key in seen
            or eligibility["attempt_index"] != 1
            or eligibility["origin"]["source_kind"] != "legacy-v0.5"
            or eligibility["origin"]["protocol_sha256"] != V05_DIGEST
            or eligibility["failure"] is not None
            or not all(eligibility["checks"].values())
        ):
            raise CompleteCaseError("complete_case_eligible_record_invalid")
        seen.add(key)
        run_counts[run] += 1
        method_counts[method] += 1
        cells.append(
            {
                "method_id": method,
                "run_index": run,
                "source_path": eligibility["origin"]["attempt_path"],
                "source_snapshot": eligibility["source_snapshot"],
                "eligibility_sha256": item["eligibility_sha256"],
            }
        )
    all_methods = {f"tse-{index:03d}" for index in range(1, 51)}
    missing = sorted(all_methods - set(method_counts))
    distribution = Counter(method_counts.values())
    if (
        len(cells) != 120
        or dict(sorted(run_counts.items())) != EXPECTED_RUN_COUNTS
        or len(method_counts) != 49
        or dict(sorted(distribution.items())) != EXPECTED_VALID_RUN_DISTRIBUTION
        or missing != EXPECTED_MISSING_METHODS
    ):
        raise CompleteCaseError("complete_case_cohort_invariants_mismatch")
    cells.sort(key=lambda row: (row["run_index"], row["method_id"]))
    return {
        "schema_version": COHORT_SCHEMA,
        "inventory_sha256": INVENTORY_SHA256,
        "source_generation_digest": V05_DIGEST,
        "valid_cells": 120,
        "methods_with_valid_runs": 49,
        "run_counts": {str(key): run_counts[key] for key in sorted(run_counts)},
        "valid_runs_per_method_distribution": {
            str(key): distribution[key] for key in sorted(distribution)
        },
        "missing_method_ids": missing,
        "cells": cells,
    }


def _candidate_code(project: Path, cell: Mapping[str, Any]) -> str:
    path = project / str(cell["source_path"])
    observed = snapshot_selection_evidence_tree(path)
    if observed != cell["source_snapshot"]:
        raise CompleteCaseError("complete_case_source_snapshot_mismatch")
    payload = _read_bytes(
        path / "regeneration.output.txt",
        "complete_case_regeneration_output_read_failed",
    )
    try:
        raw = json.loads(payload)
        regenerated = validate_regenerated_code(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, SchemaValidationError) as exc:
        raise CompleteCaseError("complete_case_regeneration_output_invalid") from exc
    return regenerated.code


def build_score_bundle(
    *,
    project_directory: Path,
    inventory_path: Path,
    analysis_manifest_sha256: str,
    tokenizer: Any | None = None,
    codebert_model: Any | None = None,
) -> dict[str, Any]:
    """Recompute all six metrics for the fixed 120 cells without outcomes."""

    cohort = fixed_cohort(
        project_directory=project_directory,
        inventory_path=inventory_path,
    )
    cases = {
        case.method_id: case
        for case in load_study_cases(project_directory / "data" / "tse")
    }
    if tokenizer is None or codebert_model is None:
        tokenizer, codebert_model = load_pinned_codebert(
            project_directory / "models" / "codebert-base",
            project_directory / "config" / "codebert-base-revision.json",
        )
    records: list[dict[str, Any]] = []
    for cell in cohort["cells"]:
        case: StudyCase = cases[cell["method_id"]]
        candidate = _candidate_code(project_directory, cell)
        reference_analysis = analyze_java_method(case.code_1, case.target_declaration)
        candidate_analysis = analyze_java_method(candidate, case.target_declaration)
        if not reference_analysis.structurally_valid or not candidate_analysis.structurally_valid:
            raise CompleteCaseError("complete_case_java_revalidation_failed")
        ruby = ruby_similarity(case.code_1, candidate, case.target_declaration)
        rouge = rouge_scores(reference_analysis.lex.tokens, candidate_analysis.lex.tokens)
        bleu = bleu_score(reference_analysis.lex.tokens, candidate_analysis.lex.tokens)
        codebert = codebert_similarity(
            reference_analysis.lex.tokens,
            candidate_analysis.lex.tokens,
            tokenizer=tokenizer,
            model=codebert_model,
        )
        values = {
            "ruby": float(ruby.score),
            "codebert": float(codebert.cosine_similarity),
            "rouge_1": float(rouge.rouge1.f1),
            "rouge_2": float(rouge.rouge2.f1),
            "rouge_l": float(rouge.rouge_l.f1),
            "bleu": float(bleu.score),
        }
        if any(not math.isfinite(value) for value in values.values()):
            raise CompleteCaseError("complete_case_score_nonfinite")
        records.append(
            {
                "method_id": cell["method_id"],
                "run_index": cell["run_index"],
                "eligibility_sha256": cell["eligibility_sha256"],
                "source_tree_sha256": cell["source_snapshot"]["tree_sha256"],
                "code_1_sha256": case.code_1_sha256,
                "code_2_sha256": _sha256(candidate.encode("utf-8")),
                "scores": values,
                "ruby_selected_tier": ruby.selected_tier,
            }
        )
    if len(records) != 120:
        raise CompleteCaseError("complete_case_score_count_mismatch")
    return {
        "schema_version": SCORE_SCHEMA,
        "analysis_manifest_sha256": analysis_manifest_sha256,
        "cohort": {key: value for key, value in cohort.items() if key != "cells"},
        "metric_definitions": {
            "ruby": RUBY_DEFINITION,
            "codebert": "microsoft-codebert-base-final-layer-mean-cosine-v1",
            "rouge": "rouge-score-0.1.2-exact-java-token-arrays-v1",
            "bleu": BLEU_DEFINITION,
        },
        "records": records,
    }


def aggregate_method_scores(score_bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Average every method's one, two, or three valid runs equally."""

    if score_bundle.get("schema_version") != SCORE_SCHEMA:
        raise CompleteCaseError("complete_case_score_schema_invalid")
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    seen: set[tuple[int, str]] = set()
    records = score_bundle.get("records")
    if not isinstance(records, list) or len(records) != 120:
        raise CompleteCaseError("complete_case_score_count_mismatch")
    for record in records:
        if not isinstance(record, Mapping):
            raise CompleteCaseError("complete_case_score_record_invalid")
        method = record.get("method_id")
        run = record.get("run_index")
        scores = record.get("scores")
        if (
            not isinstance(method, str)
            or not isinstance(run, int)
            or isinstance(run, bool)
            or run not in (0, 1, 2)
            or (run, method) in seen
            or not isinstance(scores, Mapping)
            or set(scores) != set(METRICS)
        ):
            raise CompleteCaseError("complete_case_score_record_invalid")
        for metric in METRICS:
            value = scores[metric]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                raise CompleteCaseError("complete_case_score_record_invalid")
        seen.add((run, method))
        grouped[method].append(record)
    distribution = Counter(len(rows) for rows in grouped.values())
    if len(grouped) != 49 or dict(sorted(distribution.items())) != EXPECTED_VALID_RUN_DISTRIBUTION:
        raise CompleteCaseError("complete_case_method_aggregation_mismatch")
    output: list[dict[str, Any]] = []
    for method in sorted(grouped):
        rows = grouped[method]
        output.append(
            {
                "method_id": method,
                "valid_run_count": len(rows),
                "valid_run_indices": sorted(int(row["run_index"]) for row in rows),
                **{
                    metric: float(
                        sum(float(row["scores"][metric]) for row in rows) / len(rows)
                    )
                    for metric in METRICS
                },
            }
        )
    return output


def analyze_score_bundle(
    *,
    score_bundle: Mapping[str, Any],
    outcome_path: Path,
    source_manifest_path: Path,
    bootstrap_replicates: int = 10_000,
    permutation_replicates: int = 100_000,
    outcome_loader: Callable[..., list[dict[str, Any]]] | None = None,
    analysis_manifest_sha256: str | None = None,
    parallel_workers: int = 1,
) -> dict[str, Any]:
    """Load outcomes only after score validation and compute associations."""

    method_scores = aggregate_method_scores(score_bundle)
    # Deliberately delayed import: predictor validation is complete first.
    if outcome_loader is None:
        from .outcomes import load_tse_evaluations

        outcome_loader = load_tse_evaluations
    evaluations = outcome_loader(
        outcome_path,
        source_manifest_path,
        allow_repeated_participant_method=True,
    )
    outcomes = aggregate_method_outcomes(
        evaluations,
        expected=TSE_EXPANDED_COHORT,
        allow_repeated_participant_method=True,
    )
    outcome_by_method = {row.method_id: row.to_record() for row in outcomes}
    joined: list[dict[str, Any]] = []
    for scores in method_scores:
        outcome = outcome_by_method.get(scores["method_id"])
        if outcome is None:
            raise CompleteCaseError("complete_case_outcome_join_missing")
        joined.append({**scores, **outcome})
    if len(joined) != 49:
        raise CompleteCaseError("complete_case_analysis_denominator_mismatch")

    associations: dict[str, dict[str, Any]] = {"au": {}, "pbu": {}}
    tasks = [
        (joined, metric, outcome, bootstrap_replicates, permutation_replicates)
        for outcome in ("au", "pbu")
        for metric in METRICS
    ]
    if not isinstance(parallel_workers, int) or parallel_workers < 1:
        raise CompleteCaseError("complete_case_parallel_workers_invalid")
    if parallel_workers == 1:
        completed = map(_association_task, tasks)
        for outcome, metric, result in completed:
            associations[outcome][metric] = result
    else:
        with ProcessPoolExecutor(max_workers=min(parallel_workers, len(tasks))) as executor:
            for outcome, metric, result in executor.map(_association_task, tasks):
                associations[outcome][metric] = result

    def partial_p(outcome: str, metric: str) -> float | None:
        value = associations[outcome][metric]["partial_spearman_loc"]["freedman_lane"]["two_sided_p"]
        return None if value is None else float(value)

    au_support = {metric: partial_p("au", metric) for metric in METRICS if metric != "ruby"}
    pbu_family = {metric: partial_p("pbu", metric) for metric in METRICS}
    return {
        "schema_version": ANALYSIS_SCHEMA,
        "analysis_manifest_sha256": (
            analysis_manifest_sha256 or score_bundle["analysis_manifest_sha256"]
        ),
        "score_manifest_sha256": score_bundle["analysis_manifest_sha256"],
        "estimand": "method_mean_similarity_conditional_on_at_least_one_valid_v0.5_roundtrip",
        "method_denominator": 49,
        "valid_cell_count": 120,
        "missing_method_ids": EXPECTED_MISSING_METHODS,
        "valid_runs_per_method_distribution": {
            str(key): EXPECTED_VALID_RUN_DISTRIBUTION[key]
            for key in sorted(EXPECTED_VALID_RUN_DISTRIBUTION)
        },
        "primary_metric": "ruby",
        "primary_outcome": "au_mean",
        "decision_gate": None,
        "associations": associations,
        "holm_families": {
            "supporting_au_fidelity": holm_adjust(au_support),
            "pbu_fidelity": holm_adjust(pbu_family),
        },
        "limitations": [
            "complete_case_validity_conditioned_analysis",
            "unequal_valid_run_counts_averaged_within_method",
            "tse_020_has_no_valid_roundtrip",
            "association_does_not_establish_causation",
            "seven_repeated_participant_method_evaluations_retained_from_authoritative_444_row_file",
        ],
    }


def score_and_analyze(
    *,
    project_directory: Path,
    inventory_path: Path,
    analysis_manifest_sha256: str,
    output_directory: Path,
    outcome_path: Path,
    source_manifest_path: Path,
) -> dict[str, Any]:
    """Compute/verify scores, then analyze and publish the aggregate result."""

    score_bundle = build_score_bundle(
        project_directory=project_directory,
        inventory_path=inventory_path,
        analysis_manifest_sha256=analysis_manifest_sha256,
    )
    score_path = output_directory / "scores.json"
    _write_or_verify(score_path, score_bundle, "complete_case_scores_changed")
    if _canonical_object(score_path, "complete_case_scores_changed") != score_bundle:
        raise CompleteCaseError("complete_case_scores_changed")
    result = analyze_score_bundle(
        score_bundle=score_bundle,
        outcome_path=outcome_path,
        source_manifest_path=source_manifest_path,
    )
    _write_or_verify(
        output_directory / "analysis.json",
        result,
        "complete_case_analysis_changed",
    )
    return result
