from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backtranslation.artifacts import write_json_once
from backtranslation.cases import load_study_cases
from backtranslation.ruby_scoring import (
    AST_REPRESENTATION,
    GRS_DEFINITION,
    GRS_UNAVAILABLE_REASON,
    RUBY_ARTIFACT,
    RUBY_DEFINITION,
    RUBY_FAILURE_SCHEMA,
    RUBY_SCHEMA,
    STS_DEFINITION,
    TREE_EDIT_ALGORITHM,
    TRS_DEFINITION,
    OrderedAstNode,
    RubyScoringError,
    ordered_java_method_ast,
    ordered_tree_edit_distance,
    ruby_failure_artifact,
    ruby_fidelity_artifact,
    ruby_similarity,
    score_generated_run_ruby,
    string_similarity,
    token_levenshtein,
    tree_similarity,
)


PROJECT = Path(__file__).resolve().parents[1]


def test_ruby_adaptation_manifest_binds_implementation_and_fixtures() -> None:
    manifest = json.loads(
        (PROJECT / "config" / "ruby-java-adaptation.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["schema_version"] == "backtranslation.ruby-java-adaptation.v1"
    assert manifest["definition"] == RUBY_DEFINITION
    assert manifest["grs"] == {
        "definition": GRS_DEFINITION,
        "available": False,
        "reason": GRS_UNAVAILABLE_REASON,
        "ast_or_cfg_proxy_permitted": False,
    }
    assert manifest["trs_adaptation"]["definition"] == TRS_DEFINITION
    assert manifest["trs_adaptation"]["representation"] == AST_REPRESENTATION
    assert manifest["trs_adaptation"]["algorithm"] == TREE_EDIT_ALGORITHM
    assert manifest["sts"]["definition"] == STS_DEFINITION
    assert manifest["fixtures"]["canonical_code1_identity"] == {
        "study_case_count": 50,
        "study_cases_sha256": hashlib.sha256(
            (PROJECT / "data" / "study_cases.jsonl").read_bytes()
        ).hexdigest(),
        "selected_tier": "trs_adaptation",
        "score": 1.0,
    }
    for record in manifest["implementation_files"]:
        payload = (PROJECT / record["path"]).read_bytes()
        assert len(payload) == record["bytes"], record["path"]
        assert hashlib.sha256(payload).hexdigest() == record["sha256"], record["path"]


def test_sts_is_exact_token_levenshtein_over_max_length() -> None:
    reference = ("if", "(", "x", ")", "return", "x", ";")
    candidate = ("if", "(", "y", ")", "return", ";")
    assert token_levenshtein(reference, candidate) == 2
    result = string_similarity(reference, candidate)
    assert result.score == pytest.approx(1 - 2 / 7)
    assert result.distance == 2
    assert result.reference_token_count == 7
    assert result.candidate_token_count == 6
    assert result.as_dict()["definition"] == STS_DEFINITION
    # STS is symmetric despite the reference/candidate labels.
    assert string_similarity(candidate, reference).score == result.score


def test_sts_handles_one_empty_stream_but_rejects_two_empty_streams() -> None:
    result = string_similarity(("return",), ())
    assert result.distance == 1
    assert result.score == 0.0
    no_overlap = string_similarity(("a", "b"), ("x", "y"))
    assert no_overlap.distance == 2
    assert no_overlap.score == 0.0
    with pytest.raises(RubyScoringError, match="ruby_sts_both_token_streams_empty"):
        string_similarity((), ())


def test_ordered_ted_nontrivial_children_are_visited_once() -> None:
    # Reference postorder has exactly four nodes: a, c, b, root. If a child
    # were visited twice, these exact edit distances and normalized sizes would
    # change and the fixture would fail.
    reference = OrderedAstNode(
        "root",
        (
            OrderedAstNode("a"),
            OrderedAstNode("b", (OrderedAstNode("c"),)),
        ),
    )
    relabel = OrderedAstNode(
        "root",
        (
            OrderedAstNode("a"),
            OrderedAstNode("b", (OrderedAstNode("d"),)),
        ),
    )
    deleted = OrderedAstNode("root", (OrderedAstNode("b", (OrderedAstNode("c"),)),))
    assert ordered_tree_edit_distance(reference, reference) == 0
    assert ordered_tree_edit_distance(reference, relabel) == 1
    assert ordered_tree_edit_distance(reference, deleted) == 1
    result = tree_similarity(reference, relabel)
    assert result.distance == 1
    assert result.reference_node_count == 4
    assert result.candidate_node_count == 4
    assert result.score == pytest.approx(7 / 8)


def test_ordered_ted_has_no_move_operation() -> None:
    left = OrderedAstNode("root", (OrderedAstNode("a"), OrderedAstNode("b")))
    right = OrderedAstNode("root", (OrderedAstNode("b"), OrderedAstNode("a")))
    # A move-aware algorithm could count one move. The pinned adaptation has
    # unit insert/delete/relabel only; two relabels are the minimum here.
    assert ordered_tree_edit_distance(left, right) == 2
    assert tree_similarity(left, right).score == pytest.approx(2 / 3)


def test_java_ast_representation_is_pinned_and_token_sensitive() -> None:
    one = "int f(int x) { return x + 1; }"
    two = "int f(int x) { return x + 2; }"
    first = ordered_java_method_ast(one, "int f(int x)")
    second = ordered_java_method_ast(two, "int f(int x)")
    first_again = ordered_java_method_ast(one, "int f(int x)")
    assert first == first_again
    result = tree_similarity(first, second)
    assert result.distance == 1
    assert result.reference_node_count == result.candidate_node_count == 12
    assert result.score == pytest.approx(23 / 24)
    assert result.reference_representation_sha256 != result.candidate_representation_sha256
    assert AST_REPRESENTATION == "tree-sitter-java-named-ast-enriched-terminals-v1"
    assert TREE_EDIT_ALGORITHM == "zhang-shasha-ordered-unit-cost-no-move-v1"


def test_composite_selects_trs_adaptation_when_both_asts_exist() -> None:
    result = ruby_similarity(
        "int f(int x) { return x + 1; }",
        "int f(int x) { return x + 2; }",
        "int f(int x)",
    )
    assert result.selected_tier == "trs_adaptation"
    assert result.score == result.trs_adaptation["score"]
    assert result.grs == {
        "definition": GRS_DEFINITION,
        "available": False,
        "score": None,
        "reason": GRS_UNAVAILABLE_REASON,
    }
    assert result.trs_adaptation["definition"] == TRS_DEFINITION
    assert result.sts["available"] is True
    assert result.selection_reasons == (
        GRS_UNAVAILABLE_REASON,
        "trs_adaptation_selected_both_asts_available",
    )


def test_all_50_canonical_code1_methods_have_exact_trs_identity() -> None:
    cases = load_study_cases(PROJECT / "data" / "tse")
    assert len(cases) == 50
    for case in cases:
        result = ruby_similarity(
            case.code_1, case.code_1, case.target_declaration
        )
        assert result.selected_tier == "trs_adaptation", case.method_id
        assert result.score == 1.0, case.method_id
        assert result.trs_adaptation["score"] == 1.0, case.method_id
        assert result.trs_adaptation["distance"] == 0, case.method_id
        assert result.grs == {
            "definition": GRS_DEFINITION,
            "available": False,
            "score": None,
            "reason": GRS_UNAVAILABLE_REASON,
        }


def test_composite_falls_back_to_sts_on_invalid_candidate_ast() -> None:
    result = ruby_similarity(
        "int f(int x) { return x + 1; }",
        "int f(int x) { return x + ; }",
        "int f(int x)",
    )
    assert result.selected_tier == "sts"
    assert result.score == result.sts["score"]
    assert result.trs_adaptation["available"] is False
    assert result.trs_adaptation["score"] is None
    assert "trs_candidate_parse_invalid" in result.selection_reasons
    assert result.selection_reasons[-1] == "sts_selected_trs_adaptation_unavailable"


def test_success_and_failure_artifact_schemas_are_exact() -> None:
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    freeze_hash = "a" * 64
    success = ruby_fidelity_artifact(
        case=case,
        run_index=0,
        freeze_manifest_sha256=freeze_hash,
        candidate_code=case.code_1,
    )
    assert set(success) == {
        "schema_version",
        "definition",
        "method_id",
        "run_index",
        "freeze_manifest_sha256",
        "code_1_sha256",
        "code_2_sha256",
        "score",
        "selected_tier",
        "tiers",
        "selection_reasons",
    }
    assert success["schema_version"] == RUBY_SCHEMA
    assert success["definition"] == RUBY_DEFINITION
    assert success["score"] == 1.0
    assert success["selected_tier"] == "trs_adaptation"
    assert set(success["tiers"]) == {"grs", "trs_adaptation", "sts"}

    failure = ruby_failure_artifact(
        case=case,
        run_index=1,
        freeze_manifest_sha256=freeze_hash,
        code_2_sha256="b" * 64,
        failure_code="ruby_test_failure",
    )
    assert set(failure) == {
        "schema_version",
        "definition",
        "method_id",
        "run_index",
        "freeze_manifest_sha256",
        "code_1_sha256",
        "code_2_sha256",
        "failure_code",
    }
    assert failure["schema_version"] == RUBY_FAILURE_SCHEMA


def test_per_run_ruby_artifact_is_identity_bound_and_write_once(tmp_path: Path) -> None:
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    freeze_hash = "c" * 64
    run = tmp_path / "run"
    write_json_once(
        run / "status.json",
        {
            "status": "generated",
            "method_id": case.method_id,
            "run_index": 0,
            "protocol_hash": freeze_hash,
        },
    )
    output = {
        "schema_version": "regenerated-code-v1",
        "language": "java",
        "code": case.code_1,
    }
    write_json_once(
        run / "regeneration.result.json",
        {
            "method_id": case.method_id,
            "run_index": 0,
            "output": output,
            "code_2_sha256": hashlib.sha256(case.code_1.encode()).hexdigest(),
        },
    )
    result = score_generated_run_ruby(
        case=case,
        run_directory=run,
        freeze_manifest_sha256=freeze_hash,
    )
    assert json.loads((run / RUBY_ARTIFACT).read_text()) == result
    with pytest.raises(RubyScoringError, match="artifact_already_exists"):
        score_generated_run_ruby(
            case=case,
            run_directory=run,
            freeze_manifest_sha256=freeze_hash,
        )
