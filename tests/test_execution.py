from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from pathlib import Path

import pytest

from backtranslation.cases import load_study_cases
from backtranslation.execution import (
    ExecutionError,
    FreezeAuthorization,
    execution_status,
    execute_schedule,
    initialize_schedule,
    schedule_document,
    verify_freeze_authorization,
)
from backtranslation.artifacts import read_json_object, write_json_once
from backtranslation.freeze import build_manifest, manifest_sha256
from backtranslation.provider import ProviderConfig


PROJECT = Path(__file__).resolve().parents[1]


def _authorization() -> FreezeAuthorization:
    return FreezeAuthorization(
        manifest_sha256="a" * 64,
        manifest_relative_path="protocol/freeze-manifest-v1.json",
        frozen_at_utc="2026-08-11T12:00:00Z",
        reviewer="fixture-reviewer",
    )


def test_schedule_is_exact_run_major_3_by_50(tmp_path: Path) -> None:
    cases = load_study_cases(PROJECT / "data" / "tse")
    schedule = schedule_document(cases, _authorization())
    assert schedule["case_count"] == 50
    assert schedule["request_pair_count"] == 150
    assert schedule["maximum_concurrent_method_pairs"] == 5
    entries = schedule["entries"]
    assert [(item["run_index"], item["method_id"]) for item in entries[:2]] == [
        (0, "tse-001"),
        (0, "tse-002"),
    ]
    assert (entries[49]["run_index"], entries[49]["method_id"]) == (0, "tse-050")
    assert (entries[50]["run_index"], entries[50]["method_id"]) == (1, "tse-001")
    path = initialize_schedule(
        artifact_root=tmp_path / "runs", cases=cases, authorization=_authorization()
    )
    assert initialize_schedule(
        artifact_root=tmp_path / "runs", cases=cases, authorization=_authorization()
    ) == path


