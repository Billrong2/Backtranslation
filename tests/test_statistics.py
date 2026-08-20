from __future__ import annotations

import json
import math

import numpy as np
import pytest
import statsmodels.api as sm
from scipy import stats

from backtranslation.statistics import (
    PRESPECIFIED_FAMILIES,
    AnalysisValidationError,
    CohortInvariants,
    adjust_prespecified_families,
    aggregate_method_outcomes,
    analyze_association,
    analyze_complexity_loc_association,
    au_pbu_agreement,
    boundary_missing_score_sensitivity,
    canonical_json_bytes,
    decide_ruby_au_gate,
    freedman_lane_test,
    fisher_z_interval,
    hc3_ols_sensitivity,
    holm_adjust,
    leave_one_project_out_partial,
    missingness_summary,
    partial_spearman,
    participant_level_gee_sensitivity,
    raw_spearman,
    stability_summary,
    write_json_exclusive,
)


def evaluation(
    participant: str,
    method: str,
    project: str,
    loc: float,
    au: float,
    pbu: int,
) -> dict:
    return {
        "participant_id": participant,
        "method_id": method,
        "project": project,
        "loc": loc,
        "au": au,
        "pbu": pbu,
    }


def test_method_aggregation_uses_every_row_and_sample_sd() -> None:
    rows = [
        evaluation("p1", "m2", "B", 8, 0, 0),
        evaluation("p2", "m2", "B", 8, 1, 1),
        evaluation("p1", "m1", "A", 5, 1 / 3 + 5e-13, 1),
        evaluation("p3", "m1", "A", 5, 2 / 3, 1),
    ]
    result = aggregate_method_outcomes(
        rows, expected=CohortInvariants(4, 2, 2, 3)
    )
    assert [item.method_id for item in result] == ["m1", "m2"]
    assert result[0].n_evaluations == 2
    assert result[0].au_mean == pytest.approx(0.5)
    assert result[0].au_sd == pytest.approx(math.sqrt(1 / 18))
    assert result[0].pbu_numerator == 2
    assert result[0].pbu_denominator == 2
    assert result[0].pbu_mean == 1.0
    assert result[1].au_mean == 0.5
    assert result[1].pbu_mean == 0.5


def test_method_aggregation_can_retain_authoritative_repeated_evaluations() -> None:
    rows = [
        evaluation("p1", "m1", "A", 5, 1 / 3, 1),
        evaluation("p1", "m1", "A", 5, 2 / 3, 1),
    ]
    result = aggregate_method_outcomes(
        rows,
        expected=CohortInvariants(2, 1, 1, 1),
        allow_repeated_participant_method=True,
    )
    assert result[0].n_evaluations == 2
    assert result[0].au_mean == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        (
            [
                evaluation("p1", "m1", "A", 5, 0, 0),
                evaluation("p1", "m1", "A", 5, 0, 0),
            ],
            "duplicate_participant_method",
        ),
        ([evaluation("p1", "m1", "A", 5, 0.5, 1)], "invalid_au"),
        ([evaluation("p1", "m1", "A", 5, 1 / 3, 0)], "pbu_zero_nonzero_au"),
        (
            [
                evaluation("p1", "m1", "A", 5, 0, 0),
                evaluation("p2", "m1", "A", 6, 1, 1),
            ],
            "inconsistent_method_loc",
        ),
    ],
)
def test_method_aggregation_halts_on_authoritative_invariant_failures(
    rows: list[dict], code: str
) -> None:
    with pytest.raises(AnalysisValidationError, match=code):
        aggregate_method_outcomes(rows)


def test_partial_spearman_matches_independent_rank_residual_calculation() -> None:
    predictor = np.asarray([1, 1, 3, 5, 4, 8, 8, 9], dtype=float)
    outcome = np.asarray([2, 4, 4, 3, 8, 7, 9, 10], dtype=float)
    loc = np.asarray([1, 2, 2, 4, 5, 7, 8, 10], dtype=float)

    def residual(values: np.ndarray) -> np.ndarray:
        ranked_values = stats.rankdata(values, method="average")
        ranked_loc = stats.rankdata(loc, method="average")
        design = np.column_stack((np.ones(len(loc)), ranked_loc))
        return ranked_values - design @ np.linalg.lstsq(
            design, ranked_values, rcond=None
        )[0]

    expected = float(np.corrcoef(residual(predictor), residual(outcome))[0, 1])
    assert partial_spearman(predictor, outcome, (loc,)) == pytest.approx(
        expected, abs=1e-14
    )
    assert raw_spearman(predictor, outcome)["estimate"] == pytest.approx(
        stats.spearmanr(predictor, outcome).statistic
    )
    interval = fisher_z_interval(expected, len(predictor), control_count=1)
    assert interval["lower"] < expected < interval["upper"]
    assert interval["diagnostic_only"] is True


