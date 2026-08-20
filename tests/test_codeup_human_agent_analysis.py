from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtranslation.codeup_human_agent_analysis import (
    _auc,
    build_reports,
    build_results,
    code_intent_prompt,
    intent_metrics,
    reference_intent_prompt,
    static_features,
    validate_code_judgment,
    validate_reference,
)
from backtranslation.codeup_stage1 import Stage1Error, canonical_json_bytes, sha256_bytes


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def test_intent_prompts_are_arm_local_and_pre_revision() -> None:
    reference = reference_intent_prompt("replace the unsafe call", "A.java")
    assert "replace the unsafe call" in reference
    assert "HUMAN_SECRET" not in reference
    judgment = code_intent_prompt(["replace the unsafe call"], "safe();", "agent-original")
    assert "safe();" in judgment
    assert "agent-original" in judgment
    assert "HUMAN_SECRET" not in judgment


def test_intent_contract_and_metrics() -> None:
    assert validate_reference({"intents": ["one", "two"]}) == ["one", "two"]
    judgment = validate_code_judgment(
        {
            "code_intents": ["does one", "adds logging"],
            "reference_statuses": ["preserved", "changed", "lost"],
            "added_code_intent_indices": [1],
        },
        3,
    )
    metrics = intent_metrics(judgment)
    assert metrics["strict_preservation_rate"] == pytest.approx(1 / 3)
    assert metrics["weighted_preservation_rate"] == pytest.approx(0.5)
    assert metrics["addition_rate"] == pytest.approx(0.5)
    with pytest.raises(Stage1Error):
        validate_code_judgment(
            {
                "code_intents": ["one"],
                "reference_statuses": ["preserved"],
                "added_code_intent_indices": [2],
            },
            1,
        )
    empty = validate_code_judgment(
        {
            "code_intents": [],
            "reference_statuses": ["lost"],
            "added_code_intent_indices": [],
        },
        1,
    )
    assert intent_metrics(empty)["code_intent_count"] == 0
    assert intent_metrics(empty)["intent_fidelity_f1"] == 0.0


def test_static_features_use_same_transparent_rules() -> None:
    values = static_features(
        "if (x > 20) { try { Thread.sleep(2); } catch (Exception e) { e.printStackTrace(); } }"
    )
    assert values["cyclomatic_complexity_proxy"] == 3
    assert "magic_number" in values["smell_rules"]
    assert "generic_exception_catch" in values["smell_rules"]
    assert "print_stack_trace" in values["smell_rules"]
    assert "thread_sleep" in values["smell_rules"]


def test_auc_is_tie_aware_and_directional() -> None:
    assert _auc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == 1.0
    assert _auc([1, 0], [0.5, 0.5]) == 0.5


def test_markdown_and_latex_reports_render_table(tmp_path: Path) -> None:
    comparison = {
        "human": {"mean": 0.8},
        "agent": {"mean": 0.7},
        "paired_difference_human_minus_agent": {"mean": 0.1},
        "paired_wilcoxon_p_value": 0.01,
        "roc_auc_human_as_positive": 0.6,
        "roc_auc_separation": 0.6,
        "direction_of_higher_values": "human",
    }
    keys = (
        "roundtrip_codebert",
        "roundtrip_bleu",
        "roundtrip_rouge_l",
        "intent_fidelity_original",
        "intent_fidelity_roundtrip",
        "intent_count_original",
        "ccn_proxy_original",
        "smell_count_original",
        "token_count_original",
        "revision_codebert_from_pre_review",
        "revision_bleu_from_pre_review",
        "revision_rouge_l_from_pre_review",
        "roundtrip_change_intent_count",
        "roundtrip_change_ccn_proxy",
        "roundtrip_change_smell_count",
        "roundtrip_drift_intent_fidelity",
        "roundtrip_drift_strict_preservation_rate",
        "roundtrip_drift_change_rate",
        "roundtrip_drift_loss_rate",
        "roundtrip_drift_addition_rate",
    )
    output = tmp_path / "artifacts"
    output.mkdir()
    (output / "results.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "codeup.human-agent.v1.results",
                "design": {"cases": 2},
                "comparisons": {key: comparison for key in keys},
                "human_agent_original_similarity": {
                    key: {
                        "mean": 0.5,
                        "median": 0.5,
                        "mean_ci95_low": 0.4,
                        "mean_ci95_high": 0.6,
                    }
                    for key in ("codebert", "bleu", "rouge_1_f1", "rouge_2_f1", "rouge_l_f1")
                },
                "review_metadata_summary": {
                    "numeric": {
                        "inline_review_comment_count": {
                            "available": 2,
                            "missing": 0,
                            "values": {"mean": 1.5, "median": 1.5},
                        }
                    },
                    "categorical": {
                        "rq3_acceptability": {"available": 1},
                        "rq4_improvement": {"available": 1},
                    },
                },
                "case_rows": [],
                "limitations": ["A limitation."],
            }
        )
        + b"\n"
    )
    markdown, latex = build_reports(output, tmp_path / "reports")
    assert "Overall paired results" in markdown.read_text()
    assert "Round-trip CodeBERT similarity" in markdown.read_text()
    assert "\\begin{longtable}" in latex.read_text()