def test_freeze_authorization_binds_current_manifest_bytes(tmp_path: Path) -> None:
    (tmp_path / "protocol").mkdir()
    (tmp_path / "stable.txt").write_text("stable\n", encoding="utf-8")
    manifest = build_manifest(tmp_path, ["stable.txt"])
    manifest_path = tmp_path / "protocol" / "freeze-manifest-v1.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    digest = manifest_sha256(manifest)
    record_path = tmp_path / "protocol" / "freeze-record.jsonl"
    record = {
        "schema_version": "backtranslation.freeze-record.v1",
        "frozen_at_utc": "2026-08-11T12:00:00Z",
        "manifest_path": "protocol/freeze-manifest-v1.json",
        "manifest_sha256": digest,
        "reviewer": "fixture-reviewer",
    }
    record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    authorization = verify_freeze_authorization(
        project_directory=tmp_path,
        manifest_path=manifest_path,
        freeze_record_path=record_path,
    )
    assert authorization.manifest_sha256 == digest
    (tmp_path / "stable.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ExecutionError, match="freeze_manifest_verification_failed"):
        verify_freeze_authorization(
            project_directory=tmp_path,
            manifest_path=manifest_path,
            freeze_record_path=record_path,
        )


def test_unmatched_freeze_record_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "protocol").mkdir()
    (tmp_path / "stable.txt").write_text("stable\n", encoding="utf-8")
    manifest = build_manifest(tmp_path, ["stable.txt"])
    manifest_path = tmp_path / "protocol" / "freeze-manifest-v1.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    record_path = tmp_path / "protocol" / "freeze-record.jsonl"
    record_path.write_text(
        json.dumps(
            {
                "schema_version": "backtranslation.freeze-record.v1",
                "frozen_at_utc": "2026-08-11T12:00:00Z",
                "manifest_path": "protocol/freeze-manifest-v1.json",
                "manifest_sha256": "b" * 64,
                "reviewer": "fixture-reviewer",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ExecutionError, match="freeze_manifest_not_authorized"):
        verify_freeze_authorization(
            project_directory=tmp_path,
            manifest_path=manifest_path,
            freeze_record_path=record_path,
        )


def test_status_treats_claim_without_terminal_as_nonretryable(tmp_path: Path) -> None:
    cases = load_study_cases(PROJECT / "data" / "tse")
    auth = _authorization()
    claimed = tmp_path / auth.manifest_sha256 / "run-0" / "tse-001"
    claimed.mkdir(parents=True)
    (claimed / "run.claim.json").write_text("{}\n", encoding="utf-8")
    failed = tmp_path / auth.manifest_sha256 / "run-0" / "tse-002"
    failed.mkdir(parents=True)
    (failed / "run.claim.json").write_text("{}\n", encoding="utf-8")
    (failed / "status.json").write_text('{"status":"failed"}\n', encoding="utf-8")
    status = execution_status(artifact_root=tmp_path, authorization=auth, cases=cases)
    assert status["totals"] == {
        "unclaimed": 148,
        "claimed_no_status": 1,
        "generated": 0,
        "failed": 1,
    }


def test_real_scheduler_claims_in_order_limits_outstanding_and_has_run_barriers(
    tmp_path: Path, monkeypatch
) -> None:
    import backtranslation.execution as execution

    cases = load_study_cases(PROJECT / "data" / "tse")
    auth = _authorization()
    credential = tmp_path / "credential"
    credential.write_text("fixture-synthetic-provider-credential", encoding="ascii")
    credential.chmod(0o600)
    claim_ordinals: list[int] = []
    original_claim = execution.claim_case_run

    def recording_claim(**kwargs):
        claim_ordinals.append(kwargs["schedule_ordinal"])
        return original_claim(**kwargs)

    monkeypatch.setattr(execution, "claim_case_run", recording_claim)
    outstanding = 0
    maximum_outstanding = 0
    counter_lock = threading.Lock()

    class RecordingExecutor:
        def __init__(self, *args, **kwargs):
            self.delegate = RealThreadPoolExecutor(*args, **kwargs)

        def __enter__(self):
            self.delegate.__enter__()
            return self

        def __exit__(self, *args):
            return self.delegate.__exit__(*args)

        def submit(self, function, /, *args, **kwargs):
            nonlocal outstanding, maximum_outstanding
            with counter_lock:
                outstanding += 1
                maximum_outstanding = max(maximum_outstanding, outstanding)
            try:
                future = self.delegate.submit(function, *args, **kwargs)
            except Exception:
                with counter_lock:
                    outstanding -= 1
                raise

            def finished(_future):
                nonlocal outstanding
                with counter_lock:
                    outstanding -= 1

            future.add_done_callback(finished)
            return future

    monkeypatch.setattr(execution, "ThreadPoolExecutor", RecordingExecutor)
    started_by_run = {0: 0, 1: 0, 2: 0}
    runner_lock = threading.Lock()

    def fake_runner(**kwargs):
        run = kwargs["run_index"]
        case = kwargs["case"]
        ordinal = kwargs["preclaimed_schedule_ordinal"]
        directory = (
            kwargs["artifact_root"]
            / kwargs["protocol_hash"]
            / f"run-{run}"
            / case.method_id
        )
        claim = read_json_object(directory / "run.claim.json")
        assert claim["schedule_ordinal"] == ordinal
        with runner_lock:
            if run > 0:
                assert started_by_run[run - 1] == 50
            started_by_run[run] += 1
        time.sleep(0.005)
        status = {
            "schema_version": "backtranslation.run_status.v1",
            "status": "generated",
            "method_id": case.method_id,
            "run_index": run,
            "protocol_hash": kwargs["protocol_hash"],
        }
        write_json_once(directory / "status.json", status)
        return status

    result = execute_schedule(
        project_directory=PROJECT,
        artifact_root=tmp_path / "runs",
        credential_path=credential,
        cases=cases,
        authorization=auth,
        case_runner=fake_runner,
    )
    assert result["submitted_unclaimed_pairs"] == 150
    assert result["status"]["totals"]["generated"] == 150
    assert claim_ordinals == list(range(1, 151))
    assert 1 <= maximum_outstanding <= 5
    assert started_by_run == {0: 50, 1: 50, 2: 50}


def test_production_scheduler_rejects_nonfrozen_provider_settings(tmp_path: Path) -> None:
    cases = load_study_cases(PROJECT / "data" / "tse")
    with pytest.raises(ExecutionError, match="pilot_provider_config_not_frozen"):
        execute_schedule(
            project_directory=PROJECT,
            artifact_root=tmp_path / "runs",
            credential_path=tmp_path / "credential-never-opened",
            cases=cases,
            authorization=_authorization(),
            provider_config=ProviderConfig(max_tokens=128),
        )
