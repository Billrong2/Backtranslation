"""Outcome-blind RUBY-style fidelity scoring for Java method pairs.

Tran et al.'s RUBY selects the highest representation available for both
programs: PDG graph similarity (GRS), AST tree similarity (TRS), then token
string similarity (STS).  This module is deliberately named and versioned as
an independent Java paper-specification *adaptation*, not the unavailable
authors' C#/Roslyn/TREED/Exas implementation.

GRS is always reported unavailable: this project has neither a pinned Java PDG
construction nor Exas reproduction.  STS follows the paper's equation exactly
over the project's pinned normalized Java tokens.  The tree tier is an
explicit ordered-tree adaptation using the pinned Tree-sitter Java grammar and
unit-cost ordered tree edit distance.  It does not claim TREED's move-aware
algorithm; moves are represented by the minimum sequence of insertions,
deletions, and relabels under the ordered-tree definition below.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, read_json_object, write_json_once
from .cases import StudyCase
from .directions import validate_regenerated_code
from .java_validation import (
    JavaMethodAnalysis,
    JavaValidationError,
    _CALLABLE_TYPES,
    _COMMENT_TYPES,
    _target_spec,
    _wrapped_parse,
    analyze_java_method,
)
from .scoring import ScoringError, validate_scoring_tokens


RUBY_ARTIFACT = "ruby-fidelity.json"
RUBY_FAILURE_ARTIFACT = "ruby-fidelity.failure.json"
RUBY_SCHEMA = "backtranslation.ruby-fidelity.v1"
RUBY_FAILURE_SCHEMA = "backtranslation.ruby-fidelity-failure.v1"
RUBY_DEFINITION = "ruby-java-paper-specification-adaptation-v1"
STS_DEFINITION = "ruby-paper-sts-token-levenshtein-v1"
TRS_DEFINITION = "ruby-style-trs-java-ordered-ted-no-move-v1"
GRS_DEFINITION = "ruby-paper-grs-unavailable-no-pdg-exas-v1"
AST_REPRESENTATION = "tree-sitter-java-named-ast-enriched-terminals-v1"
TREE_EDIT_ALGORITHM = "zhang-shasha-ordered-unit-cost-no-move-v1"
GRS_UNAVAILABLE_REASON = "no_authenticated_java_pdg_exas_pipeline"

_SHA256_LENGTH = 64


class RubyScoringError(ValueError):
    """Stable, source-free RUBY scoring or artifact failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OrderedAstNode:
    """One immutable node in the pinned ordered Java AST representation."""

    label: str
    children: tuple["OrderedAstNode", ...] = ()


@dataclass(frozen=True)
class StringSimilarity:
    score: float
    distance: int
    reference_token_count: int
    candidate_token_count: int
    reference_tokens_sha256: str
    candidate_tokens_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "definition": STS_DEFINITION,
            "available": True,
            "score": self.score,
            "distance": self.distance,
            "reference_token_count": self.reference_token_count,
            "candidate_token_count": self.candidate_token_count,
            "reference_tokens_sha256": self.reference_tokens_sha256,
            "candidate_tokens_sha256": self.candidate_tokens_sha256,
        }


@dataclass(frozen=True)
class TreeSimilarity:
    score: float
    distance: int
    reference_node_count: int
    candidate_node_count: int
    reference_representation_sha256: str
    candidate_representation_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "definition": TRS_DEFINITION,
            "available": True,
            "score": self.score,
            "distance": self.distance,
            "reference_node_count": self.reference_node_count,
            "candidate_node_count": self.candidate_node_count,
            "reference_representation_sha256": self.reference_representation_sha256,
            "candidate_representation_sha256": self.candidate_representation_sha256,
            "unavailability_reasons": [],
        }


@dataclass(frozen=True)
class RubyScore:
    score: float
    selected_tier: str
    selection_reasons: tuple[str, ...]
    grs: Mapping[str, Any]
    trs_adaptation: Mapping[str, Any]
    sts: Mapping[str, Any]

    def tiers_dict(self) -> dict[str, Any]:
        return {
            "grs": dict(self.grs),
            "trs_adaptation": dict(self.trs_adaptation),
            "sts": dict(self.sts),
        }


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RubyScoringError("ruby_canonical_json_failed") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validated_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    if isinstance(tokens, (str, bytes, bytearray)) or not isinstance(tokens, Sequence):
        raise RubyScoringError("ruby_tokens_not_sequence")
    frozen = tuple(tokens)
    if not frozen:
        return frozen
    try:
        return validate_scoring_tokens(frozen)
    except ScoringError as exc:
        raise RubyScoringError(exc.code) from exc


