"""Execution engine for the outcome-blind v0.6 first-valid quota.

Raw imported and native attempts are immutable.  Eligibility records live
outside raw attempt directories so their source-tree hashes are acyclic.  A
write-once selection chooses the lowest eligible attempt.  Only after all 150
cells have selections can this module publish the quota receipt and a derived,
flat selected view for the frozen v0.5 downstream tools.

This module intentionally imports neither scores nor human outcomes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, read_json_object, write_bytes_once, write_json_once
from .cases import (
    StudyCase,
    extraction_input,
    load_study_cases,
    regeneration_input,
    render_prompt,
    sha256_bytes,
)
from .directions import (
    SchemaValidationError,
    validate_directions_document,
    validate_regenerated_code,
)
from .java_validation import JavaValidationError, analyze_java_method
from .provider import ProviderConfig, ProviderError, ProviderResult, credential_metadata, send_json_request
from .quota import (
    ATTEMPT_ELIGIBILITY_SCHEMA,
    EXPECTED_CELL_COUNT,
    LEGACY_INVENTORY_SCHEMA,
    MAX_ATTEMPTS_PER_CELL,
    METHOD_IDS,
    QUOTA_COMPLETE_SCHEMA,
    RUN_INDICES,
    SELECTED_ATTEMPT_SCHEMA,
    SELECTION_POLICY,
    canonical_json_bytes,
    document_sha256,
    load_canonical_json,
    quota_blocked_document,
    run_barrier_witness_document,
    run_barrier_witness_sha256,
    selection_evidence_snapshot,
    snapshot_selection_evidence_tree,
    snapshot_source_tree,
    validate_attempt_eligibility,
    validate_legacy_attempt_inventory,
    validate_quota_complete,
    validate_quota_blocked,
    validate_run_barrier_witness,
    validate_selected_attempt,
    verify_selection_evidence_snapshot,
    verify_source_tree_snapshot,
)
from .execution import FreezeAuthorization, schedule_document, verify_freeze_authorization
from .freeze import FreezeError, manifest_sha256, verify_manifest, verify_runtime_lock
from .roundtrip import _call_stage, _load_prompt


MAX_WORKERS = 5
PREDICATE_ID = "first-valid-roundtrip-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")
_JAVA_INFRASTRUCTURE_NAME = "java-infrastructure.json"
_CANDIDATE_JAVA_INVALID_CODES = frozenset(
    {
        "java_source_not_utf8",
        "java_source_not_text_or_bytes",
        "java_parser_spans_overlap",
        "java_token_not_utf8",
        "java_parser_emitted_empty_token",
        "comment_removal_not_utf8",
        "synthetic_wrapper_parse_failed",
        "synthetic_wrapper_body_missing",
        "callable_declaration_span_invalid",
    }
)
_RAW_COPY_NAMES = (
    "extraction.claim.json",
    "extraction.provider.json",
    "extraction.output.txt",
    "regeneration.claim.json",
    "regeneration.provider.json",
    "regeneration.output.txt",
)
_SELECTED_VIEW_WRITE_ORDER = _RAW_COPY_NAMES + (
    "extraction.result.json",
    "regeneration.result.json",
    "run.claim.json",
    "status.json",
    "selected-attempt.json",
    "selection.json",
)
_LEGACY_RAW_NAMES = {
    "run.claim.json",
    "status.json",
    "extraction.claim.json",
    "extraction.provider.json",
    "extraction.output.txt",
    "extraction.result.json",
    "regeneration.claim.json",
    "regeneration.provider.json",
    "regeneration.output.txt",
    "regeneration.result.json",
}
_NATIVE_RAW_NAMES = {
    "attempt.claim.json",
    "status.json",
    "extraction.claim.json",
    "extraction.provider.json",
    "extraction.output.txt",
    "extraction.result.json",
    "regeneration.claim.json",
    "regeneration.provider.json",
    "regeneration.output.txt",
    "regeneration.result.json",
    _JAVA_INFRASTRUCTURE_NAME,
}
_STAGE_ORDER = ("claim", "provider", "output", "result")


class QuotaExecutionError(RuntimeError):
    """Stable, outcome-free execution or provenance failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


ProviderSender = Callable[..., ProviderResult]


