"""Outcome-side statistical analysis for the frozen backtranslation study.

This module deliberately has no knowledge of generation or scoring paths.  It
accepts normalized records only after the protocol's outcome-blind freeze gate
has been satisfied.  All public result builders return JSON-safe dictionaries:
undefined numerical results are represented by ``None``, never NaN.

The implementation follows Sections 3 and 11--14 of the protocol that is
included in the authorized freeze manifest. Production defaults are
intentionally costly (10,000 bootstrap samples and 100,000 permutations);
tests pass smaller values explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import warnings
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import statsmodels.api as sm
from scipy import stats
from statsmodels.genmod.cov_struct import Exchangeable


ANALYSIS_SCHEMA_VERSION = "backtranslation-analysis-v1"
BOOTSTRAP_SEED = 20_260_811
PRIMARY_PERMUTATION_SEED = 20_260_812
SECONDARY_PERMUTATION_SEED = 20_260_813
PRIMARY_BOOTSTRAP_REPLICATES = 10_000
PRIMARY_PERMUTATION_REPLICATES = 100_000
AU_LEVELS = np.asarray((0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0), dtype=np.float64)
AU_TOLERANCE = 1e-12

PRESPECIFIED_FAMILIES: dict[str, tuple[str, ...]] = {
    "supporting_au_fidelity": (
        "codebert",
        "rouge_1",
        "rouge_2",
        "rouge_l",
        "bleu",
    ),
    "secondary_au_complexity": (
        "direction_word_count",
        "condition_count",
        "condition_density",
        "dependency_edge_count",
        "dependency_depth",
    ),
    "pbu_fidelity": (
        "ruby",
        "codebert",
        "rouge_1",
        "rouge_2",
        "rouge_l",
        "bleu",
    ),
    "pbu_complexity": (
        "atomic_instruction_count",
        "direction_word_count",
        "condition_count",
        "condition_density",
        "dependency_edge_count",
        "dependency_depth",
    ),
}


class AnalysisValidationError(ValueError):
    """Stable-code validation error suitable for sanitized logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CohortInvariants:
    evaluations: int
    methods: int
    projects: int
    participants: int


TSE_EXPANDED_COHORT = CohortInvariants(
    evaluations=444,
    methods=50,
    projects=10,
    participants=63,
)


@dataclass(frozen=True)
class MethodOutcome:
    method_id: str
    project: str
    loc: float
    n_evaluations: int
    au_mean: float
    au_sd: float
    pbu_mean: float
    pbu_numerator: int
    pbu_denominator: int

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def _required(record: Mapping[str, Any], key: str) -> Any:
    if key not in record:
        raise AnalysisValidationError(f"missing_{key}")
    return record[key]


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AnalysisValidationError(code)
    return value


def _finite_number(value: Any, code: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise AnalysisValidationError(code)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AnalysisValidationError(code) from error
    if not math.isfinite(number):
        raise AnalysisValidationError(code)
    return number


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (bool, np.bool_)):
        raise AnalysisValidationError("nonnumeric_analysis_value")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AnalysisValidationError("nonnumeric_analysis_value") from error
    if math.isnan(number):
        return None
    if not math.isfinite(number):
        raise AnalysisValidationError("nonfinite_analysis_value")
    return number


def aggregate_method_outcomes(
    evaluations: Iterable[Mapping[str, Any]],
    *,
    expected: CohortInvariants | None = None,
    allow_repeated_participant_method: bool = False,
) -> list[MethodOutcome]:
    """Validate participant rows and aggregate AU/PBU at method level.

    Input keys are ``participant_id``, ``method_id``, ``project``, ``loc``,
    ``au``, and ``pbu``.  CSV numeric strings are accepted, but booleans,
    non-finite values, and out-of-domain values are rejected.  AU standard
    deviation is the sample SD (ddof=1), or 0 for a singleton method.
    """

    normalized: list[tuple[str, str, str, float, float, int]] = []
    participant_method_keys: set[tuple[str, str]] = set()
    for record in evaluations:
        participant_id = _identifier(
            _required(record, "participant_id"), "invalid_participant_id"
        )
        method_id = _identifier(_required(record, "method_id"), "invalid_method_id")
        project = _identifier(_required(record, "project"), "invalid_project")
        loc = _finite_number(_required(record, "loc"), "invalid_loc")
        if loc <= 0:
            raise AnalysisValidationError("invalid_loc")
        au = _finite_number(_required(record, "au"), "invalid_au")
        distances = np.abs(AU_LEVELS - au)
        nearest = int(np.argmin(distances))
        if float(distances[nearest]) > AU_TOLERANCE:
            raise AnalysisValidationError("invalid_au")
        au = float(AU_LEVELS[nearest])
        pbu_number = _finite_number(_required(record, "pbu"), "invalid_pbu")
        if pbu_number not in (0.0, 1.0):
            raise AnalysisValidationError("invalid_pbu")
        pbu = int(pbu_number)
        if pbu == 0 and au != 0.0:
            raise AnalysisValidationError("pbu_zero_nonzero_au")

        key = (participant_id, method_id)
        if key in participant_method_keys and not allow_repeated_participant_method:
            raise AnalysisValidationError("duplicate_participant_method")
        participant_method_keys.add(key)
        normalized.append((participant_id, method_id, project, loc, au, pbu))

    if not normalized:
        raise AnalysisValidationError("empty_evaluations")

    if expected is not None:
        observed = CohortInvariants(
            evaluations=len(normalized),
            methods=len({item[1] for item in normalized}),
            projects=len({item[2] for item in normalized}),
            participants=len({item[0] for item in normalized}),
        )
        if observed != expected:
            raise AnalysisValidationError("cohort_invariants_mismatch")

    grouped: dict[str, list[tuple[str, str, str, float, float, int]]] = defaultdict(list)
    for item in normalized:
        grouped[item[1]].append(item)

    results: list[MethodOutcome] = []
    for method_id in sorted(grouped):
        rows = grouped[method_id]
        projects = {row[2] for row in rows}
        locs = {row[3] for row in rows}
        if len(projects) != 1:
            raise AnalysisValidationError("inconsistent_method_project")
        if len(locs) != 1:
            raise AnalysisValidationError("inconsistent_method_loc")
        au_values = np.asarray([row[4] for row in rows], dtype=np.float64)
        pbu_values = np.asarray([row[5] for row in rows], dtype=np.int64)
        results.append(
            MethodOutcome(
                method_id=method_id,
                project=next(iter(projects)),
                loc=next(iter(locs)),
                n_evaluations=len(rows),
                au_mean=float(np.mean(au_values)),
                au_sd=(
                    float(np.std(au_values, ddof=1)) if len(au_values) > 1 else 0.0
                ),
                pbu_mean=float(np.mean(pbu_values)),
                pbu_numerator=int(np.sum(pbu_values)),
                pbu_denominator=len(pbu_values),
            )
        )
    return results


def _rank(values: np.ndarray) -> np.ndarray:
    return np.asarray(stats.rankdata(values, method="average"), dtype=np.float64)


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) != len(y) or len(x) < 3:
        return math.nan
    x_centered = x - np.mean(x)
    y_centered = y - np.mean(y)
    denominator = float(np.linalg.norm(x_centered) * np.linalg.norm(y_centered))
    if not math.isfinite(denominator) or denominator <= np.finfo(np.float64).eps:
        return math.nan
    return float(np.dot(x_centered, y_centered) / denominator)


def _design_matrix(controls: Sequence[np.ndarray], n: int) -> np.ndarray:
    columns = [np.ones(n, dtype=np.float64)] + [_rank(value) for value in controls]
    design = np.column_stack(columns)
    if np.linalg.matrix_rank(design) != design.shape[1]:
        raise AnalysisValidationError("rank_control_design_singular")
    return design


