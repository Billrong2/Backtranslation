from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtranslation.artifacts import ArtifactError, read_json_object, write_json_once
from backtranslation.cases import load_study_cases
from backtranslation.provider import ProviderConfig, ProviderResult
from backtranslation.roundtrip import RoundTripError, execute_case_run


PROJECT = Path(__file__).resolve().parents[1]


def test_write_json_once_never_replaces(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    write_json_once(path, {"b": 2, "a": 1})
    assert path.read_bytes() == b'{"a":1,"b":2}\n'
    with pytest.raises(ArtifactError, match="artifact_already_exists"):
        write_json_once(path, {"a": 9})
    assert read_json_object(path) == {"a": 1, "b": 2}


class FakeSender:
    def __init__(self, code_2: str) -> None:
        self.code_2 = code_2
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> ProviderResult:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            value = {
                "schema_version": "implementation-directions-v1",
                "directions": [
                    {
                        "id": "D01",
                        "action": "Return the configured store.",
                        "conditions": [],
                        "depends_on": [],
                    }
                ],
            }
        else:
            value = {
                "schema_version": "regenerated-code-v1",
                "language": "java",
                "code": self.code_2,
            }
        return ProviderResult(
            content=json.dumps(value),
            event={"schema_version": "fake.provider.v1", "call": len(self.calls)},
        )


def test_roundtrip_uses_two_fresh_isolated_requests(tmp_path: Path) -> None:
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    fake = FakeSender(case.target_declaration + " { return null; }")
    status = execute_case_run(
        case=case,
        run_index=0,
        protocol_hash="a" * 64,
        project_directory=PROJECT,
        artifact_root=tmp_path / "runs",
        credential_path=tmp_path / "unused-key",
        sender=fake,
        provider_config=ProviderConfig(max_tokens=128),
    )
    assert status["status"] == "generated"
    assert len(fake.calls) == 2
    assert fake.calls[0]["system_prompt"] != fake.calls[1]["system_prompt"]
    regeneration_prompt = fake.calls[1]["user_prompt"]
    assert case.code_1 not in regeneration_prompt
    assert case.code_1_sha256 not in regeneration_prompt
    run_dir = tmp_path / "runs" / ("a" * 64) / "run-0" / case.method_id
    assert (run_dir / "extraction.claim.json").is_file()
    extraction_claim = read_json_object(run_dir / "extraction.claim.json")
    assert extraction_claim["output_schema_path"] == "directions-v1.schema.json"
    assert len(extraction_claim["output_schema_sha256"]) == 64
    assert extraction_claim["requested_settings"] == {
        "model": "deepseek-v4-pro",
        "thinking": "enabled",
        "reasoning_effort": "high",
        "max_tokens": 128,
        "response_format": "json_object",
        "stream": False,
        "temperature": "omitted",
        "top_p": "omitted",
        "seed": "omitted",
    }
    assert read_json_object(run_dir / "extraction.provider.json")["provider_event"] == {
        "schema_version": "fake.provider.v1",
        "call": 1,
    }
    assert json.loads((run_dir / "extraction.output.txt").read_text())[
        "schema_version"
    ] == "implementation-directions-v1"
    assert (run_dir / "regeneration.claim.json").is_file()
    assert read_json_object(run_dir / "regeneration.provider.json")["provider_event"] == {
        "schema_version": "fake.provider.v1",
        "call": 2,
    }
    assert json.loads((run_dir / "regeneration.output.txt").read_text())[
        "schema_version"
    ] == "regenerated-code-v1"
    regeneration = read_json_object(run_dir / "regeneration.result.json")
    assert regeneration["java_validation"]["structurally_valid"] is True
    assert read_json_object(run_dir / "status.json")["status"] == "generated"
    with pytest.raises(RoundTripError, match="run_already_claimed"):
        execute_case_run(
            case=case,
            run_index=0,
            protocol_hash="a" * 64,
            project_directory=PROJECT,
            artifact_root=tmp_path / "runs",
            credential_path=tmp_path / "unused-key",
            sender=fake,
        )
    assert len(fake.calls) == 2


class InvalidSchemaSender:
    def __call__(self, **kwargs) -> ProviderResult:
        return ProviderResult(
            content=json.dumps({"schema_version": "wrong", "directions": []}),
            event={"schema_version": "fake.provider.v1", "usage": {"total_tokens": 9}},
        )


class MalformedJsonSender:
    def __call__(self, **kwargs) -> ProviderResult:
        return ProviderResult(
            content="{malformed task output",
            event={"schema_version": "fake.provider.v1", "usage": {"total_tokens": 4}},
        )


def test_malformed_provider_task_output_is_retained_before_parse_failure(
    tmp_path: Path,
) -> None:
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    status = execute_case_run(
        case=case,
        run_index=0,
        protocol_hash="d" * 64,
        project_directory=PROJECT,
        artifact_root=tmp_path / "runs",
        credential_path=tmp_path / "unused-key",
        sender=MalformedJsonSender(),
        provider_config=ProviderConfig(max_tokens=128),
    )
    assert status["status"] == "failed"
    assert status["stage"] == "extraction_parse"
    run_dir = tmp_path / "runs" / ("d" * 64) / "run-0" / case.method_id
    assert (run_dir / "extraction.output.txt").read_bytes() == b"{malformed task output"
    assert (run_dir / "extraction.provider.json").is_file()
    assert not (run_dir / "extraction.result.json").exists()


def test_successful_provider_event_survives_task_schema_failure(tmp_path: Path) -> None:
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    status = execute_case_run(
        case=case,
        run_index=0,
        protocol_hash="c" * 64,
        project_directory=PROJECT,
        artifact_root=tmp_path / "runs",
        credential_path=tmp_path / "unused-key",
        sender=InvalidSchemaSender(),
        provider_config=ProviderConfig(max_tokens=128),
    )
    assert status["status"] == "failed"
    assert status["stage"] == "extraction_schema"
    run_dir = tmp_path / "runs" / ("c" * 64) / "run-0" / case.method_id
    event = read_json_object(run_dir / "extraction.provider.json")
    assert event["provider_event"]["usage"]["total_tokens"] == 9
    assert json.loads((run_dir / "extraction.output.txt").read_text()) == {
        "schema_version": "wrong",
        "directions": [],
    }