class _DispatchAbort:
    """A process-local stop flag shared by all cell workers in one scheduler."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._records: dict[tuple[int, str], dict[str, Any]] = {}
        self._admitted_senders = 0

    def is_set(self) -> bool:
        return self.event.is_set()

    def stop(self, record: Mapping[str, Any]) -> None:
        cell = record.get("cell")
        if not isinstance(cell, Mapping):
            raise QuotaExecutionError("quota_block_record_cell_invalid")
        key = (int(cell.get("run_index", -1)), str(cell.get("method_id", "")))
        with self._lock:
            self._records.setdefault(key, dict(record))
            self.event.set()

    def invoke_sender(
        self, sender: ProviderSender, /, **kwargs: Any
    ) -> ProviderResult:
        """Atomically admit one already-issued request against the stop gate.

        The admission decision and ``stop()`` share one lock.  A sender that
        wins admission is an already-issued request and may finish; once a
        stop wins the lock, no later admission is possible.  The lock is
        released before the blocking provider call so distinct cells keep
        their frozen five-way concurrency.
        """

        with self._lock:
            if self.event.is_set():
                raise QuotaExecutionError("quota_dispatch_aborted")
            self._admitted_senders += 1
        try:
            return sender(**kwargs)
        finally:
            with self._lock:
                self._admitted_senders -= 1

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._records[key] for key in sorted(self._records)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _valid_utc_milliseconds(value: Any) -> bool:
    """Accept only a real, canonical UTC timestamp with millisecond precision."""

    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_text(value: str) -> str:
    return _hash_bytes(value.encode("utf-8"))


def _relative(project: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(project.resolve(strict=True)).as_posix()
    except (OSError, ValueError) as exc:
        raise QuotaExecutionError("quota_path_outside_project") from exc


def _assert_no_symlink_ancestors(
    project: Path, path: Path, *, leaf_kind: str | None = None, allow_missing: bool = False
) -> None:
    """Reject symlink traversal beneath the trusted project directory.

    The lexical path is intentional: resolving first would hide the symlink
    that this check is meant to detect.  Missing suffixes are permitted only
    for paths that a later write will create.
    """

    try:
        trusted = project.resolve(strict=True)
        candidate = path if path.is_absolute() else trusted / path
        relative = candidate.relative_to(trusted)
    except (OSError, ValueError) as exc:
        raise QuotaExecutionError("quota_path_outside_project") from exc
    current = trusted
    parts = relative.parts
    for position, part in enumerate(parts):
        if part in {"", ".", ".."}:
            raise QuotaExecutionError("quota_path_component_invalid")
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise QuotaExecutionError("quota_path_component_missing")
        except OSError as exc:
            raise QuotaExecutionError("quota_path_component_unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise QuotaExecutionError("quota_path_ancestor_symlink_prohibited")
        last = position == len(parts) - 1
        if not last and not stat.S_ISDIR(metadata.st_mode):
            raise QuotaExecutionError("quota_path_ancestor_not_directory")
        if last and leaf_kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
            raise QuotaExecutionError("quota_path_leaf_not_directory")
        if last and leaf_kind == "file" and not stat.S_ISREG(metadata.st_mode):
            raise QuotaExecutionError("quota_path_leaf_not_regular")


def _mkdir_beneath(project: Path, path: Path) -> None:
    """Create a directory one component at a time, checking every ancestor."""

    _assert_no_symlink_ancestors(project, path, allow_missing=True)
    trusted = project.resolve(strict=True)
    try:
        relative = path.relative_to(trusted)
    except ValueError as exc:
        raise QuotaExecutionError("quota_path_outside_project") from exc
    current = trusted
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise QuotaExecutionError("quota_directory_create_failed") from exc
        _assert_no_symlink_ancestors(project, current, leaf_kind="directory")


def _strict_attempt_file_audit(directory: Path, source_kind: str) -> None:
    """Allow only the frozen raw workflow files and only valid stage prefixes."""

    allowed = _LEGACY_RAW_NAMES if source_kind == "legacy-v0.5" else _NATIVE_RAW_NAMES
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise QuotaExecutionError("quota_attempt_directory_read_failed") from exc
    names: set[str] = set()
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError as exc:
            raise QuotaExecutionError("quota_attempt_entry_read_failed") from exc
        if entry.name in {
            "status.json",
            "extraction.result.json",
            "regeneration.result.json",
        }:
            # Descriptive convenience leaves are outside the first-valid
            # predicate.  Their bytes, type, links, presence, and freshness
            # cannot turn a raw provider attempt into a retry or a block.
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or entry.name not in allowed
        ):
            raise QuotaExecutionError("quota_attempt_artifact_not_allowed")
        names.add(entry.name)
    identity = "run.claim.json" if source_kind == "legacy-v0.5" else "attempt.claim.json"
    if identity not in names:
        raise QuotaExecutionError("quota_attempt_identity_missing")
    for stage in ("extraction", "regeneration"):
        actual = {
            part
            for part, filename in (
                ("claim", f"{stage}.claim.json"),
                ("provider", f"{stage}.provider.json"),
                ("output", f"{stage}.output.txt"),
            )
            if filename in names
        }
        # Local result files are descriptive convenience views.  Their
        # presence, absence, ordering, or content cannot affect whether a raw
        # provider attempt is selectable or retryable.
        if actual not in (set(), {"claim"}, {"claim", "provider"}, {"claim", "provider", "output"}):
            raise QuotaExecutionError("quota_attempt_stage_prefix_invalid")
    extraction_output = "extraction.output.txt" in names
    regeneration_any = any(
        name in names
        for name in (
            "regeneration.claim.json",
            "regeneration.provider.json",
            "regeneration.output.txt",
        )
    )
    if regeneration_any and not extraction_output:
        raise QuotaExecutionError("quota_regeneration_without_extraction_output")
    if (
        _JAVA_INFRASTRUCTURE_NAME in names
        and (
            source_kind != "v0.6-retry"
            or "regeneration.output.txt" not in names
        )
    ):
        raise QuotaExecutionError("quota_java_infrastructure_marker_prefix_invalid")


def _audit_run_tree(root: Path) -> None:
    """Fail closed on every unexpected scheduler-owned entry."""

    if not root.exists():
        return
    allowed_top = {
        "quota-execution.lock",
        "quota-blocked.json",
        "quota-complete.json",
        "attempts",
        "cells",
        "selected-view",
    }
    try:
        top = {entry.name: entry for entry in root.iterdir()}
    except OSError as exc:
        raise QuotaExecutionError("quota_run_root_read_failed") from exc
    if not set(top) <= allowed_top:
        raise QuotaExecutionError("quota_run_root_entry_not_allowed")
    for file_name in ("quota-execution.lock", "quota-blocked.json", "quota-complete.json"):
        entry = top.get(file_name)
        if entry is not None:
            metadata = entry.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise QuotaExecutionError("quota_run_root_file_invalid")
    for tree_name in ("attempts", "cells"):
        tree = top.get(tree_name)
        if tree is None:
            continue
        if not stat.S_ISDIR(tree.lstat().st_mode):
            raise QuotaExecutionError("quota_run_tree_not_directory")
        expected_run_names = {f"run-{run}" for run in RUN_INDICES}
        for run_entry in tree.iterdir():
            if run_entry.name not in expected_run_names or not stat.S_ISDIR(
                run_entry.lstat().st_mode
            ):
                raise QuotaExecutionError("quota_run_tree_entry_not_allowed")
            for cell_entry in run_entry.iterdir():
                if cell_entry.name not in METHOD_IDS or not stat.S_ISDIR(
                    cell_entry.lstat().st_mode
                ):
                    raise QuotaExecutionError("quota_run_cell_entry_not_allowed")
                if tree_name == "attempts":
                    for attempt in cell_entry.iterdir():
                        match = re.fullmatch(r"attempt-(\d{4})", attempt.name)
                        if (
                            match is None
                            or not stat.S_ISDIR(attempt.lstat().st_mode)
                            or not 2 <= int(match.group(1)) <= MAX_ATTEMPTS_PER_CELL
                        ):
                            raise QuotaExecutionError("quota_raw_attempt_entry_invalid")
                        _strict_attempt_file_audit(attempt, "v0.6-retry")
                else:
                    allowed_cell = {"selected-attempt.json"} | {
                        f"attempt-{index:04d}.eligibility.json"
                        for index in range(1, MAX_ATTEMPTS_PER_CELL + 1)
                    }
                    for artifact in cell_entry.iterdir():
                        metadata = artifact.lstat()
                        if (
                            artifact.name not in allowed_cell
                            or not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_nlink != 1
                        ):
                            raise QuotaExecutionError("quota_cell_ledger_entry_invalid")
    selected_view = top.get("selected-view")
    if selected_view is not None:
        if not stat.S_ISDIR(selected_view.lstat().st_mode):
            raise QuotaExecutionError("quota_selected_view_not_directory")
        expected_runs = {f"run-{run}" for run in RUN_INDICES}
        for run_entry in selected_view.iterdir():
            if (
                run_entry.name not in expected_runs
                or not stat.S_ISDIR(run_entry.lstat().st_mode)
            ):
                raise QuotaExecutionError("quota_selected_view_run_invalid")
            for cell_entry in run_entry.iterdir():
                if (
                    cell_entry.name not in METHOD_IDS
                    or not stat.S_ISDIR(cell_entry.lstat().st_mode)
                ):
                    raise QuotaExecutionError("quota_selected_view_cell_invalid")
                observed: set[str] = set()
                for artifact in cell_entry.iterdir():
                    metadata = artifact.lstat()
                    if (
                        artifact.name not in _SELECTED_VIEW_WRITE_ORDER
                        or not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                    ):
                        raise QuotaExecutionError("quota_selected_view_artifact_invalid")
                    observed.add(artifact.name)
                prefixes = {
                    frozenset(_SELECTED_VIEW_WRITE_ORDER[:index])
                    for index in range(len(_SELECTED_VIEW_WRITE_ORDER) + 1)
                }
                if frozenset(observed) not in prefixes:
                    raise QuotaExecutionError("quota_selected_view_prefix_invalid")


def _audit_legacy_root_structure(legacy_root: Path) -> None:
    expected_top = {"execution.lock", "schedule.json"} | {f"run-{run}" for run in RUN_INDICES}
    try:
        top = {entry.name: entry for entry in legacy_root.iterdir()}
    except OSError as exc:
        raise QuotaExecutionError("quota_legacy_root_read_failed") from exc
    if set(top) != expected_top:
        raise QuotaExecutionError("quota_legacy_root_entry_set_invalid")
    for name in ("execution.lock", "schedule.json"):
        metadata = top[name].lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise QuotaExecutionError("quota_legacy_root_file_invalid")
    for run in RUN_INDICES:
        run_directory = top[f"run-{run}"]
        run_metadata = run_directory.lstat()
        if not stat.S_ISDIR(run_metadata.st_mode):
            raise QuotaExecutionError("quota_legacy_run_directory_invalid")
        try:
            cells = {entry.name: entry for entry in run_directory.iterdir()}
        except OSError as exc:
            raise QuotaExecutionError("quota_legacy_run_read_failed") from exc
        if set(cells) != set(METHOD_IDS):
            raise QuotaExecutionError("quota_legacy_run_cell_set_invalid")
        for method_id, directory in cells.items():
            cell_metadata = directory.lstat()
            if not stat.S_ISDIR(cell_metadata.st_mode):
                raise QuotaExecutionError("quota_legacy_cell_directory_invalid")
            _strict_attempt_file_audit(directory, "legacy-v0.5")


def _audit_regular_tree(root: Path) -> None:
    """Require an ordinary directory tree with single-link regular leaves."""

    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        current_metadata = current_path.lstat()
        if not stat.S_ISDIR(current_metadata.st_mode):
            raise QuotaExecutionError("quota_static_archive_directory_invalid")
        for name in directories:
            metadata = (current_path / name).lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise QuotaExecutionError("quota_static_archive_directory_invalid")
        for name in files:
            metadata = (current_path / name).lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise QuotaExecutionError("quota_static_archive_file_invalid")


def _manifest_records(manifest_path: Path) -> dict[str, dict[str, Any]]:
    manifest = _read_object(manifest_path, "quota_v06_manifest_invalid")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise QuotaExecutionError("quota_v06_manifest_files_invalid")
    by_path: dict[str, dict[str, Any]] = {}
    for record in records:
        if (
            type(record) is not dict
            or set(record) != {"path", "bytes", "sha256"}
            or not isinstance(record.get("path"), str)
            or record["path"] in by_path
        ):
            raise QuotaExecutionError("quota_v06_manifest_record_invalid")
        by_path[record["path"]] = record
    return by_path


def verify_v06_generation_scope(
    *,
    project_directory: Path,
    manifest_path: Path,
    freeze_record_path: Path,
    legacy_inventory_path: Path,
) -> None:
    """Require the exact generation-critical bundle in the authorized manifest."""

    scope_path = project_directory / "config" / "generation-scope-v0.6.json"
    _assert_no_symlink_ancestors(project_directory, manifest_path, leaf_kind="file")
    _assert_no_symlink_ancestors(project_directory, freeze_record_path, leaf_kind="file")
    _assert_no_symlink_ancestors(project_directory, scope_path, leaf_kind="file")
    _assert_no_symlink_ancestors(project_directory, legacy_inventory_path, leaf_kind="file")
    scope = _read_object(scope_path, "quota_v06_scope_invalid")
    if set(scope) != {
        "schema_version", "canonical_manifest_path", "approval_record_path",
        "legacy_inventory_path", "selection_predicate_id", "required_manifest_paths",
    } or scope.get("schema_version") != "backtranslation.generation-freeze-scope.v0.6":
        raise QuotaExecutionError("quota_v06_scope_schema_invalid")
    expected_manifest = _relative(project_directory, manifest_path)
    expected_record = _relative(project_directory, freeze_record_path)
    expected_inventory = _relative(project_directory, legacy_inventory_path)
    if (
        scope.get("canonical_manifest_path") != expected_manifest
        or scope.get("approval_record_path") != expected_record
        or scope.get("legacy_inventory_path") != expected_inventory
        or scope.get("selection_predicate_id") != PREDICATE_ID
    ):
        raise QuotaExecutionError("quota_v06_scope_path_mismatch")
    required = scope.get("required_manifest_paths")
    if not isinstance(required, list) or required != sorted(set(required)):
        raise QuotaExecutionError("quota_v06_scope_required_paths_invalid")
    minimum = {
        "GOAL.v0.6.frozen.md", "protocol/PROTOCOL.frozen.md",
        "protocol/PROTOCOL.v0.6.frozen.md", "protocol/freeze-manifest-v1.json",
        "protocol/freeze-record.jsonl", "config/freeze-spec-v0.6.json",
        "config/generation-scope-v0.6.json", "config/java-parser-revision.json",
        "config/runtime-lock.json", "config/test-suite-v0.6.contract.json",
        "config/test-suite-v0.6.json",
        "artifacts/provider-canary/v4-pro-json-thinking-high.json",
        "artifacts/provider-canary/v4-pro-json-thinking-high-max16384.json",
        "src/backtranslation/__init__.py", "src/backtranslation/artifacts.py",
        "src/backtranslation/cases.py",
        "src/backtranslation/directions.py", "src/backtranslation/execution.py",
        "src/backtranslation/freeze.py", "src/backtranslation/java_validation.py",
        "src/backtranslation/provider.py", "src/backtranslation/quota.py",
        "src/backtranslation/quota_execution.py", "src/backtranslation/roundtrip.py",
        "tools/run_quota_pilot.py", "tests/test_artifacts_roundtrip.py",
        "tests/test_execution.py", "tests/test_protocol_v06_assets.py",
        "tests/test_quota.py", "tests/test_quota_execution.py",
        "prompts/extract.system.txt", "prompts/extract.user.txt",
        "prompts/regenerate.system.txt", "prompts/regenerate.user.txt",
        "schemas/directions-v1.schema.json", "schemas/extraction-input-v1.schema.json",
        "schemas/regeneration-input-v1.schema.json", "schemas/regeneration-v1.schema.json",
        "schemas/type-context-v1.schema.json", "data/study_cases.jsonl",
        "data/tse/README.md", "data/tse/snippet_index.csv",
        "data/tse/source_manifest.jsonl", expected_inventory,
    }
    minimum.update(f"data/tse/snippets/tse-{index:03d}.java" for index in range(1, 51))
    minimum.update(f"data/tse/contexts/tse-{index:03d}.context.json" for index in range(1, 51))
    manifest_records = _manifest_records(manifest_path)
    for relative in sorted(set(required) | minimum):
        record = manifest_records.get(relative)
        if record is None:
            raise QuotaExecutionError("quota_v06_manifest_required_entry_missing")
        path = project_directory / relative
        _assert_no_symlink_ancestors(project_directory, path, leaf_kind="file")
        payload = _read_bytes(path, "quota_v06_required_entry_read_failed")
        if record.get("bytes") != len(payload) or record.get("sha256") != _hash_bytes(payload):
            raise QuotaExecutionError("quota_v06_manifest_required_entry_mismatch")


def _read_bytes(path: Path, code: str) -> bytes:
    try:
        before = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise OSError
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or opened.st_nlink != 1
        ):
            raise OSError
        return b"".join(chunks)
    except OSError as exc:
        raise QuotaExecutionError(code) from exc


def _read_object(path: Path, code: str) -> dict[str, Any]:
    try:
        payload = _read_bytes(path, code)
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QuotaExecutionError(code) from exc
    if type(value) is not dict or payload != canonical_json_bytes(value):
        raise QuotaExecutionError(code)
    return value


def _open_lock(path: Path, *, create: bool) -> int:
    """Securely open a regular non-symlink lock and bind lstat to fstat."""

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        before = path.lstat() if path.exists() or path.is_symlink() else None
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise QuotaExecutionError("quota_lock_open_failed") from exc
    if not stat.S_ISREG(opened.st_mode) or (
        before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(descriptor)
        raise QuotaExecutionError("quota_lock_not_regular")
    return descriptor


def _verify_scheduler_lock_binding(
    *, root: Path, descriptor: int, root_identity: tuple[int, int]
) -> None:
    """Prove the locked inode still belongs to the same quota-root path."""

    try:
        root_metadata = root.lstat()
        path_lock = (root / "quota-execution.lock").lstat()
        opened_lock = os.fstat(descriptor)
    except OSError as exc:
        raise QuotaExecutionError("quota_execution_lock_binding_changed") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or (root_metadata.st_dev, root_metadata.st_ino) != root_identity
        or not stat.S_ISREG(path_lock.st_mode)
        or path_lock.st_nlink != 1
        or (path_lock.st_dev, path_lock.st_ino)
        != (opened_lock.st_dev, opened_lock.st_ino)
    ):
        raise QuotaExecutionError("quota_execution_lock_binding_changed")


def _verify_runtime_environment(project: Path) -> str:
    """Bind the executing interpreter/distributions to the frozen runtime lock."""

    path = project / "config" / "runtime-lock.json"
    _assert_no_symlink_ancestors(project, path, leaf_kind="file")
    payload = _read_bytes(path, "quota_runtime_lock_read_failed")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuotaExecutionError("quota_runtime_lock_invalid") from exc
    if not isinstance(value, Mapping):
        raise QuotaExecutionError("quota_runtime_lock_invalid")
    if verify_runtime_lock(project, value):
        raise QuotaExecutionError("quota_runtime_environment_mismatch")
    return _hash_bytes(payload)


def _dispatch_preflight(
    *, project: Path, manifest_path: Path, freeze_record_path: Path,
    legacy_inventory_path: Path, expected_protocol_sha256: str,
) -> None:
    authorization = verify_freeze_authorization(
        project_directory=project,
        manifest_path=manifest_path,
        freeze_record_path=freeze_record_path,
    )
    if authorization.manifest_sha256 != expected_protocol_sha256:
        raise QuotaExecutionError("quota_v06_authorization_digest_mismatch")
    verify_v06_generation_scope(
        project_directory=project,
        manifest_path=manifest_path,
        freeze_record_path=freeze_record_path,
        legacy_inventory_path=legacy_inventory_path,
    )
    _verify_runtime_environment(project)


def _case_identity(case: StudyCase) -> tuple[Any, ...]:
    return (
        case.method_id,
        case.code_1,
        case.code_1_sha256,
        case.type_context,
        case.type_context_sha256,
        case.target_declaration,
    )


def _verify_cases_current(
    project: Path, cases: Sequence[StudyCase]
) -> tuple[StudyCase, ...]:
    """Bind in-memory cells to the current manifested TSE case bytes."""

    current = tuple(load_study_cases(project / "data" / "tse"))
    supplied = tuple(cases)
    if (
        len(current) != 50
        or len(supplied) != 50
        or tuple(map(_case_identity, current)) != tuple(map(_case_identity, supplied))
    ):
        raise QuotaExecutionError("quota_cases_changed_after_authorization")
    return current


def quota_root(artifact_root: Path, protocol_sha256: str) -> Path:
    if _SHA256.fullmatch(protocol_sha256) is None:
        raise QuotaExecutionError("quota_protocol_hash_invalid")
    return artifact_root / protocol_sha256


def native_attempt_directory(root: Path, run: int, method_id: str, index: int) -> Path:
    return root / "attempts" / f"run-{run}" / method_id / f"attempt-{index:04d}"


def eligibility_path(root: Path, run: int, method_id: str, index: int) -> Path:
    return root / "cells" / f"run-{run}" / method_id / f"attempt-{index:04d}.eligibility.json"


def selection_path(root: Path, run: int, method_id: str) -> Path:
    return root / "cells" / f"run-{run}" / method_id / "selected-attempt.json"


def selected_view_directory(root: Path, run: int, method_id: str) -> Path:
    return root / "selected-view" / f"run-{run}" / method_id


def _requested_settings(config: ProviderConfig) -> dict[str, Any]:
    return {
        "model": config.model,
        "thinking": config.thinking,
        "reasoning_effort": config.reasoning_effort,
        "max_tokens": config.max_tokens,
        "response_format": config.response_format,
        "stream": False,
        "temperature": "omitted",
        "top_p": "omitted",
        "seed": "omitted",
    }


def _stage_expected(
    *, project: Path, stage: str, system: str, user: str, config: ProviderConfig
) -> dict[str, Any]:
    schema_name = "directions-v1.schema.json" if stage == "extraction" else "regeneration-v1.schema.json"
    schema = _read_bytes(project / "schemas" / schema_name, "quota_schema_read_failed")
    return {
        "schema_version": "backtranslation.stage_claim.v1",
        "stage": stage,
        "system_prompt_sha256": _hash_text(system),
        "user_prompt_sha256": _hash_text(user),
        "output_schema_path": schema_name,
        "output_schema_sha256": _hash_bytes(schema),
        "requested_settings": _requested_settings(config),
    }


def _assert_runtime_inputs(project: Path) -> None:
    for relative in (
        "prompts/extract.system.txt",
        "prompts/extract.user.txt",
        "prompts/regenerate.system.txt",
        "prompts/regenerate.user.txt",
        "schemas/directions-v1.schema.json",
        "schemas/regeneration-v1.schema.json",
        "config/java-parser-revision.json",
    ):
        _assert_no_symlink_ancestors(project, project / relative, leaf_kind="file")


def _provider_evidence(
    directory: Path,
    *,
    project: Path,
    stage: str,
    system: str,
    user: str,
    config: ProviderConfig,
) -> tuple[bool, bool, bool, bytes | None, Mapping[str, Any] | None]:
    """Return transport, request, artifact-integrity, output, provider-event."""

    claim_path = directory / f"{stage}.claim.json"
    provider_path = directory / f"{stage}.provider.json"
    output_path = directory / f"{stage}.output.txt"
    if not claim_path.exists() and not provider_path.exists() and not output_path.exists():
        return False, True, True, None, None
    if not claim_path.exists():
        return False, False, False, None, None
    try:
        claim = _read_object(claim_path, "quota_stage_claim_invalid")
    except QuotaExecutionError:
        return False, False, False, None, None
    expected = _stage_expected(project=project, stage=stage, system=system, user=user, config=config)
    claimed_at = claim.get("claimed_at_utc")
    reconstructed = (
        _valid_utc_milliseconds(claimed_at)
        and set(claim) == set(expected) | {"claimed_at_utc"}
        and {key: claim.get(key) for key in expected} == expected
    )
    if not provider_path.exists():
        # A claim without a completed provider/output pair is a retained failed
        # attempt, but its exact intended request remains reconstructible.
        return False, reconstructed, not output_path.exists(), None, None
    try:
        provider = _read_object(provider_path, "quota_stage_provider_invalid")
        output = (
            _read_bytes(output_path, "quota_stage_output_invalid")
            if output_path.exists()
            else None
        )
    except QuotaExecutionError:
        return False, reconstructed, False, None, None
    event = provider.get("provider_event")
    if (
        set(provider) != {"schema_version", "stage", "provider_event"}
        or provider.get("schema_version") != "backtranslation.stage_provider_response.v1"
        or provider.get("stage") != stage
        or not isinstance(event, Mapping)
    ):
        return False, reconstructed, False, output, None
    if set(event) != {
        "schema_version", "provider", "protocol", "request", "response",
        "elapsed_milliseconds", "credential",
    }:
        return False, reconstructed, False, output, None
    request = event.get("request")
    response = event.get("response")
    credential = event.get("credential")
    request_body = json.dumps(
        {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
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
    request_keys = {
        "host", "endpoint", "model", "thinking", "reasoning_effort",
        "max_tokens", "stream", "response_format", "system_prompt_utf8_bytes",
        "system_prompt_sha256", "user_prompt_utf8_bytes", "user_prompt_sha256",
        "request_body_utf8_bytes", "request_body_sha256",
    }
    response_keys = {
        "response_id", "returned_model", "system_fingerprint", "finish_reason",
        "content_utf8_bytes", "content_sha256", "reasoning_content_retained",
        "reasoning_content_utf8_bytes", "usage",
    }
    usage_keys = {
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    }
    expected_credential = {
        "mode": "0600", "owner_uid_matches_process": True, "regular_file": True,
        "symlink": False, "hard_link_count": 1,
    }
    usage = response.get("usage") if isinstance(response, Mapping) else None
    usage_valid = (
        isinstance(usage, Mapping)
        and set(usage) == usage_keys
        and all(value is None or (type(value) is int and value >= 0) for value in usage.values())
        and type(usage.get("prompt_tokens")) is int
        and type(usage.get("completion_tokens")) is int
        and usage.get("total_tokens") == usage.get("prompt_tokens") + usage.get("completion_tokens")
    )
    request_binding = (
        isinstance(request, Mapping)
        and set(request) == request_keys
        and request.get("host") == config.host
        and request.get("endpoint") == config.endpoint
        and request.get("model") == config.model
        and request.get("thinking") == config.thinking
        and request.get("reasoning_effort") == config.reasoning_effort
        and request.get("max_tokens") == config.max_tokens
        and request.get("response_format") == config.response_format
        and request.get("stream") is False
        and request.get("system_prompt_sha256") == _hash_text(system)
        and request.get("user_prompt_sha256") == _hash_text(user)
        and request.get("system_prompt_utf8_bytes") == len(system.encode("utf-8"))
        and request.get("user_prompt_utf8_bytes") == len(user.encode("utf-8"))
        and request.get("request_body_utf8_bytes") == len(request_body)
        and request.get("request_body_sha256") == _hash_bytes(request_body)
    )
    reconstructed = reconstructed and request_binding
    event_valid = (
        event.get("schema_version") == "backtranslation.provider_event.v1"
        and event.get("provider") == "deepseek"
        and event.get("protocol") == "openai_chat_completions"
        and isinstance(request, Mapping)
        and isinstance(response, Mapping)
        and set(request) == request_keys
        and set(response) == response_keys
        and request_binding
        and response.get("returned_model") == config.model
        and response.get("finish_reason") == "stop"
        and response.get("reasoning_content_retained") is False
        and (
            (
                output is not None
                and response.get("content_sha256") == _hash_bytes(output)
                and response.get("content_utf8_bytes") == len(output)
            )
            or (
                output is None
                and isinstance(response.get("content_sha256"), str)
                and _SHA256.fullmatch(response["content_sha256"]) is not None
                and type(response.get("content_utf8_bytes")) is int
                and response.get("content_utf8_bytes") >= 0
            )
        )
        and type(response.get("reasoning_content_utf8_bytes")) is int
        and response.get("reasoning_content_utf8_bytes") >= 0
        and (response.get("response_id") is None or isinstance(response.get("response_id"), str))
        and (response.get("system_fingerprint") is None or isinstance(response.get("system_fingerprint"), str))
        and usage_valid
        and isinstance(credential, Mapping)
        and dict(credential) == expected_credential
        and type(event.get("elapsed_milliseconds")) is int
        and event.get("elapsed_milliseconds") >= 0
    )
    # The provider event is written before the exact assistant output.  A
    # crash between those writes is a valid, retryable workflow prefix: the
    # retained request/event has provenance, but provider completion cannot
    # count without the exact output bytes.  Conversely, output without its
    # provider event was rejected above as an integrity failure.
    completed = event_valid and output is not None
    return completed, reconstructed, event_valid, output, event


def _cell_identity(
    directory: Path,
    *,
    case: StudyCase,
    run: int,
    protocol_sha256: str,
    attempt_index: int,
    source_kind: str,
    expected_predecessor_sha256: str | None,
    expected_barrier_witness_sha256: str | None = None,
) -> bool:
    name = "run.claim.json" if source_kind == "legacy-v0.5" else "attempt.claim.json"
    try:
        claim = _read_object(directory / name, "quota_attempt_claim_invalid")
    except QuotaExecutionError:
        return False
    common = (
        claim.get("method_id") == case.method_id
        and claim.get("run_index") == run
        and claim.get("protocol_hash") == protocol_sha256
        and claim.get("code_1_sha256") == case.code_1_sha256
        and claim.get("type_context_sha256") == case.type_context_sha256
    )
    if source_kind == "legacy-v0.5":
        return (
            set(claim) == {
                "schema_version", "method_id", "run_index", "protocol_hash",
                "claimed_at_utc", "code_1_sha256", "type_context_sha256",
                "schedule_ordinal",
            }
            and claim.get("schema_version") == "backtranslation.run_claim.v1"
            and _valid_utc_milliseconds(claim.get("claimed_at_utc"))
            and common
            and claim.get("schedule_ordinal") == run * 50 + int(case.method_id[-3:])
        )
    return (
        set(claim) == {
            "schema_version", "predicate_id", "method_id", "run_index",
            "attempt_index", "protocol_hash", "claimed_at_utc", "code_1_sha256",
            "type_context_sha256", "target_declaration_sha256",
            "predecessor_eligibility_sha256", "requested_settings",
            "run_barrier_witness_sha256",
        }
        and common
        and claim.get("schema_version") == "backtranslation.quota-attempt-claim.v1"
        and claim.get("attempt_index") == attempt_index
        and claim.get("predicate_id") == PREDICATE_ID
        and _valid_utc_milliseconds(claim.get("claimed_at_utc"))
        and claim.get("target_declaration_sha256") == _hash_text(case.target_declaration)
        and expected_predecessor_sha256 is not None
        and claim.get("predecessor_eligibility_sha256") == expected_predecessor_sha256
        and claim.get("run_barrier_witness_sha256") == expected_barrier_witness_sha256
        and claim.get("requested_settings") == _requested_settings(ProviderConfig())
    )


def _barrier_witness(
    *, root: Path, protocol_sha256: str, run: int, cases: Sequence[StudyCase]
) -> dict[str, Any]:
    """Hash the exact prior-run selections authorizing a new-run dispatch."""

    if run == 0:
        return run_barrier_witness_document(
            protocol_sha256=protocol_sha256,
            target_run_index=0,
        )
    prior = []
    for case in cases:
        path = selection_path(root, run - 1, case.method_id)
        selection = validate_selected_attempt(load_canonical_json(path))
        if (
            selection["protocol_sha256"] != protocol_sha256
            or selection["cell"] != {"run_index": run - 1, "method_id": case.method_id}
        ):
            raise QuotaExecutionError("quota_run_barrier_selection_invalid")
        prior.append(
            {
                "cell": {"run_index": run - 1, "method_id": case.method_id},
                "selection_sha256": document_sha256(selection),
            }
        )
    return run_barrier_witness_document(
        protocol_sha256=protocol_sha256,
        target_run_index=run,
        predecessor_selections=prior,
    )


def _terminal_valid(
    directory: Path,
    *,
    case: StudyCase,
    run: int,
    index: int,
    source_kind: str,
    protocol_sha256: str,
    extraction_transport: bool,
    extraction_contract: bool,
    regeneration_transport: bool,
    regeneration_contract: bool,
    structural: bool,
) -> tuple[bool, bool]:
    """Bind exact terminal schemas to the retained workflow prefix."""

    try:
        value = _read_object(directory / "status.json", "quota_status_invalid")
    except QuotaExecutionError:
        return False, False
    common_identity = value.get("method_id") == case.method_id and value.get("run_index") == run
    elapsed = value.get("elapsed_milliseconds")
    timestamp = value.get("finished_at_utc")
    if (
        not common_identity
        or type(elapsed) is not int
        or elapsed < 0
        or not _valid_utc_milliseconds(timestamp)
    ):
        return False, False
    if source_kind == "v0.6-retry":
        expected_keys = {
            "schema_version", "status", "stage", "method_id", "run_index",
            "attempt_index", "finished_at_utc", "elapsed_milliseconds",
        }
        if value.get("failure_code") is not None:
            expected_keys.add("failure_code")
        expected_status = "eligible" if structural else "rejected"
        if structural:
            stage_valid = value.get("stage") == "predicate_complete" and "failure_code" not in value
        elif value.get("stage") == "interrupted":
            stage_valid = value.get("failure_code") == "attempt_interrupted_before_terminal"
        elif value.get("stage") == "java_infrastructure":
            stage_valid = "failure_code" in value
        elif not extraction_transport:
            stage_valid = value.get("stage") == "extraction_api" and "failure_code" in value
        elif not extraction_contract:
            stage_valid = value.get("stage") in {"extraction_parse", "extraction_schema"} and "failure_code" in value
        elif not regeneration_transport:
            stage_valid = value.get("stage") == "regeneration_api" and "failure_code" in value
        elif not regeneration_contract:
            stage_valid = value.get("stage") in {"regeneration_parse", "regeneration_schema"} and "failure_code" in value
        else:
            stage_valid = value.get("stage") == "predicate_complete" and value.get("failure_code") == "java_structurally_invalid"
        valid = (
            set(value) == expected_keys
            and value.get("schema_version") == "backtranslation.quota-attempt-status.v1"
            and value.get("attempt_index") == index
            and value.get("status") == expected_status
            and stage_valid
        )
        return valid, valid and value.get("status") == "eligible"
    common_keys = {
        "schema_version", "status", "stage", "method_id", "run_index",
        "finished_at_utc", "elapsed_milliseconds",
    }
    if value.get("schema_version") != "backtranslation.run_status.v1":
        return False, False
    if value.get("status") == "generated":
        valid = (
            set(value) == common_keys | {"protocol_hash"}
            and value.get("stage") == "generation_complete"
            and value.get("protocol_hash") == protocol_sha256
            and extraction_transport
            and extraction_contract
            and regeneration_transport
            and regeneration_contract
        )
        return valid, valid
    stage = value.get("stage")
    stage_classes = {
        "extraction_api": "provider",
        "extraction_parse": "parse",
        "extraction_schema": "schema",
        "regeneration_api": "provider",
        "regeneration_parse": "parse",
        "regeneration_schema": "schema",
        "infrastructure": "unexpected_runtime",
    }
    expected_keys = common_keys | {"failure_class", "failure_code"}
    if "http_status" in value:
        expected_keys.add("http_status")
    if (
        value.get("status") != "failed"
        or stage not in stage_classes
        or value.get("failure_class") != stage_classes[stage]
        or set(value) != expected_keys
        or not isinstance(value.get("failure_code"), str)
    ):
        return False, False
    prefix_match = {
        "extraction_api": not extraction_transport and not extraction_contract and not regeneration_transport and not regeneration_contract,
        "extraction_parse": extraction_transport and not extraction_contract and not regeneration_transport and not regeneration_contract,
        "extraction_schema": extraction_transport and not extraction_contract and not regeneration_transport and not regeneration_contract,
        "regeneration_api": extraction_transport and extraction_contract and not regeneration_transport and not regeneration_contract,
        "regeneration_parse": extraction_transport and extraction_contract and regeneration_transport and not regeneration_contract,
        "regeneration_schema": extraction_transport and extraction_contract and regeneration_transport and not regeneration_contract,
        "infrastructure": True,
    }
    return prefix_match[stage], False


def evaluate_attempt(
    *,
    project_directory: Path,
    source_directory: Path,
    source_root: Path,
    case: StudyCase,
    run_index: int,
    attempt_index: int,
    source_kind: str,
    origin_protocol_sha256: str,
    provider_config: ProviderConfig | None = None,
    expected_predecessor_sha256: str | None = None,
    expected_barrier_witness_sha256: str | None = None,
    expected_barrier_witness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconstruct one raw attempt and return its canonical eligibility record."""

    config = provider_config or ProviderConfig()
    if config != ProviderConfig():
        raise QuotaExecutionError("quota_provider_config_not_frozen")
    if source_kind == "v0.6-retry" and expected_barrier_witness is None:
        if run_index != 0:
            raise QuotaExecutionError("quota_run_barrier_witness_required")
        expected_barrier_witness = run_barrier_witness_document(
            protocol_sha256=origin_protocol_sha256,
            target_run_index=0,
        )
    if source_kind == "v0.6-retry" and expected_barrier_witness_sha256 is None:
        expected_barrier_witness_sha256 = run_barrier_witness_sha256(
            expected_barrier_witness
        )
    _assert_runtime_inputs(project_directory)
    _assert_no_symlink_ancestors(project_directory, source_root, leaf_kind="directory")
    _assert_no_symlink_ancestors(project_directory, source_directory, leaf_kind="directory")
    _strict_attempt_file_audit(source_directory, source_kind)
    if source_kind == "v0.6-retry":
        _raise_if_java_infrastructure_marked(
            project_directory=project_directory,
            source_directory=source_directory,
            case=case,
            run_index=run_index,
            attempt_index=attempt_index,
            protocol_sha256=origin_protocol_sha256,
        )
    snapshot = snapshot_selection_evidence_tree(source_directory)
    identity = _cell_identity(
        source_directory,
        case=case,
        run=run_index,
        protocol_sha256=origin_protocol_sha256,
        attempt_index=attempt_index,
        source_kind=source_kind,
        expected_predecessor_sha256=expected_predecessor_sha256,
        expected_barrier_witness_sha256=expected_barrier_witness_sha256,
    )
    extraction_system = _load_prompt(project_directory / "prompts" / "extract.system.txt")
    extraction_template = _load_prompt(project_directory / "prompts" / "extract.user.txt")
    extraction_user = render_prompt(extraction_template, "EXTRACTION_INPUT_JSON", extraction_input(case))
    extraction_transport, extraction_request, extraction_artifacts, extraction_output, extraction_event = _provider_evidence(
        source_directory,
        project=project_directory,
        stage="extraction",
        system=extraction_system,
        user=extraction_user,
        config=config,
    )
    raw_directions: dict[str, Any] | None = None
    extraction_contract = False
    extraction_result_integrity = True
    if extraction_transport and extraction_output is not None:
        try:
            parsed = json.loads(extraction_output.decode("utf-8"))
            validate_directions_document(parsed)
            extraction_contract = type(parsed) is dict
            if extraction_contract:
                raw_directions = parsed
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, QuotaExecutionError):
            extraction_contract = False
            extraction_result_integrity = True

    regeneration_transport = False
    regeneration_request = False
    regeneration_contract = False
    java_performed = False
    structural = False
    java_artifact_hash: str | None = None
    regeneration_artifacts = True
    regeneration_result_integrity = True
    if raw_directions is not None:
        regeneration_system = _load_prompt(project_directory / "prompts" / "regenerate.system.txt")
        regeneration_template = _load_prompt(project_directory / "prompts" / "regenerate.user.txt")
        regeneration_user = render_prompt(
            regeneration_template,
            "REGENERATION_INPUT_JSON",
            regeneration_input(case, raw_directions),
        )
        regeneration_transport, regeneration_request, regeneration_artifacts, regeneration_output, regeneration_event = _provider_evidence(
            source_directory,
            project=project_directory,
            stage="regeneration",
            system=regeneration_system,
            user=regeneration_user,
            config=config,
        )
        if regeneration_transport and regeneration_output is not None:
            try:
                parsed_code = json.loads(regeneration_output.decode("utf-8"))
                regenerated = validate_regenerated_code(parsed_code)
                regeneration_contract = type(parsed_code) is dict
            except (UnicodeDecodeError, json.JSONDecodeError, SchemaValidationError):
                regeneration_contract = False
                regeneration_result_integrity = True
            if regeneration_contract:
                try:
                    analysis = analyze_java_method(regenerated.code, case.target_declaration)
                except JavaValidationError as exc:
                    if str(exc) not in _CANDIDATE_JAVA_INVALID_CODES:
                        raise QuotaExecutionError(
                            "quota_java_validation_infrastructure_failure"
                        ) from exc
                    java_performed = True
                    structural = False
                    java_artifact_hash = _hash_bytes(regeneration_output)
                except Exception as exc:
                    raise QuotaExecutionError(
                        "quota_java_validation_infrastructure_failure"
                    ) from exc
                else:
                    java_performed = True
                    structural = analysis.structurally_valid
                    java_artifact_hash = _hash_bytes(regeneration_output)
    elif any((source_directory / name).exists() for name in (
        "regeneration.claim.json", "regeneration.provider.json", "regeneration.output.txt"
    )):
        regeneration_artifacts = False

    terminal_record = True
    terminal_success = all((extraction_transport, extraction_contract, regeneration_transport, regeneration_contract, structural))

    checks = {
        "provider_extraction_completed": extraction_transport,
        "extraction_contract_valid": extraction_contract,
        "provider_regeneration_completed": regeneration_transport,
        "regeneration_contract_valid": regeneration_contract,
        "java_structurally_valid": structural,
        "terminal_success": terminal_success,
        "cell_identity_valid": identity,
        "artifact_hashes_valid": (
            terminal_record
            and extraction_artifacts
            and extraction_result_integrity
            and regeneration_artifacts
            and regeneration_result_integrity
        ),
        "request_reconstruction_valid": extraction_request and (
            not extraction_contract or regeneration_request
        ),
    }
    eligible = all(checks.values())
    rejection_codes = [f"check_failed_{name}" for name in SELECTION_POLICY["selection_inputs"] if not checks[name]]
    parser_policy = _read_bytes(project_directory / "config" / "java-parser-revision.json", "quota_java_policy_read_failed")
    attempt_relative = _relative(project_directory, source_directory)
    root_relative = _relative(project_directory, source_root)
    # Close the classification race: no file may change or appear after the
    # initial snapshot and before eligibility publication.
    if snapshot_selection_evidence_tree(source_directory) != snapshot:
        raise QuotaExecutionError("quota_attempt_changed_during_evaluation")
    false_provenance = next(
        (name for name in ("cell_identity_valid", "artifact_hashes_valid", "request_reconstruction_valid") if not checks[name]),
        None,
    )
    primary = false_provenance or next((name for name in SELECTION_POLICY["selection_inputs"] if not checks[name]), None)
    failure_semantics = {
        "provider_extraction_completed": ("extraction_provider", "provider"),
        "extraction_contract_valid": ("extraction_contract", "contract"),
        "provider_regeneration_completed": ("regeneration_provider", "provider"),
        "regeneration_contract_valid": ("regeneration_contract", "contract"),
        "java_structurally_valid": ("java_structure", "structural"),
        "terminal_success": ("terminal", "operational"),
        "cell_identity_valid": ("identity", "provenance"),
        "artifact_hashes_valid": ("artifact_hashes", "provenance"),
        "request_reconstruction_valid": ("request_reconstruction", "provenance"),
    }
    failure = None
    if primary is not None:
        stage, failure_class = failure_semantics[primary]
        retryable = failure_class != "provenance"
        failure = {
            "primary_check": primary,
            "stage": stage,
            "failure_class": failure_class,
            "code": f"failed_{primary}",
            "retryable": retryable,
            "disposition": "retry_whole_roundtrip" if retryable else "block_study",
            "source_terminal_stage": None,
            "source_terminal_class": None,
            "source_terminal_code": None,
        }
    value = {
        "schema_version": ATTEMPT_ELIGIBILITY_SCHEMA,
        "cell": {"run_index": run_index, "method_id": case.method_id},
        "attempt_index": attempt_index,
        "origin": {
            "source_kind": source_kind,
            "protocol_sha256": origin_protocol_sha256,
            "source_root_path": root_relative,
            "attempt_path": attempt_relative,
            "source_tree_sha256": snapshot["tree_sha256"],
        },
        "source_snapshot": snapshot,
        "run_barrier_witness": (
            None if source_kind == "legacy-v0.5" else dict(expected_barrier_witness or {})
        ),
        "run_barrier_witness_sha256": (
            None if source_kind == "legacy-v0.5" else expected_barrier_witness_sha256
        ),
        "predicate": SELECTION_POLICY,
        "checks": checks,
        "java_validation": {
            "performed": java_performed,
            "analyzer_id": "analyze_java_method-v1" if java_performed else None,
            "analyzer_version": "tree-sitter-java-0.23.5" if java_performed else None,
            "validation_policy_sha256": _hash_bytes(parser_policy) if java_performed else None,
            "artifact_path": "regeneration.output.txt" if java_performed else None,
            "artifact_sha256": java_artifact_hash if java_performed else None,
            "structurally_valid": structural,
        },
        "eligible": eligible,
        "rejection_codes": rejection_codes,
        "failure": failure,
    }
    return validate_attempt_eligibility(value)