def token_levenshtein(left: Sequence[str], right: Sequence[str]) -> int:
    """Return unit-cost Levenshtein distance between exact token sequences."""

    a = _validated_tokens(left)
    b = _validated_tokens(right)
    # Retain only the shorter row in memory; unit insert/delete/substitute
    # costs make the distance symmetric.
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for row, left_token in enumerate(a, start=1):
        current = [row]
        for column, right_token in enumerate(b, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (left_token != right_token),
                )
            )
        previous = current
    return previous[-1]


def string_similarity(
    reference_tokens: Sequence[str], candidate_tokens: Sequence[str]
) -> StringSimilarity:
    """Compute paper STS = 1 - token Levenshtein / maximum token length."""

    reference = _validated_tokens(reference_tokens)
    candidate = _validated_tokens(candidate_tokens)
    denominator = max(len(reference), len(candidate))
    if denominator == 0:
        raise RubyScoringError("ruby_sts_both_token_streams_empty")
    distance = token_levenshtein(reference, candidate)
    score = 1.0 - distance / denominator
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        raise RubyScoringError("ruby_sts_score_out_of_range")
    return StringSimilarity(
        score=score,
        distance=distance,
        reference_token_count=len(reference),
        candidate_token_count=len(candidate),
        reference_tokens_sha256=_sha256_json(reference),
        candidate_tokens_sha256=_sha256_json(candidate),
    )


def _node_label(
    node: Any,
    *,
    source: bytes,
    incoming_field: str | None,
) -> tuple[str, list[tuple[Any, str | None]]]:
    """Build one explicit label and the represented named children.

    The ordered tree retains named Tree-sitter Java nodes. Each label binds the
    incoming Tree-sitter field, exact spelling for a named terminal, and every
    direct anonymous grammar terminal (keywords, punctuation, and operators)
    with its comment-independent syntax position. Comments are omitted.
    """

    represented_children: list[tuple[Any, str | None]] = []
    anonymous: list[list[Any]] = []
    syntax_position = 0
    for child_index, child in enumerate(node.children):
        if child.type in _COMMENT_TYPES:
            continue
        if child.is_missing or child.is_error:
            raise RubyScoringError("ruby_ast_contains_error_or_missing_node")
        field = node.field_name_for_child(child_index)
        if child.is_named:
            represented_children.append((child, field))
        else:
            if child.child_count != 0 or child.end_byte <= child.start_byte:
                raise RubyScoringError("ruby_ast_unnamed_terminal_invalid")
            try:
                spelling = source[child.start_byte : child.end_byte].decode(
                    "utf-8", errors="strict"
                )
            except UnicodeDecodeError as exc:
                raise RubyScoringError("ruby_ast_terminal_not_utf8") from exc
            anonymous.append([syntax_position, child.type, spelling])
        syntax_position += 1

    terminal: str | None = None
    if node.child_count == 0:
        if node.end_byte <= node.start_byte:
            raise RubyScoringError("ruby_ast_named_terminal_invalid")
        try:
            terminal = source[node.start_byte : node.end_byte].decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError as exc:
            raise RubyScoringError("ruby_ast_terminal_not_utf8") from exc
    label = _canonical_json(
        {
            "anonymous_terminals": anonymous,
            "incoming_field": incoming_field,
            "node_type": node.type,
            "terminal_spelling": terminal,
        }
    ).decode("utf-8")
    return label, represented_children


def _ordered_ast_from_node(
    node: Any, *, source: bytes, incoming_field: str | None = None
) -> OrderedAstNode:
    label, represented = _node_label(
        node, source=source, incoming_field=incoming_field
    )
    return OrderedAstNode(
        label=label,
        children=tuple(
            _ordered_ast_from_node(child, source=source, incoming_field=field)
            for child, field in represented
        ),
    )