def test_build_results_wires_paired_arms_and_gpu_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "artifacts" / "codeup-human-agent"
    cases = []
    for index in range(2):
        case_id = f"case-{index}"
        cases.append(
            {
                "case_id": case_id,
                "project": "o/r",
                "pr_number": index + 1,
                "path": "A.java",
                "pre_review_code": "return oldValue;",
                "metadata": {},
            }
        )
        attempt_path = output / "runs" / case_id / "attempt-001" / "attempt.json"
        attempt = {
            "status": "valid",
            "case_id": case_id,
            "arms": {
                "human": {
                    "original_code": "return safeValue;",
                    "reconstructed_code": "return safeValue;",
                },
                "agent": {
                    "original_code": "if (safeValue != null) return safeValue;",
                    "reconstructed_code": "return safeValue;",
                },
            },
        }
        _write_json(attempt_path, attempt)
        _write_json(
            output / "runs" / case_id / "selected.json",
            {
                "attempt_path": str(attempt_path.relative_to(output)),
                "attempt_sha256": __import__("hashlib").sha256(attempt_path.read_bytes()).hexdigest(),
            },
        )
        metric = {
            "reference_intent_count": 1,
            "code_intent_count": 1,
            "preserved_count": 1,
            "changed_count": 0,
            "lost_count": 0,
            "added_count": 0,
            "intent_fidelity_f1": 1.0,
            "strict_preservation_rate": 1.0,
            "weighted_preservation_rate": 1.0,
            "change_rate": 0.0,
            "loss_rate": 0.0,
            "addition_rate": 0.0,
        }
        intent_root = output / "intent-analysis"
        reference = ["return the safe value"]
        reference_attempt = intent_root / "runs" / case_id / "reference" / "attempt-001" / "attempt.json"
        _write_json(
            reference_attempt,
            {
                "schema_version": "codeup.human-agent.v1.intent-stage",
                "status": "valid",
                "case_id": case_id,
                "stage": "reference",
                "attempt_index": 1,
                "model": "deepseek-v4-pro",
                "value": reference,
            },
        )
        _write_json(
            intent_root / "runs" / case_id / "reference" / "selected.json",
            {
                "case_id": case_id,
                "stage": "reference",
                "attempt_path": str(reference_attempt.relative_to(intent_root)),
                "attempt_sha256": sha256_bytes(reference_attempt.read_bytes()),
            },
        )
        judgment = {
            "code_intents": ["return the safe value"],
            "reference_statuses": ["preserved"],
            "added_code_intent_indices": [],
        }
        judgments = {}
        for stage in (
            "human-original",
            "human-roundtrip",
            "agent-original",
            "agent-roundtrip",
        ):
            intent_attempt = intent_root / "runs" / case_id / stage / "attempt-001" / "attempt.json"
            _write_json(
                intent_attempt,
                {
                    "schema_version": "codeup.human-agent.v1.intent-stage",
                    "status": "valid",
                    "case_id": case_id,
                    "stage": stage,
                    "attempt_index": 1,
                    "model": "deepseek-v4-pro",
                    "value": judgment,
                },
            )
            _write_json(
                intent_root / "runs" / case_id / stage / "selected.json",
                {
                    "case_id": case_id,
                    "stage": stage,
                    "attempt_path": str(intent_attempt.relative_to(intent_root)),
                    "attempt_sha256": sha256_bytes(intent_attempt.read_bytes()),
                },
            )
            judgments[stage] = {"value": judgment, "metrics": metric}
        _write_json(
            intent_root / "runs" / case_id / "complete.json",
            {
                "schema_version": "codeup.human-agent.v1.intent-case",
                "case_id": case_id,
                "generation_attempt_path": str(attempt_path.relative_to(output)),
                "generation_attempt_sha256": sha256_bytes(attempt_path.read_bytes()),
                "reference_intents": reference,
                "reference_intents_sha256": sha256_bytes(canonical_json_bytes(reference)),
                "model": "deepseek-v4-pro",
                "judgments": judgments,
            },
        )
    _write_json(output / "cohort.json", {"cases": cases})
    _write_json(output / "intent-analysis" / "summary.json", {"complete": True})
    monkeypatch.setattr(
        "backtranslation.codeup_human_agent_analysis.load_pinned_codebert",
        lambda *args: (object(), object()),
    )
    monkeypatch.setattr(
        "backtranslation.codeup_human_agent_analysis.codebert_batch_similarities",
        lambda pairs, **kwargs: ([0.75] * len(pairs), "Synthetic GPU"),
    )
    result = build_results(tmp_path, output)
    assert result["design"]["cases"] == 2
    assert result["design"]["codebert_device"] == "Synthetic GPU"
    assert result["human_agent_original_similarity"]["codebert"]["mean"] == 0.75
    assert result["comparisons"]["roundtrip_codebert"]["human"]["n"] == 2