def _attempt_status(
    directory: Path,
    *,
    case: StudyCase,
    run: int,
    index: int,
    status: str,
    stage: str,
    code: str | None,
    started: float,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "backtranslation.quota-attempt-status.v1",
        "status": status,
        "stage": stage,
        "method_id": case.method_id,
        "run_index": run,
        "attempt_index": index,
        "finished_at_utc": _utc_now(),
        "elapsed_milliseconds": max(0, int((time.monotonic() - started) * 1000)),
    }
    if code is not None:
        value["failure_code"] = code
    write_json_once(directory / "status.json", value)
    return value


def _java_infrastructure_document(
    *,
    project_directory: Path,
    source_directory: Path,
    case: StudyCase,
    run_index: int,
    attempt_index: int,
    protocol_sha256: str,
) -> dict[str, Any]:
    """Build durable evidence that the pinned Java predicate could not run.

    This is operational evidence, never a model-validity input and never a
    reason to issue another provider request.  It deliberately contains no
    wall-clock value so a crash/resume can verify it byte-for-byte.
    """

    output = _read_bytes(
        source_directory / "regeneration.output.txt",
        "quota_java_infrastructure_output_missing",
    )
    parser_policy = _read_bytes(
        project_directory / "config" / "java-parser-revision.json",
        "quota_java_policy_read_failed",
    )
    return {
        "schema_version": "backtranslation.java-infrastructure-block.v1",
        "failure_code": "quota_java_validation_infrastructure_failure",
        "method_id": case.method_id,
        "run_index": run_index,
        "attempt_index": attempt_index,
        "protocol_sha256": protocol_sha256,
        "target_declaration_sha256": _hash_text(case.target_declaration),
        "regeneration_output_sha256": _hash_bytes(output),
        "java_parser_policy_sha256": _hash_bytes(parser_policy),
    }


def _raise_if_java_infrastructure_marked(
    *,
    project_directory: Path,
    source_directory: Path,
    case: StudyCase,
    run_index: int,
    attempt_index: int,
    protocol_sha256: str,
) -> None:
    marker_path = source_directory / _JAVA_INFRASTRUCTURE_NAME
    if not marker_path.exists():
        return
    observed = _read_object(marker_path, "quota_java_infrastructure_marker_invalid")
    expected = _java_infrastructure_document(
        project_directory=project_directory,
        source_directory=source_directory,
        case=case,
        run_index=run_index,
        attempt_index=attempt_index,
        protocol_sha256=protocol_sha256,
    )
    if observed != expected:
        raise QuotaExecutionError("quota_java_infrastructure_marker_invalid")
    raise QuotaExecutionError("quota_java_validation_infrastructure_failure")