def _residual_maker(design: np.ndarray) -> np.ndarray:
    return np.eye(len(design), dtype=np.float64) - design @ np.linalg.pinv(design)


def _partial_rank_statistic(
    predictor: np.ndarray,
    outcome: np.ndarray,
    controls: Sequence[np.ndarray],
) -> float:
    if len(predictor) < len(controls) + 3:
        return math.nan
    if np.ptp(predictor) == 0.0 or np.ptp(outcome) == 0.0:
        return math.nan
    try:
        design = _design_matrix(controls, len(predictor))
    except AnalysisValidationError:
        return math.nan
    residual_maker = _residual_maker(design)
    return _pearson(residual_maker @ _rank(predictor), residual_maker @ _rank(outcome))


def raw_spearman(predictor: Sequence[float], outcome: Sequence[float]) -> dict[str, Any]:
    """Average-rank Spearman rho and SciPy's two-sided asymptotic p-value."""

    x = np.asarray(predictor, dtype=np.float64)
    y = np.asarray(outcome, dtype=np.float64)
    if x.ndim != 1 or y.ndim != 1 or len(x) != len(y) or len(x) < 3:
        raise AnalysisValidationError("invalid_correlation_vectors")
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise AnalysisValidationError("nonfinite_correlation_vectors")
    if np.ptp(x) == 0.0 or np.ptp(y) == 0.0:
        return {
            "estimate": None,
            "asymptotic_two_sided_p": None,
            "n": len(x),
            "rank_tie_method": "average",
            "failure": "constant_input",
        }
    result = stats.spearmanr(x, y, alternative="two-sided")
    return {
        "estimate": _json_float(float(result.statistic)),
        "asymptotic_two_sided_p": _json_float(float(result.pvalue)),
        "n": len(x),
        "rank_tie_method": "average",
    }


def partial_spearman(
    predictor: Sequence[float],
    outcome: Sequence[float],
    controls: Sequence[Sequence[float]],
) -> float:
    """Partial Spearman via independent average ranks and OLS residuals."""

    x = np.asarray(predictor, dtype=np.float64)
    y = np.asarray(outcome, dtype=np.float64)
    control_arrays = [np.asarray(value, dtype=np.float64) for value in controls]
    if (
        x.ndim != 1
        or y.ndim != 1
        or len(x) != len(y)
        or any(value.ndim != 1 or len(value) != len(x) for value in control_arrays)
    ):
        raise AnalysisValidationError("invalid_correlation_vectors")
    if not all(np.all(np.isfinite(value)) for value in [x, y, *control_arrays]):
        raise AnalysisValidationError("nonfinite_correlation_vectors")
    value = _partial_rank_statistic(x, y, control_arrays)
    if not math.isfinite(value):
        raise AnalysisValidationError("undefined_partial_spearman")
    return value


def fisher_z_interval(
    estimate: float | None,
    n: int,
    *,
    control_count: int = 0,
) -> dict[str, Any]:
    """Diagnostic Fisher-z interval; never used for a protocol decision."""

    if estimate is None or not math.isfinite(float(estimate)):
        return {"lower": None, "upper": None, "diagnostic_only": True}
    if not -1.0 <= float(estimate) <= 1.0 or n <= control_count + 3:
        return {"lower": None, "upper": None, "diagnostic_only": True}
    bounded = float(np.clip(estimate, np.nextafter(-1.0, 0.0), np.nextafter(1.0, 0.0)))
    standard_error = 1.0 / math.sqrt(n - control_count - 3)
    critical = float(stats.norm.ppf(0.975))
    transformed = math.atanh(bounded)
    return {
        "lower": math.tanh(transformed - critical * standard_error),
        "upper": math.tanh(transformed + critical * standard_error),
        "confidence_level": 0.95,
        "control_count": control_count,
        "diagnostic_only": True,
    }


def _metric_seed(metric_name: str, base: int = SECONDARY_PERMUTATION_SEED) -> int:
    if not metric_name:
        raise AnalysisValidationError("empty_metric_name")
    offset = int.from_bytes(hashlib.sha256(metric_name.encode("utf-8")).digest(), "big")
    return base + offset


def freedman_lane_test(
    predictor: Sequence[float],
    outcome: Sequence[float],
    controls: Sequence[Sequence[float]],
    projects: Sequence[str],
    *,
    replicates: int = PRIMARY_PERMUTATION_REPLICATES,
    seed: int = PRIMARY_PERMUTATION_SEED,
) -> dict[str, Any]:
    """Within-project Freedman--Lane test on ranked variables.

    Outcome residuals are permuted independently inside each project.  Both
    positive one-sided and absolute-statistic two-sided Monte Carlo p-values
    use the preregistered plus-one correction and the full requested ``B`` in
    the denominator.
    """

    x = np.asarray(predictor, dtype=np.float64)
    y = np.asarray(outcome, dtype=np.float64)
    control_arrays = [np.asarray(value, dtype=np.float64) for value in controls]
    project_array = np.asarray(projects, dtype=object)
    n = len(x)
    if (
        n < 3
        or y.shape != (n,)
        or project_array.shape != (n,)
        or any(value.shape != (n,) for value in control_arrays)
    ):
        raise AnalysisValidationError("invalid_permutation_vectors")
    if not all(np.all(np.isfinite(value)) for value in [x, y, *control_arrays]):
        raise AnalysisValidationError("nonfinite_permutation_vectors")
    if not isinstance(replicates, int) or replicates < 1:
        raise AnalysisValidationError("invalid_permutation_replicates")
    if any(not isinstance(item, str) or not item for item in project_array):
        raise AnalysisValidationError("invalid_project")
    if np.ptp(x) == 0.0 or np.ptp(y) == 0.0:
        raise AnalysisValidationError("undefined_permutation_observed")

    design = _design_matrix(control_arrays, n)
    residual_maker = _residual_maker(design)
    ranked_y = _rank(y)
    fitted_y = (np.eye(n, dtype=np.float64) - residual_maker) @ ranked_y
    outcome_residual = ranked_y - fitted_y
    predictor_residual = residual_maker @ _rank(x)
    observed = _pearson(predictor_residual, outcome_residual)
    if not math.isfinite(observed):
        raise AnalysisValidationError("undefined_permutation_observed")

    project_indices = [
        np.flatnonzero(project_array == project)
        for project in sorted(set(project_array.tolist()))
    ]
    rng = np.random.Generator(np.random.PCG64(seed))
    positive_extreme = 0
    absolute_extreme = 0
    undefined = 0
    for _ in range(replicates):
        permuted = np.empty(n, dtype=np.float64)
        for indices in project_indices:
            permuted[indices] = outcome_residual[rng.permutation(indices)]
        # Adding fitted values then refitting is algebraically this projection.
        permuted_residual = residual_maker @ (fitted_y + permuted)
        statistic = _pearson(predictor_residual, permuted_residual)
        if not math.isfinite(statistic):
            undefined += 1
            continue
        if statistic >= observed:
            positive_extreme += 1
        if abs(statistic) >= abs(observed):
            absolute_extreme += 1

    return {
        "observed": observed,
        "one_sided_positive_p": (1 + positive_extreme) / (1 + replicates),
        "two_sided_p": (1 + absolute_extreme) / (1 + replicates),
        "replicates": replicates,
        "undefined_replicates": undefined,
        "seed": seed,
        "bit_generator": "PCG64",
        "permutation_scope": "within_project",
        "plus_one_denominator": 1 + replicates,
    }