def test_freedman_lane_matches_independent_seeded_reference() -> None:
    predictor = np.asarray([1, 3, 2, 6, 4, 7, 5, 9, 8, 10], dtype=float)
    outcome = np.asarray([2, 1, 4, 3, 7, 5, 9, 6, 10, 8], dtype=float)
    loc = np.asarray([1, 2, 4, 5, 7, 8, 10, 11, 13, 14], dtype=float)
    projects = np.asarray(["A"] * 5 + ["B"] * 5, dtype=object)
    seed = 9921
    replicates = 127

    ranked_x = stats.rankdata(predictor, method="average")
    ranked_y = stats.rankdata(outcome, method="average")
    ranked_loc = stats.rankdata(loc, method="average")
    design = np.column_stack((np.ones(len(loc)), ranked_loc))
    beta_y = np.linalg.lstsq(design, ranked_y, rcond=None)[0]
    fitted_y = design @ beta_y
    residual_y = ranked_y - fitted_y
    residual_x = ranked_x - design @ np.linalg.lstsq(
        design, ranked_x, rcond=None
    )[0]
    observed = float(np.corrcoef(residual_x, residual_y)[0, 1])
    groups = [np.flatnonzero(projects == name) for name in ("A", "B")]
    rng = np.random.Generator(np.random.PCG64(seed))
    one_extreme = two_extreme = 0
    for _ in range(replicates):
        permuted = np.empty(len(loc))
        for indices in groups:
            permuted[indices] = residual_y[rng.permutation(indices)]
        pseudo_y = fitted_y + permuted
        pseudo_residual = pseudo_y - design @ np.linalg.lstsq(
            design, pseudo_y, rcond=None
        )[0]
        statistic = float(np.corrcoef(residual_x, pseudo_residual)[0, 1])
        one_extreme += statistic >= observed
        two_extreme += abs(statistic) >= abs(observed)

    actual = freedman_lane_test(
        predictor,
        outcome,
        (loc,),
        projects,
        replicates=replicates,
        seed=seed,
    )
    assert actual["observed"] == pytest.approx(observed, abs=1e-14)
    assert actual["one_sided_positive_p"] == (1 + one_extreme) / (1 + replicates)
    assert actual["two_sided_p"] == (1 + two_extreme) / (1 + replicates)
    assert actual["undefined_replicates"] == 0


def synthetic_method_records() -> list[dict]:
    rows = []
    noise = [0.2, -0.4, 0.1, 0.5, -0.2]
    for index in range(20):
        loc = 5 + (index % 5) * 3 + index // 5
        score = 0.1 * index + noise[index % 5]
        au = 0.12 + 0.018 * index + 0.008 * loc + noise[(index + 2) % 5] * 0.05
        rows.append(
            {
                "method_id": f"M{index:02d}",
                "project": f"P{index // 5}",
                "loc": loc,
                "ruby": score,
                "au_mean": au,
                "pbu_mean": min(1.0, au + 0.08),
            }
        )
    rows[7]["ruby"] = None
    return rows