def _ast_unavailability(analysis: JavaMethodAnalysis, side: str) -> tuple[str, ...]:
    reasons: list[str] = []
    if not analysis.lex.parse_success:
        reasons.append(f"trs_{side}_parse_invalid")
    if not analysis.exactly_one_target_callable:
        reasons.append(f"trs_{side}_callable_count_not_one")
    if not analysis.no_sibling_members:
        reasons.append(f"trs_{side}_sibling_member_present")
    if not analysis.no_enclosing_type:
        reasons.append(f"trs_{side}_enclosing_type_present")
    if not analysis.body_present:
        reasons.append(f"trs_{side}_body_missing")
    return tuple(reasons)


def ordered_java_method_ast(
    source: str | bytes,
    target_declaration: str | bytes,
    *,
    analysis: JavaMethodAnalysis | None = None,
) -> OrderedAstNode:
    """Return the pinned ordered AST for one unambiguous parseable method."""

    checked = analysis or analyze_java_method(source, target_declaration)
    reasons = _ast_unavailability(checked, "input")
    if reasons:
        raise RubyScoringError(reasons[0])
    try:
        target = _target_spec(target_declaration)
        parsed = _wrapped_parse(source, target.wrapper_class_name)
    except JavaValidationError as exc:
        raise RubyScoringError(exc.code) from exc
    callables = tuple(
        member for member in parsed.members if member.type in _CALLABLE_TYPES
    )
    if len(callables) != 1:
        raise RubyScoringError("ruby_ast_callable_count_changed")
    return _ordered_ast_from_node(
        callables[0], source=parsed.wrapper_bytes, incoming_field=None
    )


def _postorder(
    root: OrderedAstNode,
) -> tuple[list[str], list[int], list[int]]:
    """Return one-indexed labels, leftmost-leaf indices, and keyroots."""

    labels = [""]
    leftmost = [0]

    def visit(node: OrderedAstNode) -> int:
        child_indices = [visit(child) for child in node.children]
        index = len(labels)
        labels.append(node.label)
        leftmost.append(leftmost[child_indices[0]] if child_indices else index)
        return index

    visit(root)
    last_for_leftmost: dict[int, int] = {}
    for index in range(1, len(labels)):
        last_for_leftmost[leftmost[index]] = index
    return labels, leftmost, sorted(last_for_leftmost.values())


def ordered_tree_edit_distance(left: OrderedAstNode, right: OrderedAstNode) -> int:
    """Exact Zhang-Shasha distance for ordered trees with unit edit costs.

    Insert and delete each cost one node and use the standard ordered-tree
    semantics (deleting a node promotes its children). Relabel costs zero for
    identical labels and one otherwise. Reordering/move is not an operation.
    """

    left_labels, leftmost_left, left_keyroots = _postorder(left)
    right_labels, leftmost_right, right_keyroots = _postorder(right)
    left_size = len(left_labels) - 1
    right_size = len(right_labels) - 1
    tree_distance = [
        [0] * (right_size + 1) for _ in range(left_size + 1)
    ]

    for left_root in left_keyroots:
        left_start = leftmost_left[left_root] - 1
        for right_root in right_keyroots:
            right_start = leftmost_right[right_root] - 1
            forest_distance: dict[tuple[int, int], int] = {
                (left_start, right_start): 0
            }
            for left_index in range(left_start + 1, left_root + 1):
                forest_distance[(left_index, right_start)] = (
                    forest_distance[(left_index - 1, right_start)] + 1
                )
            for right_index in range(right_start + 1, right_root + 1):
                forest_distance[(left_start, right_index)] = (
                    forest_distance[(left_start, right_index - 1)] + 1
                )
            for left_index in range(left_start + 1, left_root + 1):
                for right_index in range(right_start + 1, right_root + 1):
                    delete = forest_distance[(left_index - 1, right_index)] + 1
                    insert = forest_distance[(left_index, right_index - 1)] + 1
                    if (
                        leftmost_left[left_index] == leftmost_left[left_root]
                        and leftmost_right[right_index]
                        == leftmost_right[right_root]
                    ):
                        relabel = forest_distance[
                            (left_index - 1, right_index - 1)
                        ] + (left_labels[left_index] != right_labels[right_index])
                        value = min(delete, insert, relabel)
                        forest_distance[(left_index, right_index)] = value
                        tree_distance[left_index][right_index] = value
                    else:
                        subtree = forest_distance[
                            (
                                leftmost_left[left_index] - 1,
                                leftmost_right[right_index] - 1,
                            )
                        ] + tree_distance[left_index][right_index]
                        forest_distance[(left_index, right_index)] = min(
                            delete, insert, subtree
                        )
    return tree_distance[left_size][right_size]


