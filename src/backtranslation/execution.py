"""Freeze-gated, outcome-blind execution of the 3 x 50 pilot schedule.

This module deliberately does not import the outcome loader or statistics
code.  A production request can start only after the exact freeze manifest is
verified and an append-only authorization record binds that manifest path to
its SHA-256 digest.  Existing claims are never retried.
"""

from __future__ import annotations

import json
import fcntl
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .artifacts import ArtifactError, read_json_object, write_json_once
from .cases import StudyCase
from .freeze import FreezeError, manifest_sha256, verify_manifest
from .provider import ProviderConfig, credential_metadata
from .roundtrip import claim_case_run, execute_case_run


SCHEDULE_SCHEMA = "backtranslation.execution_schedule.v1"
FROZEN_RUNS = (0, 1, 2)
FROZEN_MAX_WORKERS = 5
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class ExecutionError(RuntimeError):
    """A stable-code execution or authorization failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FreezeAuthorization:
    manifest_sha256: str
    manifest_relative_path: str
    frozen_at_utc: str
    reviewer: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _read_object(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionError(code) from exc
    if not isinstance(value, dict):
        raise ExecutionError(code)
    return value


def _relative_regular_path(project_directory: Path, path: Path) -> str:
    try:
        project = project_directory.resolve(strict=True)
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(project)
    except (OSError, ValueError) as exc:
        raise ExecutionError("freeze_path_outside_project") from exc
    if path.is_symlink() or not path.is_file():
        raise ExecutionError("freeze_path_not_regular")
    return relative.as_posix()


def verify_freeze_authorization(
    *,
    project_directory: Path,
    manifest_path: Path,
    freeze_record_path: Path,
) -> FreezeAuthorization:
    """Verify current bytes and an append-only record authorizing those bytes."""

    relative_manifest = _relative_regular_path(project_directory, manifest_path)
    manifest = _read_object(manifest_path, "freeze_manifest_read_failed")
    try:
        digest = verify_manifest(project_directory, manifest)
    except FreezeError as exc:
        raise ExecutionError("freeze_manifest_verification_failed") from exc
    if manifest_sha256(manifest) != digest or not _SHA256.fullmatch(digest):
        raise ExecutionError("freeze_manifest_digest_invalid")

    try:
        lines = freeze_record_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ExecutionError("freeze_record_read_failed") from exc
    if not lines:
        raise ExecutionError("freeze_record_empty")
    matching: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExecutionError("freeze_record_not_jsonl") from exc
        if not isinstance(record, dict) or set(record) != {
            "schema_version",
            "frozen_at_utc",
            "manifest_path",
            "manifest_sha256",
            "reviewer",
        }:
            raise ExecutionError("freeze_record_schema_invalid")
        if record.get("schema_version") != "backtranslation.freeze-record.v1":
            raise ExecutionError("freeze_record_schema_invalid")
        timestamp = record.get("frozen_at_utc")
        reviewer = record.get("reviewer")
        if (
            not isinstance(timestamp, str)
            or not _UTC_SECOND.fullmatch(timestamp)
            or not isinstance(reviewer, str)
            or not reviewer.strip()
            or reviewer != reviewer.strip()
        ):
            raise ExecutionError("freeze_record_fields_invalid")
        if (
            record.get("manifest_path") == relative_manifest
            and record.get("manifest_sha256") == digest
        ):
            matching.append(record)
    if not matching:
        raise ExecutionError("freeze_manifest_not_authorized")
    selected = matching[-1]
    return FreezeAuthorization(
        manifest_sha256=digest,
        manifest_relative_path=relative_manifest,
        frozen_at_utc=str(selected["frozen_at_utc"]),
        reviewer=str(selected["reviewer"]),
    )


def schedule_document(
    cases: Sequence[StudyCase], authorization: FreezeAuthorization
) -> dict[str, Any]:
    """Build the exact run-major, method-ID-major production schedule."""

    if len(cases) != 50:
        raise ExecutionError("schedule_case_count_not_50")
    ordered = sorted(cases, key=lambda case: case.method_id)
    expected = [f"tse-{number:03d}" for number in range(1, 51)]
    if [case.method_id for case in ordered] != expected:
        raise ExecutionError("schedule_method_ids_invalid")
    entries = [
        {
            "ordinal": ordinal,
            "run_index": run_index,
            "method_id": case.method_id,
            "code_1_sha256": case.code_1_sha256,
            "type_context_sha256": case.type_context_sha256,
        }
        for ordinal, (run_index, case) in enumerate(
            (
                (run_index, case)
                for run_index in FROZEN_RUNS
                for case in ordered
            ),
            start=1,
        )
    ]
    return {
        "schema_version": SCHEDULE_SCHEMA,
        "freeze_manifest_sha256": authorization.manifest_sha256,
        "freeze_manifest_path": authorization.manifest_relative_path,
        "runs": list(FROZEN_RUNS),
        "case_count": len(ordered),
        "request_pair_count": len(entries),
        "ordering": "run_index_then_ascending_opaque_method_id",
        "run_barrier": True,
        "maximum_concurrent_method_pairs": FROZEN_MAX_WORKERS,
        "within_run_submission_order": "ascending_opaque_method_id",
        "within_batch_start_and_completion_order": "not_an_analysis_input",
        "within_run_retry_requests": 0,
        "entries": entries,
    }


def initialize_schedule(
    *, artifact_root: Path, cases: Sequence[StudyCase], authorization: FreezeAuthorization
) -> Path:
    directory = artifact_root / authorization.manifest_sha256
    path = directory / "schedule.json"
    expected = schedule_document(cases, authorization)
    if path.exists():
        if read_json_object(path) != expected:
            raise ExecutionError("existing_schedule_mismatch")
        return path
    try:
        write_json_once(path, expected)
    except ArtifactError as exc:
        raise ExecutionError(exc.code) from exc
    return path


def _run_directory(artifact_root: Path, digest: str, run: int, method_id: str) -> Path:
    return artifact_root / digest / f"run-{run}" / method_id


def execution_status(
    *,
    artifact_root: Path,
    authorization: FreezeAuthorization,
    cases: Sequence[StudyCase],
) -> dict[str, Any]:
    """Return source-free counts; never open generated code or directions."""

    by_run: dict[str, dict[str, int]] = {}
    totals = {"unclaimed": 0, "claimed_no_status": 0, "generated": 0, "failed": 0}
    for run in FROZEN_RUNS:
        counts = {key: 0 for key in totals}
        for case in sorted(cases, key=lambda item: item.method_id):
            directory = _run_directory(
                artifact_root, authorization.manifest_sha256, run, case.method_id
            )
            claim = directory / "run.claim.json"
            terminal = directory / "status.json"
            if not claim.exists():
                category = "unclaimed"
            elif not terminal.exists():
                category = "claimed_no_status"
            else:
                status = read_json_object(terminal).get("status")
                category = "generated" if status == "generated" else "failed"
            counts[category] += 1
            totals[category] += 1
        by_run[str(run)] = counts
    return {
        "schema_version": "backtranslation.execution_status.v1",
        "freeze_manifest_sha256": authorization.manifest_sha256,
        "method_pairs": 150,
        "by_run": by_run,
        "totals": totals,
    }


def _unexpected_terminal(
    directory: Path,
    run: int,
    method_id: str,
    error: BaseException | None = None,
    started_monotonic: float | None = None,
) -> dict[str, Any]:
    terminal = directory / "status.json"
    if terminal.exists():
        try:
            return read_json_object(terminal)
        except ArtifactError:
            return {"status": "failed", "failure_code": "terminal_status_unreadable"}
    failure_code = getattr(error, "code", "unexpected_runtime_exception")
    if (
        not isinstance(failure_code, str)
        or not failure_code
        or re.fullmatch(r"[a-z0-9_]{1,100}", failure_code) is None
    ):
        failure_code = "unexpected_runtime_exception"
    value = {
        "schema_version": "backtranslation.run_status.v1",
        "status": "failed",
        "stage": "infrastructure",
        "failure_class": "unexpected_runtime",
        "failure_code": failure_code,
        "method_id": method_id,
        "run_index": run,
        "finished_at_utc": _utc_now(),
        "elapsed_milliseconds": (
            0
            if started_monotonic is None
            else max(0, int((time.monotonic() - started_monotonic) * 1000))
        ),
    }
    try:
        write_json_once(terminal, value)
    except ArtifactError:
        return {"status": "failed", "failure_code": "terminal_status_write_failed"}
    return value


def execute_schedule(
    *,
    project_directory: Path,
    artifact_root: Path,
    credential_path: Path,
    cases: Sequence[StudyCase],
    authorization: FreezeAuthorization,
    provider_config: ProviderConfig | None = None,
    max_workers: int = FROZEN_MAX_WORKERS,
    case_runner: Callable[..., dict[str, Any]] = execute_case_run,
) -> dict[str, Any]:
    """Execute only unclaimed pairs, with a barrier between the three runs."""

    effective_provider_config = provider_config or ProviderConfig()
    if effective_provider_config != ProviderConfig():
        # ProviderConfig is intentionally reusable by small canaries, but the
        # freeze-authorized production scheduler has exactly one admissible
        # setting vector.
        raise ExecutionError("pilot_provider_config_not_frozen")
    if max_workers != FROZEN_MAX_WORKERS:
        raise ExecutionError("worker_count_not_frozen")
    credential_metadata(credential_path)
    initialize_schedule(
        artifact_root=artifact_root, cases=cases, authorization=authorization
    )
    run_root = artifact_root / authorization.manifest_sha256
    lock_path = run_root / "execution.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ExecutionError("pilot_execution_already_running") from exc
        ordered = sorted(cases, key=lambda case: case.method_id)
        submitted = 0
        completed = 0
        for run in FROZEN_RUNS:
            pending: list[tuple[StudyCase, int]] = []
            for position, case in enumerate(ordered, start=1):
                directory = _run_directory(
                    artifact_root, authorization.manifest_sha256, run, case.method_id
                )
                # A claim is permanent. A missing terminal status is an
                # interrupted attempt, not permission for another model call.
                if (directory / "run.claim.json").exists():
                    continue
                pending.append((case, run * len(ordered) + position))
            with ThreadPoolExecutor(
                max_workers=FROZEN_MAX_WORKERS,
                thread_name_prefix=f"backtranslation-run-{run}",
            ) as executor:
                futures: dict[
                    Future[dict[str, Any]], tuple[StudyCase, int, float]
                ] = {}
                pending_iterator = iter(pending)

                def submit_one(case: StudyCase, ordinal: int) -> None:
                    nonlocal submitted
                    directory = claim_case_run(
                        case=case,
                        run_index=run,
                        protocol_hash=authorization.manifest_sha256,
                        artifact_root=artifact_root,
                        schedule_ordinal=ordinal,
                    )
                    submitted_at = time.monotonic()
                    try:
                        future = executor.submit(
                            case_runner,
                            case=case,
                            run_index=run,
                            protocol_hash=authorization.manifest_sha256,
                            project_directory=project_directory,
                            artifact_root=artifact_root,
                            credential_path=credential_path,
                            provider_config=effective_provider_config,
                            preclaimed_schedule_ordinal=ordinal,
                        )
                    except Exception as exc:
                        _unexpected_terminal(
                            directory,
                            run,
                            case.method_id,
                            exc,
                            started_monotonic=submitted_at,
                        )
                        raise
                    futures[future] = (case, ordinal, submitted_at)
                    submitted += 1

                while len(futures) < FROZEN_MAX_WORKERS:
                    try:
                        next_case, next_ordinal = next(pending_iterator)
                    except StopIteration:
                        break
                    submit_one(next_case, next_ordinal)

                while futures:
                    done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                    # Processing simultaneous completions by frozen ordinal
                    # makes replenishment deterministic even though completion
                    # timing itself is explicitly not an analysis input.
                    for future in sorted(done, key=lambda item: futures[item][1]):
                        case, _, submitted_at = futures.pop(future)
                        try:
                            future.result()
                        except Exception as exc:
                            directory = _run_directory(
                                artifact_root,
                                authorization.manifest_sha256,
                                run,
                                case.method_id,
                            )
                            _unexpected_terminal(
                                directory,
                                run,
                                case.method_id,
                                exc,
                                started_monotonic=submitted_at,
                            )
                        completed += 1
                    while len(futures) < FROZEN_MAX_WORKERS:
                        try:
                            next_case, next_ordinal = next(pending_iterator)
                        except StopIteration:
                            break
                        submit_one(next_case, next_ordinal)
    finally:
        os.close(lock_descriptor)
    status = execution_status(
        artifact_root=artifact_root, authorization=authorization, cases=cases
    )
    return {
        "schema_version": "backtranslation.execution_invocation.v1",
        "freeze_manifest_sha256": authorization.manifest_sha256,
        "submitted_unclaimed_pairs": submitted,
        "completed_workers": completed,
        "status": status,
    }