def execute_native_attempt(
    *,
    project_directory: Path,
    source_directory: Path,
    case: StudyCase,
    run_index: int,
    attempt_index: int,
    protocol_sha256: str,
    credential_path: Path,
    predecessor_eligibility_sha256: str,
    run_barrier_witness: Mapping[str, Any] | None = None,
    barrier_root: Path | None = None,
    barrier_cases: Sequence[StudyCase] | None = None,
    sender: ProviderSender = send_json_request,
    provider_config: ProviderConfig | None = None,
    abort_event: threading.Event | None = None,
    dispatch_guard: Callable[[], None] | None = None,
    dispatch_admission: _DispatchAbort | None = None,
) -> dict[str, Any]:
    """Issue at most one extraction and one regeneration call for a new attempt."""

    config = provider_config or ProviderConfig()
    if config != ProviderConfig():
        raise QuotaExecutionError("quota_provider_config_not_frozen")
    if attempt_index < 2 or attempt_index > MAX_ATTEMPTS_PER_CELL:
        raise QuotaExecutionError("quota_native_attempt_index_invalid")
    if run_barrier_witness is None:
        # Direct unit-level invocation still binds an explicit deterministic
        # witness; production execution always supplies the verified run-wide
        # selection witness computed by the scheduler.
        if run_index != 0:
            raise QuotaExecutionError("quota_run_barrier_witness_required")
        run_barrier_witness = run_barrier_witness_document(
            protocol_sha256=protocol_sha256,
            target_run_index=0,
        )
    try:
        barrier = validate_run_barrier_witness(run_barrier_witness)
    except Exception as exc:
        raise QuotaExecutionError("quota_run_barrier_witness_invalid") from exc
    if (
        barrier["protocol_sha256"] != protocol_sha256
        or barrier["target_run_index"] != run_index
    ):
        raise QuotaExecutionError("quota_run_barrier_witness_invalid")
    if run_index > 0:
        if barrier_root is None or barrier_cases is None:
            raise QuotaExecutionError("quota_run_barrier_source_required")
        observed_barrier = _barrier_witness(
            root=barrier_root,
            protocol_sha256=protocol_sha256,
            run=run_index,
            cases=barrier_cases,
        )
        if barrier != observed_barrier:
            raise QuotaExecutionError("quota_run_barrier_witness_invalid")
    barrier_sha256 = globals()["run_barrier_witness_sha256"](barrier)
    if dispatch_guard is None:
        # Callable identity cannot prove that a sender is synthetic: a wrapper
        # can reach the real transport.  Every provider-capable entrypoint is
        # therefore guarded; tests pass an explicit no-op guard.
        raise QuotaExecutionError("quota_dispatch_guard_required")
    if run_index != 0 and (dispatch_guard is None or barrier_root is None):
        raise QuotaExecutionError("quota_dispatch_guard_required")

    def admitted_sender(**kwargs: Any) -> ProviderResult:
        if dispatch_admission is None:
            return sender(**kwargs)
        return dispatch_admission.invoke_sender(sender, **kwargs)

    _assert_runtime_inputs(project_directory)
    _assert_no_symlink_ancestors(project_directory, source_directory, allow_missing=True)
    if source_directory.exists():
        raise QuotaExecutionError("quota_attempt_already_claimed")
    _mkdir_beneath(project_directory, source_directory.parent)
    started = time.monotonic()
    claim = {
        "schema_version": "backtranslation.quota-attempt-claim.v1",
        "predicate_id": PREDICATE_ID,
        "method_id": case.method_id,
        "run_index": run_index,
        "attempt_index": attempt_index,
        "protocol_hash": protocol_sha256,
        "claimed_at_utc": _utc_now(),
        "code_1_sha256": case.code_1_sha256,
        "type_context_sha256": case.type_context_sha256,
        "target_declaration_sha256": _hash_text(case.target_declaration),
        "predecessor_eligibility_sha256": predecessor_eligibility_sha256,
        "run_barrier_witness_sha256": barrier_sha256,
        "requested_settings": _requested_settings(config),
    }
    write_json_once(source_directory / "attempt.claim.json", claim)
    _assert_no_symlink_ancestors(project_directory, source_directory, leaf_kind="directory")
    if abort_event is not None and abort_event.is_set():
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="interrupted", code="attempt_interrupted_before_terminal", started=started)
    if dispatch_guard is not None:
        dispatch_guard()
    _assert_no_symlink_ancestors(
        project_directory, source_directory, leaf_kind="directory"
    )
    extraction_system = _load_prompt(project_directory / "prompts" / "extract.system.txt")
    extraction_template = _load_prompt(project_directory / "prompts" / "extract.user.txt")
    extraction_user = render_prompt(extraction_template, "EXTRACTION_INPUT_JSON", extraction_input(case))
    if dispatch_guard is not None:
        dispatch_guard()
    if (
        extraction_system
        != _load_prompt(project_directory / "prompts" / "extract.system.txt")
        or extraction_template
        != _load_prompt(project_directory / "prompts" / "extract.user.txt")
    ):
        raise QuotaExecutionError("quota_prompt_changed_during_dispatch")
    _assert_no_symlink_ancestors(
        project_directory, source_directory, leaf_kind="directory"
    )
    if abort_event is not None and abort_event.is_set():
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="interrupted", code="attempt_interrupted_before_terminal", started=started)
    try:
        extraction_result = _call_stage(
            stage="extraction",
            run_directory=source_directory,
            credential_path=credential_path,
            system_prompt=extraction_system,
            user_prompt=extraction_user,
            sender=admitted_sender,
            provider_config=config,
            output_schema_path=project_directory / "schemas" / "directions-v1.schema.json",
        )
    except ProviderError as exc:
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="extraction_api", code=exc.code, started=started)
    try:
        raw_directions = json.loads(extraction_result.content)
        validate_directions_document(raw_directions)
    except json.JSONDecodeError:
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="extraction_parse", code="extraction_not_json", started=started)
    except SchemaValidationError as exc:
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="extraction_schema", code=exc.code, started=started)
    write_json_once(source_directory / "extraction.result.json", {
        "schema_version": "backtranslation.quota-extraction-result.v2",
        "method_id": case.method_id,
        "run_index": run_index,
        "directions": raw_directions,
        "provider_event": extraction_result.event,
    })
    if abort_event is not None and abort_event.is_set():
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="interrupted", code="attempt_interrupted_before_terminal", started=started)
    if dispatch_guard is not None:
        dispatch_guard()
    _assert_no_symlink_ancestors(
        project_directory, source_directory, leaf_kind="directory"
    )
    regeneration_system = _load_prompt(project_directory / "prompts" / "regenerate.system.txt")
    regeneration_template = _load_prompt(project_directory / "prompts" / "regenerate.user.txt")
    regeneration_user = render_prompt(
        regeneration_template,
        "REGENERATION_INPUT_JSON",
        regeneration_input(case, raw_directions),
    )
    if dispatch_guard is not None:
        dispatch_guard()
    if (
        regeneration_system
        != _load_prompt(project_directory / "prompts" / "regenerate.system.txt")
        or regeneration_template
        != _load_prompt(project_directory / "prompts" / "regenerate.user.txt")
    ):
        raise QuotaExecutionError("quota_prompt_changed_during_dispatch")
    _assert_no_symlink_ancestors(
        project_directory, source_directory, leaf_kind="directory"
    )
    if abort_event is not None and abort_event.is_set():
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="interrupted", code="attempt_interrupted_before_terminal", started=started)
    try:
        regeneration_result = _call_stage(
            stage="regeneration",
            run_directory=source_directory,
            credential_path=credential_path,
            system_prompt=regeneration_system,
            user_prompt=regeneration_user,
            sender=admitted_sender,
            provider_config=config,
            output_schema_path=project_directory / "schemas" / "regeneration-v1.schema.json",
        )
    except ProviderError as exc:
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="regeneration_api", code=exc.code, started=started)
    try:
        raw_code = json.loads(regeneration_result.content)
        regenerated = validate_regenerated_code(raw_code)
    except json.JSONDecodeError:
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="regeneration_parse", code="regeneration_not_json", started=started)
    except SchemaValidationError as exc:
        return _attempt_status(source_directory, case=case, run=run_index, index=attempt_index, status="rejected", stage="regeneration_schema", code=exc.code, started=started)
    try:
        analysis = analyze_java_method(regenerated.code, case.target_declaration)
    except JavaValidationError as exc:
        if str(exc) in _CANDIDATE_JAVA_INVALID_CODES:
            return _attempt_status(
                source_directory,
                case=case,
                run=run_index,
                index=attempt_index,
                status="rejected",
                stage="predicate_complete",
                code="java_structurally_invalid",
                started=started,
            )
        _write_or_verify(
            source_directory / _JAVA_INFRASTRUCTURE_NAME,
            _java_infrastructure_document(
                project_directory=project_directory,
                source_directory=source_directory,
                case=case,
                run_index=run_index,
                attempt_index=attempt_index,
                protocol_sha256=protocol_sha256,
            ),
            "quota_java_infrastructure_marker_mismatch",
        )
        raise QuotaExecutionError(
            "quota_java_validation_infrastructure_failure"
        ) from exc
    except Exception as exc:
        # Parser/runtime failure is not a model-output rejection and must
        # never authorize another generation.  The coordinator persists the
        # raw pair and a study-block receipt instead of consulting a later,
        # potentially transient re-run.
        _write_or_verify(
            source_directory / _JAVA_INFRASTRUCTURE_NAME,
            _java_infrastructure_document(
                project_directory=project_directory,
                source_directory=source_directory,
                case=case,
                run_index=run_index,
                attempt_index=attempt_index,
                protocol_sha256=protocol_sha256,
            ),
            "quota_java_infrastructure_marker_mismatch",
        )
        raise QuotaExecutionError(
            "quota_java_validation_infrastructure_failure"
        ) from exc
    write_json_once(source_directory / "regeneration.result.json", {
        "schema_version": "backtranslation.regeneration_result.v1",
        "method_id": case.method_id,
        "run_index": run_index,
        "output": raw_code,
        "code_2_sha256": sha256_bytes(regenerated.code.encode("utf-8")),
        "java_validation": analysis.as_metadata(),
        "provider_event": regeneration_result.event,
    })
    return _attempt_status(
        source_directory,
        case=case,
        run=run_index,
        index=attempt_index,
        status="eligible" if analysis.structurally_valid else "rejected",
        stage="predicate_complete",
        code=None if analysis.structurally_valid else "java_structurally_invalid",
        started=started,
    )


def _write_or_verify(path: Path, value: dict[str, Any], mismatch: str) -> None:
    if path.exists():
        if load_canonical_json(path) != value:
            raise QuotaExecutionError(mismatch)
        return
    try:
        write_json_once(path, value)
    except ArtifactError as exc:
        raise QuotaExecutionError(exc.code) from exc


def build_legacy_inventory(
    *,
    project_directory: Path,
    legacy_root: Path,
    legacy_protocol_sha256: str,
    output_path: Path,
    cases: Sequence[StudyCase],
) -> dict[str, Any]:
    """Snapshot a quiescent v0.5 tree and independently classify attempt 1."""

    if len(cases) != 50 or [case.method_id for case in cases] != list(METHOD_IDS):
        raise QuotaExecutionError("quota_cases_not_exact_50")
    _assert_runtime_inputs(project_directory)
    if output_path.exists():
        _assert_no_symlink_ancestors(project_directory, output_path, leaf_kind="file")
        existing = verify_legacy_inventory_physical(
            project_directory=project_directory,
            inventory=load_canonical_json(output_path),
        )
        if existing["origin"]["protocol_sha256"] != legacy_protocol_sha256:
            raise QuotaExecutionError("quota_existing_legacy_inventory_protocol_mismatch")
        return existing
    lock_descriptor = _open_lock(legacy_root / "execution.lock", create=False)
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise QuotaExecutionError("quota_legacy_scheduler_not_quiescent") from exc
        _assert_no_symlink_ancestors(project_directory, legacy_root, leaf_kind="directory")
        _assert_no_symlink_ancestors(project_directory, output_path, allow_missing=True)
        _audit_legacy_root_structure(legacy_root)
        schedule = _read_object(legacy_root / "schedule.json", "quota_legacy_schedule_invalid")
        legacy_authorization = FreezeAuthorization(
            manifest_sha256=legacy_protocol_sha256,
            manifest_relative_path="protocol/freeze-manifest-v1.json",
            frozen_at_utc="2026-08-12T08:31:44Z",
            reviewer="codex-independent-ruby-protocol-review",
        )
        if schedule != schedule_document(cases, legacy_authorization):
            raise QuotaExecutionError("quota_legacy_schedule_semantics_mismatch")
        source_snapshot = snapshot_source_tree(legacy_root)
        cells = []
        for run in RUN_INDICES:
            for case in cases:
                directory = legacy_root / f"run-{run}" / case.method_id
                if not directory.is_dir():
                    raise QuotaExecutionError("quota_legacy_not_quiescent_terminal_150")
                eligibility = evaluate_attempt(
                    project_directory=project_directory,
                    source_directory=directory,
                    source_root=legacy_root,
                    case=case,
                    run_index=run,
                    attempt_index=1,
                    source_kind="legacy-v0.5",
                    origin_protocol_sha256=legacy_protocol_sha256,
                )
                cells.append({
                    "cell": {"run_index": run, "method_id": case.method_id},
                    "eligibility_sha256": document_sha256(eligibility),
                    "eligibility": eligibility,
                })
        manifest_path = project_directory / "protocol" / "freeze-manifest-v1.json"
        record_path = project_directory / "protocol" / "freeze-record.jsonl"
        archive_root = project_directory / "artifacts" / "provenance" / "v0.5-static"
        archived_manifest_path = archive_root / "protocol" / "freeze-manifest-v1.json"
        archived_record_path = archive_root / "protocol" / "freeze-record.jsonl"
        schedule_path = legacy_root / "schedule.json"
        for path, kind in (
            (manifest_path, "file"), (record_path, "file"),
            (archive_root, "directory"), (archived_manifest_path, "file"),
            (archived_record_path, "file"), (schedule_path, "file"),
        ):
            _assert_no_symlink_ancestors(project_directory, path, leaf_kind=kind)
        def identity(path: Path) -> dict[str, Any]:
            payload = _read_bytes(path, "quota_legacy_identity_read_failed")
            return {"path": _relative(project_directory, path), "bytes": len(payload), "sha256": _hash_bytes(payload)}
        archive_snapshot = snapshot_source_tree(archive_root)
        archived_manifest = _read_object(archived_manifest_path, "quota_archived_manifest_invalid")
        try:
            archive_digest = verify_manifest(archive_root, archived_manifest)
        except FreezeError as exc:
            raise QuotaExecutionError("quota_archived_manifest_verification_failed") from exc
        if archive_digest != legacy_protocol_sha256 or manifest_sha256(archived_manifest) != legacy_protocol_sha256:
            raise QuotaExecutionError("quota_archived_manifest_digest_mismatch")
        archive_file_paths = {record["path"] for record in archive_snapshot["files"]}
        manifest_file_paths = {record["path"] for record in archived_manifest["files"]}
        if archive_file_paths != manifest_file_paths | {
            "protocol/freeze-manifest-v1.json", "protocol/freeze-record.jsonl"
        }:
            raise QuotaExecutionError("quota_static_archive_entry_set_invalid")
        approval_lines = _read_bytes(archived_record_path, "quota_archived_freeze_record_invalid").decode("utf-8").splitlines()
        if len(approval_lines) != 1:
            raise QuotaExecutionError("quota_archived_freeze_record_invalid")
        approved = False
        for line in approval_lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise QuotaExecutionError("quota_archived_freeze_record_invalid") from exc
            if (
                isinstance(record, dict)
                and set(record) == {
                    "schema_version", "frozen_at_utc", "manifest_path",
                    "manifest_sha256", "reviewer",
                }
                and record.get("schema_version") == "backtranslation.freeze-record.v1"
                and record.get("frozen_at_utc") == legacy_authorization.frozen_at_utc
                and record.get("manifest_path") == "protocol/freeze-manifest-v1.json"
                and record.get("manifest_sha256") == legacy_protocol_sha256
                and record.get("reviewer") == legacy_authorization.reviewer
            ):
                approved = True
        if not approved:
            raise QuotaExecutionError("quota_archived_manifest_not_approved")
        value = {
        "schema_version": LEGACY_INVENTORY_SCHEMA,
        "inventoried_at_utc": _utc_now(),
        "origin": {
            "source_kind": "legacy-v0.5",
            "protocol_sha256": legacy_protocol_sha256,
            "source_root_path": _relative(project_directory, legacy_root),
            "source_tree_sha256": source_snapshot["tree_sha256"],
        },
        "freeze_identity": {
            "authorized_manifest_sha256": legacy_protocol_sha256,
            "static_archive_root_path": _relative(project_directory, archive_root),
            "static_archive_source_snapshot": archive_snapshot,
            "static_archive_source_tree_sha256": archive_snapshot["tree_sha256"],
            "freeze_manifest": identity(manifest_path),
            "archived_freeze_manifest": identity(archived_manifest_path),
            "execution_schedule": identity(schedule_path),
            "freeze_record_log": identity(record_path),
            "archived_freeze_record_log": identity(archived_record_path),
        },
        "source_snapshot": source_snapshot,
        "cells": cells,
        }
        if snapshot_source_tree(legacy_root) != source_snapshot:
            raise QuotaExecutionError("quota_legacy_changed_during_inventory")
        value = validate_legacy_attempt_inventory(value)
        _mkdir_beneath(project_directory, output_path.parent)
        _assert_no_symlink_ancestors(project_directory, output_path, allow_missing=True)
        _write_or_verify(output_path, value, "quota_existing_legacy_inventory_mismatch")
        return value
    finally:
        os.close(lock_descriptor)


def _inventory_map(inventory: Mapping[str, Any]) -> dict[tuple[int, str], dict[str, Any]]:
    validated = validate_legacy_attempt_inventory(inventory)
    return {
        (item["cell"]["run_index"], item["cell"]["method_id"]): item["eligibility"]
        for item in validated["cells"]
    }


def _write_selection(
    *, root: Path, protocol_sha256: str, run: int, method_id: str, attempts: list[dict[str, Any]]
) -> dict[str, Any]:
    if not attempts or not attempts[-1]["eligible"]:
        raise QuotaExecutionError("quota_selection_without_eligible_attempt")
    records = [{"eligibility_sha256": document_sha256(item), "eligibility": item} for item in attempts]
    selected = attempts[-1]
    value = {
        "schema_version": SELECTED_ATTEMPT_SCHEMA,
        "protocol_sha256": protocol_sha256,
        "cell": {"run_index": run, "method_id": method_id},
        "policy": SELECTION_POLICY,
        "attempts": records,
        "selected_attempt_index": selected["attempt_index"],
        "selected_eligibility_sha256": records[-1]["eligibility_sha256"],
        "selected_origin": selected["origin"],
    }
    value = validate_selected_attempt(value)
    _write_or_verify(selection_path(root, run, method_id), value, "quota_existing_selection_mismatch")
    return value


def _blocked_cell_record(
    eligibility: Mapping[str, Any], reason: str, evidence_code: str
) -> dict[str, Any]:
    validated = validate_attempt_eligibility(eligibility)
    return {
        "cell": validated["cell"],
        "reason": reason,
        "evidence_code": evidence_code,
        "final_attempt_index": validated["attempt_index"],
        "eligibility_sha256": document_sha256(validated),
        "eligibility": validated,
        "source_tree_sha256": validated["source_snapshot"]["tree_sha256"],
    }


