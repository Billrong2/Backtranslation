from __future__ import annotations

import copy

import pytest

from backtranslation.directions import (
    SchemaValidationError,
    complexity_features,
    validate_directions_document,
    validate_regenerated_code,
)


def example() -> dict:
    return {
        "schema_version": "implementation-directions-v1",
        "directions": [
            {
                "id": "D01",
                "action": "Initialize the running total to zero.",
                "conditions": [],
                "depends_on": [],
            },
            {
                "id": "D02",
                "action": "Add each accepted value to the running total.",
                "conditions": ["The value satisfies the acceptance predicate."],
                "depends_on": ["D01"],
            },
        ],
    }


def test_validate_and_compute_features() -> None:
    document = validate_directions_document(example())
    features = complexity_features(document)
    assert features["instruction_count"] == 2
    assert features["condition_count"] == 1
    assert features["condition_density"] == 0.5
    assert features["dependency_edge_count"] == 1
    assert features["dependency_edge_density"] == 1.0
    assert features["dependency_max_depth"] == 1
    assert features["dependency_max_fan_out"] == 1


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda value: value.update(extra=True), "directions_root_keys"),
        (
            lambda value: value["directions"][1].update(id="D03"),
            "direction_id_not_sequential",
        ),
        (
            lambda value: value["directions"][0].update(depends_on=["D02"]),
            "direction_dependency_not_prior",
        ),
        (
            lambda value: value["directions"][0].update(action=" padded "),
            "direction_action_not_trimmed_nonempty",
        ),
    ],
)
def test_rejects_noncanonical_documents(mutation, error: str) -> None:
    value = copy.deepcopy(example())
    mutation(value)
    with pytest.raises(SchemaValidationError, match=error):
        validate_directions_document(value)


def test_regenerated_code_wrapper() -> None:
    result = validate_regenerated_code(
        {
            "schema_version": "regenerated-code-v1",
            "language": "java",
            "code": "int add(int a, int b) { return a + b; }",
        }
    )
    assert result.language == "java"
    assert "return" in result.code