def test_full_association_is_deterministic_and_input_order_independent() -> None:
    records = synthetic_method_records()
    first = analyze_association(
        records,
        predictor_key="ruby",
        outcome_key="au_mean",
        metric_name="ruby",
        primary_gate=True,
        bootstrap_replicates=149,
        permutation_replicates=199,
    )
    second = analyze_association(
        list(reversed(records)),
        predictor_key="ruby",
        outcome_key="au_mean",
        metric_name="ruby",
        primary_gate=True,
        bootstrap_replicates=149,
        permutation_replicates=199,
    )
    assert first == second
    assert first["n_total"] == 20
    assert first["n_complete"] == 19
    assert first["missing_method_ids"] == ["M07"]
    assert first["raw_spearman"]["bootstrap_95"]["replicates"] == 149
    assert first["partial_spearman_loc"]["freedman_lane"]["replicates"] == 199
    assert first["partial_spearman_loc"]["freedman_lane"]["seed"] == 20_260_812
    assert (
        first["raw_spearman"]["freedman_lane"]["seed"]
        != first["partial_spearman_loc"]["freedman_lane"]["seed"]
    )
    # Strict machine-readable output: no implementation-specific NaN tokens.
    payload = canonical_json_bytes(first)
    assert b"NaN" not in payload
    assert json.loads(payload)["metric"] == "ruby"

    # The same engine accepts the prespecified secondary PBU outcome.
    pbu = analyze_association(
        records,
        predictor_key="ruby",
        outcome_key="pbu_mean",
        metric_name="ruby",
        bootstrap_replicates=31,
        permutation_replicates=31,
    )
    assert pbu["outcome"] == "pbu_mean"
    assert pbu["primary_gate"] is False
    with pytest.raises(AnalysisValidationError, match="primary_gate_must_be_ruby_au"):
        analyze_association(
            records,
            predictor_key="ruby",
            outcome_key="pbu_mean",
            metric_name="ruby",
            primary_gate=True,
            bootstrap_replicates=3,
            permutation_replicates=3,
        )


def test_constant_predictor_is_retained_as_an_undefined_null_result() -> None:
    records = synthetic_method_records()
    for row in records:
        row["ruby"] = 0.5
    result = analyze_association(
        records,
        predictor_key="ruby",
        outcome_key="au_mean",
        metric_name="ruby",
        primary_gate=True,
        bootstrap_replicates=11,
        permutation_replicates=13,
    )
    assert result["raw_spearman"]["estimate"] is None
    assert result["partial_spearman_loc"]["estimate"] is None
    assert (
        result["partial_spearman_loc"]["freedman_lane"]["failure"]
        == "undefined_observed_statistic"
    )


def test_complexity_loc_association_is_reported_before_outcome_interpretation() -> None:
    records = synthetic_method_records()
    for index, row in enumerate(records):
        row["instruction_count"] = index % 7 + 1
    result = analyze_complexity_loc_association(
        records,
        predictor_key="instruction_count",
        metric_name="atomic_instruction_count",
        bootstrap_replicates=39,
        permutation_replicates=41,
    )
    assert result["n_complete"] == 20
    assert result["raw_spearman"]["bootstrap_95"]["replicates"] == 39
    assert result["raw_spearman"]["freedman_lane"]["replicates"] == 41


def test_holm_step_down_and_exact_prespecified_family_membership() -> None:
    adjusted = holm_adjust({"a": 0.01, "b": 0.04, "c": 0.03, "missing": None})
    assert adjusted["a"]["holm_adjusted_p"] == pytest.approx(0.04)
    assert adjusted["c"]["holm_adjusted_p"] == pytest.approx(0.09)
    assert adjusted["b"]["holm_adjusted_p"] == pytest.approx(0.09)
    assert adjusted["missing"]["holm_adjusted_p"] is None

    families = {
        family: {name: 0.01 * (index + 1) for index, name in enumerate(names)}
        for family, names in PRESPECIFIED_FAMILIES.items()
    }
    result = adjust_prespecified_families(families)
    assert set(result["families"]) == set(PRESPECIFIED_FAMILIES)
    malformed = dict(families)
    malformed["pbu_fidelity"] = {"ruby": 0.1}
    with pytest.raises(AnalysisValidationError, match="multiplicity_members_pbu_fidelity"):
        adjust_prespecified_families(malformed)


def test_missingness_and_boundary_sensitivities_keep_all_methods() -> None:
    records = synthetic_method_records()
    summary = missingness_summary(records, predictor_key="ruby")
    assert summary["missing"] == 1
    assert summary["missing_method_ids"] == ["M07"]
    assert summary["by_project"]["P1"]["missing"] == 1
    boundary = boundary_missing_score_sensitivity(
        records, predictor_key="ruby", outcome_key="au_mean"
    )
    assert boundary["n_missing"] == 1
    assert boundary["results"]["below_minimum"]["assigned_value"] < min(
        row["ruby"] for row in records if row["ruby"] is not None
    )
    assert boundary["results"]["above_maximum"]["assigned_value"] > max(
        row["ruby"] for row in records if row["ruby"] is not None
    )
    invalid = [dict(row) for row in records]
    invalid[0]["ruby"] = float("inf")
    with pytest.raises(AnalysisValidationError, match="nonfinite_analysis_value"):
        missingness_summary(invalid, predictor_key="ruby")