def bootstrap_correlation(
    predictor: Sequence[float],
    outcome: Sequence[float],
    controls: Sequence[Sequence[float]],
    projects: Sequence[str],
    *,
    replicates: int = PRIMARY_BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
    cluster: Literal["within_project", "whole_project"] = "within_project",
) -> dict[str, Any]:
    """Percentile CI for a raw or partial Spearman statistic.

    An empty ``controls`` sequence requests raw Spearman (implemented as
    Pearson correlation of average ranks).  Within-project sampling retains
    every project's original method count.  Whole-project sampling draws the
    original number of projects with replacement and takes every method in a
    selected project.
    """

    x = np.asarray(predictor, dtype=np.float64)
    y = np.asarray(outcome, dtype=np.float64)
    control_arrays = [np.asarray(value, dtype=np.float64) for value in controls]
    project_array = np.asarray(projects, dtype=object)
    n = len(x)
    if (
        n < 3
        or y.shape != (n,)
        or project_array.shape != (n,)
        or any(value.shape != (n,) for value in control_arrays)
    ):
        raise AnalysisValidationError("invalid_bootstrap_vectors")
    if not all(np.all(np.isfinite(value)) for value in [x, y, *control_arrays]):
        raise AnalysisValidationError("nonfinite_bootstrap_vectors")
    if not isinstance(replicates, int) or replicates < 1:
        raise AnalysisValidationError("invalid_bootstrap_replicates")
    if cluster not in ("within_project", "whole_project"):
        raise AnalysisValidationError("invalid_bootstrap_cluster")

    project_names = sorted(set(project_array.tolist()))
    project_indices = [
        np.flatnonzero(project_array == project) for project in project_names
    ]
    rng = np.random.Generator(np.random.PCG64(seed))
    estimates: list[float] = []
    for _ in range(replicates):
        if cluster == "within_project":
            sampled = np.concatenate(
                [indices[rng.integers(0, len(indices), len(indices))] for indices in project_indices]
            )
        else:
            selected = rng.integers(0, len(project_indices), len(project_indices))
            sampled = np.concatenate([project_indices[index] for index in selected])
        estimate = _partial_rank_statistic(
            x[sampled],
            y[sampled],
            [value[sampled] for value in control_arrays],
        )
        if math.isfinite(estimate):
            estimates.append(estimate)

    if estimates:
        lower, upper = np.quantile(
            np.asarray(estimates, dtype=np.float64),
            (0.025, 0.975),
            method="linear",
        )
        lower_value: float | None = float(lower)
        upper_value: float | None = float(upper)
    else:
        lower_value = upper_value = None
    return {
        "lower": lower_value,
        "upper": upper_value,
        "confidence_level": 0.95,
        "method": "percentile_linear",
        "cluster": cluster,
        "replicates": replicates,
        "valid_replicates": len(estimates),
        "undefined_replicates": replicates - len(estimates),
        "seed": seed,
        "bit_generator": "PCG64",
    }