def _ast_value(node: OrderedAstNode) -> list[Any]:
    return [node.label, [_ast_value(child) for child in node.children]]


def _ast_size(node: OrderedAstNode) -> int:
    return 1 + sum(_ast_size(child) for child in node.children)


def tree_similarity(
    reference: OrderedAstNode, candidate: OrderedAstNode
) -> TreeSimilarity:
    """Compute the pinned no-move TRS adaptation and paper normalization."""

    reference_value = _ast_value(reference)
    candidate_value = _ast_value(candidate)
    reference_size = _ast_size(reference)
    candidate_size = _ast_size(candidate)
    if reference_value == candidate_value:
        distance = 0
    else:
        distance = ordered_tree_edit_distance(reference, candidate)
    denominator = reference_size + candidate_size
    if denominator <= 0:  # pragma: no cover - every tree has a root
        raise RubyScoringError("ruby_trs_empty_tree")
    score = 1.0 - distance / denominator
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        raise RubyScoringError("ruby_trs_score_out_of_range")
    return TreeSimilarity(
        score=score,
        distance=distance,
        reference_node_count=reference_size,
        candidate_node_count=candidate_size,
        reference_representation_sha256=_sha256_json(reference_value),
        candidate_representation_sha256=_sha256_json(candidate_value),
    )


def ruby_similarity(
    reference_source: str | bytes,
    candidate_source: str | bytes,
    target_declaration: str | bytes,
) -> RubyScore:
    """Compute all reproducible tiers and select the highest available one."""

    try:
        reference_analysis = analyze_java_method(reference_source, target_declaration)
        candidate_analysis = analyze_java_method(candidate_source, target_declaration)
    except JavaValidationError as exc:
        raise RubyScoringError(exc.code) from exc
    if not reference_analysis.structurally_valid:
        raise RubyScoringError("ruby_reference_not_structurally_valid")

    sts = string_similarity(
        reference_analysis.lex.tokens, candidate_analysis.lex.tokens
    )
    grs = {
        "definition": GRS_DEFINITION,
        "available": False,
        "score": None,
        "reason": GRS_UNAVAILABLE_REASON,
    }
    reasons = [GRS_UNAVAILABLE_REASON]

    reference_ast = ordered_java_method_ast(
        reference_source,
        target_declaration,
        analysis=reference_analysis,
    )
    candidate_unavailability = _ast_unavailability(candidate_analysis, "candidate")
    if not candidate_unavailability:
        candidate_ast = ordered_java_method_ast(
            candidate_source,
            target_declaration,
            analysis=candidate_analysis,
        )
        trs_score = tree_similarity(reference_ast, candidate_ast)
        trs = trs_score.as_dict()
        reasons.append("trs_adaptation_selected_both_asts_available")
        selected_tier = "trs_adaptation"
        score = trs_score.score
    else:
        reference_value = _ast_value(reference_ast)
        trs = {
            "definition": TRS_DEFINITION,
            "available": False,
            "score": None,
            "distance": None,
            "reference_node_count": _ast_size(reference_ast),
            "candidate_node_count": None,
            "reference_representation_sha256": _sha256_json(reference_value),
            "candidate_representation_sha256": None,
            "unavailability_reasons": list(candidate_unavailability),
        }
        reasons.extend(candidate_unavailability)
        reasons.append("sts_selected_trs_adaptation_unavailable")
        selected_tier = "sts"
        score = sts.score

    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        raise RubyScoringError("ruby_composite_score_out_of_range")
    return RubyScore(
        score=score,
        selected_tier=selected_tier,
        selection_reasons=tuple(reasons),
        grs=grs,
        trs_adaptation=trs,
        sts=sts.as_dict(),
    )