def test_stability_reports_pairwise_icc_ranges_means_and_success() -> None:
    records = []
    for method in range(6):
        for run in (0, 1, 2):
            value = float(method + run * 10)
            if method == 5 and run == 2:
                value = None
            records.append({"method_id": f"M{method}", "run": run, "score": value})
    result = stability_summary(records, value_key="score")
    assert result["pairwise_run_spearman"]["run_0_vs_1"]["spearman_rho"] == 1.0
    assert result["icc_3_1_consistency"]["estimate"] == pytest.approx(1.0)
    assert result["icc_3_1_consistency"]["n_complete_methods"] == 5
    assert result["median_within_method_absolute_range"] == 20.0
    assert result["success_rates"]["2"]["successes"] == 5
    assert result["per_method_available_run_mean"]["M5"] == 10.0


def test_hc3_matches_statsmodels_reference() -> None:
    predictor = np.asarray([0.2, 0.5, 0.7, 1.1, 1.4, 1.8, 2.2, 2.7])
    outcome = np.asarray([0.1, 0.3, 0.25, 0.55, 0.52, 0.72, 0.77, 0.91])
    loc = np.asarray([4, 5, 8, 9, 13, 16, 20, 31], dtype=float)
    actual = hc3_ols_sensitivity(predictor, outcome, loc)
    z_x = (predictor - predictor.mean()) / predictor.std(ddof=1)
    log_loc = np.log2(loc)
    z_loc = (log_loc - log_loc.mean()) / log_loc.std(ddof=1)
    model = sm.OLS(outcome, np.column_stack((np.ones(len(loc)), z_x, z_loc))).fit(
        cov_type="HC3", use_t=False
    )
    assert actual["standardized_predictor_coefficient"] == pytest.approx(
        model.params[1], abs=1e-13
    )
    assert actual["hc3_standard_error"] == pytest.approx(model.bse[1], abs=1e-13)


def synthetic_gee_records() -> tuple[list[dict], list[dict]]:
    method_records = [
        {
            "method_id": f"M{method}",
            "loc": [5, 8, 12, 17, 23, 31, 42, 56][method],
            "score": [0.2, 0.9, 0.5, 1.7, 1.2, 2.8, 2.1, 3.4][method],
        }
        for method in range(8)
    ]
    labels = [
        "bachelor student",
        "Master student",
        "Ph.D. student",
        "professional developer",
        "Professional Developer",
        "visiting researcher",
    ]
    evaluations = []
    for participant, label in enumerate(labels):
        for method, method_record in enumerate(method_records):
            correct = (method + 2 * participant + int(participant in (3, 4))) % 4
            evaluations.append(
                {
                    "participant_id": f"P{participant}",
                    "participant_group": label,
                    "method_id": f"M{method}",
                    "loc": method_record["loc"],
                    "au": correct / 3,
                    "pbu": (method + participant + method * participant) % 2,
                }
            )
    return evaluations, method_records


