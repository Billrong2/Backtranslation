from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtranslation.cases import (
    CaseValidationError,
    assert_regeneration_isolated,
    extraction_input,
    load_study_cases,
    regeneration_input,
    render_prompt,
)


PROJECT = Path(__file__).resolve().parents[1]


def directions() -> dict:
    return {
        "schema_version": "implementation-directions-v1",
        "directions": [
            {"id": "D01", "action": "Return the input.", "conditions": [], "depends_on": []}
        ],
    }


def test_all_cases_load_without_outcomes() -> None:
    cases = load_study_cases(PROJECT / "data" / "tse")
    assert len(cases) == 50
    assert len({case.project for case in cases}) == 10
    for case in cases:
        payload = extraction_input(case)
        assert set(payload) == {"schema_version", "type_context", "code_1"}
        assert case.code_1_sha256
        assert case.target_declaration
        assert not case.target_declaration.rstrip().endswith("{")
        assert payload["type_context"]["referenced_fields"] == []


def test_regeneration_payload_is_outcome_and_code_isolated() -> None:
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    payload = regeneration_input(case, directions())
    serialized = json.dumps(payload, sort_keys=True)
    assert case.code_1 not in serialized
    assert case.code_1_sha256 not in serialized
    assert set(payload) == {"schema_version", "target_declaration", "type_context", "directions"}


def test_isolation_rejects_forbidden_key() -> None:
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    for key in ("AU", "ruby", "bleu"):
        payload = regeneration_input(case, directions())
        payload[key] = 1
        with pytest.raises(
            CaseValidationError, match="regeneration_contains_forbidden_key"
        ):
            assert_regeneration_isolated(case, payload)


def test_render_prompt_is_exact_and_canonical() -> None:
    rendered = render_prompt("before {{VALUE}} after", "VALUE", {"z": 1, "a": "é"})
    assert rendered == 'before {"a":"é","z":1} after'