def _complete_method_records(
    records: Iterable[Mapping[str, Any]],
    *,
    predictor_key: str,
    outcome_key: str,
    control_keys: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in records:
        method_id = _identifier(_required(source, "method_id"), "invalid_method_id")
        if method_id in seen:
            raise AnalysisValidationError("duplicate_method_analysis_row")
        seen.add(method_id)
        project = _identifier(_required(source, "project"), "invalid_project")
        outcome = _finite_number(_required(source, outcome_key), "invalid_method_outcome")
        if not 0.0 <= outcome <= 1.0:
            raise AnalysisValidationError("invalid_method_outcome")
        row: dict[str, Any] = {
            "method_id": method_id,
            "project": project,
            predictor_key: _optional_number(_required(source, predictor_key)),
            outcome_key: outcome,
        }
        for key in control_keys:
            control = _finite_number(_required(source, key), "invalid_analysis_control")
            if key == "loc" and control <= 0:
                raise AnalysisValidationError("invalid_loc")
            row[key] = control
        all_rows.append(row)
    if not all_rows:
        raise AnalysisValidationError("empty_analysis_rows")
    all_rows.sort(key=lambda row: (row["project"], row["method_id"]))
    complete = [
        row
        for row in all_rows
        if row[predictor_key] is not None
    ]
    return all_rows, complete


def analyze_association(
    records: Iterable[Mapping[str, Any]],
    *,
    predictor_key: str,
    outcome_key: Literal["au_mean", "pbu_mean"],
    metric_name: str,
    primary_gate: bool = False,
    bootstrap_replicates: int = PRIMARY_BOOTSTRAP_REPLICATES,
    permutation_replicates: int = PRIMARY_PERMUTATION_REPLICATES,
) -> dict[str, Any]:
    """Run the protocol's raw and dataset-LOC-adjusted association analysis."""

    if outcome_key not in ("au_mean", "pbu_mean"):
        raise AnalysisValidationError("invalid_outcome_key")
    if primary_gate and (metric_name != "ruby" or outcome_key != "au_mean"):
        raise AnalysisValidationError("primary_gate_must_be_ruby_au")
    all_rows, complete = _complete_method_records(
        records,
        predictor_key=predictor_key,
        outcome_key=outcome_key,
        control_keys=("loc",),
    )
    if len(complete) < 3:
        raise AnalysisValidationError("insufficient_complete_methods")
    x = np.asarray([row[predictor_key] for row in complete], dtype=np.float64)
    y = np.asarray([row[outcome_key] for row in complete], dtype=np.float64)
    loc = np.asarray([row["loc"] for row in complete], dtype=np.float64)
    projects = [row["project"] for row in complete]

    raw = raw_spearman(x, y)
    raw["fisher_z_diagnostic_95"] = fisher_z_interval(
        raw["estimate"], len(complete), control_count=0
    )
    raw["bootstrap_95"] = bootstrap_correlation(
        x,
        y,
        (),
        projects,
        replicates=bootstrap_replicates,
    )
    # Raw RUBY--AU is descriptive/non-gating; only the LOC-partial test gets
    # the dedicated confirmatory seed.
    raw_permutation_seed = _metric_seed(f"{metric_name}:{outcome_key}:raw")
    try:
        raw["freedman_lane"] = freedman_lane_test(
            x,
            y,
            (),
            projects,
            replicates=permutation_replicates,
            seed=raw_permutation_seed,
        )
    except AnalysisValidationError as error:
        if error.code != "undefined_permutation_observed":
            raise
        raw["freedman_lane"] = _undefined_permutation_result(
            permutation_replicates, raw_permutation_seed
        )

    try:
        partial_estimate: float | None = partial_spearman(x, y, (loc,))
    except AnalysisValidationError as error:
        if error.code != "undefined_partial_spearman":
            raise
        partial_estimate = None
    partial_permutation_seed = (
        PRIMARY_PERMUTATION_SEED
        if primary_gate
        else _metric_seed(f"{metric_name}:{outcome_key}:partial_loc")
    )
    partial = {
        "estimate": partial_estimate,
        "n": len(complete),
        "controls": ["ranked_dataset_loc"],
        "fisher_z_diagnostic_95": fisher_z_interval(
            partial_estimate, len(complete), control_count=1
        ),
        "bootstrap_95": bootstrap_correlation(
            x,
            y,
            (loc,),
            projects,
            replicates=bootstrap_replicates,
        ),
        "whole_project_bootstrap_95": bootstrap_correlation(
            x,
            y,
            (loc,),
            projects,
            replicates=bootstrap_replicates,
            cluster="whole_project",
        ),
    }
    try:
        partial["freedman_lane"] = freedman_lane_test(
            x,
            y,
            (loc,),
            projects,
            replicates=permutation_replicates,
            seed=partial_permutation_seed,
        )
    except AnalysisValidationError as error:
        if error.code != "undefined_permutation_observed":
            raise
        partial["freedman_lane"] = _undefined_permutation_result(
            permutation_replicates, partial_permutation_seed
        )
    return json_safe(
        {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analysis": "method_level_association",
            "metric": metric_name,
            "predictor_key": predictor_key,
            "outcome": outcome_key,
            "primary_gate": primary_gate,
            "n_total": len(all_rows),
            "n_complete": len(complete),
            "n_missing": len(all_rows) - len(complete),
            "missing_method_ids": [
                row["method_id"] for row in all_rows if row not in complete
            ],
            "raw_spearman": raw,
            "partial_spearman_loc": partial,
        }
    )


def _undefined_permutation_result(replicates: int, seed: int) -> dict[str, Any]:
    return {
        "observed": None,
        "one_sided_positive_p": None,
        "two_sided_p": None,
        "replicates": replicates,
        "undefined_replicates": replicates,
        "seed": seed,
        "bit_generator": "PCG64",
        "permutation_scope": "within_project",
        "plus_one_denominator": 1 + replicates,
        "failure": "undefined_observed_statistic",
    }


def analyze_complexity_loc_association(
    records: Iterable[Mapping[str, Any]],
    *,
    predictor_key: str,
    metric_name: str,
    bootstrap_replicates: int = PRIMARY_BOOTSTRAP_REPLICATES,
    permutation_replicates: int = PRIMARY_PERMUTATION_REPLICATES,
) -> dict[str, Any]:
    """Report the required raw association between a complexity measure and LOC."""

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in records:
        method_id = _identifier(_required(source, "method_id"), "invalid_method_id")
        if method_id in seen:
            raise AnalysisValidationError("duplicate_method_analysis_row")
        seen.add(method_id)
        loc = _finite_number(_required(source, "loc"), "invalid_loc")
        if loc <= 0:
            raise AnalysisValidationError("invalid_loc")
        normalized.append(
            {
                "method_id": method_id,
                "project": _identifier(_required(source, "project"), "invalid_project"),
                "predictor": _optional_number(_required(source, predictor_key)),
                "loc": loc,
            }
        )
    normalized.sort(key=lambda row: (row["project"], row["method_id"]))
    complete = [row for row in normalized if row["predictor"] is not None]
    if len(complete) < 3:
        raise AnalysisValidationError("insufficient_complete_methods")
    predictor = np.asarray([row["predictor"] for row in complete], dtype=np.float64)
    loc = np.asarray([row["loc"] for row in complete], dtype=np.float64)
    projects = [row["project"] for row in complete]
    association = raw_spearman(predictor, loc)
    association["fisher_z_diagnostic_95"] = fisher_z_interval(
        association["estimate"], len(complete), control_count=0
    )
    association["bootstrap_95"] = bootstrap_correlation(
        predictor,
        loc,
        (),
        projects,
        replicates=bootstrap_replicates,
    )
    association["freedman_lane"] = freedman_lane_test(
        predictor,
        loc,
        (),
        projects,
        replicates=permutation_replicates,
        seed=_metric_seed(f"{metric_name}:dataset_loc:raw"),
    )
    return json_safe(
        {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analysis": "complexity_dataset_loc_association",
            "metric": metric_name,
            "predictor_key": predictor_key,
            "n_total": len(normalized),
            "n_complete": len(complete),
            "n_missing": len(normalized) - len(complete),
            "raw_spearman": association,
        }
    )


def holm_adjust(p_values: Mapping[str, float | None], alpha: float = 0.05) -> dict[str, Any]:
    """Holm adjustment over the complete planned family.

    A missing test is never declared significant and remains ``None`` in the
    output, but it still occupies a family slot.  This prevents generation
    failures from silently shrinking a prespecified multiplicity family.
    """

    if not 0.0 < alpha < 1.0:
        raise AnalysisValidationError("invalid_alpha")
    valid: list[tuple[str, float]] = []
    for name, value in p_values.items():
        if value is None:
            continue
        number = _finite_number(value, "invalid_p_value")
        if not 0.0 <= number <= 1.0:
            raise AnalysisValidationError("invalid_p_value")
        valid.append((name, number))
    valid.sort(key=lambda item: (item[1], item[0]))
    m = len(p_values)
    adjusted: dict[str, float] = {}
    running_max = 0.0
    for index, (name, value) in enumerate(valid):
        running_max = max(running_max, min(1.0, (m - index) * value))
        adjusted[name] = running_max
    return {
        name: {
            "raw_p": None if value is None else float(value),
            "holm_adjusted_p": adjusted.get(name),
            "reject": adjusted.get(name) is not None and adjusted[name] <= alpha,
        }
        for name, value in p_values.items()
    }


def adjust_prespecified_families(
    p_values_by_family: Mapping[str, Mapping[str, float | None]],
    *,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Validate and adjust the four exact secondary families in Section 13."""

    output: dict[str, Any] = {}
    if set(p_values_by_family) != set(PRESPECIFIED_FAMILIES):
        raise AnalysisValidationError("multiplicity_family_names")
    for family, expected_names in PRESPECIFIED_FAMILIES.items():
        supplied = p_values_by_family[family]
        if set(supplied) != set(expected_names):
            raise AnalysisValidationError(f"multiplicity_members_{family}")
        output[family] = holm_adjust(
            {name: supplied[name] for name in expected_names}, alpha=alpha
        )
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "method": "holm_step_down",
        "alpha": alpha,
        "families": output,
    }


def _numeric_summary(values: Sequence[float]) -> dict[str, Any]:
    array = np.asarray(values, dtype=np.float64)
    if not len(array):
        return {"n": 0, "min": None, "max": None, "mean": None, "median": None, "sd": None}
    return {
        "n": len(array),
        "min": float(np.min(array)),
        "max": float(np.max(array)),
        "mean": float(np.mean(array)),
        "median": float(np.median(array)),
        "sd": float(np.std(array, ddof=1)) if len(array) > 1 else 0.0,
    }


def missingness_summary(
    records: Iterable[Mapping[str, Any]], *, predictor_key: str
) -> dict[str, Any]:
    """Describe score missingness by project, LOC, AU, and PBU."""

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        method_id = _identifier(_required(record, "method_id"), "invalid_method_id")
        if method_id in seen:
            raise AnalysisValidationError("duplicate_method_analysis_row")
        seen.add(method_id)
        au = _finite_number(_required(record, "au_mean"), "invalid_au_mean")
        pbu = _finite_number(_required(record, "pbu_mean"), "invalid_pbu_mean")
        if not 0.0 <= au <= 1.0:
            raise AnalysisValidationError("invalid_au_mean")
        if not 0.0 <= pbu <= 1.0:
            raise AnalysisValidationError("invalid_pbu_mean")
        normalized.append(
            {
                "method_id": method_id,
                "project": _identifier(_required(record, "project"), "invalid_project"),
                "predictor": _optional_number(_required(record, predictor_key)),
                "loc": _finite_number(_required(record, "loc"), "invalid_loc"),
                "au": au,
                "pbu": pbu,
            }
        )
    observed = [row for row in normalized if row["predictor"] is not None]
    missing = [row for row in normalized if row["predictor"] is None]
    projects: dict[str, Any] = {}
    for project in sorted({row["project"] for row in normalized}):
        project_rows = [row for row in normalized if row["project"] == project]
        count_missing = sum(row["predictor"] is None for row in project_rows)
        projects[project] = {
            "total": len(project_rows),
            "observed": len(project_rows) - count_missing,
            "missing": count_missing,
            "missing_fraction": count_missing / len(project_rows),
        }
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "predictor_key": predictor_key,
        "total": len(normalized),
        "observed": len(observed),
        "missing": len(missing),
        "missing_fraction": len(missing) / len(normalized) if normalized else None,
        "missing_method_ids": sorted(row["method_id"] for row in missing),
        "by_project": projects,
        "observed_characteristics": {
            key: _numeric_summary([row[key] for row in observed])
            for key in ("loc", "au", "pbu")
        },
        "missing_characteristics": {
            key: _numeric_summary([row[key] for row in missing])
            for key in ("loc", "au", "pbu")
        },
    }


def boundary_missing_score_sensitivity(
    records: Iterable[Mapping[str, Any]],
    *,
    predictor_key: str,
    outcome_key: Literal["au_mean", "pbu_mean"],
) -> dict[str, Any]:
    """Tie missing scores immediately below/above the observed score range."""

    rows = [dict(record) for record in records]
    observed = [
        value
        for row in rows
        if (value := _optional_number(_required(row, predictor_key))) is not None
    ]
    if not observed:
        raise AnalysisValidationError("no_observed_predictor")
    lower = float(np.nextafter(min(observed), -np.inf))
    upper = float(np.nextafter(max(observed), np.inf))
    results: dict[str, Any] = {}
    for label, boundary in (("below_minimum", lower), ("above_maximum", upper)):
        x: list[float] = []
        y: list[float] = []
        loc: list[float] = []
        for row in rows:
            predictor = _optional_number(_required(row, predictor_key))
            x.append(boundary if predictor is None else predictor)
            y.append(_finite_number(_required(row, outcome_key), "invalid_outcome"))
            loc.append(_finite_number(_required(row, "loc"), "invalid_loc"))
        results[label] = {
            "assigned_value": boundary,
            "raw_spearman": raw_spearman(x, y)["estimate"],
            "partial_spearman_loc": partial_spearman(x, y, (loc,)),
        }
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis": "missing_score_boundary_sensitivity",
        "n_missing": len(rows) - len(observed),
        "results": results,
    }


def stability_summary(
    records: Iterable[Mapping[str, Any]],
    *,
    value_key: str,
    runs: Sequence[int] = (0, 1, 2),
) -> dict[str, Any]:
    """Run-pair correlations, ICC(3,1), ranges, means, and success rates."""

    run_tuple = tuple(runs)
    if not run_tuple or len(set(run_tuple)) != len(run_tuple):
        raise AnalysisValidationError("invalid_runs")
    values: dict[str, dict[int, float | None]] = defaultdict(dict)
    for row in records:
        method_id = _identifier(_required(row, "method_id"), "invalid_method_id")
        run_raw = _required(row, "run")
        if isinstance(run_raw, bool):
            raise AnalysisValidationError("invalid_run")
        try:
            run = int(run_raw)
        except (TypeError, ValueError) as error:
            raise AnalysisValidationError("invalid_run") from error
        if run not in run_tuple:
            raise AnalysisValidationError("unexpected_run")
        if run in values[method_id]:
            raise AnalysisValidationError("duplicate_method_run")
        values[method_id][run] = _optional_number(_required(row, value_key))
    if not values:
        raise AnalysisValidationError("empty_stability_rows")

    method_ids = sorted(values)
    pairwise: dict[str, Any] = {}
    for left_index, left in enumerate(run_tuple):
        for right in run_tuple[left_index + 1 :]:
            pairs = [
                (values[mid].get(left), values[mid].get(right)) for mid in method_ids
            ]
            complete = [(a, b) for a, b in pairs if a is not None and b is not None]
            key = f"run_{left}_vs_{right}"
            if len(complete) < 3:
                pairwise[key] = {"n": len(complete), "spearman_rho": None}
            else:
                pairwise[key] = {
                    "n": len(complete),
                    "spearman_rho": raw_spearman(
                        [item[0] for item in complete], [item[1] for item in complete]
                    )["estimate"],
                }

    complete_matrix = np.asarray(
        [
            [values[mid].get(run) for run in run_tuple]
            for mid in method_ids
            if all(values[mid].get(run) is not None for run in run_tuple)
        ],
        dtype=np.float64,
    )
    icc = _icc_3_1(complete_matrix)
    ranges = [
        max(present) - min(present)
        for mid in method_ids
        if len(
            present := [
                values[mid][run]
                for run in run_tuple
                if values[mid].get(run) is not None
            ]
        )
        >= 2
    ]
    means = {
        mid: (
            float(np.mean(present))
            if (
                present := [
                    values[mid][run]
                    for run in run_tuple
                    if values[mid].get(run) is not None
                ]
            )
            else None
        )
        for mid in method_ids
    }
    success_rates = {}
    for run in run_tuple:
        successes = sum(values[mid].get(run) is not None for mid in method_ids)
        success_rates[str(run)] = {
            "successes": successes,
            "denominator": len(method_ids),
            "rate": successes / len(method_ids),
        }
    return json_safe(
        {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analysis": "run_stability",
            "value_key": value_key,
            "methods": len(method_ids),
            "pairwise_run_spearman": pairwise,
            "icc_3_1_consistency": icc,
            "median_within_method_absolute_range": (
                float(np.median(ranges)) if ranges else None
            ),
            "range_method_count": len(ranges),
            "success_rates": success_rates,
            "per_method_available_run_mean": means,
        }
    )


def _icc_3_1(matrix: np.ndarray) -> dict[str, Any]:
    if matrix.ndim != 2 or matrix.shape[0] < 2 or matrix.shape[1] < 2:
        return {
            "estimate": None,
            "n_complete_methods": int(matrix.shape[0]) if matrix.ndim == 2 else 0,
            "runs": int(matrix.shape[1]) if matrix.ndim == 2 else 0,
            "omission_reason": "insufficient_complete_matrix",
        }
    n, k = matrix.shape
    grand = float(np.mean(matrix))
    row_means = np.mean(matrix, axis=1)
    column_means = np.mean(matrix, axis=0)
    ss_rows = k * float(np.sum((row_means - grand) ** 2))
    residual = matrix - row_means[:, None] - column_means[None, :] + grand
    ss_error = float(np.sum(residual**2))
    ms_rows = ss_rows / (n - 1)
    ms_error = ss_error / ((n - 1) * (k - 1))
    denominator = ms_rows + (k - 1) * ms_error
    if denominator <= np.finfo(np.float64).eps or not math.isfinite(denominator):
        return {
            "estimate": None,
            "n_complete_methods": n,
            "runs": k,
            "omission_reason": "undefined_variance_components",
        }
    return {
        "estimate": (ms_rows - ms_error) / denominator,
        "n_complete_methods": n,
        "runs": k,
        "model": "two_way_mixed_consistency_single_measure",
        "omission_reason": None,
    }


def leave_one_project_out_partial(
    records: Iterable[Mapping[str, Any]],
    *,
    predictor_key: str,
    outcome_key: Literal["au_mean", "pbu_mean"],
    control_keys: Sequence[str] = ("loc",),
) -> dict[str, Any]:
    """Ten-project leave-one-out partial-rho sensitivity (generic project count)."""

    _, complete = _complete_method_records(
        records,
        predictor_key=predictor_key,
        outcome_key=outcome_key,
        control_keys=control_keys,
    )
    projects = sorted({row["project"] for row in complete})
    estimates: dict[str, float | None] = {}
    for omitted in projects:
        kept = [row for row in complete if row["project"] != omitted]
        try:
            estimate = partial_spearman(
                [row[predictor_key] for row in kept],
                [row[outcome_key] for row in kept],
                [[row[key] for row in kept] for key in control_keys],
            )
        except AnalysisValidationError:
            estimate = None
        estimates[omitted] = estimate
    finite = [value for value in estimates.values() if value is not None]
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis": "leave_one_project_out_partial_spearman",
        "estimates": estimates,
        "positive": sum(value > 0 for value in finite),
        "zero": sum(value == 0 for value in finite),
        "negative": sum(value < 0 for value in finite),
        "undefined": len(estimates) - len(finite),
    }


def _gee_group(value: Any) -> Literal["student", "professional", "other"]:
    """Map the authoritative participant-position labels to frozen GEE groups."""

    label = _identifier(value, "invalid_participant_group")
    normalized = " ".join(
        label.casefold().replace(".", "").replace("-", " ").replace("’", "'").split()
    )
    if normalized == "professional developer":
        return "professional"
    compact = normalized.replace("'", "").replace(" ", "")
    if "student" in normalized.split() and (
        "bachelor" in normalized or "master" in normalized or "phd" in compact
    ):
        return "student"
    return "other"


def _gee_failure(
    code: str,
    *,
    terms: Sequence[str],
    n_rows: int,
    n_participants: int,
) -> dict[str, Any]:
    """Return a stable failure record without exposing library exception text."""

    return {
        "status": "failure",
        "failure": code,
        "terms": list(terms),
        "n_rows": n_rows,
        "n_participants": n_participants,
        "coefficients": None,
    }


def _fit_participant_gee(
    *,
    endog: np.ndarray,
    design: np.ndarray,
    participant_ids: Sequence[str],
    terms: Sequence[str],
    outcome_key: Literal["au", "pbu"],
) -> dict[str, Any]:
    """Fit one exchangeable-cluster binomial GEE with robust covariance."""

    groups = np.asarray(participant_ids, dtype=object)
    n_rows = int(design.shape[0])
    n_participants = len(set(participant_ids))
    failure_arguments = {
        "terms": terms,
        "n_rows": n_rows,
        "n_participants": n_participants,
    }
    if design.ndim != 2 or design.shape[1] != len(terms):
        return _gee_failure("invalid_gee_design", **failure_arguments)
    if groups.shape != (n_rows,) or endog.shape[0] != n_rows:
        return _gee_failure("invalid_gee_vectors", **failure_arguments)
    if n_participants < 2:
        return _gee_failure("insufficient_participant_clusters", **failure_arguments)
    if n_rows < len(terms) or np.linalg.matrix_rank(design) != design.shape[1]:
        return _gee_failure("singular_gee_design", **failure_arguments)
    proportions = (
        endog[:, 0] / np.sum(endog, axis=1)
        if outcome_key == "au"
        else endog
    )
    if np.ptp(proportions) == 0.0:
        return _gee_failure("constant_gee_outcome", **failure_arguments)

    try:
        # The statsmodels version is runtime-pinned.  Make its otherwise
        # version-dependent defaults explicit as part of the reproducible fit.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = sm.GEE(
                endog,
                design,
                groups=groups,
                family=sm.families.Binomial(),
                cov_struct=Exchangeable(),
            )
            fitted = model.fit(
                maxiter=60,
                ctol=1e-6,
                params_niter=1,
                first_dep_update=0,
                cov_type="robust",
            )
    except Exception:  # statsmodels exposes several backend exception classes
        return _gee_failure("gee_fit_failed", **failure_arguments)

    if not bool(getattr(fitted, "converged", False)):
        return _gee_failure("gee_not_converged", **failure_arguments)
    parameters = np.asarray(fitted.params, dtype=np.float64)
    standard_errors = np.asarray(fitted.bse, dtype=np.float64)
    if (
        parameters.shape != (len(terms),)
        or standard_errors.shape != (len(terms),)
        or not np.all(np.isfinite(parameters))
        or not np.all(np.isfinite(standard_errors))
        or np.any(standard_errors < 0.0)
    ):
        return _gee_failure("nonfinite_gee_result", **failure_arguments)

    critical = float(stats.norm.ppf(0.975))
    coefficients: dict[str, Any] = {}
    for index, term in enumerate(terms):
        estimate = float(parameters[index])
        standard_error = float(standard_errors[index])
        if standard_error == 0.0:
            p_value = 0.0 if estimate != 0.0 else 1.0
        else:
            p_value = float(2.0 * stats.norm.sf(abs(estimate / standard_error)))
        coefficients[term] = {
            "estimate": estimate,
            "robust_standard_error": standard_error,
            "ci_95": [
                estimate - critical * standard_error,
                estimate + critical * standard_error,
            ],
            "two_sided_p": p_value,
        }

    cluster_sizes: dict[str, int] = defaultdict(int)
    for participant_id in participant_ids:
        cluster_sizes[participant_id] += 1
    try:
        dependence = _optional_number(getattr(fitted.cov_struct, "dep_params", None))
    except AnalysisValidationError:
        dependence = None
    return json_safe(
        {
            "status": "success",
            "failure": None,
            "family": "binomial",
            "link": "logit",
            "outcome_encoding": (
                "grouped_correct_out_of_3" if outcome_key == "au" else "bernoulli"
            ),
            "working_correlation": "exchangeable",
            "exchangeable_correlation_estimate": dependence,
            "covariance": "robust_sandwich",
            "critical_distribution": "standard_normal",
            "terms": list(terms),
            "n_rows": n_rows,
            "n_participants": n_participants,
            "cluster_size_min": min(cluster_sizes.values()),
            "cluster_size_max": max(cluster_sizes.values()),
            "fit_settings": {
                "maxiter": 60,
                "ctol": 1e-6,
                "params_niter": 1,
                "first_dep_update": 0,
                "cov_type": "robust",
            },
            "coefficients": coefficients,
        }
    )


def participant_level_gee_sensitivity(
    evaluations: Iterable[Mapping[str, Any]],
    method_records: Iterable[Mapping[str, Any]],
    *,
    predictor_key: str,
    outcome_key: Literal["au", "pbu"],
    metric_name: str,
) -> dict[str, Any]:
    """Fit the predeclared participant-level AU or PBU GEE sensitivities.

    Predictor and ``log2(dataset LOC)`` z-scores are computed exactly once on
    unique complete methods (sample SD, ``ddof=1``), then joined to participant
    rows.  Consequently, unequal response counts do not affect either
    standardization.  AU is represented as a grouped binomial correct count out
    of three; PBU is Bernoulli.  Fit-time problems are returned as sanitized,
    JSON-safe failure records rather than leaking backend exception details.
    """

    if outcome_key not in ("au", "pbu"):
        raise AnalysisValidationError("invalid_gee_outcome_key")
    _identifier(predictor_key, "invalid_predictor_key")
    _identifier(metric_name, "empty_metric_name")

    methods: dict[str, dict[str, float | None]] = {}
    for source in method_records:
        method_id = _identifier(_required(source, "method_id"), "invalid_method_id")
        if method_id in methods:
            raise AnalysisValidationError("duplicate_method_analysis_row")
        loc = _finite_number(_required(source, "loc"), "invalid_loc")
        if loc <= 0.0:
            raise AnalysisValidationError("invalid_loc")
        methods[method_id] = {
            "loc": loc,
            "predictor": _optional_number(_required(source, predictor_key)),
        }
    if not methods:
        raise AnalysisValidationError("empty_method_analysis_rows")

    eligible_by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    participant_method_keys: set[tuple[str, str]] = set()
    participant_groups: dict[str, str] = {}
    total_evaluations = 0
    missing_outcomes = 0
    for source in evaluations:
        total_evaluations += 1
        participant_id = _identifier(
            _required(source, "participant_id"), "invalid_participant_id"
        )
        method_id = _identifier(_required(source, "method_id"), "invalid_method_id")
        if method_id not in methods:
            raise AnalysisValidationError("gee_method_not_in_method_records")
        key = (participant_id, method_id)
        if key in participant_method_keys:
            raise AnalysisValidationError("duplicate_participant_method")
        participant_method_keys.add(key)

        loc = _finite_number(_required(source, "loc"), "invalid_loc")
        if loc <= 0.0 or loc != methods[method_id]["loc"]:
            raise AnalysisValidationError("inconsistent_method_loc")
        group = _gee_group(_required(source, "participant_group"))
        prior_group = participant_groups.setdefault(participant_id, group)
        if prior_group != group:
            raise AnalysisValidationError("inconsistent_participant_group")

        raw_outcome = _required(source, outcome_key)
        if raw_outcome is None or raw_outcome == "":
            missing_outcomes += 1
            continue
        outcome = _finite_number(raw_outcome, f"invalid_{outcome_key}")
        if outcome_key == "au":
            correct = outcome * 3.0
            rounded = int(round(correct))
            if rounded not in (0, 1, 2, 3) or abs(correct - rounded) > AU_TOLERANCE:
                raise AnalysisValidationError("invalid_au_correct_count")
            encoded_outcome: int = rounded
        else:
            if outcome not in (0.0, 1.0):
                raise AnalysisValidationError("invalid_pbu")
            encoded_outcome = int(outcome)
        eligible_by_method[method_id].append(
            {
                "participant_id": participant_id,
                "group": group,
                "outcome": encoded_outcome,
            }
        )
    if total_evaluations == 0:
        raise AnalysisValidationError("empty_evaluations")

    complete_method_ids = sorted(
        method_id
        for method_id, method in methods.items()
        if method["predictor"] is not None and eligible_by_method.get(method_id)
    )
    missing_predictor_ids = sorted(
        method_id for method_id, method in methods.items() if method["predictor"] is None
    )
    no_eligible_response_ids = sorted(
        method_id for method_id in methods if not eligible_by_method.get(method_id)
    )
    predictor_values = np.asarray(
        [methods[method_id]["predictor"] for method_id in complete_method_ids],
        dtype=np.float64,
    )
    log_loc_values = np.asarray(
        [math.log2(float(methods[method_id]["loc"])) for method_id in complete_method_ids],
        dtype=np.float64,
    )

    standardization_failure: str | None = None
    if len(complete_method_ids) < 2:
        standardization_failure = "insufficient_complete_methods_for_standardization"
        predictor_mean = predictor_sd = log_loc_mean = log_loc_sd = None
    else:
        predictor_mean = float(np.mean(predictor_values))
        predictor_sd = float(np.std(predictor_values, ddof=1))
        log_loc_mean = float(np.mean(log_loc_values))
        log_loc_sd = float(np.std(log_loc_values, ddof=1))
        if not math.isfinite(predictor_sd) or predictor_sd == 0.0:
            standardization_failure = "zero_or_nonfinite_predictor_sample_sd"
        elif not math.isfinite(log_loc_sd) or log_loc_sd == 0.0:
            standardization_failure = "zero_or_nonfinite_log2_loc_sample_sd"

    base_terms = ("intercept", "z_predictor", "z_log2_dataset_loc")
    interaction_terms = (
        "intercept",
        "z_predictor",
        "z_log2_dataset_loc",
        "professional_vs_student",
        "z_predictor_x_professional",
    )
    candidate_rows = [
        {**row, "method_id": method_id}
        for method_id in complete_method_ids
        for row in eligible_by_method[method_id]
    ]
    candidate_rows.sort(key=lambda row: (row["participant_id"], row["method_id"]))
    if standardization_failure is not None:
        candidate_interaction_rows = [
            row for row in candidate_rows if row["group"] != "other"
        ]
        base_model = _gee_failure(
            standardization_failure,
            terms=base_terms,
            n_rows=len(candidate_rows),
            n_participants=len({row["participant_id"] for row in candidate_rows}),
        )
        interaction_model = _gee_failure(
            standardization_failure,
            terms=interaction_terms,
            n_rows=len(candidate_interaction_rows),
            n_participants=len(
                {row["participant_id"] for row in candidate_interaction_rows}
            ),
        )
        expanded: list[dict[str, Any]] = candidate_rows
    else:
        assert predictor_mean is not None and predictor_sd is not None
        assert log_loc_mean is not None and log_loc_sd is not None
        method_z = {
            method_id: (
                (float(methods[method_id]["predictor"]) - predictor_mean)
                / predictor_sd,
                (math.log2(float(methods[method_id]["loc"])) - log_loc_mean)
                / log_loc_sd,
            )
            for method_id in complete_method_ids
        }
        expanded = [
            {
                **row,
                "z_predictor": method_z[row["method_id"]][0],
                "z_log_loc": method_z[row["method_id"]][1],
            }
            for row in candidate_rows
        ]

        base_design = np.asarray(
            [
                [1.0, row["z_predictor"], row["z_log_loc"]]
                for row in expanded
            ],
            dtype=np.float64,
        )
        if outcome_key == "au":
            base_endog = np.asarray(
                [[row["outcome"], 3 - row["outcome"]] for row in expanded],
                dtype=np.float64,
            )
        else:
            base_endog = np.asarray(
                [row["outcome"] for row in expanded], dtype=np.float64
            )
        base_model = _fit_participant_gee(
            endog=base_endog,
            design=base_design,
            participant_ids=[row["participant_id"] for row in expanded],
            terms=base_terms,
            outcome_key=outcome_key,
        )

        interaction_rows = [row for row in expanded if row["group"] != "other"]
        interaction_design = np.asarray(
            [
                [
                    1.0,
                    row["z_predictor"],
                    row["z_log_loc"],
                    float(row["group"] == "professional"),
                    row["z_predictor"] * float(row["group"] == "professional"),
                ]
                for row in interaction_rows
            ],
            dtype=np.float64,
        )
        if outcome_key == "au":
            interaction_endog = np.asarray(
                [[row["outcome"], 3 - row["outcome"]] for row in interaction_rows],
                dtype=np.float64,
            )
        else:
            interaction_endog = np.asarray(
                [row["outcome"] for row in interaction_rows], dtype=np.float64
            )
        interaction_model = _fit_participant_gee(
            endog=interaction_endog,
            design=interaction_design,
            participant_ids=[row["participant_id"] for row in interaction_rows],
            terms=interaction_terms,
            outcome_key=outcome_key,
        )

    base_group_rows = {
        group: sum(row["group"] == group for row in expanded)
        for group in ("student", "professional", "other")
    }
    base_group_participants = {
        group: len(
            {row["participant_id"] for row in expanded if row["group"] == group}
        )
        for group in ("student", "professional", "other")
    }
    return json_safe(
        {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analysis": "participant_level_binomial_gee_sensitivity",
            "metric": metric_name,
            "predictor_key": predictor_key,
            "outcome": outcome_key,
            "sensitivity_only": True,
            "standardization": {
                "scope": "unique_complete_methods_before_participant_row_expansion",
                "ddof": 1,
                "n_methods": len(complete_method_ids),
                "method_ids": complete_method_ids,
                "predictor": {"mean": predictor_mean, "sample_sd": predictor_sd},
                "log2_dataset_loc": {
                    "mean": log_loc_mean,
                    "sample_sd": log_loc_sd,
                },
                "failure": standardization_failure,
            },
            "input_counts": {
                "methods": len(methods),
                "evaluations": total_evaluations,
                "missing_outcomes": missing_outcomes,
                "missing_predictor_method_ids": missing_predictor_ids,
                "no_eligible_response_method_ids": no_eligible_response_ids,
            },
            "participant_groups": {
                "base_row_counts": base_group_rows,
                "base_participant_counts": base_group_participants,
                "interaction_reference": "student",
                "interaction_other_rows_excluded": base_group_rows["other"],
                "interaction_other_participants_excluded": base_group_participants[
                    "other"
                ],
            },
            "base_model": base_model,
            "interaction_model": interaction_model,
        }
    )


def hc3_ols_sensitivity(
    predictor: Sequence[float], outcome: Sequence[float], loc: Sequence[float]
) -> dict[str, Any]:
    """Scale-sensitive ``outcome ~ z(x) + z(log2(LOC))`` HC3 sensitivity."""

    x = np.asarray(predictor, dtype=np.float64)
    y = np.asarray(outcome, dtype=np.float64)
    loc_array = np.asarray(loc, dtype=np.float64)
    if x.shape != y.shape or x.shape != loc_array.shape or x.ndim != 1 or len(x) < 4:
        raise AnalysisValidationError("invalid_hc3_vectors")
    if not all(np.all(np.isfinite(value)) for value in (x, y, loc_array)) or np.any(loc_array <= 0):
        raise AnalysisValidationError("invalid_hc3_values")

    def zscore(value: np.ndarray) -> np.ndarray:
        sd = float(np.std(value, ddof=1))
        if sd <= np.finfo(np.float64).eps:
            raise AnalysisValidationError("undefined_hc3_zscore")
        return (value - np.mean(value)) / sd

    design = np.column_stack((np.ones(len(x)), zscore(x), zscore(np.log2(loc_array))))
    if np.linalg.matrix_rank(design) != design.shape[1]:
        raise AnalysisValidationError("singular_hc3_design")
    inverse = np.linalg.inv(design.T @ design)
    beta = inverse @ design.T @ y
    residuals = y - design @ beta
    leverage = np.einsum("ij,jk,ik->i", design, inverse, design)
    if np.any(1.0 - leverage <= np.finfo(np.float64).eps):
        raise AnalysisValidationError("invalid_hc3_leverage")
    adjusted = residuals / (1.0 - leverage)
    meat = design.T @ (design * (adjusted**2)[:, None])
    covariance = inverse @ meat @ inverse
    standard_error = math.sqrt(max(0.0, float(covariance[1, 1])))
    critical = float(stats.norm.ppf(0.975))
    coefficient = float(beta[1])
    return {
        "standardized_predictor_coefficient": coefficient,
        "hc3_standard_error": standard_error,
        "ci_95": [coefficient - critical * standard_error, coefficient + critical * standard_error],
        "n": len(x),
        "critical_distribution": "standard_normal",
        "model": "outcome ~ z(predictor) + z(log2(dataset_LOC))",
        "limitation": "OLS sensitivity for a bounded outcome; not primary inference",
    }


def au_pbu_agreement(
    records: Iterable[Mapping[str, Any]],
    *,
    bootstrap_replicates: int = PRIMARY_BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Method-level AU/PBU agreement and within-project CI for mean difference."""

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in records:
        method_id = _identifier(_required(source, "method_id"), "invalid_method_id")
        if method_id in seen:
            raise AnalysisValidationError("duplicate_method_analysis_row")
        seen.add(method_id)
        au = _finite_number(_required(source, "au_mean"), "invalid_au_mean")
        pbu = _finite_number(_required(source, "pbu_mean"), "invalid_pbu_mean")
        if not 0.0 <= au <= 1.0:
            raise AnalysisValidationError("invalid_au_mean")
        if not 0.0 <= pbu <= 1.0:
            raise AnalysisValidationError("invalid_pbu_mean")
        rows.append(
            {
                "method_id": method_id,
                "project": _identifier(_required(source, "project"), "invalid_project"),
                "au": au,
                "pbu": pbu,
            }
        )
    if len(rows) < 3:
        raise AnalysisValidationError("insufficient_agreement_methods")
    rows.sort(key=lambda row: (row["project"], row["method_id"]))
    differences = np.asarray([row["pbu"] - row["au"] for row in rows], dtype=np.float64)
    project_array = np.asarray([row["project"] for row in rows], dtype=object)
    groups = [
        np.flatnonzero(project_array == project)
        for project in sorted(set(project_array.tolist()))
    ]
    rng = np.random.Generator(np.random.PCG64(seed))
    means = np.empty(bootstrap_replicates, dtype=np.float64)
    for index in range(bootstrap_replicates):
        sampled = np.concatenate(
            [group[rng.integers(0, len(group), len(group))] for group in groups]
        )
        means[index] = np.mean(differences[sampled])
    lower, upper = np.quantile(means, (0.025, 0.975), method="linear")
    return json_safe({
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis": "au_pbu_agreement",
        "n": len(rows),
        "spearman_rho": raw_spearman(
            [row["au"] for row in rows], [row["pbu"] for row in rows]
        )["estimate"],
        "pbu_minus_au_mean": float(np.mean(differences)),
        "pbu_minus_au_median": float(np.median(differences)),
        "mean_difference_bootstrap_95": {
            "lower": float(lower),
            "upper": float(upper),
            "replicates": bootstrap_replicates,
            "seed": seed,
            "bit_generator": "PCG64",
            "cluster": "within_project",
            "method": "percentile_linear",
        },
        "signed_differences": {
            row["method_id"]: row["pbu"] - row["au"] for row in rows
        },
    })


def decide_ruby_au_gate(
    *,
    valid_run_0_ruby: int,
    total_methods: int,
    partial_rho: float | None,
    bootstrap_lower: float | None,
    one_sided_permutation_p: float | None,
) -> dict[str, Any]:
    """Evaluate the exact Section 14 fail-fast conjunction."""

    if total_methods != 50:
        raise AnalysisValidationError("gate_total_methods_not_50")
    if not 0 <= valid_run_0_ruby <= total_methods:
        raise AnalysisValidationError("gate_invalid_valid_count")
    enough_scores = valid_run_0_ruby >= 45
    if not enough_scores:
        decision = "NO-GO"
        category = "technical_no_go_inconclusive_association"
    else:
        values = (partial_rho, bootstrap_lower, one_sided_permutation_p)
        statistics_defined = all(
            value is not None and math.isfinite(float(value)) for value in values
        )
        if (
            one_sided_permutation_p is not None
            and math.isfinite(float(one_sided_permutation_p))
            and not 0.0 <= float(one_sided_permutation_p) <= 1.0
        ):
            raise AnalysisValidationError("gate_invalid_p_value")
        go = statistics_defined and (
            float(partial_rho) >= 0.30
            and float(bootstrap_lower) > 0.0
            and float(one_sided_permutation_p) <= 0.05
        )
        decision = "GO" if go else "NO-GO"
        category = "empirical_gate_pass" if go else "empirical_fail_fast"
    return json_safe({
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis": "ruby_au_fail_fast_gate",
        "decision": decision,
        "category": category,
        "inputs": {
            "valid_run_0_ruby": valid_run_0_ruby,
            "total_methods": total_methods,
            "partial_rho": partial_rho,
            "bootstrap_lower": bootstrap_lower,
            "one_sided_permutation_p": one_sided_permutation_p,
        },
        "criteria": {
            "valid_at_least_45": enough_scores,
            "partial_rho_at_least_0_30": (
                partial_rho is not None and float(partial_rho) >= 0.30
            ),
            "bootstrap_lower_above_0": (
                bootstrap_lower is not None and float(bootstrap_lower) > 0.0
            ),
            "one_sided_p_at_most_0_05": (
                one_sided_permutation_p is not None
                and float(one_sided_permutation_p) <= 0.05
            ),
        },
    })


def _json_float(value: float) -> float | None:
    return value if math.isfinite(value) else None


def json_safe(value: Any) -> Any:
    """Recursively convert NumPy scalars/non-finite floats for strict JSON."""

    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return _json_float(value)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize an analysis artifact deterministically and reject NaN."""

    return (
        json.dumps(
            json_safe(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_json_exclusive(path: str | os.PathLike[str], value: Any) -> str:
    """Atomically create (never overwrite) a canonical machine-readable artifact."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(value)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, destination)
        except FileExistsError as error:
            raise AnalysisValidationError("artifact_already_exists") from error
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return hashlib.sha256(payload).hexdigest()