def test_participant_gee_au_matches_grouped_binomial_statsmodels_reference() -> None:
    evaluations, methods = synthetic_gee_records()
    actual = participant_level_gee_sensitivity(
        list(reversed(evaluations)),
        list(reversed(methods)),
        predictor_key="score",
        outcome_key="au",
        metric_name="fixture",
    )
    assert actual == participant_level_gee_sensitivity(
        evaluations,
        methods,
        predictor_key="score",
        outcome_key="au",
        metric_name="fixture",
    )

    x = np.asarray([row["score"] for row in methods], dtype=float)
    log_loc = np.log2([row["loc"] for row in methods])
    z_x = (x - x.mean()) / x.std(ddof=1)
    z_loc = (log_loc - log_loc.mean()) / log_loc.std(ddof=1)
    ordered = sorted(evaluations, key=lambda row: (row["participant_id"], row["method_id"]))
    design = np.asarray(
        [
            [1.0, z_x[int(row["method_id"][1:])], z_loc[int(row["method_id"][1:])]]
            for row in ordered
        ]
    )
    endog = np.asarray(
        [[round(row["au"] * 3), 3 - round(row["au"] * 3)] for row in ordered]
    )
    expected = sm.GEE(
        endog,
        design,
        groups=[row["participant_id"] for row in ordered],
        family=sm.families.Binomial(),
        cov_struct=sm.cov_struct.Exchangeable(),
    ).fit(
        maxiter=60,
        ctol=1e-6,
        params_niter=1,
        first_dep_update=0,
        cov_type="robust",
    )

    assert actual["base_model"]["status"] == "success"
    for index, term in enumerate(("intercept", "z_predictor", "z_log2_dataset_loc")):
        coefficient = actual["base_model"]["coefficients"][term]
        assert coefficient["estimate"] == pytest.approx(expected.params[index], abs=1e-12)
        assert coefficient["robust_standard_error"] == pytest.approx(
            expected.bse[index], abs=1e-12
        )
    assert actual["base_model"]["outcome_encoding"] == "grouped_correct_out_of_3"
    assert actual["base_model"]["working_correlation"] == "exchangeable"
    assert actual["base_model"]["covariance"] == "robust_sandwich"
    assert actual["base_model"]["n_participants"] == 6
    assert actual["participant_groups"]["base_row_counts"] == {
        "student": 24,
        "professional": 16,
        "other": 8,
    }
    assert actual["interaction_model"]["n_rows"] == 40
    assert actual["participant_groups"]["interaction_reference"] == "student"
    assert json.loads(canonical_json_bytes(actual))["base_model"]["status"] == "success"


def test_participant_gee_pbu_and_interaction_match_statsmodels_reference() -> None:
    evaluations, methods = synthetic_gee_records()
    actual = participant_level_gee_sensitivity(
        evaluations,
        methods,
        predictor_key="score",
        outcome_key="pbu",
        metric_name="fixture",
    )
    x = np.asarray([row["score"] for row in methods], dtype=float)
    log_loc = np.log2([row["loc"] for row in methods])
    z_x = (x - x.mean()) / x.std(ddof=1)
    z_loc = (log_loc - log_loc.mean()) / log_loc.std(ddof=1)
    ordered = sorted(
        [row for row in evaluations if row["participant_group"] != "visiting researcher"],
        key=lambda row: (row["participant_id"], row["method_id"]),
    )
    design = []
    for row in ordered:
        method = int(row["method_id"][1:])
        professional = float("professional" in row["participant_group"].casefold())
        design.append(
            [1.0, z_x[method], z_loc[method], professional, z_x[method] * professional]
        )
    expected = sm.GEE(
        np.asarray([row["pbu"] for row in ordered], dtype=float),
        np.asarray(design),
        groups=[row["participant_id"] for row in ordered],
        family=sm.families.Binomial(),
        cov_struct=sm.cov_struct.Exchangeable(),
    ).fit(
        maxiter=60,
        ctol=1e-6,
        params_niter=1,
        first_dep_update=0,
        cov_type="robust",
    )
    assert actual["base_model"]["outcome_encoding"] == "bernoulli"
    assert actual["interaction_model"]["status"] == "success"
    terms = (
        "intercept",
        "z_predictor",
        "z_log2_dataset_loc",
        "professional_vs_student",
        "z_predictor_x_professional",
    )
    for index, term in enumerate(terms):
        coefficient = actual["interaction_model"]["coefficients"][term]
        assert coefficient["estimate"] == pytest.approx(expected.params[index], abs=1e-12)
        assert coefficient["robust_standard_error"] == pytest.approx(
            expected.bse[index], abs=1e-12
        )


def test_participant_gee_standardizes_unique_methods_before_imbalanced_rows() -> None:
    scores = [1.0, 2.0, 7.0, 9.0]
    locs = [5, 8, 19, 41]
    methods = [
        {"method_id": f"M{index}", "loc": locs[index], "score": scores[index]}
        for index in range(4)
    ]
    evaluations = []
    row_counts = [1, 2, 5, 9]
    for method, count in enumerate(row_counts):
        for participant in range(count):
            evaluations.append(
                {
                    "participant_id": f"P{participant}",
                    "participant_group": (
                        "professional developer" if participant % 2 else "bachelor student"
                    ),
                    "method_id": f"M{method}",
                    "loc": locs[method],
                    "au": ((method + participant) % 4) / 3,
                    "pbu": (method + participant) % 2,
                }
            )
    result = participant_level_gee_sensitivity(
        evaluations,
        methods,
        predictor_key="score",
        outcome_key="au",
        metric_name="fixture",
    )
    expected_score_mean = float(np.mean(scores))
    row_weighted_mean = float(np.average(scores, weights=row_counts))
    assert expected_score_mean != row_weighted_mean
    assert result["standardization"]["predictor"]["mean"] == expected_score_mean
    assert result["standardization"]["predictor"]["sample_sd"] == pytest.approx(
        np.std(scores, ddof=1)
    )
    assert result["standardization"]["log2_dataset_loc"]["mean"] == pytest.approx(
        np.mean(np.log2(locs))
    )
    assert result["standardization"]["n_methods"] == 4
    assert result["base_model"]["n_rows"] == sum(row_counts)


