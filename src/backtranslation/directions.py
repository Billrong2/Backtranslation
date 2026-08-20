"""Validation and transparent features for implementation directions.

The validator is intentionally stricter than ordinary JSON Schema validation:
it also enforces stable identifiers, topological dependency order, whitespace
normalization, and duplicate prevention.  Those constraints make the primary
instruction count and the structural features deterministic.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


DIRECTIONS_SCHEMA_VERSION = "implementation-directions-v1"
REGENERATED_CODE_SCHEMA_VERSION = "regenerated-code-v1"
MAX_DIRECTIONS = 999
MAX_ACTION_CHARS = 2_000
MAX_CONDITION_CHARS = 1_000
MAX_CONDITIONS_PER_DIRECTION = 64
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")


class SchemaValidationError(ValueError):
    """A stable-code validation failure safe to record in run manifests."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Direction:
    identifier: str
    action: str
    conditions: tuple[str, ...]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class DirectionsDocument:
    directions: tuple[Direction, ...]


@dataclass(frozen=True)
class RegeneratedCode:
    language: str
    code: str


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise SchemaValidationError(code)


def _normalized_text(value: Any, *, code: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{code}_not_string")
    if value != value.strip() or not value:
        raise SchemaValidationError(f"{code}_not_trimmed_nonempty")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SchemaValidationError(f"{code}_not_single_line")
    if len(value) > maximum:
        raise SchemaValidationError(f"{code}_too_long")
    return value


def _text_list(
    value: Any,
    *,
    code: str,
    maximum_items: int,
    maximum_chars: int,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"{code}_not_array")
    if len(value) > maximum_items:
        raise SchemaValidationError(f"{code}_too_many")
    items = tuple(
        _normalized_text(item, code=f"{code}_item", maximum=maximum_chars)
        for item in value
    )
    if len(set(items)) != len(items):
        raise SchemaValidationError(f"{code}_duplicate")
    return items


def validate_directions_document(value: Any) -> DirectionsDocument:
    """Parse a strict ``implementation-directions-v1`` JSON object."""
    if not isinstance(value, Mapping):
        raise SchemaValidationError("directions_document_not_object")
    _require_exact_keys(value, {"schema_version", "directions"}, "directions_root_keys")
    if value["schema_version"] != DIRECTIONS_SCHEMA_VERSION:
        raise SchemaValidationError("directions_schema_version")
    raw_directions = value["directions"]
    if not isinstance(raw_directions, list):
        raise SchemaValidationError("directions_not_array")
    if not raw_directions or len(raw_directions) > MAX_DIRECTIONS:
        raise SchemaValidationError("directions_count_out_of_range")

    parsed: list[Direction] = []
    prior_ids: set[str] = set()
    for position, raw in enumerate(raw_directions, start=1):
        if not isinstance(raw, Mapping):
            raise SchemaValidationError("direction_not_object")
        _require_exact_keys(
            raw,
            {"id", "action", "conditions", "depends_on"},
            "direction_keys",
        )
        expected_identifier = f"D{position:02d}"
        if raw["id"] != expected_identifier:
            raise SchemaValidationError("direction_id_not_sequential")
        action = _normalized_text(
            raw["action"], code="direction_action", maximum=MAX_ACTION_CHARS
        )
        conditions = _text_list(
            raw["conditions"],
            code="direction_conditions",
            maximum_items=MAX_CONDITIONS_PER_DIRECTION,
            maximum_chars=MAX_CONDITION_CHARS,
        )
        dependencies = _text_list(
            raw["depends_on"],
            code="direction_dependencies",
            maximum_items=MAX_DIRECTIONS,
            maximum_chars=8,
        )
        if any(dependency not in prior_ids for dependency in dependencies):
            raise SchemaValidationError("direction_dependency_not_prior")
        parsed.append(
            Direction(
                identifier=expected_identifier,
                action=action,
                conditions=conditions,
                depends_on=dependencies,
            )
        )
        prior_ids.add(expected_identifier)
    return DirectionsDocument(directions=tuple(parsed))


def validate_regenerated_code(value: Any) -> RegeneratedCode:
    """Parse a JSON wrapper containing a regenerated complete Java method."""
    if not isinstance(value, Mapping):
        raise SchemaValidationError("regenerated_document_not_object")
    _require_exact_keys(
        value, {"schema_version", "language", "code"}, "regenerated_root_keys"
    )
    if value["schema_version"] != REGENERATED_CODE_SCHEMA_VERSION:
        raise SchemaValidationError("regenerated_schema_version")
    if value["language"] != "java":
        raise SchemaValidationError("regenerated_language")
    code = value["code"]
    if not isinstance(code, str) or not code.strip():
        raise SchemaValidationError("regenerated_code_empty")
    if "\x00" in code:
        raise SchemaValidationError("regenerated_code_nul")
    if code != code.strip():
        raise SchemaValidationError("regenerated_code_not_trimmed")
    return RegeneratedCode(language="java", code=code)


def _word_count(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


def complexity_features(document: DirectionsDocument) -> dict[str, int | float]:
    """Compute the frozen, outcome-independent transparent feature family."""
    directions = document.directions
    count = len(directions)
    action_words = [_word_count(direction.action) for direction in directions]
    action_characters = [len(direction.action) for direction in directions]
    condition_words = [
        _word_count(condition)
        for direction in directions
        for condition in direction.conditions
    ]
    condition_count = len(condition_words)
    edge_count = sum(len(direction.depends_on) for direction in directions)

    depth_by_id: dict[str, int] = {}
    out_degree = {direction.identifier: 0 for direction in directions}
    for direction in directions:
        dependencies = direction.depends_on
        depth_by_id[direction.identifier] = (
            0 if not dependencies else 1 + max(depth_by_id[item] for item in dependencies)
        )
        for dependency in dependencies:
            out_degree[dependency] += 1

    possible_edges = count * (count - 1) / 2
    return {
        "instruction_count": count,
        "action_word_count_total": sum(action_words),
        "action_word_count_mean": sum(action_words) / count,
        "action_word_count_max": max(action_words),
        "action_character_count_total": sum(action_characters),
        "action_character_count_mean": sum(action_characters) / count,
        "condition_count": condition_count,
        "condition_density": condition_count / count,
        "condition_word_count_total": sum(condition_words),
        "dependency_edge_count": edge_count,
        "dependency_edge_density": edge_count / possible_edges if possible_edges else 0.0,
        "dependency_max_depth": max(depth_by_id.values()),
        "dependency_max_fan_out": max(out_degree.values()),
        "dependency_mean_fan_out": edge_count / count,
    }


def assert_finite_features(features: Mapping[str, int | float]) -> None:
    """Reject accidental NaN/infinite values before artifact serialization."""
    if any(isinstance(value, float) and not math.isfinite(value) for value in features.values()):
        raise SchemaValidationError("complexity_feature_nonfinite")
