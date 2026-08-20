from __future__ import annotations

import hashlib
import json
import shutil
import threading
from pathlib import Path

import pytest

import backtranslation.quota_execution as quota_execution

from backtranslation.artifacts import read_json_object, write_json_once
from backtranslation.cases import load_study_cases
from backtranslation.provider import ProviderResult
from backtranslation.java_validation import JavaValidationError
from backtranslation.quota import document_sha256, validate_selected_attempt
from backtranslation.quota_execution import (
    evaluate_attempt,
    eligibility_path,
    execute_native_attempt,
    materialize_selected_cell,
    native_attempt_directory,
    quota_root,
    quota_status,
    selection_path,
    verify_selected_cell,
)
from backtranslation.quota_execution import QuotaExecutionError, _write_selection


PROJECT = Path(__file__).resolve().parents[1]
PROTOCOL = hashlib.sha256(b"quota-v0.6-test").hexdigest()


def _guard() -> None:
    return None


def _test_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for name in ("prompts", "schemas", "config"):
        shutil.copytree(PROJECT / name, project / name)
    return project


class ProductionShapeSender:
    """Synthetic sender with production-shaped, sanitized provider evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    def __call__(self, **kwargs) -> ProviderResult:
        self.calls += 1
        if self.calls % 2:
            value = {
                "schema_version": "implementation-directions-v1",
                "directions": [
                    {
                        "id": "D01",
                        "action": "Reproduce the target implementation.",
                        "conditions": [],
                        "depends_on": [],
                    }
                ],
            }
        else:
            value = {
                "schema_version": "regenerated-code-v1",
                "language": "java",
                "code": self.code,
            }
        content = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        config = kwargs["config"]
        response_bytes = content.encode("utf-8")
        request_body = json.dumps(
            {
                "model": config.model,
                "messages": [
                    {"role": "system", "content": kwargs["system_prompt"]},
                    {"role": "user", "content": kwargs["user_prompt"]},
                ],
                "response_format": {"type": config.response_format},
                "thinking": {"type": config.thinking},
                "reasoning_effort": config.reasoning_effort,
                "max_tokens": config.max_tokens,
                "stream": False,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        event = {
            "schema_version": "backtranslation.provider_event.v1",
            "provider": "deepseek",
            "protocol": "openai_chat_completions",
            "credential": {
                "mode": "0600",
                "owner_uid_matches_process": True,
                "regular_file": True,
                "symlink": False,
                "hard_link_count": 1,
            },
            "elapsed_milliseconds": 1,
            "request": {
                "endpoint": config.endpoint,
                "host": config.host,
                "max_tokens": config.max_tokens,
                "model": config.model,
                "reasoning_effort": config.reasoning_effort,
                "request_body_sha256": hashlib.sha256(request_body).hexdigest(),
                "request_body_utf8_bytes": len(request_body),
                "response_format": config.response_format,
                "stream": False,
                "system_prompt_sha256": hashlib.sha256(
                    kwargs["system_prompt"].encode("utf-8")
                ).hexdigest(),
                "system_prompt_utf8_bytes": len(kwargs["system_prompt"].encode("utf-8")),
                "thinking": config.thinking,
                "user_prompt_sha256": hashlib.sha256(
                    kwargs["user_prompt"].encode("utf-8")
                ).hexdigest(),
                "user_prompt_utf8_bytes": len(kwargs["user_prompt"].encode("utf-8")),
            },
            "response": {
                "content_sha256": hashlib.sha256(response_bytes).hexdigest(),
                "content_utf8_bytes": len(response_bytes),
                "finish_reason": "stop",
                "reasoning_content_retained": False,
                "reasoning_content_utf8_bytes": 0,
                "response_id": f"synthetic-{self.calls}",
                "returned_model": config.model,
                "system_fingerprint": "synthetic",
                "usage": {
                    "completion_tokens": 1,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 1,
                    "prompt_tokens": 1,
                    "total_tokens": 2,
                },
            },
        }
        return ProviderResult(content=content, event=event)


def test_native_attempt_is_whole_pair_and_eligible_only_after_java_validation(
    tmp_path: Path,
) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    sender = ProductionShapeSender(case.code_1)
    status = execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused-synthetic-key",
        predecessor_eligibility_sha256="b" * 64,
        sender=sender,
        dispatch_guard=_guard,
    )
    assert status["status"] == "eligible"
    assert sender.calls == 2
    eligibility = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    assert eligibility["eligible"] is True
    assert eligibility["checks"]["java_structurally_valid"] is True


def test_raw_pair_remains_eligible_when_local_results_and_status_are_missing(
    tmp_path: Path,
) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    for name in ("extraction.result.json", "regeneration.result.json", "status.json"):
        (directory / name).unlink()
    eligibility = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    assert eligibility["eligible"] is True
    assert eligibility["checks"]["terminal_success"] is True


def test_first_valid_selection_and_flat_view_preserve_raw_origin(tmp_path: Path) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused-synthetic-key",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    accepted = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    rejected = json.loads(json.dumps(accepted))
    rejected["attempt_index"] = 1
    rejected["eligible"] = False
    for key in rejected["checks"]:
        rejected["checks"][key] = False
    for key in ("cell_identity_valid", "artifact_hashes_valid", "request_reconstruction_valid"):
        rejected["checks"][key] = True
    rejected["java_validation"] = {
        "performed": False,
        "analyzer_id": None,
        "analyzer_version": None,
        "validation_policy_sha256": None,
        "artifact_path": None,
        "artifact_sha256": None,
        "structurally_valid": False,
    }
    rejected["rejection_codes"] = [
        f"check_failed_{name}"
        for name in rejected["predicate"]["selection_inputs"]
        if not rejected["checks"][name]
    ]
    rejected["failure"] = {
        "primary_check": "provider_extraction_completed",
        "stage": "extraction_provider",
        "failure_class": "provider",
        "code": "failed_provider_extraction_completed",
        "retryable": True,
        "disposition": "retry_whole_roundtrip",
        "source_terminal_stage": None,
        "source_terminal_class": None,
        "source_terminal_code": None,
    }
    selection = _write_selection(
        root=root,
        protocol_sha256=PROTOCOL,
        run=0,
        method_id=case.method_id,
        attempts=[rejected, accepted],
    )
    assert validate_selected_attempt(selection)["selected_attempt_index"] == 2
    binding = materialize_selected_cell(
        project_directory=project,
        root=root,
        protocol_sha256=PROTOCOL,
        case=case,
        run=0,
    )
    view = root / "selected-view" / "run-0" / case.method_id
    assert read_json_object(view / "run.claim.json")["protocol_hash"] == PROTOCOL
    assert (view / "regeneration.output.txt").read_bytes() == (
        directory / "regeneration.output.txt"
    ).read_bytes()
    assert binding["derived_view_not_provider_execution"] is True
    assert binding["selection_sha256"] == document_sha256(selection)
    assert binding["selected_origin"]["attempt_path"].endswith("attempt-0002")
    assert selection_path(root, 0, case.method_id).is_file()


def _legacy_selected_fixture(tmp_path: Path):
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    native = project / "scratch" / "attempt-0002"
    execute_native_attempt(
        project_directory=project,
        source_directory=native,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused-synthetic-key",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    legacy_protocol = hashlib.sha256(b"legacy-v0.5-test").hexdigest()
    legacy_root = project / "artifacts" / "runs" / legacy_protocol
    legacy = legacy_root / "run-0" / case.method_id
    shutil.copytree(native, legacy)
    extraction_result = read_json_object(legacy / "extraction.result.json")
    extraction_result["schema_version"] = "backtranslation.extraction_result.v1"
    extraction_result["complexity_features"] = {
        "atomic_instruction_count": 999,
        "direction_word_count": 999,
        "condition_count": 999,
        "dependency_edge_count": 999,
    }
    (legacy / "extraction.result.json").unlink()
    write_json_once(legacy / "extraction.result.json", extraction_result)
    (legacy / "attempt.claim.json").unlink()
    (legacy / "status.json").unlink()
    (legacy / "run.claim.json").write_text(
        json.dumps(
            {
                "schema_version": "backtranslation.run_claim.v1",
                "method_id": case.method_id,
                "run_index": 0,
                "protocol_hash": legacy_protocol,
                "claimed_at_utc": "2026-08-12T00:00:00.000Z",
                "code_1_sha256": case.code_1_sha256,
                "type_context_sha256": case.type_context_sha256,
                "schedule_ordinal": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (legacy / "status.json").write_text(
        json.dumps(
            {
                "schema_version": "backtranslation.run_status.v1",
                "status": "generated",
                "stage": "generation_complete",
                "method_id": case.method_id,
                "run_index": 0,
                "protocol_hash": legacy_protocol,
                "finished_at_utc": "2026-08-12T00:00:01.000Z",
                "elapsed_milliseconds": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    eligibility = evaluate_attempt(
        project_directory=project,
        source_directory=legacy,
        source_root=legacy_root,
        case=case,
        run_index=0,
        attempt_index=1,
        source_kind="legacy-v0.5",
        origin_protocol_sha256=legacy_protocol,
    )
    assert eligibility["eligible"] is True
    write_json_once(eligibility_path(root, 0, case.method_id, 1), eligibility)
    _write_selection(
        root=root,
        protocol_sha256=PROTOCOL,
        run=0,
        method_id=case.method_id,
        attempts=[eligibility],
    )
    return project, root, legacy, case


def test_selected_verifier_detects_raw_attempt_tamper(tmp_path: Path) -> None:
    project, root, legacy, case = _legacy_selected_fixture(tmp_path)
    verify_selected_cell(
        project_directory=project,
        root=root,
        protocol_sha256=PROTOCOL,
        case=case,
        run=0,
    )
    (legacy / "extraction.output.txt").write_bytes(b"{}")
    try:
        verify_selected_cell(
            project_directory=project,
            root=root,
            protocol_sha256=PROTOCOL,
            case=case,
            run=0,
        )
    except Exception as exc:
        assert "snapshot" in str(exc)
    else:  # pragma: no cover - security assertion
        raise AssertionError("raw attempt tamper was accepted")


def test_selected_verifier_rejects_post_selection_attempt_directory(tmp_path: Path) -> None:
    project, root, _legacy, case = _legacy_selected_fixture(tmp_path)
    extra = native_attempt_directory(root, 0, case.method_id, 2)
    extra.mkdir(parents=True)
    try:
        verify_selected_cell(
            project_directory=project,
            root=root,
            protocol_sha256=PROTOCOL,
            case=case,
            run=0,
        )
    except Exception as exc:
        assert "attempt_set_after_selection" in str(exc)
    else:  # pragma: no cover - security assertion
        raise AssertionError("post-selection attempt directory was accepted")


def test_legacy_stage_claim_only_provider_failure_is_retryable(tmp_path: Path) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    legacy_protocol = hashlib.sha256(b"legacy-stage-failure").hexdigest()
    root = project / "artifacts" / "runs" / legacy_protocol
    directory = root / "run-0" / case.method_id
    sender = ProductionShapeSender(case.code_1)
    scratch = project / "scratch-attempt"
    execute_native_attempt(
        project_directory=project,
        source_directory=scratch,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=sender,
        dispatch_guard=_guard,
    )
    directory.mkdir(parents=True)
    shutil.copy2(scratch / "extraction.claim.json", directory / "extraction.claim.json")
    write_json_once(
        directory / "run.claim.json",
        {
            "schema_version": "backtranslation.run_claim.v1",
            "method_id": case.method_id,
            "run_index": 0,
            "protocol_hash": legacy_protocol,
            "claimed_at_utc": "2026-08-12T00:00:00.000Z",
            "code_1_sha256": case.code_1_sha256,
            "type_context_sha256": case.type_context_sha256,
            "schedule_ordinal": 1,
        },
    )
    write_json_once(
        directory / "status.json",
        {
            "schema_version": "backtranslation.run_status.v1",
            "status": "failed",
            "stage": "extraction_api",
            "failure_class": "provider",
            "failure_code": "provider_finish_reason_not_stop",
            "method_id": case.method_id,
            "run_index": 0,
            "finished_at_utc": "2026-08-12T00:00:01.000Z",
            "elapsed_milliseconds": 1,
        },
    )
    eligibility = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root,
        case=case,
        run_index=0,
        attempt_index=1,
        source_kind="legacy-v0.5",
        origin_protocol_sha256=legacy_protocol,
    )
    assert eligibility["failure"] == {
        "primary_check": "provider_extraction_completed",
        "stage": "extraction_provider",
        "failure_class": "provider",
        "code": "failed_provider_extraction_completed",
        "retryable": True,
        "disposition": "retry_whole_roundtrip",
        "source_terminal_stage": None,
        "source_terminal_class": None,
        "source_terminal_code": None,
    }


def test_wrong_provider_request_body_binding_is_provenance_block(tmp_path: Path) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    provider = read_json_object(directory / "extraction.provider.json")
    provider["provider_event"]["request"]["request_body_sha256"] = "0" * 64
    (directory / "extraction.provider.json").unlink()
    write_json_once(directory / "extraction.provider.json", provider)
    eligibility = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    assert eligibility["failure"]["failure_class"] == "provenance"
    assert eligibility["failure"]["disposition"] == "block_study"


@pytest.mark.parametrize(
    "last_retained",
    (
        "attempt.claim.json",
        "extraction.claim.json",
        "extraction.provider.json",
        "extraction.output.txt",
        "extraction.result.json",
        "regeneration.claim.json",
        "regeneration.provider.json",
    ),
)
def test_every_incomplete_raw_write_prefix_is_retryable(
    tmp_path: Path, last_retained: str
) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    write_order = (
        "attempt.claim.json",
        "extraction.claim.json",
        "extraction.provider.json",
        "extraction.output.txt",
        "extraction.result.json",
        "regeneration.claim.json",
        "regeneration.provider.json",
        "regeneration.output.txt",
        "regeneration.result.json",
        "status.json",
    )
    cutoff = write_order.index(last_retained)
    for name in write_order[cutoff + 1 :]:
        (directory / name).unlink(missing_ok=True)
    eligibility = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    assert eligibility["eligible"] is False
    assert eligibility["failure"]["retryable"] is True
    assert eligibility["failure"]["disposition"] == "retry_whole_roundtrip"
    assert eligibility["checks"]["artifact_hashes_valid"] is True
    assert eligibility["checks"]["request_reconstruction_valid"] is True


def test_provider_event_without_output_is_retryable_but_output_without_event_blocks(
    tmp_path: Path,
) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    for name in (
        "extraction.output.txt", "extraction.result.json", "regeneration.claim.json",
        "regeneration.provider.json", "regeneration.output.txt",
        "regeneration.result.json", "status.json",
    ):
        (directory / name).unlink(missing_ok=True)
    provider_only = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    assert provider_only["failure"]["retryable"] is True
    assert provider_only["checks"]["artifact_hashes_valid"] is True

    (directory / "extraction.provider.json").rename(directory / "held-provider.json")
    (directory / "extraction.output.txt").write_text("{}", encoding="utf-8")
    (directory / "held-provider.json").unlink()
    with pytest.raises(QuotaExecutionError, match="stage_prefix_invalid"):
        evaluate_attempt(
            project_directory=project,
            source_directory=directory,
            source_root=root / "attempts",
            case=case,
            run_index=0,
            attempt_index=2,
            source_kind="v0.6-retry",
            origin_protocol_sha256=PROTOCOL,
            expected_predecessor_sha256="b" * 64,
        )


def test_unknown_or_outcome_named_raw_artifact_is_rejected(tmp_path: Path) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    (directory / "ruby-score.json").write_text("{}", encoding="utf-8")
    with pytest.raises(QuotaExecutionError, match="artifact_not_allowed"):
        evaluate_attempt(
            project_directory=project,
            source_directory=directory,
            source_root=root / "attempts",
            case=case,
            run_index=0,
            attempt_index=2,
            source_kind="v0.6-retry",
            origin_protocol_sha256=PROTOCOL,
            expected_predecessor_sha256="b" * 64,
        )


def test_materialization_never_computes_direction_complexity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root, _legacy, case = _legacy_selected_fixture(tmp_path)
    import backtranslation.directions as directions

    monkeypatch.setattr(
        directions,
        "complexity_features",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("complexity called")),
    )
    binding = materialize_selected_cell(
        project_directory=project,
        root=root,
        protocol_sha256=PROTOCOL,
        case=case,
        run=0,
    )
    result = read_json_object(root / "selected-view" / "run-0" / case.method_id / "extraction.result.json")
    assert "complexity_features" not in result
    assert binding["derived_view_not_provider_execution"] is True


def test_quota_status_validates_existing_selection(tmp_path: Path) -> None:
    project, root, _legacy, case = _legacy_selected_fixture(tmp_path)
    selection_file = selection_path(root, 0, case.method_id)
    selection = read_json_object(selection_file)
    selection["protocol_sha256"] = "0" * 64
    selection_file.unlink()
    write_json_once(selection_file, selection)
    with pytest.raises(Exception):
        quota_status(root=root, cases=load_study_cases(PROJECT / "data" / "tse"))


def test_symlinked_runtime_ancestor_is_rejected(tmp_path: Path) -> None:
    project = _test_project(tmp_path)
    real_prompts = tmp_path / "real-prompts"
    (project / "prompts").rename(real_prompts)
    (project / "prompts").symlink_to(real_prompts, target_is_directory=True)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    directory = project / "artifacts" / "runs" / PROTOCOL / "attempts" / "run-0" / case.method_id / "attempt-0002"
    with pytest.raises(QuotaExecutionError, match="ancestor_symlink"):
        execute_native_attempt(
            project_directory=project,
            source_directory=directory,
            case=case,
            run_index=0,
            attempt_index=2,
            protocol_sha256=PROTOCOL,
            credential_path=project / "unused",
            predecessor_eligibility_sha256="b" * 64,
            sender=ProductionShapeSender(case.code_1),
            dispatch_guard=_guard,
        )


def test_descriptive_result_and_status_bytes_cannot_change_eligibility(
    tmp_path: Path,
) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    expected = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    for name in ("extraction.result.json", "regeneration.result.json", "status.json"):
        (directory / name).write_text('{"arbitrary":"ruby_score"}\n', encoding="utf-8")
    observed = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    assert observed == expected


def test_shared_abort_between_stages_prevents_regeneration_call(tmp_path: Path) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    stop = threading.Event()
    base = ProductionShapeSender(case.code_1)

    def sender(**kwargs):
        result = base(**kwargs)
        stop.set()
        return result

    status = execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=sender,
        abort_event=stop,
        dispatch_guard=lambda: None,
    )
    assert base.calls == 1
    assert status["stage"] == "interrupted"
    assert not (directory / "regeneration.claim.json").exists()


def test_dispatch_admission_closes_stop_race_at_sender_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    abort = quota_execution._DispatchAbort()
    sender = ProductionShapeSender(case.code_1)
    original_call_stage = quota_execution._call_stage

    def stop_after_outer_check(**kwargs):
        # Deterministically place another worker's terminal stop after the
        # caller's last Event check but before the provider sender boundary.
        abort.stop({"cell": {"run_index": 0, "method_id": "tse-002"}})
        return original_call_stage(**kwargs)

    monkeypatch.setattr(quota_execution, "_call_stage", stop_after_outer_check)
    with pytest.raises(QuotaExecutionError, match="quota_dispatch_aborted"):
        execute_native_attempt(
            project_directory=project,
            source_directory=directory,
            case=case,
            run_index=0,
            attempt_index=2,
            protocol_sha256=PROTOCOL,
            credential_path=project / "unused",
            predecessor_eligibility_sha256="b" * 64,
            sender=sender,
            abort_event=abort.event,
            dispatch_guard=_guard,
            dispatch_admission=abort,
        )
    assert sender.calls == 0
    assert (directory / "extraction.claim.json").is_file()


def test_dispatch_preflight_failure_makes_zero_provider_calls(tmp_path: Path) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    sender = ProductionShapeSender(case.code_1)

    def denied() -> None:
        raise QuotaExecutionError("quota_runtime_environment_mismatch")

    with pytest.raises(QuotaExecutionError, match="runtime_environment_mismatch"):
        execute_native_attempt(
            project_directory=project,
            source_directory=directory,
            case=case,
            run_index=0,
            attempt_index=2,
            protocol_sha256=PROTOCOL,
            credential_path=project / "unused",
            predecessor_eligibility_sha256="b" * 64,
            sender=sender,
            dispatch_guard=denied,
        )
    assert sender.calls == 0


def test_run_tree_rejects_attempt_eleven_and_selected_view_unknown_file(
    tmp_path: Path,
) -> None:
    project = _test_project(tmp_path)
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    bad_attempt = root / "attempts" / "run-0" / "tse-001" / "attempt-0011"
    bad_attempt.mkdir(parents=True)
    (bad_attempt / "attempt.claim.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(QuotaExecutionError, match="raw_attempt_entry_invalid"):
        quota_execution._audit_run_tree(root)
    shutil.rmtree(root / "attempts")
    bad_view = root / "selected-view" / "run-0" / "tse-001"
    bad_view.mkdir(parents=True)
    (bad_view / "score.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(QuotaExecutionError, match="selected_view_artifact_invalid"):
        quota_execution._audit_run_tree(root)


def test_native_claim_binds_exact_run_barrier_witness(tmp_path: Path) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)
    witness = quota_execution.run_barrier_witness_document(
        protocol_sha256=PROTOCOL, target_run_index=0
    )
    witness_hash = quota_execution.run_barrier_witness_sha256(witness)
    execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        run_barrier_witness=witness,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    valid = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
        expected_barrier_witness=witness,
        expected_barrier_witness_sha256=witness_hash,
    )
    assert valid["eligible"] is True
    claim_path = directory / "attempt.claim.json"
    claim = read_json_object(claim_path)
    claim["run_barrier_witness_sha256"] = "f" * 64
    claim_path.unlink()
    write_json_once(claim_path, claim)
    invalid = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
        expected_barrier_witness=witness,
        expected_barrier_witness_sha256=witness_hash,
    )
    assert invalid["eligible"] is False
    assert invalid["failure"]["disposition"] == "block_study"


def test_java_validator_infrastructure_exception_is_not_a_retryable_contract_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)

    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic parser infrastructure failure")

    monkeypatch.setattr(quota_execution, "analyze_java_method", broken)
    with pytest.raises(
        QuotaExecutionError, match="java_validation_infrastructure_failure"
    ):
        execute_native_attempt(
            project_directory=project,
            source_directory=directory,
            case=case,
            run_index=0,
            attempt_index=2,
            protocol_sha256=PROTOCOL,
            credential_path=project / "unused",
            predecessor_eligibility_sha256="b" * 64,
            sender=ProductionShapeSender(case.code_1),
            dispatch_guard=_guard,
        )
    assert (directory / "java-infrastructure.json").is_file()
    with pytest.raises(
        QuotaExecutionError, match="java_validation_infrastructure_failure"
    ):
        evaluate_attempt(
            project_directory=project,
            source_directory=directory,
            source_root=root / "attempts",
            case=case,
            run_index=0,
            attempt_index=2,
            source_kind="v0.6-retry",
            origin_protocol_sha256=PROTOCOL,
            expected_predecessor_sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "code",
    sorted(quota_execution._CANDIDATE_JAVA_INVALID_CODES),
)
def test_candidate_dependent_java_validation_errors_are_retryable_structural_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    project = _test_project(tmp_path)
    case = load_study_cases(PROJECT / "data" / "tse")[0]
    root = quota_root(project / "artifacts" / "runs", PROTOCOL)
    directory = native_attempt_directory(root, 0, case.method_id, 2)

    def invalid_candidate(*_args, **_kwargs):
        raise JavaValidationError(code)

    monkeypatch.setattr(quota_execution, "analyze_java_method", invalid_candidate)
    status = execute_native_attempt(
        project_directory=project,
        source_directory=directory,
        case=case,
        run_index=0,
        attempt_index=2,
        protocol_sha256=PROTOCOL,
        credential_path=project / "unused",
        predecessor_eligibility_sha256="b" * 64,
        sender=ProductionShapeSender(case.code_1),
        dispatch_guard=_guard,
    )
    assert status["stage"] == "predicate_complete"
    assert status["failure_code"] == "java_structurally_invalid"
    eligibility = evaluate_attempt(
        project_directory=project,
        source_directory=directory,
        source_root=root / "attempts",
        case=case,
        run_index=0,
        attempt_index=2,
        source_kind="v0.6-retry",
        origin_protocol_sha256=PROTOCOL,
        expected_predecessor_sha256="b" * 64,
    )
    assert eligibility["eligible"] is False
    assert eligibility["failure"]["retryable"] is True
    assert eligibility["failure"]["stage"] == "java_structure"