def test_participant_gee_reports_json_safe_failures_and_rejects_nonintegral_au() -> None:
    evaluations, methods = synthetic_gee_records()
    constant = [{**row, "score": 1.0} for row in methods]
    failed = participant_level_gee_sensitivity(
        evaluations,
        constant,
        predictor_key="score",
        outcome_key="au",
        metric_name="fixture",
    )
    assert failed["standardization"]["failure"] == "zero_or_nonfinite_predictor_sample_sd"
    assert failed["base_model"]["status"] == "failure"
    assert failed["base_model"]["coefficients"] is None
    payload = canonical_json_bytes(failed)
    assert b"NaN" not in payload and b"Traceback" not in payload

    invalid = [dict(row) for row in evaluations]
    invalid[0]["au"] = 0.5
    with pytest.raises(AnalysisValidationError, match="invalid_au_correct_count"):
        participant_level_gee_sensitivity(
            invalid,
            methods,
            predictor_key="score",
            outcome_key="au",
            metric_name="fixture",
        )


def test_au_pbu_agreement_and_leave_one_project_out_are_deterministic() -> None:
    records = synthetic_method_records()
    first = au_pbu_agreement(records, bootstrap_replicates=99, seed=81)
    second = au_pbu_agreement(list(reversed(records)), bootstrap_replicates=99, seed=81)
    assert first == second
    assert first["pbu_minus_au_mean"] > 0
    sensitivity = leave_one_project_out_partial(
        records,
        predictor_key="ruby",
        outcome_key="pbu_mean",
    )
    assert set(sensitivity["estimates"]) == {"P0", "P1", "P2", "P3"}
    assert sensitivity["positive"] + sensitivity["zero"] + sensitivity["negative"] + sensitivity["undefined"] == 4


@pytest.mark.parametrize(
    ("arguments", "decision", "category"),
    [
        (
            dict(
                valid_run_0_ruby=44,
                total_methods=50,
                partial_rho=None,
                bootstrap_lower=None,
                one_sided_permutation_p=None,
            ),
            "NO-GO",
            "technical_no_go_inconclusive_association",
        ),
        (
            dict(
                valid_run_0_ruby=45,
                total_methods=50,
                partial_rho=0.30,
                bootstrap_lower=np.nextafter(0.0, 1.0),
                one_sided_permutation_p=0.05,
            ),
            "GO",
            "empirical_gate_pass",
        ),
        (
            dict(
                valid_run_0_ruby=50,
                total_methods=50,
                partial_rho=0.299999,
                bootstrap_lower=0.1,
                one_sided_permutation_p=0.01,
            ),
            "NO-GO",
            "empirical_fail_fast",
        ),
        (
            dict(
                valid_run_0_ruby=50,
                total_methods=50,
                partial_rho=None,
                bootstrap_lower=None,
                one_sided_permutation_p=None,
            ),
            "NO-GO",
            "empirical_fail_fast",
        ),
    ],
)
def test_gate_exact_boundaries(arguments: dict, decision: str, category: str) -> None:
    result = decide_ruby_au_gate(**arguments)
    assert result["decision"] == decision
    assert result["category"] == category


def test_write_json_exclusive_is_canonical_and_write_once(tmp_path) -> None:
    target = tmp_path / "decision.json"
    value = {"z": np.float64(1.25), "a": [1, float("nan")]}
    digest = write_json_exclusive(target, value)
    assert target.read_bytes() == b'{"a":[1,null],"z":1.25}\n'
    assert len(digest) == 64
    with pytest.raises(AnalysisValidationError, match="artifact_already_exists"):
        write_json_exclusive(target, value)