def _publish_blocked(
    *, root: Path, protocol_sha256: str, records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    path = root / "quota-blocked.json"
    if (root / "quota-complete.json").exists():
        raise QuotaExecutionError("quota_complete_cannot_publish_blocked")
    if path.exists():
        return _verify_blocked_receipt_physical(
            root=root,
            protocol_sha256=protocol_sha256,
            value=load_canonical_json(path),
        )
    value = quota_blocked_document(
        protocol_sha256=protocol_sha256,
        blocked_at_utc=_utc_now(),
        blocked_cells=records,
    )
    verified = _verify_blocked_receipt_physical(
        root=root, protocol_sha256=protocol_sha256, value=value
    )
    _write_or_verify(path, verified, "quota_existing_blocked_mismatch")
    return _verify_blocked_receipt_physical(
        root=root, protocol_sha256=protocol_sha256, value=verified
    )


def _verify_blocked_receipt_physical(
    *, root: Path, protocol_sha256: str, value: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind every persisted block record to its retained final attempt."""

    validated = validate_quota_blocked(value)
    if validated["protocol_sha256"] != protocol_sha256:
        raise QuotaExecutionError("quota_existing_blocked_protocol_mismatch")
    if len(root.parents) < 3:
        raise QuotaExecutionError("quota_status_root_invalid")
    project = root.parents[2]
    inventory_path = (
        project / "artifacts" / "provenance" / "legacy-attempt-inventory-v0.5.json"
    )
    inventory = validate_legacy_attempt_inventory(load_canonical_json(inventory_path))
    inventory_map: dict[tuple[int, str], dict[str, Any]] = _inventory_map(inventory)
    case_by_method = {
        case.method_id: case
        for case in load_study_cases(project / "data" / "tse")
    }
    for record in validated["blocked_cells"]:
        run = record["cell"]["run_index"]
        method_id = record["cell"]["method_id"]
        index = record["final_attempt_index"]
        if selection_path(root, run, method_id).exists():
            raise QuotaExecutionError("quota_blocked_cell_has_selection")
        raw_parent = root / "attempts" / f"run-{run}" / method_id
        observed_indices: list[int] = []
        if raw_parent.exists():
            for item in raw_parent.iterdir():
                match = re.fullmatch(r"attempt-(\d{4})", item.name)
                if match is None:
                    raise QuotaExecutionError("quota_blocked_attempt_set_invalid")
                observed_indices.append(int(match.group(1)))
        expected_indices = [] if index == 1 else list(range(2, index + 1))
        if sorted(observed_indices) != expected_indices:
            raise QuotaExecutionError("quota_blocked_final_attempt_not_actual")
        if index == 1:
            attempt = inventory_map[(run, method_id)]
            if record["source_tree_sha256"] != attempt["source_snapshot"]["tree_sha256"]:
                raise QuotaExecutionError("quota_blocked_attempt1_inventory_mismatch")
            if record["eligibility"] is not None and record["eligibility"] != attempt:
                raise QuotaExecutionError("quota_blocked_attempt1_eligibility_mismatch")
            if record["reason"] == "attempt_cap_exhausted":
                raise QuotaExecutionError("quota_blocked_attempt1_cap_invalid")
            if record["reason"] == "provenance_failure":
                failure = attempt["failure"]
                if (
                    record["eligibility"] is None
                    or failure is None
                    or failure["failure_class"] != "provenance"
                    or failure["retryable"]
                ):
                    raise QuotaExecutionError("quota_blocked_attempt1_cause_invalid")
            elif record["reason"] == "java_infrastructure_failure":
                raise QuotaExecutionError("quota_blocked_attempt1_infrastructure_unproven")
            continue
        directory = native_attempt_directory(root, run, method_id, index)
        observed = snapshot_selection_evidence_tree(directory)
        if observed["tree_sha256"] != record["source_tree_sha256"]:
            raise QuotaExecutionError("quota_blocked_source_tree_changed")
        if (
            record["eligibility"] is not None
            and record["eligibility"]["source_snapshot"] != observed
        ):
            raise QuotaExecutionError("quota_blocked_eligibility_source_changed")
        case = case_by_method[method_id]
        barrier = _barrier_witness(
            root=root,
            protocol_sha256=protocol_sha256,
            run=run,
            cases=tuple(case_by_method[name] for name in METHOD_IDS),
        )
        attempts = [inventory_map[(run, method_id)]]
        imported_failure = attempts[0]["failure"]
        if (
            attempts[0]["eligible"]
            or imported_failure is None
            or not imported_failure["retryable"]
            or imported_failure["disposition"] != "retry_whole_roundtrip"
        ):
            raise QuotaExecutionError("quota_blocked_attempt1_predecessor_invalid")
        for prior_index in range(2, index):
            prior_path = eligibility_path(root, run, method_id, prior_index)
            prior = validate_attempt_eligibility(load_canonical_json(prior_path))
            prior_observed = evaluate_attempt(
                project_directory=project,
                source_directory=native_attempt_directory(
                    root, run, method_id, prior_index
                ),
                source_root=root / "attempts",
                case=case,
                run_index=run,
                attempt_index=prior_index,
                source_kind="v0.6-retry",
                origin_protocol_sha256=protocol_sha256,
                expected_predecessor_sha256=document_sha256(attempts[-1]),
                expected_barrier_witness_sha256=run_barrier_witness_sha256(barrier),
                expected_barrier_witness=barrier,
            )
            if (
                prior_observed != prior
                or prior["eligible"]
                or prior["failure"] is None
                or not prior["failure"]["retryable"]
                or prior["failure"]["disposition"] != "retry_whole_roundtrip"
            ):
                raise QuotaExecutionError("quota_blocked_predecessor_invalid")
            attempts.append(prior)
        predecessor = attempts[-1]
        try:
            reconstructed = evaluate_attempt(
                project_directory=project,
                source_directory=directory,
                source_root=root / "attempts",
                case=case,
                run_index=run,
                attempt_index=index,
                source_kind="v0.6-retry",
                origin_protocol_sha256=protocol_sha256,
                expected_predecessor_sha256=document_sha256(predecessor),
                expected_barrier_witness_sha256=run_barrier_witness_sha256(barrier),
                expected_barrier_witness=barrier,
            )
        except QuotaExecutionError as exc:
            if (
                record["reason"] == "java_infrastructure_failure"
                and exc.code == "quota_java_validation_infrastructure_failure"
            ):
                continue
            if (
                record["reason"] == "provenance_failure"
                and exc.code != "quota_java_validation_infrastructure_failure"
            ):
                continue
            raise QuotaExecutionError("quota_blocked_cause_not_reproducible") from exc
        if record["eligibility"] is not None:
            if reconstructed != record["eligibility"]:
                raise QuotaExecutionError("quota_blocked_eligibility_reclassification_mismatch")
        elif (
            record["reason"] != "provenance_failure"
            or reconstructed["eligible"]
            or reconstructed["failure"] is None
            or reconstructed["failure"]["retryable"]
        ):
            raise QuotaExecutionError("quota_blocked_cause_not_reproducible")
    return validated


def _existing_blocked(root: Path, protocol_sha256: str) -> dict[str, Any] | None:
    path = root / "quota-blocked.json"
    if not path.exists():
        return None
    return _verify_blocked_receipt_physical(
        root=root,
        protocol_sha256=protocol_sha256,
        value=load_canonical_json(path),
    )


def _null_block_record(
    *, case: StudyCase, run: int, reason: str, evidence_code: str,
    final_attempt_index: int, source_tree_sha256: str,
) -> dict[str, Any]:
    return {
        "cell": {"run_index": run, "method_id": case.method_id},
        "reason": reason,
        "evidence_code": evidence_code,
        "final_attempt_index": final_attempt_index,
        "eligibility_sha256": None,
        "eligibility": None,
        "source_tree_sha256": source_tree_sha256,
    }


def _observed_attempt_binding(
    *,
    root: Path,
    run: int,
    case: StudyCase,
    final_attempt_index: int,
    imported: Mapping[str, Any],
) -> tuple[int, str]:
    """Bind a null-evidence block to the actual final retained attempt."""

    index = min(max(final_attempt_index, 1), MAX_ATTEMPTS_PER_CELL)
    if index == 1:
        return 1, str(imported["source_snapshot"]["tree_sha256"])
    directory = native_attempt_directory(root, run, case.method_id, index)
    if not directory.is_dir():
        raise QuotaExecutionError("quota_block_final_attempt_missing")
    snapshot = snapshot_selection_evidence_tree(directory)
    return index, snapshot["tree_sha256"]


def _global_existing_preflight(
    *, project: Path, root: Path, protocol_sha256: str,
    inventory_by_cell: Mapping[tuple[int, str], dict[str, Any]],
    cases: Sequence[StudyCase],
) -> list[dict[str, Any]]:
    """Classify every retained cell before any new provider dispatch."""

    blocked: dict[tuple[int, str], dict[str, Any]] = {}
    # First import/select every valid legacy attempt.  This is outcome-free and
    # establishes any predecessor selections available to later-run witnesses.
    for run in RUN_INDICES:
        for case in cases:
            imported = validate_attempt_eligibility(inventory_by_cell[(run, case.method_id)])
            try:
                cell_parent = root / "cells" / f"run-{run}" / case.method_id
                _mkdir_beneath(project, cell_parent)
                _write_or_verify(
                    eligibility_path(root, run, case.method_id, 1),
                    imported,
                    "quota_imported_eligibility_mismatch",
                )
                raw_parent = root / "attempts" / f"run-{run}" / case.method_id
                has_native = raw_parent.exists() and any(raw_parent.iterdir())
                if imported["eligible"] and not has_native:
                    _write_selection(
                        root=root,
                        protocol_sha256=protocol_sha256,
                        run=run,
                        method_id=case.method_id,
                        attempts=[imported],
                    )
            except Exception:
                raw_parent = root / "attempts" / f"run-{run}" / case.method_id
                native_indices: list[int] = []
                if raw_parent.is_dir():
                    for item in raw_parent.iterdir():
                        match = re.fullmatch(r"attempt-(\d{4})", item.name)
                        if match is not None:
                            native_indices.append(int(match.group(1)))
                final_index = min(
                    max(native_indices, default=1), MAX_ATTEMPTS_PER_CELL
                )
                bound_index, bound_tree = _observed_attempt_binding(
                    root=root,
                    run=run,
                    case=case,
                    final_attempt_index=final_index,
                    imported=imported,
                )
                blocked[(run, case.method_id)] = _null_block_record(
                    case=case,
                    run=run,
                    reason="provenance_failure",
                    evidence_code="legacy_import_preflight_failure",
                    final_attempt_index=bound_index,
                    source_tree_sha256=bound_tree,
                )

    for run in RUN_INDICES:
        for case in cases:
            if (run, case.method_id) in blocked:
                continue
            imported = inventory_by_cell[(run, case.method_id)]
            raw_parent = root / "attempts" / f"run-{run}" / case.method_id
            selected = selection_path(root, run, case.method_id)
            has_native = raw_parent.exists() and any(raw_parent.iterdir())
            if not has_native:
                cell_parent = root / "cells" / f"run-{run}" / case.method_id
                observed_cell_files = (
                    {item.name for item in cell_parent.iterdir()}
                    if cell_parent.is_dir()
                    else set()
                )
                allowed_without_native = {"attempt-0001.eligibility.json"}
                if selected.exists():
                    allowed_without_native.add("selected-attempt.json")
                if observed_cell_files != allowed_without_native:
                    blocked[(run, case.method_id)] = _null_block_record(
                        case=case,
                        run=run,
                        reason="provenance_failure",
                        evidence_code="orphan_eligibility_ledger",
                        final_attempt_index=1,
                        source_tree_sha256=imported["source_snapshot"]["tree_sha256"],
                    )
                    continue
                if selected.exists():
                    try:
                        selection = verify_selected_cell(
                            project_directory=project,
                            root=root,
                            protocol_sha256=protocol_sha256,
                            case=case,
                            run=run,
                            cases=cases,
                        )
                        if selection["attempts"][0]["eligibility"] != imported:
                            raise QuotaExecutionError(
                                "quota_selected_attempt1_inventory_mismatch"
                            )
                    except Exception:
                        blocked[(run, case.method_id)] = _null_block_record(
                            case=case,
                            run=run,
                            reason="provenance_failure",
                            evidence_code="selection_preflight_failure",
                            final_attempt_index=1,
                            source_tree_sha256=imported["source_snapshot"]["tree_sha256"],
                        )
                elif imported["failure"] is not None and not imported["failure"]["retryable"]:
                    blocked[(run, case.method_id)] = _blocked_cell_record(
                        imported, "provenance_failure", "provenance_failure"
                    )
                continue
            raw_indices = [
                int(match.group(1))
                for item in raw_parent.iterdir()
                if (match := re.fullmatch(r"attempt-(\d{4})", item.name)) is not None
            ]
            final_index = max(raw_indices, default=1)
            try:
                witness = _barrier_witness(
                    root=root,
                    protocol_sha256=protocol_sha256,
                    run=run,
                    cases=cases,
                )
                attempts = _load_attempts(
                    project=project,
                    root=root,
                    inventory_attempt=imported,
                    case=case,
                    run=run,
                    protocol_sha256=protocol_sha256,
                    barrier_witness=witness,
                )
            except Exception as exc:
                code = getattr(exc, "code", "existing_attempt_preflight_failure")
                reason = (
                    "java_infrastructure_failure"
                    if code == "quota_java_validation_infrastructure_failure"
                    else "provenance_failure"
                )
                bound_index, bound_tree = _observed_attempt_binding(
                    root=root,
                    run=run,
                    case=case,
                    final_attempt_index=final_index,
                    imported=imported,
                )
                blocked[(run, case.method_id)] = _null_block_record(
                    case=case,
                    run=run,
                    reason=reason,
                    evidence_code=(
                        "java_validation_infrastructure_failure"
                        if reason == "java_infrastructure_failure"
                        else "existing_attempt_preflight_failure"
                    ),
                    final_attempt_index=bound_index,
                    source_tree_sha256=bound_tree,
                )
                continue
            blocking = next(
                (
                    attempt for attempt in attempts
                    if attempt["failure"] is not None
                    and not attempt["failure"]["retryable"]
                ),
                None,
            )
            if selected.exists():
                try:
                    selection = verify_selected_cell(
                        project_directory=project,
                        root=root,
                        protocol_sha256=protocol_sha256,
                        case=case,
                        run=run,
                        cases=cases,
                    )
                    if (
                        selection["attempts"][0]["eligibility"] != imported
                        or selection["selected_attempt_index"] != len(attempts)
                        or not attempts[-1]["eligible"]
                    ):
                        raise QuotaExecutionError(
                            "quota_selection_preflight_state_mismatch"
                        )
                except Exception:
                    bound_index, bound_tree = _observed_attempt_binding(
                        root=root,
                        run=run,
                        case=case,
                        final_attempt_index=final_index,
                        imported=imported,
                    )
                    blocked[(run, case.method_id)] = _null_block_record(
                        case=case,
                        run=run,
                        reason="provenance_failure",
                        evidence_code="selection_preflight_failure",
                        final_attempt_index=bound_index,
                        source_tree_sha256=bound_tree,
                    )
                    continue
            if blocking is not None:
                blocked[(run, case.method_id)] = _blocked_cell_record(
                    blocking, "provenance_failure", "provenance_failure"
                )
            elif attempts[-1]["eligible"]:
                _write_selection(
                    root=root,
                    protocol_sha256=protocol_sha256,
                    run=run,
                    method_id=case.method_id,
                    attempts=attempts,
                )
            elif len(attempts) == MAX_ATTEMPTS_PER_CELL:
                blocked[(run, case.method_id)] = _blocked_cell_record(
                    attempts[-1], "attempt_cap_exhausted", "attempt_cap_exhausted"
                )
    return [blocked[key] for key in sorted(blocked)]


def _load_attempts(
    *, project: Path, root: Path, inventory_attempt: dict[str, Any], case: StudyCase,
    run: int, protocol_sha256: str, barrier_witness: Mapping[str, Any]
) -> list[dict[str, Any]]:
    _assert_no_symlink_ancestors(project, root, leaf_kind="directory")
    attempts = [validate_attempt_eligibility(inventory_attempt)]
    raw_parent = root / "attempts" / f"run-{run}" / case.method_id
    if raw_parent.exists():
        _assert_no_symlink_ancestors(project, raw_parent, leaf_kind="directory")
    else:
        _assert_no_symlink_ancestors(project, raw_parent, allow_missing=True)
    if raw_parent.exists():
        raw_indices = []
        for item in raw_parent.iterdir():
            match = re.fullmatch(r"attempt-(\d{4})", item.name)
            if match is None or not item.is_dir() or item.is_symlink():
                raise QuotaExecutionError("quota_raw_attempt_entry_invalid")
            raw_indices.append(int(match.group(1)))
        if sorted(raw_indices) != list(range(2, max(raw_indices, default=1) + 1)) or any(
            index > MAX_ATTEMPTS_PER_CELL for index in raw_indices
        ):
            raise QuotaExecutionError("quota_raw_attempt_indices_not_contiguous")
    cell_parent = root / "cells" / f"run-{run}" / case.method_id
    _mkdir_beneath(project, cell_parent)
    if cell_parent.exists():
        eligibility_indices = []
        for item in cell_parent.iterdir():
            match = re.fullmatch(r"attempt-(\d{4})\.eligibility\.json", item.name)
            if match:
                eligibility_indices.append(int(match.group(1)))
            elif item.name != "selected-attempt.json":
                raise QuotaExecutionError("quota_cell_ledger_entry_invalid")
        if eligibility_indices and sorted(eligibility_indices) != list(
            range(1, max(eligibility_indices) + 1)
        ):
            raise QuotaExecutionError("quota_eligibility_indices_not_contiguous")
        if any(index > MAX_ATTEMPTS_PER_CELL for index in eligibility_indices):
            raise QuotaExecutionError("quota_eligibility_attempt_cap_exceeded")
    first_path = eligibility_path(root, run, case.method_id, 1)
    _write_or_verify(first_path, attempts[0], "quota_imported_eligibility_mismatch")
    for index in range(2, MAX_ATTEMPTS_PER_CELL + 1):
        path = eligibility_path(root, run, case.method_id, index)
        directory = native_attempt_directory(root, run, case.method_id, index)
        if path.exists():
            eligibility = validate_attempt_eligibility(load_canonical_json(path))
            verify_selection_evidence_snapshot(directory, eligibility["source_snapshot"])
            observed = evaluate_attempt(
                project_directory=project,
                source_directory=directory,
                source_root=root / "attempts",
                case=case,
                run_index=run,
                attempt_index=index,
                source_kind="v0.6-retry",
                origin_protocol_sha256=protocol_sha256,
                expected_predecessor_sha256=document_sha256(attempts[-1]),
                expected_barrier_witness_sha256=run_barrier_witness_sha256(barrier_witness),
                expected_barrier_witness=barrier_witness,
            )
            if observed != eligibility:
                raise QuotaExecutionError("quota_existing_eligibility_reclassification_mismatch")
            attempts.append(eligibility)
            continue
        if not directory.exists():
            break
        eligibility = evaluate_attempt(
            project_directory=project,
            source_directory=directory,
            source_root=root / "attempts",
            case=case,
            run_index=run,
            attempt_index=index,
            source_kind="v0.6-retry",
            origin_protocol_sha256=protocol_sha256,
            expected_predecessor_sha256=document_sha256(attempts[-1]),
            expected_barrier_witness_sha256=run_barrier_witness_sha256(barrier_witness),
            expected_barrier_witness=barrier_witness,
        )
        _write_or_verify(path, eligibility, "quota_existing_eligibility_mismatch")
        attempts.append(eligibility)
    for expected, attempt in enumerate(attempts, start=1):
        if attempt["attempt_index"] != expected:
            raise QuotaExecutionError("quota_attempt_sequence_has_gap")
        if expected < len(attempts) and attempt["eligible"]:
            raise QuotaExecutionError("quota_attempt_after_eligible_prohibited")
    return attempts


def _process_cell(
    *,
    project: Path,
    root: Path,
    inventory_attempt: dict[str, Any],
    case: StudyCase,
    cases: Sequence[StudyCase],
    run: int,
    protocol_sha256: str,
    credential_path: Path,
    sender: ProviderSender,
    barrier_witness: Mapping[str, Any],
    abort: _DispatchAbort,
    dispatch_guard: Callable[[], None],
) -> str:
    if abort.is_set():
        return "aborted"
    selected_path = selection_path(root, run, case.method_id)
    attempts = _load_attempts(
        project=project,
        root=root,
        inventory_attempt=inventory_attempt,
        case=case,
        run=run,
        protocol_sha256=protocol_sha256,
        barrier_witness=barrier_witness,
    )
    if any(
        attempt["failure"] is not None and not attempt["failure"]["retryable"]
        for attempt in attempts
    ):
        blocking = next(
            attempt for attempt in attempts
            if attempt["failure"] is not None and not attempt["failure"]["retryable"]
        )
        abort.stop(_blocked_cell_record(blocking, "provenance_failure", "provenance_failure"))
        return "blocked"
    if selected_path.exists():
        selection = validate_selected_attempt(load_canonical_json(selected_path))
        if selection["selected_attempt_index"] != len(attempts) or not attempts[-1]["eligible"]:
            raise QuotaExecutionError("quota_selection_state_mismatch")
        return "selected"
    if attempts[-1]["eligible"]:
        _write_selection(root=root, protocol_sha256=protocol_sha256, run=run, method_id=case.method_id, attempts=attempts)
        return "selected"
    while len(attempts) < MAX_ATTEMPTS_PER_CELL:
        if abort.is_set():
            return "aborted"
        index = len(attempts) + 1
        directory = native_attempt_directory(root, run, case.method_id, index)
        eligibility: dict[str, Any] | None = None
        try:
            execute_native_attempt(
                project_directory=project,
                source_directory=directory,
                case=case,
                run_index=run,
                attempt_index=index,
                protocol_sha256=protocol_sha256,
                credential_path=credential_path,
                predecessor_eligibility_sha256=document_sha256(attempts[-1]),
                run_barrier_witness=barrier_witness,
                barrier_root=root,
                barrier_cases=cases,
                sender=sender,
                abort_event=abort.event,
                dispatch_guard=dispatch_guard,
                dispatch_admission=abort,
            )
        except Exception as exc:
            code = getattr(exc, "code", "dispatch_preflight_failure")
            if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,119}", code) is None:
                code = "dispatch_preflight_failure"
            if code == "quota_dispatch_aborted" and abort.is_set():
                return "aborted"
            if code == "quota_java_validation_infrastructure_failure":
                snapshot = snapshot_selection_evidence_tree(directory)
                abort.stop(_null_block_record(
                    case=case,
                    run=run,
                    reason="java_infrastructure_failure",
                    evidence_code="java_validation_infrastructure_failure",
                    final_attempt_index=index,
                    source_tree_sha256=snapshot["tree_sha256"],
                ))
                return "blocked"
            # An unexpected transport/process interruption can leave one of
            # the explicitly valid raw write prefixes.  Reconstruct that
            # prefix and retry only when the frozen evaluator says it is an
            # ordinary retryable attempt.  Authorization/provenance errors
            # remain terminal and can never be converted into another call.
            if isinstance(exc, QuotaExecutionError):
                snapshot = snapshot_selection_evidence_tree(directory)
                abort.stop(_null_block_record(
                    case=case,
                    run=run,
                    reason="provenance_failure",
                    evidence_code=code,
                    final_attempt_index=index,
                    source_tree_sha256=snapshot["tree_sha256"],
                ))
                return "blocked"
            try:
                eligibility = evaluate_attempt(
                    project_directory=project,
                    source_directory=directory,
                    source_root=root / "attempts",
                    case=case,
                    run_index=run,
                    attempt_index=index,
                    source_kind="v0.6-retry",
                    origin_protocol_sha256=protocol_sha256,
                    expected_predecessor_sha256=document_sha256(attempts[-1]),
                    expected_barrier_witness_sha256=run_barrier_witness_sha256(barrier_witness),
                    expected_barrier_witness=barrier_witness,
                )
            except Exception:
                snapshot = snapshot_selection_evidence_tree(directory)
                abort.stop(_null_block_record(
                    case=case,
                    run=run,
                    reason="provenance_failure",
                    evidence_code="interrupted_attempt_reconstruction_failure",
                    final_attempt_index=index,
                    source_tree_sha256=snapshot["tree_sha256"],
                ))
                return "blocked"
        try:
            if eligibility is None:
                eligibility = evaluate_attempt(
                    project_directory=project,
                    source_directory=directory,
                    source_root=root / "attempts",
                    case=case,
                    run_index=run,
                    attempt_index=index,
                    source_kind="v0.6-retry",
                    origin_protocol_sha256=protocol_sha256,
                    expected_predecessor_sha256=document_sha256(attempts[-1]),
                    expected_barrier_witness_sha256=run_barrier_witness_sha256(barrier_witness),
                    expected_barrier_witness=barrier_witness,
                )
        except QuotaExecutionError as exc:
            if exc.code == "quota_java_validation_infrastructure_failure":
                snapshot = snapshot_selection_evidence_tree(directory)
                abort.stop({
                    "cell": {"run_index": run, "method_id": case.method_id},
                    "reason": "java_infrastructure_failure",
                    "evidence_code": "java_validation_infrastructure_failure",
                    "final_attempt_index": index,
                    "eligibility_sha256": None,
                    "eligibility": None,
                    "source_tree_sha256": snapshot["tree_sha256"],
                })
                return "blocked"
            raise
        _write_or_verify(eligibility_path(root, run, case.method_id, index), eligibility, "quota_existing_eligibility_mismatch")
        attempts.append(eligibility)
        if eligibility["failure"] is not None and not eligibility["failure"]["retryable"]:
            abort.stop(_blocked_cell_record(eligibility, "provenance_failure", "provenance_failure"))
            return "blocked"
        if eligibility["eligible"]:
            _write_selection(root=root, protocol_sha256=protocol_sha256, run=run, method_id=case.method_id, attempts=attempts)
            return "selected"
    final = attempts[-1]
    abort.stop(_blocked_cell_record(final, "attempt_cap_exhausted", "attempt_cap_exhausted"))
    return "exhausted"


def quota_status(*, root: Path, cases: Sequence[StudyCase]) -> dict[str, Any]:
    if len(cases) != 50 or [case.method_id for case in cases] != list(METHOD_IDS):
        raise QuotaExecutionError("quota_cases_not_exact_50")
    protocol_sha256 = root.name
    if _SHA256.fullmatch(protocol_sha256) is None or len(root.parents) < 3:
        raise QuotaExecutionError("quota_status_root_invalid")
    project = root.parents[2]
    if root.exists():
        _assert_no_symlink_ancestors(project, root, leaf_kind="directory")
        _audit_run_tree(root)
    else:
        _assert_no_symlink_ancestors(project, root, allow_missing=True)
    blocked_receipt = _existing_blocked(root, protocol_sha256) if root.exists() else None
    complete_receipt = None
    complete_path = root / "quota-complete.json"
    if complete_path.exists():
        complete_receipt = validate_quota_complete(load_canonical_json(complete_path))
        if complete_receipt["protocol_sha256"] != protocol_sha256:
            raise QuotaExecutionError("quota_complete_protocol_mismatch")
        _verify_complete_receipt_physical(
            project_directory=project,
            root=root,
            protocol_sha256=protocol_sha256,
            cases=cases,
            receipt=complete_receipt,
        )
    if blocked_receipt is not None and complete_receipt is not None:
        raise QuotaExecutionError("quota_blocked_and_complete_conflict")
    by_run: dict[str, Any] = {}
    total_selected = 0
    total_exhausted = 0
    exhausted_cells: list[dict[str, Any]] = []
    for run in RUN_INDICES:
        selected = exhausted = attempts = 0
        for case in cases:
            selected_file = selection_path(root, run, case.method_id)
            if selected_file.exists():
                verify_selected_cell(
                    project_directory=project,
                    root=root,
                    protocol_sha256=protocol_sha256,
                    case=case,
                    run=run,
                    cases=cases,
                )
                selected += 1
            existing_indices = [
                index
                for index in range(1, MAX_ATTEMPTS_PER_CELL + 1)
                if eligibility_path(root, run, case.method_id, index).exists()
            ]
            if existing_indices and existing_indices != list(range(1, max(existing_indices) + 1)):
                raise QuotaExecutionError("quota_status_attempt_indices_not_contiguous")
            for index in existing_indices:
                eligibility = validate_attempt_eligibility(
                    load_canonical_json(eligibility_path(root, run, case.method_id, index))
                )
                if eligibility["cell"] != {"run_index": run, "method_id": case.method_id} or eligibility["attempt_index"] != index:
                    raise QuotaExecutionError("quota_status_eligibility_identity_mismatch")
            count = len(existing_indices)
            attempts += count
            if count == MAX_ATTEMPTS_PER_CELL and not selected_file.exists():
                exhausted += 1
                exhausted_cells.append({"run_index": run, "method_id": case.method_id})
        by_run[str(run)] = {"selected": selected, "unselected": 50 - selected, "exhausted": exhausted, "retained_attempts": attempts}
        total_selected += selected
        total_exhausted += exhausted
    return {
        "schema_version": "backtranslation.quota-execution-status.v1",
        "verification_level": "selection_verified_attempt_ledgers_descriptive",
        "status": (
            "blocked"
            if blocked_receipt is not None
            else "complete"
            if complete_receipt is not None
            else "incomplete"
        ),
        "blocked_receipt": blocked_receipt,
        "selected": total_selected,
        "required": EXPECTED_CELL_COUNT,
        "exhausted": total_exhausted,
        "exhausted_cells": exhausted_cells,
        "quota_satisfied": (
            complete_receipt is not None
            and total_selected == EXPECTED_CELL_COUNT
        ),
        "by_run": by_run,
    }


def execute_quota(
    *,
    project_directory: Path,
    artifact_root: Path,
    protocol_sha256: str,
    legacy_inventory_path: Path,
    cases: Sequence[StudyCase],
    credential_path: Path,
    freeze_manifest_path: Path,
    freeze_record_path: Path,
    sender: ProviderSender = send_json_request,
    max_workers: int = MAX_WORKERS,
) -> dict[str, Any]:
    """Retry unselected cells with strict success barriers and five workers."""

    if max_workers != MAX_WORKERS:
        raise QuotaExecutionError("quota_worker_count_not_frozen")
    authorization = verify_freeze_authorization(
        project_directory=project_directory,
        manifest_path=freeze_manifest_path,
        freeze_record_path=freeze_record_path,
    )
    if authorization.manifest_sha256 != protocol_sha256:
        raise QuotaExecutionError("quota_v06_authorization_digest_mismatch")
    verify_v06_generation_scope(
        project_directory=project_directory,
        manifest_path=freeze_manifest_path,
        freeze_record_path=freeze_record_path,
        legacy_inventory_path=legacy_inventory_path,
    )
    credential_metadata(credential_path)
    if len(cases) != 50 or [case.method_id for case in cases] != list(METHOD_IDS):
        raise QuotaExecutionError("quota_cases_not_exact_50")
    inventory = load_canonical_json(legacy_inventory_path)
    verify_legacy_inventory_physical(
        project_directory=project_directory,
        inventory=inventory,
    )
    inventory_by_cell = _inventory_map(inventory)
    root = quota_root(artifact_root, protocol_sha256)
    _mkdir_beneath(project_directory, root)
    _assert_no_symlink_ancestors(project_directory, root, leaf_kind="directory")
    root_metadata = root.lstat()
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    lock_descriptor = _open_lock(root / "quota-execution.lock", create=True)
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise QuotaExecutionError("quota_execution_already_running") from exc
        _verify_scheduler_lock_binding(
            root=root, descriptor=lock_descriptor, root_identity=root_identity
        )
        # Recheck every trusted path while holding the exclusive scheduler
        # lock, before a worker can invoke the provider sender.
        _assert_no_symlink_ancestors(project_directory, root, leaf_kind="directory")
        verify_v06_generation_scope(
            project_directory=project_directory,
            manifest_path=freeze_manifest_path,
            freeze_record_path=freeze_record_path,
            legacy_inventory_path=legacy_inventory_path,
        )
        verify_legacy_inventory_physical(
            project_directory=project_directory,
            inventory=inventory,
        )
        _dispatch_preflight(
            project=project_directory,
            manifest_path=freeze_manifest_path,
            freeze_record_path=freeze_record_path,
            legacy_inventory_path=legacy_inventory_path,
            expected_protocol_sha256=protocol_sha256,
        )
        cases = _verify_cases_current(project_directory, cases)
        _audit_run_tree(root)
        existing_block = _existing_blocked(root, protocol_sha256)
        complete_path = root / "quota-complete.json"
        if existing_block is not None and complete_path.exists():
            raise QuotaExecutionError("quota_blocked_and_complete_conflict")
        if existing_block is not None:
            return {"status": "blocked", "quota_blocked": existing_block}
        if complete_path.exists():
            complete = validate_quota_complete(load_canonical_json(complete_path))
            if complete["protocol_sha256"] != protocol_sha256:
                raise QuotaExecutionError("quota_complete_protocol_mismatch")
            return quota_status(root=root, cases=cases)
        # Global all-cell preflight occurs before any worker is created.  It
        # validates every historical ledger/selection and persists a terminal
        # block before a provider call if any cell is capped or provenance-invalid.
        abort = _DispatchAbort()
        preflight_blocks = _global_existing_preflight(
            project=project_directory,
            root=root,
            protocol_sha256=protocol_sha256,
            inventory_by_cell=inventory_by_cell,
            cases=cases,
        )
        if preflight_blocks:
            blocked = _publish_blocked(
                root=root,
                protocol_sha256=protocol_sha256,
                records=preflight_blocks,
            )
            return {"status": "blocked", "quota_blocked": blocked}
        for run in RUN_INDICES:
            barrier_witness = _barrier_witness(
                root=root,
                protocol_sha256=protocol_sha256,
                run=run,
                cases=cases,
            )
            if run:
                for prior_case in cases:
                    prior_selection = verify_selected_cell(
                        project_directory=project_directory,
                        root=root,
                        protocol_sha256=protocol_sha256,
                        case=prior_case,
                        run=run - 1,
                        cases=cases,
                    )
                    if prior_selection["attempts"][0]["eligibility"] != inventory_by_cell[(run - 1, prior_case.method_id)]:
                        raise QuotaExecutionError("quota_selected_attempt1_inventory_mismatch")
            pending = [case for case in cases if not selection_path(root, run, case.method_id).exists()]
            for selected_case in (case for case in cases if selection_path(root, run, case.method_id).exists()):
                existing = verify_selected_cell(
                    project_directory=project_directory,
                    root=root,
                    protocol_sha256=protocol_sha256,
                    case=selected_case,
                    run=run,
                    cases=cases,
                )
                if existing["attempts"][0]["eligibility"] != inventory_by_cell[(run, selected_case.method_id)]:
                    raise QuotaExecutionError("quota_selected_attempt1_inventory_mismatch")
            for pending_case in pending:
                try:
                    attempts = _load_attempts(
                        project=project_directory,
                        root=root,
                        inventory_attempt=inventory_by_cell[(run, pending_case.method_id)],
                        case=pending_case,
                        run=run,
                        protocol_sha256=protocol_sha256,
                        barrier_witness=barrier_witness,
                    )
                except Exception as exc:
                    parent = root / "attempts" / f"run-{run}" / pending_case.method_id
                    indices = []
                    if parent.is_dir():
                        for item in parent.iterdir():
                            match = re.fullmatch(r"attempt-(\d{4})", item.name)
                            if match is not None:
                                indices.append(int(match.group(1)))
                    final_index = min(max(indices, default=1), MAX_ATTEMPTS_PER_CELL)
                    imported = inventory_by_cell[(run, pending_case.method_id)]
                    bound_index, bound_tree = _observed_attempt_binding(
                        root=root,
                        run=run,
                        case=pending_case,
                        final_attempt_index=final_index,
                        imported=imported,
                    )
                    code = getattr(exc, "code", "preflight_provenance_failure")
                    reason = (
                        "java_infrastructure_failure"
                        if code == "quota_java_validation_infrastructure_failure"
                        else "provenance_failure"
                    )
                    abort.stop(_null_block_record(
                        case=pending_case,
                        run=run,
                        reason=reason,
                        evidence_code=(
                            "java_validation_infrastructure_failure"
                            if reason == "java_infrastructure_failure"
                            else "preflight_provenance_failure"
                        ),
                        final_attempt_index=bound_index,
                        source_tree_sha256=bound_tree,
                    ))
                    continue
                blocking = next(
                    (
                        attempt for attempt in attempts
                        if attempt["failure"] is not None
                        and not attempt["failure"]["retryable"]
                    ),
                    None,
                )
                if blocking is not None:
                    abort.stop(_blocked_cell_record(blocking, "provenance_failure", "provenance_failure"))
                elif len(attempts) == MAX_ATTEMPTS_PER_CELL and not attempts[-1]["eligible"]:
                    abort.stop(_blocked_cell_record(attempts[-1], "attempt_cap_exhausted", "attempt_cap_exhausted"))
            if abort.is_set():
                blocked = _publish_blocked(
                    root=root,
                    protocol_sha256=protocol_sha256,
                    records=abort.records(),
                )
                return {"status": "blocked", "quota_blocked": blocked}

            def dispatch_guard() -> None:
                if abort.is_set():
                    raise QuotaExecutionError("quota_dispatch_aborted")
                _verify_scheduler_lock_binding(
                    root=root,
                    descriptor=lock_descriptor,
                    root_identity=root_identity,
                )
                _dispatch_preflight(
                    project=project_directory,
                    manifest_path=freeze_manifest_path,
                    freeze_record_path=freeze_record_path,
                    legacy_inventory_path=legacy_inventory_path,
                    expected_protocol_sha256=protocol_sha256,
                )
                verify_legacy_inventory_physical(
                    project_directory=project_directory,
                    inventory=inventory,
                )
                _verify_cases_current(project_directory, cases)
                if _barrier_witness(
                    root=root,
                    protocol_sha256=protocol_sha256,
                    run=run,
                    cases=cases,
                ) != barrier_witness:
                    raise QuotaExecutionError("quota_run_barrier_witness_changed")
                _dispatch_preflight(
                    project=project_directory,
                    manifest_path=freeze_manifest_path,
                    freeze_record_path=freeze_record_path,
                    legacy_inventory_path=legacy_inventory_path,
                    expected_protocol_sha256=protocol_sha256,
                )
                if abort.is_set():
                    raise QuotaExecutionError("quota_dispatch_aborted")
                _verify_scheduler_lock_binding(
                    root=root,
                    descriptor=lock_descriptor,
                    root_identity=root_identity,
                )

            with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix=f"quota-run-{run}") as executor:
                iterator = iter(pending)
                futures: dict[Future[str], StudyCase] = {}

                def submit(case: StudyCase) -> None:
                    futures[executor.submit(
                        _process_cell,
                        project=project_directory,
                        root=root,
                        inventory_attempt=inventory_by_cell[(run, case.method_id)],
                        case=case,
                        cases=cases,
                        run=run,
                        protocol_sha256=protocol_sha256,
                        credential_path=credential_path,
                        sender=sender,
                        barrier_witness=barrier_witness,
                        abort=abort,
                        dispatch_guard=dispatch_guard,
                    )] = case

                while len(futures) < MAX_WORKERS:
                    try:
                        submit(next(iterator))
                    except StopIteration:
                        break
                while futures:
                    done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        failed_case = futures.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:
                            code = getattr(exc, "code", "worker_execution_failure")
                            reason = (
                                "java_infrastructure_failure"
                                if code == "quota_java_validation_infrastructure_failure"
                                else "provenance_failure"
                            )
                            parent = root / "attempts" / f"run-{run}" / failed_case.method_id
                            indices = []
                            if parent.exists():
                                for item in parent.iterdir():
                                    match = re.fullmatch(r"attempt-(\d{4})", item.name)
                                    if match is not None:
                                        indices.append(int(match.group(1)))
                            final_index = max(indices, default=1)
                            observed_hash = inventory_by_cell[
                                (run, failed_case.method_id)
                            ]["source_snapshot"]["tree_sha256"]
                            if final_index > 1:
                                try:
                                    observed_hash = selection_evidence_snapshot(
                                        snapshot_source_tree(
                                            native_attempt_directory(
                                                root, run, failed_case.method_id, final_index
                                            )
                                        )
                                    )["tree_sha256"]
                                except Exception:
                                    pass
                            abort.stop(
                                _null_block_record(
                                    case=failed_case,
                                    run=run,
                                    reason=reason,
                                    evidence_code=(
                                        "java_validation_infrastructure_failure"
                                        if reason == "java_infrastructure_failure"
                                        else "worker_execution_failure"
                                    ),
                                    final_attempt_index=min(
                                        final_index, MAX_ATTEMPTS_PER_CELL
                                    ),
                                    source_tree_sha256=observed_hash,
                                )
                            )
                            result = "blocked"
                        if result in {"blocked", "exhausted"}:
                            abort.event.set()
                    if not abort.is_set():
                        while len(futures) < MAX_WORKERS:
                            try:
                                submit(next(iterator))
                            except StopIteration:
                                break
                if abort.is_set():
                    blocked = _publish_blocked(
                        root=root,
                        protocol_sha256=protocol_sha256,
                        records=abort.records(),
                    )
                    return {"status": "blocked", "quota_blocked": blocked}
                if any(not selection_path(root, run, case.method_id).exists() for case in cases):
                    raise QuotaExecutionError("quota_run_barrier_incomplete")
    finally:
        os.close(lock_descriptor)
    return quota_status(root=root, cases=cases)


def _copy_exact_once(source: Path, destination: Path) -> str:
    payload = _read_bytes(source, "quota_selected_source_read_failed")
    if destination.exists():
        if _read_bytes(destination, "quota_selected_destination_read_failed") != payload:
            raise QuotaExecutionError("quota_selected_view_existing_file_mismatch")
    else:
        try:
            write_bytes_once(destination, payload)
        except ArtifactError as exc:
            raise QuotaExecutionError(exc.code) from exc
    return _hash_bytes(payload)


def materialize_selected_cell(
    *, project_directory: Path, root: Path, protocol_sha256: str, case: StudyCase, run: int
) -> dict[str, Any]:
    """Publish an explicitly derived flat view without relabeling raw calls."""

    selected_file = selection_path(root, run, case.method_id)
    _assert_no_symlink_ancestors(project_directory, selected_file, leaf_kind="file")
    selection = validate_selected_attempt(load_canonical_json(selected_file))
    selection_hash = document_sha256(selection)
    origin = selection["selected_origin"]
    source = project_directory / origin["attempt_path"]
    _assert_no_symlink_ancestors(project_directory, source, leaf_kind="directory")
    verify_selection_evidence_snapshot(
        source, selection["attempts"][-1]["eligibility"]["source_snapshot"]
    )
    destination = selected_view_directory(root, run, case.method_id)
    _mkdir_beneath(project_directory, destination)
    _assert_no_symlink_ancestors(project_directory, destination, leaf_kind="directory")
    copied: dict[str, str] = {}
    for name in _RAW_COPY_NAMES:
        path = source / name
        if path.exists():
            copied[name] = _copy_exact_once(path, destination / name)
    required = set(_RAW_COPY_NAMES)
    if set(copied) != required:
        raise QuotaExecutionError("quota_selected_eligible_source_incomplete")
    source_claim_name = "run.claim.json" if origin["source_kind"] == "legacy-v0.5" else "attempt.claim.json"
    source_claim = _read_object(source / source_claim_name, "quota_selected_source_claim_invalid")
    extraction_output = json.loads(_read_bytes(source / "extraction.output.txt", "quota_selected_extraction_invalid").decode("utf-8"))
    regeneration_output = json.loads(_read_bytes(source / "regeneration.output.txt", "quota_selected_regeneration_invalid").decode("utf-8"))
    directions = validate_directions_document(extraction_output)
    regenerated = validate_regenerated_code(regeneration_output)
    try:
        analysis = analyze_java_method(regenerated.code, case.target_declaration)
    except Exception as exc:
        raise QuotaExecutionError(
            "quota_java_validation_infrastructure_failure"
        ) from exc
    extraction_provider = _read_object(source / "extraction.provider.json", "quota_selected_extraction_provider_invalid")
    regeneration_provider = _read_object(source / "regeneration.provider.json", "quota_selected_regeneration_provider_invalid")
    regeneration_claim = _read_object(source / "regeneration.claim.json", "quota_selected_regeneration_claim_invalid")
    derived_extraction = {
        "schema_version": "backtranslation.selected-view-extraction.v1",
        "method_id": case.method_id,
        "run_index": run,
        "directions": extraction_output,
        "provider_event": extraction_provider["provider_event"],
    }
    derived_regeneration = {
        "schema_version": "backtranslation.regeneration_result.v1",
        "method_id": case.method_id,
        "run_index": run,
        "output": regeneration_output,
        "code_2_sha256": sha256_bytes(regenerated.code.encode("utf-8")),
        "java_validation": analysis.as_metadata(),
        "provider_event": regeneration_provider["provider_event"],
    }
    _write_or_verify(destination / "extraction.result.json", derived_extraction, "quota_selected_derived_extraction_mismatch")
    _write_or_verify(destination / "regeneration.result.json", derived_regeneration, "quota_selected_derived_regeneration_mismatch")
    claim = {
        "schema_version": "backtranslation.selected-view-claim.v2",
        "derived_view_not_provider_execution": True,
        "method_id": case.method_id,
        "run_index": run,
        "protocol_hash": protocol_sha256,
        "claimed_at_utc": source_claim["claimed_at_utc"],
        "code_1_sha256": case.code_1_sha256,
        "type_context_sha256": case.type_context_sha256,
        "schedule_ordinal": run * 50 + int(case.method_id[-3:]),
        "selection_sha256": selection_hash,
        "selected_attempt_index": selection["selected_attempt_index"],
        "selected_origin": origin,
    }
    status = {
        "schema_version": "backtranslation.selected-view-status.v2",
        "derived_view_not_provider_execution": True,
        "status": "generated",
        "stage": "generation_complete",
        "method_id": case.method_id,
        "run_index": run,
        "protocol_hash": protocol_sha256,
        "finished_at_utc": regeneration_claim["claimed_at_utc"],
        "elapsed_milliseconds": extraction_provider["provider_event"]["elapsed_milliseconds"] + regeneration_provider["provider_event"]["elapsed_milliseconds"],
        "selection_sha256": selection_hash,
        "selected_attempt_index": selection["selected_attempt_index"],
        "selected_origin": origin,
    }
    _write_or_verify(destination / "run.claim.json", claim, "quota_selected_derived_claim_mismatch")
    _write_or_verify(destination / "status.json", status, "quota_selected_derived_status_mismatch")
    _write_or_verify(destination / "selected-attempt.json", selection, "quota_selected_view_selection_mismatch")
    derived = {
        "schema_version": "backtranslation.selected-view-binding.v1",
        "derived_view_not_provider_execution": True,
        "protocol_sha256": protocol_sha256,
        "cell": {"run_index": run, "method_id": case.method_id},
        "selection_path": _relative(project_directory, selected_file),
        "selection_sha256": selection_hash,
        "selected_origin": origin,
        "raw_source_claim_path": f"{origin['attempt_path']}/{source_claim_name}",
        "raw_source_claim_sha256": _hash_bytes(_read_bytes(source / source_claim_name, "quota_selected_source_claim_invalid")),
        "exact_copied_file_sha256": copied,
        "derived_file_sha256": {
            "run.claim.json": document_sha256(claim),
            "status.json": document_sha256(status),
            "extraction.result.json": document_sha256(derived_extraction),
            "regeneration.result.json": document_sha256(derived_regeneration),
        },
    }
    _write_or_verify(destination / "selection.json", derived, "quota_selected_view_binding_mismatch")
    return derived


def verify_selected_view_cell(
    *, project_directory: Path, root: Path, protocol_sha256: str, case: StudyCase,
    run: int, cases: Sequence[StudyCase] | None = None,
) -> dict[str, Any]:
    """Verify derived metadata and every exact-copied byte against raw origin."""

    selection = verify_selected_cell(
        project_directory=project_directory,
        root=root,
        protocol_sha256=protocol_sha256,
        case=case,
        run=run,
        cases=cases,
    )
    view = selected_view_directory(root, run, case.method_id)
    binding = load_canonical_json(view / "selection.json")
    if (
        binding.get("derived_view_not_provider_execution") is not True
        or binding.get("selection_sha256") != document_sha256(selection)
        or binding.get("selected_origin") != selection["selected_origin"]
    ):
        raise QuotaExecutionError("quota_selected_view_binding_invalid")
    source = project_directory / selection["selected_origin"]["attempt_path"]
    origin = selection["selected_origin"]
    for name in _RAW_COPY_NAMES:
        if _read_bytes(view / name, "quota_selected_view_read_failed") != _read_bytes(
            source / name, "quota_selected_source_read_failed"
        ):
            raise QuotaExecutionError("quota_selected_view_exact_copy_mismatch")
    claim = load_canonical_json(view / "run.claim.json")
    status = load_canonical_json(view / "status.json")
    for value, schema in (
        (claim, "backtranslation.selected-view-claim.v2"),
        (status, "backtranslation.selected-view-status.v2"),
    ):
        if (
            value.get("schema_version") != schema
            or value.get("derived_view_not_provider_execution") is not True
            or value.get("protocol_hash") != protocol_sha256
            or value.get("selection_sha256") != document_sha256(selection)
            or value.get("selected_attempt_index") != selection["selected_attempt_index"]
            or value.get("selected_origin") != selection["selected_origin"]
        ):
            raise QuotaExecutionError("quota_selected_view_derived_metadata_invalid")
    source_claim_name = (
        "run.claim.json"
        if origin["source_kind"] == "legacy-v0.5"
        else "attempt.claim.json"
    )
    source_claim = _read_object(
        source / source_claim_name, "quota_selected_source_claim_invalid"
    )
    extraction_output = json.loads(
        _read_bytes(
            source / "extraction.output.txt", "quota_selected_extraction_invalid"
        ).decode("utf-8")
    )
    regeneration_output = json.loads(
        _read_bytes(
            source / "regeneration.output.txt", "quota_selected_regeneration_invalid"
        ).decode("utf-8")
    )
    validate_directions_document(extraction_output)
    regenerated = validate_regenerated_code(regeneration_output)
    try:
        analysis = analyze_java_method(regenerated.code, case.target_declaration)
    except Exception as exc:
        raise QuotaExecutionError(
            "quota_java_validation_infrastructure_failure"
        ) from exc
    extraction_provider = _read_object(
        source / "extraction.provider.json", "quota_selected_extraction_provider_invalid"
    )
    regeneration_provider = _read_object(
        source / "regeneration.provider.json", "quota_selected_regeneration_provider_invalid"
    )
    regeneration_claim = _read_object(
        source / "regeneration.claim.json", "quota_selected_regeneration_claim_invalid"
    )
    selection_hash = document_sha256(selection)
    expected_extraction = {
        "schema_version": "backtranslation.selected-view-extraction.v1",
        "method_id": case.method_id,
        "run_index": run,
        "directions": extraction_output,
        "provider_event": extraction_provider["provider_event"],
    }
    expected_regeneration = {
        "schema_version": "backtranslation.regeneration_result.v1",
        "method_id": case.method_id,
        "run_index": run,
        "output": regeneration_output,
        "code_2_sha256": sha256_bytes(regenerated.code.encode("utf-8")),
        "java_validation": analysis.as_metadata(),
        "provider_event": regeneration_provider["provider_event"],
    }
    expected_claim = {
        "schema_version": "backtranslation.selected-view-claim.v2",
        "derived_view_not_provider_execution": True,
        "method_id": case.method_id,
        "run_index": run,
        "protocol_hash": protocol_sha256,
        "claimed_at_utc": source_claim["claimed_at_utc"],
        "code_1_sha256": case.code_1_sha256,
        "type_context_sha256": case.type_context_sha256,
        "schedule_ordinal": run * 50 + int(case.method_id[-3:]),
        "selection_sha256": selection_hash,
        "selected_attempt_index": selection["selected_attempt_index"],
        "selected_origin": origin,
    }
    expected_status = {
        "schema_version": "backtranslation.selected-view-status.v2",
        "derived_view_not_provider_execution": True,
        "status": "generated",
        "stage": "generation_complete",
        "method_id": case.method_id,
        "run_index": run,
        "protocol_hash": protocol_sha256,
        "finished_at_utc": regeneration_claim["claimed_at_utc"],
        "elapsed_milliseconds": extraction_provider["provider_event"]["elapsed_milliseconds"]
        + regeneration_provider["provider_event"]["elapsed_milliseconds"],
        "selection_sha256": selection_hash,
        "selected_attempt_index": selection["selected_attempt_index"],
        "selected_origin": origin,
    }
    if (
        load_canonical_json(view / "extraction.result.json") != expected_extraction
        or load_canonical_json(view / "regeneration.result.json") != expected_regeneration
        or claim != expected_claim
        or status != expected_status
        or load_canonical_json(view / "selected-attempt.json") != selection
    ):
        raise QuotaExecutionError("quota_selected_view_derived_metadata_invalid")
    exact_copied = {
        name: _hash_bytes(
            _read_bytes(source / name, "quota_selected_source_read_failed")
        )
        for name in _RAW_COPY_NAMES
    }
    expected_binding = {
        "schema_version": "backtranslation.selected-view-binding.v1",
        "derived_view_not_provider_execution": True,
        "protocol_sha256": protocol_sha256,
        "cell": {"run_index": run, "method_id": case.method_id},
        "selection_path": _relative(
            project_directory, selection_path(root, run, case.method_id)
        ),
        "selection_sha256": selection_hash,
        "selected_origin": origin,
        "raw_source_claim_path": f"{origin['attempt_path']}/{source_claim_name}",
        "raw_source_claim_sha256": _hash_bytes(
            _read_bytes(source / source_claim_name, "quota_selected_source_claim_invalid")
        ),
        "exact_copied_file_sha256": exact_copied,
        "derived_file_sha256": {
            "run.claim.json": document_sha256(expected_claim),
            "status.json": document_sha256(expected_status),
            "extraction.result.json": document_sha256(expected_extraction),
            "regeneration.result.json": document_sha256(expected_regeneration),
        },
    }
    if binding != expected_binding:
        raise QuotaExecutionError("quota_selected_view_binding_invalid")
    if binding.get("derived_file_sha256") != {
        "run.claim.json": document_sha256(claim),
        "status.json": document_sha256(status),
        "extraction.result.json": document_sha256(load_canonical_json(view / "extraction.result.json")),
        "regeneration.result.json": document_sha256(load_canonical_json(view / "regeneration.result.json")),
    }:
        raise QuotaExecutionError("quota_selected_view_derived_hash_mismatch")
    return binding


def _verify_complete_receipt_physical(
    *,
    project_directory: Path,
    root: Path,
    protocol_sha256: str,
    cases: Sequence[StudyCase],
    receipt: Mapping[str, Any],
) -> None:
    """Rebind a persisted completion receipt to all selected-view bytes."""

    validated = validate_quota_complete(receipt)
    if validated["protocol_sha256"] != protocol_sha256:
        raise QuotaExecutionError("quota_complete_protocol_mismatch")
    view_root = root / "selected-view"
    if validated["selected_view"]["root_path"] != _relative(
        project_directory, view_root
    ):
        raise QuotaExecutionError("quota_complete_selected_view_path_mismatch")
    inventory_identity = validated["legacy_inventory_identity"]
    if (
        inventory_identity["path"]
        != "artifacts/provenance/legacy-attempt-inventory-v0.5.json"
    ):
        raise QuotaExecutionError("quota_complete_inventory_path_mismatch")
    inventory_path = project_directory / inventory_identity["path"]
    payload = _read_bytes(
        inventory_path, "quota_complete_inventory_identity_read_failed"
    )
    if (
        inventory_identity["bytes"] != len(payload)
        or inventory_identity["sha256"] != _hash_bytes(payload)
    ):
        raise QuotaExecutionError("quota_complete_inventory_identity_changed")
    inventory = verify_legacy_inventory_physical(
        project_directory=project_directory,
        inventory=load_canonical_json(inventory_path),
    )
    if (
        inventory_identity["source_tree_sha256"]
        != inventory["source_snapshot"]["tree_sha256"]
        or inventory_identity["authorized_manifest_sha256"]
        != inventory["freeze_identity"]["authorized_manifest_sha256"]
    ):
        raise QuotaExecutionError("quota_complete_inventory_binding_mismatch")
    try:
        verify_source_tree_snapshot(
            view_root, validated["selected_view"]["source_snapshot"]
        )
    except Exception as exc:
        raise QuotaExecutionError("quota_complete_selected_view_changed") from exc
    for run in RUN_INDICES:
        for case in cases:
            verify_selected_view_cell(
                project_directory=project_directory,
                root=root,
                protocol_sha256=protocol_sha256,
                case=case,
                run=run,
                cases=cases,
            )


def _file_identity_matches(project: Path, value: Mapping[str, Any]) -> bool:
    path = project / str(value.get("path"))
    try:
        payload = _read_bytes(path, "quota_identity_read_failed")
    except QuotaExecutionError:
        return False
    return value.get("bytes") == len(payload) and value.get("sha256") == _hash_bytes(payload)


def verify_legacy_inventory_physical(
    *, project_directory: Path, inventory: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify the full old root and freeze identities under its execution lock."""

    validated = validate_legacy_attempt_inventory(inventory)
    legacy_root = project_directory / validated["origin"]["source_root_path"]
    _assert_no_symlink_ancestors(project_directory, legacy_root, leaf_kind="directory")
    lock_descriptor = _open_lock(legacy_root / "execution.lock", create=False)
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise QuotaExecutionError("quota_legacy_scheduler_not_quiescent") from exc
        _audit_legacy_root_structure(legacy_root)
        verify_source_tree_snapshot(legacy_root, validated["source_snapshot"])
        archive_root = project_directory / validated["freeze_identity"]["static_archive_root_path"]
        _assert_no_symlink_ancestors(project_directory, archive_root, leaf_kind="directory")
        _audit_regular_tree(archive_root)
        verify_source_tree_snapshot(
            archive_root, validated["freeze_identity"]["static_archive_source_snapshot"]
        )
        archived_manifest = _read_object(
            archive_root / "protocol" / "freeze-manifest-v1.json",
            "quota_archived_manifest_invalid",
        )
        try:
            if verify_manifest(archive_root, archived_manifest) != validated["origin"]["protocol_sha256"]:
                raise QuotaExecutionError("quota_archived_manifest_digest_mismatch")
        except FreezeError as exc:
            raise QuotaExecutionError("quota_archived_manifest_verification_failed") from exc
        if not all(
            _file_identity_matches(project_directory, validated["freeze_identity"][name])
            for name in (
                "freeze_manifest", "archived_freeze_manifest", "execution_schedule",
                "freeze_record_log", "archived_freeze_record_log",
            )
        ):
            raise QuotaExecutionError("quota_legacy_freeze_identity_changed")
        return validated
    finally:
        os.close(lock_descriptor)


def verify_selected_cell(
    *, project_directory: Path, root: Path, protocol_sha256: str, case: StudyCase, run: int,
    cases: Sequence[StudyCase] | None = None,
) -> dict[str, Any]:
    """Recompute every retained attempt and reject any post-selection attempt."""

    path = selection_path(root, run, case.method_id)
    _assert_no_symlink_ancestors(project_directory, root, leaf_kind="directory")
    _assert_no_symlink_ancestors(project_directory, path, leaf_kind="file")
    selection = validate_selected_attempt(load_canonical_json(path))
    selected_index = selection["selected_attempt_index"]
    if selection["protocol_sha256"] != protocol_sha256:
        raise QuotaExecutionError("quota_selection_protocol_mismatch")
    predecessor: str | None = None
    has_native = any(
        record["eligibility"]["origin"]["source_kind"] == "v0.6-retry"
        for record in selection["attempts"]
    )
    if has_native:
        if cases is None:
            raise QuotaExecutionError("quota_run_barrier_cases_required")
        barrier_witness = _barrier_witness(
            root=root,
            protocol_sha256=protocol_sha256,
            run=run,
            cases=cases,
        )
        barrier_hash = run_barrier_witness_sha256(barrier_witness)
    else:
        barrier_witness = None
        barrier_hash = None
    for record in selection["attempts"]:
        expected = record["eligibility"]
        origin = expected["origin"]
        source = project_directory / origin["attempt_path"]
        _assert_no_symlink_ancestors(project_directory, source, leaf_kind="directory")
        verify_selection_evidence_snapshot(source, expected["source_snapshot"])
        observed = evaluate_attempt(
            project_directory=project_directory,
            source_directory=source,
            source_root=project_directory / origin["source_root_path"],
            case=case,
            run_index=run,
            attempt_index=expected["attempt_index"],
            source_kind=origin["source_kind"],
            origin_protocol_sha256=origin["protocol_sha256"],
            expected_predecessor_sha256=predecessor,
            expected_barrier_witness_sha256=(
                None if origin["source_kind"] == "legacy-v0.5" else barrier_hash
            ),
            expected_barrier_witness=(
                None if origin["source_kind"] == "legacy-v0.5" else barrier_witness
            ),
        )
        if observed != expected:
            raise QuotaExecutionError("quota_attempt_reclassification_mismatch")
        predecessor = record["eligibility_sha256"]
    cell_attempt_root = root / "attempts" / f"run-{run}" / case.method_id
    if cell_attempt_root.exists():
        _assert_no_symlink_ancestors(project_directory, cell_attempt_root, leaf_kind="directory")
    else:
        _assert_no_symlink_ancestors(project_directory, cell_attempt_root, allow_missing=True)
    expected_native_names = {
        f"attempt-{index:04d}" for index in range(2, selected_index + 1)
    }
    actual_native_names = (
        {item.name for item in cell_attempt_root.iterdir()} if cell_attempt_root.exists() else set()
    )
    if actual_native_names != expected_native_names:
        raise QuotaExecutionError("quota_native_attempt_set_after_selection_invalid")
    cell_root = root / "cells" / f"run-{run}" / case.method_id
    expected_eligibility_names = {
        f"attempt-{index:04d}.eligibility.json" for index in range(1, selected_index + 1)
    }
    allowed_cell_names = expected_eligibility_names | {"selected-attempt.json"}
    actual_cell_names = {item.name for item in cell_root.iterdir()}
    actual_eligibility_names = {
        name for name in actual_cell_names if name.endswith(".eligibility.json")
    }
    if actual_eligibility_names != expected_eligibility_names:
        raise QuotaExecutionError("quota_eligibility_set_after_selection_invalid")
    if actual_cell_names != allowed_cell_names:
        raise QuotaExecutionError("quota_cell_ledger_entry_invalid")
    return selection


def _publish_quota_complete_locked(
    *,
    project_directory: Path,
    artifact_root: Path,
    protocol_sha256: str,
    cases: Sequence[StudyCase],
    legacy_inventory_path: Path,
    freeze_manifest_path: Path,
    freeze_record_path: Path,
    lock_guard: Callable[[], None],
) -> dict[str, Any]:
    """Require, materialize, and bind exactly 150 first-valid selections."""

    authorization = verify_freeze_authorization(
        project_directory=project_directory,
        manifest_path=freeze_manifest_path,
        freeze_record_path=freeze_record_path,
    )
    if authorization.manifest_sha256 != protocol_sha256:
        raise QuotaExecutionError("quota_v06_authorization_digest_mismatch")
    _dispatch_preflight(
        project=project_directory,
        manifest_path=freeze_manifest_path,
        freeze_record_path=freeze_record_path,
        legacy_inventory_path=legacy_inventory_path,
        expected_protocol_sha256=protocol_sha256,
    )
    inventory = verify_legacy_inventory_physical(
        project_directory=project_directory,
        inventory=load_canonical_json(legacy_inventory_path),
    )
    root = quota_root(artifact_root, protocol_sha256)
    _audit_run_tree(root)
    if (root / "quota-blocked.json").exists():
        validate_quota_blocked(load_canonical_json(root / "quota-blocked.json"))
        raise QuotaExecutionError("quota_blocked_cannot_publish_complete")
    inventory_by_cell = _inventory_map(inventory)
    records = []
    for run in RUN_INDICES:
        for case in cases:
            lock_guard()
            path = selection_path(root, run, case.method_id)
            if not path.exists():
                raise QuotaExecutionError("quota_not_150_selected")
            selection = verify_selected_cell(
                project_directory=project_directory,
                root=root,
                protocol_sha256=protocol_sha256,
                case=case,
                run=run,
                cases=cases,
            )
            if selection["attempts"][0]["eligibility"] != inventory_by_cell[(run, case.method_id)]:
                raise QuotaExecutionError("quota_selected_attempt1_inventory_mismatch")
            records.append({"selection_sha256": document_sha256(selection), "selection": selection})
    # Only after every one of the 150 raw selections has passed the global
    # preflight may a derived selected-view byte be written.
    for run in RUN_INDICES:
        for case in cases:
            lock_guard()
            try:
                materialize_selected_cell(
                    project_directory=project_directory,
                    root=root,
                    protocol_sha256=protocol_sha256,
                    case=case,
                    run=run,
                )
                verify_selected_view_cell(
                    project_directory=project_directory,
                    root=root,
                    protocol_sha256=protocol_sha256,
                    case=case,
                    run=run,
                    cases=cases,
                )
            except QuotaExecutionError as exc:
                # A publication-time parser/filesystem fault occurs after a
                # valid attempt has already been selected.  It cannot justify
                # another model call and has no stable attempt-level failure
                # evidence.  Fail closed without writing a false terminal
                # receipt; the local publication operation may be repaired and
                # resumed under the same immutable selections.
                raise
    attempt_histogram = {str(index): 0 for index in range(1, MAX_ATTEMPTS_PER_CELL + 1)}
    rejected_by_stage: dict[str, int] = {}
    rejected_by_class: dict[str, int] = {}
    rejected_by_code: dict[str, int] = {}
    rejected_by_source_terminal_stage: dict[str, int] = {}
    rejected_by_source_terminal_class: dict[str, int] = {}
    rejected_by_source_terminal_code: dict[str, int] = {}
    source_terminal_unreadable = 0
    selected_by_origin = {"legacy-v0.5": 0, "v0.6-retry": 0}
    per_run = {
        str(run): {
            "total_retained_attempts": 0,
            "rejected_attempts": 0,
            "attempts_to_success_histogram": {str(index): 0 for index in range(1, MAX_ATTEMPTS_PER_CELL + 1)},
            "rejected_by_stage": {}, "rejected_by_class": {}, "rejected_by_code": {},
            "source_terminal_unreadable": 0,
            "rejected_by_source_terminal_stage": {},
            "rejected_by_source_terminal_class": {},
            "rejected_by_source_terminal_code": {},
            "selected_by_origin": {"legacy-v0.5": 0, "v0.6-retry": 0},
        }
        for run in RUN_INDICES
    }
    total_attempts = rejected_attempts = 0
    view_cells = []
    view_root = root / "selected-view"
    view_snapshot = snapshot_source_tree(view_root)
    lock_guard()
    view_files = {record["path"]: record for record in view_snapshot["files"]}
    for record in records:
        selection = record["selection"]
        index = selection["selected_attempt_index"]
        attempt_histogram[str(index)] += 1
        total_attempts += len(selection["attempts"])
        rejected_attempts += len(selection["attempts"]) - 1
        selected_by_origin[selection["selected_origin"]["source_kind"]] += 1
        run_summary = per_run[str(selection["cell"]["run_index"])]
        run_summary["total_retained_attempts"] += len(selection["attempts"])
        run_summary["rejected_attempts"] += len(selection["attempts"]) - 1
        run_summary["attempts_to_success_histogram"][str(index)] += 1
        run_summary["selected_by_origin"][selection["selected_origin"]["source_kind"]] += 1
        for attempt in selection["attempts"][:-1]:
            failure = attempt["eligibility"]["failure"]
            for mapping, key in (
                (rejected_by_stage, failure["stage"]),
                (rejected_by_class, failure["failure_class"]),
                (rejected_by_code, failure["code"]),
            ):
                mapping[key] = mapping.get(key, 0) + 1
            for map_name, key in (
                ("rejected_by_stage", failure["stage"]),
                ("rejected_by_class", failure["failure_class"]),
                ("rejected_by_code", failure["code"]),
            ):
                run_summary[map_name][key] = run_summary[map_name].get(key, 0) + 1
            if failure["source_terminal_stage"] is None:
                source_terminal_unreadable += 1
                run_summary["source_terminal_unreadable"] += 1
            else:
                for mapping, key in (
                    (rejected_by_source_terminal_stage, failure["source_terminal_stage"]),
                    (rejected_by_source_terminal_class, failure["source_terminal_class"]),
                    (rejected_by_source_terminal_code, failure["source_terminal_code"]),
                ):
                    mapping[key] = mapping.get(key, 0) + 1
                for map_name, key in (
                    ("rejected_by_source_terminal_stage", failure["source_terminal_stage"]),
                    ("rejected_by_source_terminal_class", failure["source_terminal_class"]),
                    ("rejected_by_source_terminal_code", failure["source_terminal_code"]),
                ):
                    run_summary[map_name][key] = run_summary[map_name].get(key, 0) + 1
        cell = selection["cell"]
        binding_path = f"run-{cell['run_index']}/{cell['method_id']}/selected-attempt.json"
        view_cells.append({
            "cell": cell,
            "selection_sha256": record["selection_sha256"],
            "binding_file": view_files[binding_path],
        })
    inventory_payload = _read_bytes(legacy_inventory_path, "quota_inventory_identity_read_failed")
    receipt = {
        "schema_version": QUOTA_COMPLETE_SCHEMA,
        "protocol_sha256": protocol_sha256,
        "policy": SELECTION_POLICY,
        "counts": {
            "runs": 3,
            "methods_per_run": 50,
            "required_cells": EXPECTED_CELL_COUNT,
            "selected_cells": EXPECTED_CELL_COUNT,
            "quota_satisfied": True,
        },
        "legacy_inventory_identity": {
            "path": _relative(project_directory, legacy_inventory_path),
            "bytes": len(inventory_payload),
            "sha256": _hash_bytes(inventory_payload),
            "source_tree_sha256": inventory["source_snapshot"]["tree_sha256"],
            "authorized_manifest_sha256": inventory["freeze_identity"]["authorized_manifest_sha256"],
        },
        "attempt_counts": {
            "total_retained_attempts": total_attempts,
            "rejected_attempts": rejected_attempts,
            "selected_by_attempt_index": attempt_histogram,
            "attempts_to_success_histogram": attempt_histogram,
            "rejected_by_stage": dict(sorted(rejected_by_stage.items())),
            "rejected_by_class": dict(sorted(rejected_by_class.items())),
            "rejected_by_code": dict(sorted(rejected_by_code.items())),
            "source_terminal_unreadable": source_terminal_unreadable,
            "rejected_by_source_terminal_stage": dict(sorted(rejected_by_source_terminal_stage.items())),
            "rejected_by_source_terminal_class": dict(sorted(rejected_by_source_terminal_class.items())),
            "rejected_by_source_terminal_code": dict(sorted(rejected_by_source_terminal_code.items())),
            "selected_by_origin": selected_by_origin,
            "by_run": {
                run: {
                    **summary,
                    "rejected_by_stage": dict(sorted(summary["rejected_by_stage"].items())),
                    "rejected_by_class": dict(sorted(summary["rejected_by_class"].items())),
                    "rejected_by_code": dict(sorted(summary["rejected_by_code"].items())),
                    "rejected_by_source_terminal_stage": dict(sorted(summary["rejected_by_source_terminal_stage"].items())),
                    "rejected_by_source_terminal_class": dict(sorted(summary["rejected_by_source_terminal_class"].items())),
                    "rejected_by_source_terminal_code": dict(sorted(summary["rejected_by_source_terminal_code"].items())),
                }
                for run, summary in per_run.items()
            },
        },
        "selected_view": {
            "root_path": _relative(project_directory, view_root),
            "source_snapshot": view_snapshot,
            "cells": view_cells,
        },
        "selections": records,
    }
    receipt = validate_quota_complete(receipt)
    for run in RUN_INDICES:
        run_directory = root / "selected-view" / f"run-{run}"
        if not run_directory.is_dir() or {item.name for item in run_directory.iterdir()} != set(METHOD_IDS):
            raise QuotaExecutionError("quota_selected_view_not_exact_150")
        for case in cases:
            names = {item.name for item in (run_directory / case.method_id).iterdir()}
            if names != set(_RAW_COPY_NAMES) | {
                "run.claim.json", "status.json", "selection.json", "selected-attempt.json",
                "extraction.result.json", "regeneration.result.json",
            }:
                raise QuotaExecutionError("quota_selected_view_file_set_invalid")
    if (root / "quota-blocked.json").exists():
        raise QuotaExecutionError("quota_blocked_cannot_publish_complete")
    lock_guard()
    _write_or_verify(root / "quota-complete.json", receipt, "quota_existing_complete_receipt_mismatch")
    _verify_complete_receipt_physical(
        project_directory=project_directory,
        root=root,
        protocol_sha256=protocol_sha256,
        cases=cases,
        receipt=receipt,
    )
    return receipt


def publish_quota_complete(
    *,
    project_directory: Path,
    artifact_root: Path,
    protocol_sha256: str,
    cases: Sequence[StudyCase],
    legacy_inventory_path: Path,
    freeze_manifest_path: Path,
    freeze_record_path: Path,
) -> dict[str, Any]:
    """Publish under the same exclusive lock used by the retry scheduler."""

    authorization = verify_freeze_authorization(
        project_directory=project_directory,
        manifest_path=freeze_manifest_path,
        freeze_record_path=freeze_record_path,
    )
    if authorization.manifest_sha256 != protocol_sha256:
        raise QuotaExecutionError("quota_v06_authorization_digest_mismatch")
    verify_v06_generation_scope(
        project_directory=project_directory,
        manifest_path=freeze_manifest_path,
        freeze_record_path=freeze_record_path,
        legacy_inventory_path=legacy_inventory_path,
    )
    root = quota_root(artifact_root, protocol_sha256)
    _assert_no_symlink_ancestors(project_directory, root, leaf_kind="directory")
    root_metadata = root.lstat()
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    lock_descriptor = _open_lock(root / "quota-execution.lock", create=False)
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise QuotaExecutionError("quota_execution_already_running") from exc
        _verify_scheduler_lock_binding(
            root=root, descriptor=lock_descriptor, root_identity=root_identity
        )
        _assert_no_symlink_ancestors(project_directory, root, leaf_kind="directory")
        verify_v06_generation_scope(
            project_directory=project_directory,
            manifest_path=freeze_manifest_path,
            freeze_record_path=freeze_record_path,
            legacy_inventory_path=legacy_inventory_path,
        )
        return _publish_quota_complete_locked(
            project_directory=project_directory,
            artifact_root=artifact_root,
            protocol_sha256=protocol_sha256,
            cases=cases,
            legacy_inventory_path=legacy_inventory_path,
            freeze_manifest_path=freeze_manifest_path,
            freeze_record_path=freeze_record_path,
            lock_guard=lambda: _verify_scheduler_lock_binding(
                root=root,
                descriptor=lock_descriptor,
                root_identity=root_identity,
            ),
        )
    finally:
        os.close(lock_descriptor)
