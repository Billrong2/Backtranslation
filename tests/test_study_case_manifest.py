from __future__ import annotations

import json
from pathlib import Path

from backtranslation.cases import canonical_json_bytes, load_study_cases


PROJECT = Path(__file__).resolve().parents[1]


def test_materialized_study_cases_match_runtime_builder() -> None:
    cases = load_study_cases(PROJECT / "data" / "tse")
    records = [
        json.loads(line)
        for line in (PROJECT / "data" / "study_cases.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == len(cases) == 50
    for record, case in zip(records, cases, strict=True):
        assert record["schema_version"] == "backtranslation.study_case.v1"
        assert record["method_id"] == case.method_id
        assert record["code_1_sha256"] == case.code_1_sha256
        assert record["target_declaration"] == case.target_declaration
        assert record["type_context"] == case.type_context
        assert record["type_context_sha256"] == case.type_context_sha256
        assert canonical_json_bytes(record["type_context"])