def ruby_fidelity_artifact(
    *,
    case: StudyCase,
    run_index: int,
    freeze_manifest_sha256: str,
    candidate_code: str,
) -> dict[str, Any]:
    """Build the exact source-free per-run RUBY success artifact."""

    if not isinstance(run_index, int) or isinstance(run_index, bool) or run_index < 0:
        raise RubyScoringError("ruby_run_index_invalid")
    if (
        not isinstance(freeze_manifest_sha256, str)
        or len(freeze_manifest_sha256) != _SHA256_LENGTH
    ):
        raise RubyScoringError("ruby_freeze_hash_invalid")
    try:
        int(freeze_manifest_sha256, 16)
    except ValueError as exc:
        raise RubyScoringError("ruby_freeze_hash_invalid") from exc
    if not isinstance(candidate_code, str):
        raise RubyScoringError("ruby_candidate_code_not_string")
    candidate_hash = hashlib.sha256(candidate_code.encode("utf-8")).hexdigest()
    result = ruby_similarity(case.code_1, candidate_code, case.target_declaration)
    return {
        "schema_version": RUBY_SCHEMA,
        "definition": RUBY_DEFINITION,
        "method_id": case.method_id,
        "run_index": run_index,
        "freeze_manifest_sha256": freeze_manifest_sha256,
        "code_1_sha256": case.code_1_sha256,
        "code_2_sha256": candidate_hash,
        "score": result.score,
        "selected_tier": result.selected_tier,
        "tiers": result.tiers_dict(),
        "selection_reasons": list(result.selection_reasons),
    }


def ruby_failure_artifact(
    *,
    case: StudyCase,
    run_index: int,
    freeze_manifest_sha256: str,
    code_2_sha256: str,
    failure_code: str,
) -> dict[str, Any]:
    """Build the exact identity-bound per-run RUBY failure artifact."""

    if not isinstance(failure_code, str) or not failure_code:
        raise RubyScoringError("ruby_failure_code_invalid")
    if not isinstance(code_2_sha256, str) or len(code_2_sha256) != _SHA256_LENGTH:
        raise RubyScoringError("ruby_code_2_hash_invalid")
    try:
        int(code_2_sha256, 16)
    except ValueError as exc:
        raise RubyScoringError("ruby_code_2_hash_invalid") from exc
    # Reuse success-artifact identity validation without scoring source code.
    if not isinstance(run_index, int) or isinstance(run_index, bool) or run_index < 0:
        raise RubyScoringError("ruby_run_index_invalid")
    if (
        not isinstance(freeze_manifest_sha256, str)
        or len(freeze_manifest_sha256) != _SHA256_LENGTH
    ):
        raise RubyScoringError("ruby_freeze_hash_invalid")
    try:
        int(freeze_manifest_sha256, 16)
    except ValueError as exc:
        raise RubyScoringError("ruby_freeze_hash_invalid") from exc
    return {
        "schema_version": RUBY_FAILURE_SCHEMA,
        "definition": RUBY_DEFINITION,
        "method_id": case.method_id,
        "run_index": run_index,
        "freeze_manifest_sha256": freeze_manifest_sha256,
        "code_1_sha256": case.code_1_sha256,
        "code_2_sha256": code_2_sha256,
        "failure_code": failure_code,
    }


def score_generated_run_ruby(
    *, case: StudyCase, run_directory: Path, freeze_manifest_sha256: str
) -> dict[str, Any]:
    """Validate one generated result and publish RUBY exactly once."""

    try:
        status = read_json_object(run_directory / "status.json")
        regeneration = read_json_object(run_directory / "regeneration.result.json")
    except ArtifactError as exc:
        raise RubyScoringError(exc.code) from exc
    run_index = status.get("run_index")
    if (
        status.get("status") != "generated"
        or status.get("method_id") != case.method_id
        or status.get("protocol_hash") != freeze_manifest_sha256
        or not isinstance(run_index, int)
        or isinstance(run_index, bool)
    ):
        raise RubyScoringError("ruby_run_status_invalid")
    if (
        regeneration.get("method_id") != case.method_id
        or regeneration.get("run_index") != run_index
    ):
        raise RubyScoringError("ruby_regeneration_identity_invalid")
    try:
        regenerated = validate_regenerated_code(regeneration.get("output"))
    except ValueError as exc:
        raise RubyScoringError("ruby_regeneration_output_invalid") from exc
    code_2_sha256 = hashlib.sha256(regenerated.code.encode("utf-8")).hexdigest()
    if regeneration.get("code_2_sha256") != code_2_sha256:
        raise RubyScoringError("ruby_regeneration_code_hash_mismatch")

    artifact = ruby_fidelity_artifact(
        case=case,
        run_index=run_index,
        freeze_manifest_sha256=freeze_manifest_sha256,
        candidate_code=regenerated.code,
    )
    try:
        write_json_once(run_directory / RUBY_ARTIFACT, artifact)
    except ArtifactError as exc:
        raise RubyScoringError(exc.code) from exc
    return artifact
