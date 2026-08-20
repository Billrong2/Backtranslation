"""Outcome-blind first-valid quota and provenance artifacts.

This module is deliberately standalone: it imports no provider, scoring,
complexity, or human-outcome code.  It defines the immutable JSON contracts
used to retain every generation attempt while selecting only the first attempt
that satisfies a pinned structural-validity predicate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict


RUN_INDICES = (0, 1, 2)
METHOD_IDS = tuple(f"tse-{index:03d}" for index in range(1, 51))
EXPECTED_CELL_COUNT = 150
MAX_ATTEMPTS_PER_CELL = 10
PREDICATE_ID = "first-valid-roundtrip-v1"

SOURCE_TREE_SCHEMA = "backtranslation.quota-source-tree.v1"
ATTEMPT_ELIGIBILITY_SCHEMA = "backtranslation.attempt-eligibility.v1"
LEGACY_INVENTORY_SCHEMA = "backtranslation.legacy-attempt-inventory.v1"
SELECTED_ATTEMPT_SCHEMA = "backtranslation.first-valid-selected-attempt.v1"
QUOTA_COMPLETE_SCHEMA = "backtranslation.quota-complete.v1"
QUOTA_BLOCKED_SCHEMA = "backtranslation.quota-blocked.v1"
RUN_BARRIER_SCHEMA = "backtranslation.run-barrier-witness.v1"

BLOCK_REASONS = (
    "attempt_cap_exhausted",
    "provenance_failure",
    "java_infrastructure_failure",
)
# A mixed blocked receipt has one deterministic headline reason.  Provenance
# takes precedence because it invalidates the audit trail itself; an
# infrastructure failure precedes ordinary, fully reconstructed cap exhaustion.
BLOCK_REASON_PRIORITY = (
    "provenance_failure",
    "java_infrastructure_failure",
    "attempt_cap_exhausted",
)

# These convenience artifacts may describe a process result, but neither their
# presence nor their bytes are allowed to change first-valid eligibility.  Raw
# claims, sanitized provider events, and exact stage outputs remain evidence.
DESCRIPTIVE_ATTEMPT_FILES = frozenset(
    {"status.json", "extraction.result.json", "regeneration.result.json"}
)

SOURCE_KINDS = ("legacy-v0.5", "v0.6-retry")
CHECK_NAMES = (
    "provider_extraction_completed",
    "extraction_contract_valid",
    "provider_regeneration_completed",
    "regeneration_contract_valid",
    "java_structurally_valid",
    "terminal_success",
    "cell_identity_valid",
    "artifact_hashes_valid",
    "request_reconstruction_valid",
)
REJECTION_CODE_BY_CHECK = {
    name: f"check_failed_{name}" for name in CHECK_NAMES
}
FAILURE_SEMANTICS = {
    "provider_extraction_completed": ("extraction_provider", "provider"),
    "extraction_contract_valid": ("extraction_contract", "contract"),
    "provider_regeneration_completed": ("regeneration_provider", "provider"),
    "regeneration_contract_valid": ("regeneration_contract", "contract"),
    "java_structurally_valid": ("java_structure", "structural"),
    "cell_identity_valid": ("identity", "provenance"),
    "artifact_hashes_valid": ("artifact_hashes", "provenance"),
    "request_reconstruction_valid": ("request_reconstruction", "provenance"),
}
PRIMARY_FAILURE_ORDER = (
    "cell_identity_valid",
    "artifact_hashes_valid",
    "request_reconstruction_valid",
    "provider_extraction_completed",
    "extraction_contract_valid",
    "provider_regeneration_completed",
    "regeneration_contract_valid",
    "java_structurally_valid",
)
_EXCLUDED_SELECTION_INPUTS = (
    "ruby_score",
    "codebert_score",
    "rouge_score",
    "bleu_score",
    "direction_complexity",
    "actual_understandability",
    "perceived_understandability",
    "build_status",
    "semantic_plausibility",
    "local_terminal_status",
    "local_status_file_presence",
)


def _selection_policy() -> dict[str, Any]:
    return {
        "policy_id": PREDICATE_ID,
        "terminal_success_definition": "derived-raw-whole-pair-v1",
        "local_terminal_status_effect": "descriptive-only",
        "retry_scope": "whole_roundtrip",
        "maximum_attempts_per_cell": MAX_ATTEMPTS_PER_CELL,
        "stop_after_first_valid": True,
        "later_attempts_prohibited": True,
        "selection_inputs": list(CHECK_NAMES),
        "excluded_inputs": list(_EXCLUDED_SELECTION_INPUTS),
    }


# A convenient serialization template.  Validators reconstruct the policy from
# immutable tuples rather than trusting this caller-visible mutable object.
SELECTION_POLICY = _selection_policy()

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}\Z")
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,119}\Z")
_UTC_MILLISECONDS = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z"
)
_FORBIDDEN_FAILURE_CODE_TOKENS = {
    "au",
    "pbu",
    "ruby",
    "codebert",
    "rouge",
    "bleu",
    "score",
    "complexity",
    "understandability",
    "semantic",
    "plausibility",
    "build",
}
SOURCE_TERMINAL_STAGES = (
    "extraction_api",
    "extraction_parse",
    "extraction_schema",
    "regeneration_api",
    "regeneration_parse",
    "regeneration_schema",
    "infrastructure",
    "terminal",
)
SOURCE_TERMINAL_CLASSES = (
    "provider",
    "parse",
    "schema",
    "unexpected_runtime",
    "operational",
    "structural",
)
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TREE_BYTES = 1024 * 1024 * 1024
_MAX_TREE_FILES = 100_000


class Cell(TypedDict):
    run_index: int
    method_id: str


class FileRecord(TypedDict):
    path: str
    bytes: int
    sha256: str


class SourceTreeSnapshot(TypedDict):
    schema_version: str
    directories: list[str]
    files: list[FileRecord]
    tree_sha256: str


class Origin(TypedDict):
    source_kind: str
    protocol_sha256: str
    source_root_path: str
    attempt_path: str
    source_tree_sha256: str


class JavaStructuralValidation(TypedDict):
    performed: bool
    analyzer_id: str | None
    analyzer_version: str | None
    validation_policy_sha256: str | None
    artifact_path: str | None
    artifact_sha256: str | None
    structurally_valid: bool


class AttemptEligibility(TypedDict):
    schema_version: str
    cell: Cell
    attempt_index: int
    origin: Origin
    run_barrier_witness: RunBarrierWitness | None
    run_barrier_witness_sha256: str | None
    source_snapshot: SourceTreeSnapshot
    predicate: dict[str, Any]
    checks: dict[str, bool]
    java_validation: JavaStructuralValidation
    eligible: bool
    rejection_codes: list[str]
    failure: Failure | None


class Failure(TypedDict):
    primary_check: str
    stage: str
    failure_class: str
    code: str
    retryable: bool
    disposition: str
    source_terminal_stage: str | None
    source_terminal_class: str | None
    source_terminal_code: str | None


class BarrierSelection(TypedDict):
    cell: Cell
    selection_sha256: str


class RunBarrierWitness(TypedDict):
    schema_version: str
    protocol_sha256: str
    policy_id: str
    target_run_index: int
    predecessor_run_index: int | None
    predecessor_selection_count: int
    predecessor_selections: list[BarrierSelection]


class LegacyAttemptRecord(TypedDict):
    cell: Cell
    eligibility_sha256: str
    eligibility: AttemptEligibility


class LegacyAttemptInventory(TypedDict):
    schema_version: str
    inventoried_at_utc: str
    origin: dict[str, Any]
    freeze_identity: dict[str, Any]
    source_snapshot: SourceTreeSnapshot
    cells: list[LegacyAttemptRecord]


class EligibilityRecord(TypedDict):
    eligibility_sha256: str
    eligibility: AttemptEligibility


class FirstValidSelectedAttempt(TypedDict):
    schema_version: str
    protocol_sha256: str
    cell: Cell
    policy: dict[str, Any]
    attempts: list[EligibilityRecord]
    selected_attempt_index: int
    selected_eligibility_sha256: str
    selected_origin: Origin


class SelectionRecord(TypedDict):
    selection_sha256: str
    selection: FirstValidSelectedAttempt


class QuotaCompleteReceipt(TypedDict):
    schema_version: str
    protocol_sha256: str
    policy: dict[str, Any]
    counts: dict[str, Any]
    legacy_inventory_identity: dict[str, Any]
    attempt_counts: dict[str, Any]
    selected_view: dict[str, Any]
    selections: list[SelectionRecord]


class BlockedCell(TypedDict):
    cell: Cell
    reason: str
    evidence_code: str
    final_attempt_index: int
    eligibility_sha256: str | None
    eligibility: AttemptEligibility | None
    source_tree_sha256: str


class QuotaBlockedReceipt(TypedDict):
    schema_version: str
    protocol_sha256: str
    policy: dict[str, Any]
    status: str
    blocked_at_utc: str
    primary_reason: str
    blocked_cells: list[BlockedCell]
    counts: dict[str, Any]


class QuotaArtifactError(RuntimeError):
    """Stable, content-free failure for a quota/provenance invariant."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical artifact encoding, including the repository-standard newline."""

    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QuotaArtifactError("quota_not_canonical_json") from exc


def document_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def load_canonical_json(path: Path) -> dict[str, Any]:
    """Read a regular, non-symlink file and require exact canonical bytes."""

    payload = _read_regular_file(path)
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QuotaArtifactError("quota_json_invalid") from exc
    if type(value) is not dict:
        raise QuotaArtifactError("quota_json_not_object")
    if payload != canonical_json_bytes(value):
        raise QuotaArtifactError("quota_json_not_canonical")
    return value


def _exact_object(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise QuotaArtifactError(code)
    return value


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise QuotaArtifactError(code)
    return value


def _stable_id(value: Any, code: str) -> str:
    if not isinstance(value, str) or _STABLE_ID.fullmatch(value) is None:
        raise QuotaArtifactError(code)
    return value


def _safe_path(value: Any, code: str) -> str:
    if not isinstance(value, str):
        raise QuotaArtifactError(code)
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
    ):
        raise QuotaArtifactError(code)
    return value


def _is_beneath(path: str, root: str) -> bool:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    return len(path_parts) > len(root_parts) and path_parts[: len(root_parts)] == root_parts


def _cell(value: Any) -> Cell:
    item = _exact_object(value, {"run_index", "method_id"}, "quota_cell_invalid")
    run_index = item["run_index"]
    method_id = item["method_id"]
    if isinstance(run_index, bool) or run_index not in RUN_INDICES:
        raise QuotaArtifactError("quota_run_index_invalid")
    if method_id not in METHOD_IDS:
        raise QuotaArtifactError("quota_method_id_invalid")
    return {"run_index": run_index, "method_id": method_id}


def _cell_key(value: Mapping[str, Any]) -> tuple[int, str]:
    return int(value["run_index"]), str(value["method_id"])


def _attempt_index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ATTEMPTS_PER_CELL:
        raise QuotaArtifactError("quota_attempt_index_invalid")
    return value


def validate_run_barrier_witness(value: Any) -> RunBarrierWitness:
    """Validate the exact outcome-blind predecessor-success barrier."""

    item = _exact_object(
        value,
        {
            "schema_version",
            "protocol_sha256",
            "policy_id",
            "target_run_index",
            "predecessor_run_index",
            "predecessor_selection_count",
            "predecessor_selections",
        },
        "quota_run_barrier_invalid",
    )
    if item["schema_version"] != RUN_BARRIER_SCHEMA:
        raise QuotaArtifactError("quota_run_barrier_schema_invalid")
    protocol_sha256 = _sha256(
        item["protocol_sha256"], "quota_run_barrier_protocol_hash_invalid"
    )
    if item["policy_id"] != PREDICATE_ID:
        raise QuotaArtifactError("quota_run_barrier_policy_invalid")
    target_run = item["target_run_index"]
    if isinstance(target_run, bool) or target_run not in RUN_INDICES:
        raise QuotaArtifactError("quota_run_barrier_target_run_invalid")
    expected_predecessor = None if target_run == 0 else target_run - 1
    if item["predecessor_run_index"] != expected_predecessor:
        raise QuotaArtifactError("quota_run_barrier_predecessor_run_invalid")
    raw_selections = item["predecessor_selections"]
    expected_count = 0 if target_run == 0 else len(METHOD_IDS)
    if (
        isinstance(item["predecessor_selection_count"], bool)
        or item["predecessor_selection_count"] != expected_count
        or not isinstance(raw_selections, list)
        or len(raw_selections) != expected_count
    ):
        raise QuotaArtifactError("quota_run_barrier_selection_count_invalid")
    selections: list[BarrierSelection] = []
    for raw in raw_selections:
        record = _exact_object(
            raw, {"cell", "selection_sha256"}, "quota_run_barrier_selection_invalid"
        )
        cell = _cell(record["cell"])
        if cell["run_index"] != expected_predecessor:
            raise QuotaArtifactError("quota_run_barrier_selection_run_invalid")
        selections.append(
            {
                "cell": cell,
                "selection_sha256": _sha256(
                    record["selection_sha256"],
                    "quota_run_barrier_selection_hash_invalid",
                ),
            }
        )
    expected_cells = (
        []
        if expected_predecessor is None
        else [
            {"run_index": expected_predecessor, "method_id": method_id}
            for method_id in METHOD_IDS
        ]
    )
    if [record["cell"] for record in selections] != expected_cells:
        raise QuotaArtifactError("quota_run_barrier_selections_not_canonical")
    return {
        "schema_version": RUN_BARRIER_SCHEMA,
        "protocol_sha256": protocol_sha256,
        "policy_id": PREDICATE_ID,
        "target_run_index": target_run,
        "predecessor_run_index": expected_predecessor,
        "predecessor_selection_count": expected_count,
        "predecessor_selections": selections,
    }


def run_barrier_witness_document(
    *,
    protocol_sha256: str,
    target_run_index: int,
    predecessor_selections: Sequence[Mapping[str, Any]] = (),
) -> RunBarrierWitness:
    """Build the exact run barrier; caller order is normalized by cell."""

    raw = [dict(record) for record in predecessor_selections]
    raw.sort(
        key=lambda record: (
            record.get("cell", {}).get("run_index", -1)
            if isinstance(record.get("cell"), Mapping)
            else -1,
            record.get("cell", {}).get("method_id", "")
            if isinstance(record.get("cell"), Mapping)
            else "",
        )
    )
    return validate_run_barrier_witness(
        {
            "schema_version": RUN_BARRIER_SCHEMA,
            "protocol_sha256": protocol_sha256,
            "policy_id": PREDICATE_ID,
            "target_run_index": target_run_index,
            "predecessor_run_index": None
            if target_run_index == 0
            else target_run_index - 1,
            "predecessor_selection_count": len(raw),
            "predecessor_selections": raw,
        }
    )


def run_barrier_witness_sha256(value: Any) -> str:
    return document_sha256(validate_run_barrier_witness(value))


def _utc_milliseconds(value: Any, code: str) -> str:
    if not isinstance(value, str) or _UTC_MILLISECONDS.fullmatch(value) is None:
        raise QuotaArtifactError(code)
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise QuotaArtifactError(code) from exc
    return value


def _file_identity(value: Any, code: str) -> FileRecord:
    record = _exact_object(value, {"path", "bytes", "sha256"}, code)
    path = _safe_path(record["path"], f"{code}_path")
    size = record["bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_FILE_BYTES:
        raise QuotaArtifactError(f"{code}_bytes")
    digest = _sha256(record["sha256"], f"{code}_sha256")
    return {"path": path, "bytes": size, "sha256": digest}


def _read_regular_file(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise QuotaArtifactError("quota_source_missing") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise QuotaArtifactError("quota_source_not_regular")
    if before.st_size > _MAX_FILE_BYTES:
        raise QuotaArtifactError("quota_source_file_too_large")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QuotaArtifactError("quota_source_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = _MAX_FILE_BYTES + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_nlink)
        for item in (before, opened, after)
    }
    if (
        len(identities) != 1
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or len(payload) != after.st_size
        or len(payload) > _MAX_FILE_BYTES
    ):
        raise QuotaArtifactError("quota_source_changed_during_read")
    return payload


def _directory_entries(
    directory: Path,
) -> tuple[tuple[str, int, int, int, int], ...]:
    try:
        entries = []
        with os.scandir(directory) as stream:
            for entry in stream:
                metadata = entry.stat(follow_symlinks=False)
                entries.append(
                    (
                        entry.name,
                        stat.S_IFMT(metadata.st_mode),
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_nlink,
                    )
                )
        return tuple(sorted(entries))
    except OSError as exc:
        raise QuotaArtifactError("quota_source_tree_scan_failed") from exc


def snapshot_source_tree(root: Path) -> SourceTreeSnapshot:
    """Hash every regular file beneath ``root`` while refusing symlink traversal."""

    try:
        root_before = root.lstat()
    except OSError as exc:
        raise QuotaArtifactError("quota_source_root_missing") from exc
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        raise QuotaArtifactError("quota_source_root_not_directory")

    directories: list[str] = []
    files: list[FileRecord] = []
    total_bytes = 0

    def visit(directory: Path, prefix: PurePosixPath | None) -> None:
        nonlocal total_bytes
        before = _directory_entries(directory)
        for name, kind, _device, _inode, _links in before:
            if name in {"", ".", ".."} or "/" in name or "\\" in name:
                raise QuotaArtifactError("quota_source_name_unsafe")
            path = directory / name
            relative = PurePosixPath(name) if prefix is None else prefix / name
            if kind == stat.S_IFLNK:
                raise QuotaArtifactError("quota_source_symlink_prohibited")
            if kind == stat.S_IFDIR:
                directories.append(relative.as_posix())
                visit(path, relative)
            elif kind == stat.S_IFREG:
                payload = _read_regular_file(path)
                total_bytes += len(payload)
                if len(files) >= _MAX_TREE_FILES:
                    raise QuotaArtifactError("quota_source_tree_too_many_files")
                if total_bytes > _MAX_TREE_BYTES:
                    raise QuotaArtifactError("quota_source_tree_too_large")
                files.append(
                    {
                        "path": relative.as_posix(),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            else:
                raise QuotaArtifactError("quota_source_special_file_prohibited")
        if before != _directory_entries(directory):
            raise QuotaArtifactError("quota_source_tree_changed_during_scan")

    visit(root, None)
    root_after = root.lstat()
    if (
        root_before.st_dev,
        root_before.st_ino,
        root_before.st_mtime_ns,
    ) != (
        root_after.st_dev,
        root_after.st_ino,
        root_after.st_mtime_ns,
    ):
        raise QuotaArtifactError("quota_source_tree_changed_during_scan")
    body = {
        "schema_version": SOURCE_TREE_SCHEMA,
        "directories": sorted(directories),
        "files": sorted(files, key=lambda item: item["path"]),
    }
    return {**body, "tree_sha256": document_sha256(body)}


def snapshot_selection_evidence_tree(root: Path) -> SourceTreeSnapshot:
    """Hash only raw selection evidence without reading descriptive leaves."""

    try:
        root_before = root.lstat()
    except OSError as exc:
        raise QuotaArtifactError("quota_source_root_missing") from exc
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        raise QuotaArtifactError("quota_source_root_not_directory")
    directories: list[str] = []
    files: list[FileRecord] = []
    total_bytes = 0

    def visit(directory: Path, prefix: PurePosixPath | None) -> None:
        nonlocal total_bytes
        before = _directory_entries(directory)
        for name, kind, _device, _inode, _links in before:
            if name in {"", ".", ".."} or "/" in name or "\\" in name:
                raise QuotaArtifactError("quota_source_name_unsafe")
            if name in DESCRIPTIVE_ATTEMPT_FILES:
                continue
            path = directory / name
            relative = PurePosixPath(name) if prefix is None else prefix / name
            if kind == stat.S_IFLNK:
                raise QuotaArtifactError("quota_source_symlink_prohibited")
            if kind == stat.S_IFDIR:
                directories.append(relative.as_posix())
                visit(path, relative)
            elif kind == stat.S_IFREG:
                payload = _read_regular_file(path)
                total_bytes += len(payload)
                if len(files) >= _MAX_TREE_FILES:
                    raise QuotaArtifactError("quota_source_tree_too_many_files")
                if total_bytes > _MAX_TREE_BYTES:
                    raise QuotaArtifactError("quota_source_tree_too_large")
                files.append(
                    {
                        "path": relative.as_posix(),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            else:
                raise QuotaArtifactError("quota_source_special_file_prohibited")
        evidence_before = tuple(
            item for item in before if item[0] not in DESCRIPTIVE_ATTEMPT_FILES
        )
        evidence_after = tuple(
            item
            for item in _directory_entries(directory)
            if item[0] not in DESCRIPTIVE_ATTEMPT_FILES
        )
        if evidence_before != evidence_after:
            raise QuotaArtifactError("quota_source_tree_changed_during_scan")

    visit(root, None)
    root_after = root.lstat()
    if (root_before.st_dev, root_before.st_ino) != (
        root_after.st_dev,
        root_after.st_ino,
    ):
        raise QuotaArtifactError("quota_source_tree_changed_during_scan")
    body = {
        "schema_version": SOURCE_TREE_SCHEMA,
        "directories": sorted(directories),
        "files": sorted(files, key=lambda item: item["path"]),
    }
    return {**body, "tree_sha256": document_sha256(body)}


def validate_source_tree_snapshot(value: Any) -> SourceTreeSnapshot:
    item = _exact_object(
        value,
        {"schema_version", "directories", "files", "tree_sha256"},
        "quota_source_snapshot_invalid",
    )
    if item["schema_version"] != SOURCE_TREE_SCHEMA:
        raise QuotaArtifactError("quota_source_snapshot_schema_invalid")
    raw_directories = item["directories"]
    raw_files = item["files"]
    if not isinstance(raw_directories, list) or not isinstance(raw_files, list):
        raise QuotaArtifactError("quota_source_snapshot_entries_invalid")
    directories = [
        _safe_path(path, "quota_source_directory_path_invalid") for path in raw_directories
    ]
    if directories != sorted(set(directories)):
        raise QuotaArtifactError("quota_source_directories_not_canonical")
    files: list[FileRecord] = []
    paths: list[str] = []
    total_bytes = 0
    for value_record in raw_files:
        record = _exact_object(
            value_record, {"path", "bytes", "sha256"}, "quota_source_file_record_invalid"
        )
        path = _safe_path(record["path"], "quota_source_file_path_invalid")
        size = record["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_FILE_BYTES:
            raise QuotaArtifactError("quota_source_file_size_invalid")
        digest = _sha256(record["sha256"], "quota_source_file_hash_invalid")
        total_bytes += size
        paths.append(path)
        files.append({"path": path, "bytes": size, "sha256": digest})
    if paths != sorted(set(paths)):
        raise QuotaArtifactError("quota_source_files_not_canonical")
    if len(files) > _MAX_TREE_FILES or total_bytes > _MAX_TREE_BYTES:
        raise QuotaArtifactError("quota_source_snapshot_too_large")
    if set(directories) & set(paths):
        raise QuotaArtifactError("quota_source_snapshot_path_collision")
    body = {
        "schema_version": SOURCE_TREE_SCHEMA,
        "directories": directories,
        "files": files,
    }
    observed = _sha256(item["tree_sha256"], "quota_source_tree_hash_invalid")
    if observed != document_sha256(body):
        raise QuotaArtifactError("quota_source_tree_hash_mismatch")
    return {**body, "tree_sha256": observed}


def verify_source_tree_snapshot(root: Path, value: Any) -> str:
    expected = validate_source_tree_snapshot(value)
    observed = snapshot_source_tree(root)
    if observed != expected:
        raise QuotaArtifactError("quota_source_tree_snapshot_mismatch")
    return observed["tree_sha256"]


def selection_evidence_snapshot(value: Any) -> SourceTreeSnapshot:
    """Project a full attempt snapshot onto the raw selection-evidence files."""

    snapshot = validate_source_tree_snapshot(value)
    files = [
        record
        for record in snapshot["files"]
        if PurePosixPath(record["path"]).name not in DESCRIPTIVE_ATTEMPT_FILES
    ]
    body = {
        "schema_version": SOURCE_TREE_SCHEMA,
        "directories": snapshot["directories"],
        "files": files,
    }
    return {**body, "tree_sha256": document_sha256(body)}


def verify_selection_evidence_snapshot(root: Path, value: Any) -> str:
    """Verify a projected eligibility snapshot against the current raw tree."""

    expected = validate_source_tree_snapshot(value)
    if any(
        PurePosixPath(record["path"]).name in DESCRIPTIVE_ATTEMPT_FILES
        for record in expected["files"]
    ):
        raise QuotaArtifactError("quota_selection_snapshot_contains_descriptive_file")
    observed = snapshot_selection_evidence_tree(root)
    if observed != expected:
        raise QuotaArtifactError("quota_selection_evidence_snapshot_mismatch")
    return observed["tree_sha256"]


def source_subtree_snapshot(
    value: Any, relative_directory: str
) -> SourceTreeSnapshot:
    """Project a validated snapshot onto one directory, stripping its prefix."""

    snapshot = validate_source_tree_snapshot(value)
    prefix = _safe_path(relative_directory, "quota_source_subtree_path_invalid")
    prefix_with_separator = prefix + "/"
    directories = [
        path[len(prefix_with_separator) :]
        for path in snapshot["directories"]
        if path.startswith(prefix_with_separator)
    ]
    files = [
        {**record, "path": record["path"][len(prefix_with_separator) :]}
        for record in snapshot["files"]
        if record["path"].startswith(prefix_with_separator)
    ]
    body = {
        "schema_version": SOURCE_TREE_SCHEMA,
        "directories": directories,
        "files": files,
    }
    return {**body, "tree_sha256": document_sha256(body)}


def _origin(value: Any) -> Origin:
    item = _exact_object(
        value,
        {
            "source_kind",
            "protocol_sha256",
            "source_root_path",
            "attempt_path",
            "source_tree_sha256",
        },
        "quota_origin_invalid",
    )
    if item["source_kind"] not in SOURCE_KINDS:
        raise QuotaArtifactError("quota_origin_kind_invalid")
    root = _safe_path(item["source_root_path"], "quota_origin_root_path_invalid")
    attempt = _safe_path(item["attempt_path"], "quota_origin_attempt_path_invalid")
    if not _is_beneath(attempt, root):
        raise QuotaArtifactError("quota_origin_attempt_not_beneath_root")
    return {
        "source_kind": item["source_kind"],
        "protocol_sha256": _sha256(item["protocol_sha256"], "quota_origin_protocol_invalid"),
        "source_root_path": root,
        "attempt_path": attempt,
        "source_tree_sha256": _sha256(
            item["source_tree_sha256"], "quota_origin_tree_hash_invalid"
        ),
    }


def _java_validation(value: Any) -> JavaStructuralValidation:
    item = _exact_object(
        value,
        {
            "performed",
            "analyzer_id",
            "analyzer_version",
            "validation_policy_sha256",
            "artifact_path",
            "artifact_sha256",
            "structurally_valid",
        },
        "quota_java_validation_invalid",
    )
    if type(item["performed"]) is not bool or type(item["structurally_valid"]) is not bool:
        raise QuotaArtifactError("quota_java_validation_boolean_invalid")
    performed = item["performed"]
    detail_keys = (
        "analyzer_id",
        "analyzer_version",
        "validation_policy_sha256",
        "artifact_path",
        "artifact_sha256",
    )
    if not performed:
        if item["structurally_valid"] or any(item[key] is not None for key in detail_keys):
            raise QuotaArtifactError("quota_java_validation_unperformed_details_invalid")
        return dict(item)  # type: ignore[return-value]
    analyzer_id = _stable_id(item["analyzer_id"], "quota_java_analyzer_id_invalid")
    analyzer_version = _stable_id(
        item["analyzer_version"], "quota_java_analyzer_version_invalid"
    )
    policy_hash = _sha256(
        item["validation_policy_sha256"], "quota_java_policy_hash_invalid"
    )
    artifact_path = _safe_path(item["artifact_path"], "quota_java_artifact_path_invalid")
    artifact_hash = _sha256(item["artifact_sha256"], "quota_java_artifact_hash_invalid")
    return {
        "performed": True,
        "analyzer_id": analyzer_id,
        "analyzer_version": analyzer_version,
        "validation_policy_sha256": policy_hash,
        "artifact_path": artifact_path,
        "artifact_sha256": artifact_hash,
        "structurally_valid": item["structurally_valid"],
    }


def validate_attempt_eligibility(value: Any) -> AttemptEligibility:
    """Validate eligibility from raw-pair evidence, never local process status.

    ``terminal_success`` is intentionally redundant: it is the conjunction of
    both provider completions, both exact schema validations, and structural
    Java validity.  A missing or failed convenience ``status.json`` after that
    evidence exists cannot make the generation retryable.
    """
    item = _exact_object(
        value,
        {
            "schema_version",
            "cell",
            "attempt_index",
            "origin",
            "run_barrier_witness",
            "run_barrier_witness_sha256",
            "source_snapshot",
            "predicate",
            "checks",
            "java_validation",
            "eligible",
            "rejection_codes",
            "failure",
        },
        "quota_attempt_eligibility_invalid",
    )
    if item["schema_version"] != ATTEMPT_ELIGIBILITY_SCHEMA:
        raise QuotaArtifactError("quota_attempt_eligibility_schema_invalid")
    cell = _cell(item["cell"])
    attempt_index = _attempt_index(item["attempt_index"])
    origin = _origin(item["origin"])
    raw_barrier = item["run_barrier_witness"]
    raw_barrier_hash = item["run_barrier_witness_sha256"]
    if origin["source_kind"] == "legacy-v0.5":
        if raw_barrier is not None or raw_barrier_hash is not None:
            raise QuotaArtifactError("quota_legacy_attempt_barrier_present")
        run_barrier = None
        run_barrier_hash = None
    else:
        if raw_barrier is None or raw_barrier_hash is None:
            raise QuotaArtifactError("quota_native_attempt_barrier_missing")
        run_barrier = validate_run_barrier_witness(raw_barrier)
        run_barrier_hash = _sha256(
            raw_barrier_hash, "quota_native_attempt_barrier_hash_invalid"
        )
        if run_barrier_hash != document_sha256(run_barrier):
            raise QuotaArtifactError("quota_native_attempt_barrier_hash_mismatch")
        if (
            run_barrier["protocol_sha256"] != origin["protocol_sha256"]
            or run_barrier["target_run_index"] != cell["run_index"]
        ):
            raise QuotaArtifactError("quota_native_attempt_barrier_identity_mismatch")
    snapshot = validate_source_tree_snapshot(item["source_snapshot"])
    if any(
        PurePosixPath(record["path"]).name in DESCRIPTIVE_ATTEMPT_FILES
        for record in snapshot["files"]
    ):
        raise QuotaArtifactError("quota_attempt_snapshot_contains_descriptive_file")
    if origin["source_tree_sha256"] != snapshot["tree_sha256"]:
        raise QuotaArtifactError("quota_attempt_origin_snapshot_hash_mismatch")
    if item["predicate"] != _selection_policy():
        raise QuotaArtifactError("quota_attempt_predicate_invalid")
    checks_item = _exact_object(item["checks"], set(CHECK_NAMES), "quota_attempt_checks_invalid")
    if any(type(checks_item[name]) is not bool for name in CHECK_NAMES):
        raise QuotaArtifactError("quota_attempt_check_boolean_invalid")
    checks = {name: checks_item[name] for name in CHECK_NAMES}
    if checks["extraction_contract_valid"] and not checks["provider_extraction_completed"]:
        raise QuotaArtifactError("quota_attempt_extraction_stage_inconsistent")
    if checks["provider_regeneration_completed"] and not checks["extraction_contract_valid"]:
        raise QuotaArtifactError("quota_attempt_regeneration_stage_inconsistent")
    if checks["regeneration_contract_valid"] and not checks["provider_regeneration_completed"]:
        raise QuotaArtifactError("quota_attempt_regeneration_contract_inconsistent")
    java_validation = _java_validation(item["java_validation"])
    if java_validation["performed"] != checks["regeneration_contract_valid"]:
        raise QuotaArtifactError("quota_attempt_java_performed_inconsistent")
    if java_validation["structurally_valid"] != checks["java_structurally_valid"]:
        raise QuotaArtifactError("quota_attempt_java_result_inconsistent")
    raw_whole_pair_success = all(
        checks[name]
        for name in (
            "provider_extraction_completed",
            "extraction_contract_valid",
            "provider_regeneration_completed",
            "regeneration_contract_valid",
            "java_structurally_valid",
        )
    )
    if checks["terminal_success"] is not raw_whole_pair_success:
        raise QuotaArtifactError("quota_attempt_terminal_success_not_derived")
    if java_validation["performed"]:
        matching = [
            record
            for record in snapshot["files"]
            if record["path"] == java_validation["artifact_path"]
        ]
        if len(matching) != 1:
            raise QuotaArtifactError("quota_java_artifact_not_in_snapshot")
        if matching[0]["sha256"] != java_validation["artifact_sha256"]:
            raise QuotaArtifactError("quota_java_artifact_snapshot_hash_mismatch")
    if type(item["eligible"]) is not bool:
        raise QuotaArtifactError("quota_attempt_eligible_invalid")
    eligible = all(checks.values())
    if item["eligible"] != eligible:
        raise QuotaArtifactError("quota_attempt_eligibility_result_inconsistent")
    expected_codes = [REJECTION_CODE_BY_CHECK[name] for name in CHECK_NAMES if not checks[name]]
    if item["rejection_codes"] != expected_codes:
        raise QuotaArtifactError("quota_attempt_rejection_codes_invalid")
    if eligible:
        if item["failure"] is not None:
            raise QuotaArtifactError("quota_attempt_eligible_failure_present")
        failure = None
    else:
        failure_item = _exact_object(
            item["failure"],
            {
                "primary_check",
                "stage",
                "failure_class",
                "code",
                "retryable",
                "disposition",
                "source_terminal_stage",
                "source_terminal_class",
                "source_terminal_code",
            },
            "quota_attempt_failure_invalid",
        )
        # Provenance failures are study blockers and therefore take priority
        # over ordinary retryable stage failures when both are present.
        primary_check = next(name for name in PRIMARY_FAILURE_ORDER if not checks[name])
        stage, failure_class = FAILURE_SEMANTICS[primary_check]
        if failure_item["primary_check"] != primary_check:
            raise QuotaArtifactError("quota_attempt_primary_failure_not_first")
        if failure_item["stage"] != stage:
            raise QuotaArtifactError("quota_attempt_failure_stage_invalid")
        if failure_item["failure_class"] != failure_class:
            raise QuotaArtifactError("quota_attempt_failure_class_invalid")
        code = failure_item["code"]
        if not isinstance(code, str) or _FAILURE_CODE.fullmatch(code) is None:
            raise QuotaArtifactError("quota_attempt_failure_code_invalid")
        if set(code.split("_")) & _FORBIDDEN_FAILURE_CODE_TOKENS:
            raise QuotaArtifactError("quota_attempt_failure_code_uses_excluded_input")
        retryable = failure_class != "provenance"
        disposition = "retry_whole_roundtrip" if retryable else "block_study"
        if failure_item["retryable"] is not retryable:
            raise QuotaArtifactError("quota_attempt_failure_retryability_invalid")
        if failure_item["disposition"] != disposition:
            raise QuotaArtifactError("quota_attempt_failure_disposition_invalid")
        source_terminal_values = (
            failure_item["source_terminal_stage"],
            failure_item["source_terminal_class"],
            failure_item["source_terminal_code"],
        )
        if all(value is None for value in source_terminal_values):
            source_terminal_stage = None
            source_terminal_class = None
            source_terminal_code = None
        elif any(value is None for value in source_terminal_values):
            raise QuotaArtifactError("quota_attempt_source_terminal_partial")
        else:
            source_terminal_stage = source_terminal_values[0]
            source_terminal_class = source_terminal_values[1]
            source_terminal_code = source_terminal_values[2]
            if source_terminal_stage not in SOURCE_TERMINAL_STAGES:
                raise QuotaArtifactError("quota_attempt_source_terminal_stage_invalid")
            if source_terminal_class not in SOURCE_TERMINAL_CLASSES:
                raise QuotaArtifactError("quota_attempt_source_terminal_class_invalid")
            if (
                not isinstance(source_terminal_code, str)
                or _FAILURE_CODE.fullmatch(source_terminal_code) is None
            ):
                raise QuotaArtifactError("quota_attempt_source_terminal_code_invalid")
            if set(source_terminal_code.split("_")) & _FORBIDDEN_FAILURE_CODE_TOKENS:
                raise QuotaArtifactError(
                    "quota_attempt_source_terminal_code_uses_excluded_input"
                )
        failure = {
            "primary_check": primary_check,
            "stage": stage,
            "failure_class": failure_class,
            "code": code,
            "retryable": retryable,
            "disposition": disposition,
            "source_terminal_stage": source_terminal_stage,
            "source_terminal_class": source_terminal_class,
            "source_terminal_code": source_terminal_code,
        }
    return {
        "schema_version": ATTEMPT_ELIGIBILITY_SCHEMA,
        "cell": cell,
        "attempt_index": attempt_index,
        "origin": origin,
        "run_barrier_witness": run_barrier,
        "run_barrier_witness_sha256": run_barrier_hash,
        "source_snapshot": snapshot,
        "predicate": _selection_policy(),
        "checks": checks,
        "java_validation": java_validation,
        "eligible": eligible,
        "rejection_codes": expected_codes,
        "failure": failure,
    }


def validate_legacy_attempt_inventory(value: Any) -> LegacyAttemptInventory:
    item = _exact_object(
        value,
        {
            "schema_version",
            "inventoried_at_utc",
            "origin",
            "freeze_identity",
            "source_snapshot",
            "cells",
        },
        "quota_legacy_inventory_invalid",
    )
    if item["schema_version"] != LEGACY_INVENTORY_SCHEMA:
        raise QuotaArtifactError("quota_legacy_inventory_schema_invalid")
    inventoried_at = _utc_milliseconds(
        item["inventoried_at_utc"], "quota_legacy_inventory_timestamp_invalid"
    )
    origin_item = _exact_object(
        item["origin"],
        {"source_kind", "protocol_sha256", "source_root_path", "source_tree_sha256"},
        "quota_legacy_origin_invalid",
    )
    if origin_item["source_kind"] != "legacy-v0.5":
        raise QuotaArtifactError("quota_legacy_origin_kind_invalid")
    protocol_hash = _sha256(
        origin_item["protocol_sha256"], "quota_legacy_protocol_hash_invalid"
    )
    root_path = _safe_path(origin_item["source_root_path"], "quota_legacy_root_path_invalid")
    snapshot = validate_source_tree_snapshot(item["source_snapshot"])
    tree_hash = _sha256(
        origin_item["source_tree_sha256"], "quota_legacy_tree_hash_invalid"
    )
    if tree_hash != snapshot["tree_sha256"]:
        raise QuotaArtifactError("quota_legacy_origin_snapshot_hash_mismatch")
    freeze_identity_item = _exact_object(
        item["freeze_identity"],
        {
            "authorized_manifest_sha256",
            "static_archive_root_path",
            "static_archive_source_snapshot",
            "static_archive_source_tree_sha256",
            "freeze_manifest",
            "archived_freeze_manifest",
            "execution_schedule",
            "freeze_record_log",
            "archived_freeze_record_log",
        },
        "quota_legacy_freeze_identity_invalid",
    )
    authorized_hash = _sha256(
        freeze_identity_item["authorized_manifest_sha256"],
        "quota_legacy_authorized_manifest_hash_invalid",
    )
    if authorized_hash != protocol_hash:
        raise QuotaArtifactError("quota_legacy_authorized_manifest_mismatch")
    static_archive_root = _safe_path(
        freeze_identity_item["static_archive_root_path"],
        "quota_legacy_static_archive_root_invalid",
    )
    if static_archive_root != "artifacts/provenance/v0.5-static":
        raise QuotaArtifactError("quota_legacy_static_archive_root_mismatch")
    static_archive_snapshot = validate_source_tree_snapshot(
        freeze_identity_item["static_archive_source_snapshot"]
    )
    static_archive_tree_hash = _sha256(
        freeze_identity_item["static_archive_source_tree_sha256"],
        "quota_legacy_static_archive_tree_hash_invalid",
    )
    if static_archive_tree_hash != static_archive_snapshot["tree_sha256"]:
        raise QuotaArtifactError("quota_legacy_static_archive_tree_hash_mismatch")
    freeze_manifest = _file_identity(
        freeze_identity_item["freeze_manifest"], "quota_legacy_freeze_manifest_invalid"
    )
    archived_freeze_manifest = _file_identity(
        freeze_identity_item["archived_freeze_manifest"],
        "quota_legacy_archived_freeze_manifest_invalid",
    )
    execution_schedule = _file_identity(
        freeze_identity_item["execution_schedule"], "quota_legacy_schedule_invalid"
    )
    freeze_record_log = _file_identity(
        freeze_identity_item["freeze_record_log"], "quota_legacy_freeze_record_invalid"
    )
    archived_freeze_record_log = _file_identity(
        freeze_identity_item["archived_freeze_record_log"],
        "quota_legacy_archived_freeze_record_invalid",
    )
    if freeze_manifest["path"] != "protocol/freeze-manifest-v1.json":
        raise QuotaArtifactError("quota_legacy_freeze_manifest_path_mismatch")
    if freeze_record_log["path"] != "protocol/freeze-record.jsonl":
        raise QuotaArtifactError("quota_legacy_freeze_record_path_mismatch")
    if (
        archived_freeze_manifest["path"]
        != f"{static_archive_root}/protocol/freeze-manifest-v1.json"
    ):
        raise QuotaArtifactError("quota_legacy_archived_freeze_manifest_path_mismatch")
    if (
        archived_freeze_record_log["path"]
        != f"{static_archive_root}/protocol/freeze-record.jsonl"
    ):
        raise QuotaArtifactError("quota_legacy_archived_freeze_record_path_mismatch")
    for original, archived, relative_path in (
        (freeze_manifest, archived_freeze_manifest, "protocol/freeze-manifest-v1.json"),
        (freeze_record_log, archived_freeze_record_log, "protocol/freeze-record.jsonl"),
    ):
        if original["bytes"] != archived["bytes"] or original["sha256"] != archived["sha256"]:
            raise QuotaArtifactError("quota_legacy_static_archive_original_mismatch")
        matching_archive_records = [
            record for record in static_archive_snapshot["files"] if record["path"] == relative_path
        ]
        if len(matching_archive_records) != 1:
            raise QuotaArtifactError("quota_legacy_static_archive_file_missing")
        if (
            matching_archive_records[0]["bytes"] != archived["bytes"]
            or matching_archive_records[0]["sha256"] != archived["sha256"]
        ):
            raise QuotaArtifactError("quota_legacy_static_archive_file_mismatch")
    expected_schedule_path = f"{root_path}/schedule.json"
    if execution_schedule["path"] != expected_schedule_path:
        raise QuotaArtifactError("quota_legacy_schedule_path_mismatch")
    root_schedule = [record for record in snapshot["files"] if record["path"] == "schedule.json"]
    if len(root_schedule) != 1:
        raise QuotaArtifactError("quota_legacy_schedule_not_in_snapshot")
    if (
        root_schedule[0]["bytes"] != execution_schedule["bytes"]
        or root_schedule[0]["sha256"] != execution_schedule["sha256"]
    ):
        raise QuotaArtifactError("quota_legacy_schedule_snapshot_mismatch")
    if not isinstance(item["cells"], list) or len(item["cells"]) != EXPECTED_CELL_COUNT:
        raise QuotaArtifactError("quota_legacy_cell_count_invalid")
    expected_cells = [(run, method) for run in RUN_INDICES for method in METHOD_IDS]
    cells: list[dict[str, Any]] = []
    for expected, raw in zip(expected_cells, item["cells"], strict=True):
        record = _exact_object(
            raw, {"cell", "eligibility_sha256", "eligibility"}, "quota_legacy_cell_invalid"
        )
        cell = _cell(record["cell"])
        if _cell_key(cell) != expected:
            raise QuotaArtifactError("quota_legacy_cells_not_canonical")
        eligibility = validate_attempt_eligibility(record["eligibility"])
        if eligibility["cell"] != cell or eligibility["attempt_index"] != 1:
            raise QuotaArtifactError("quota_legacy_attempt_identity_mismatch")
        attempt_origin = eligibility["origin"]
        expected_path = f"{root_path}/run-{cell['run_index']}/{cell['method_id']}"
        if (
            attempt_origin["source_kind"] != "legacy-v0.5"
            or attempt_origin["protocol_sha256"] != protocol_hash
            or attempt_origin["source_root_path"] != root_path
            or attempt_origin["attempt_path"] != expected_path
        ):
            raise QuotaArtifactError("quota_legacy_attempt_origin_mismatch")
        relative_attempt = attempt_origin["attempt_path"][len(root_path) + 1 :]
        expected_attempt_snapshot = selection_evidence_snapshot(
            source_subtree_snapshot(snapshot, relative_attempt)
        )
        if eligibility["source_snapshot"] != expected_attempt_snapshot:
            raise QuotaArtifactError("quota_legacy_attempt_snapshot_mismatch")
        eligibility_hash = _sha256(
            record["eligibility_sha256"], "quota_legacy_eligibility_hash_invalid"
        )
        if eligibility_hash != document_sha256(eligibility):
            raise QuotaArtifactError("quota_legacy_eligibility_hash_mismatch")
        cells.append(
            {"cell": cell, "eligibility_sha256": eligibility_hash, "eligibility": eligibility}
        )
    return {
        "schema_version": LEGACY_INVENTORY_SCHEMA,
        "inventoried_at_utc": inventoried_at,
        "origin": {
            "source_kind": "legacy-v0.5",
            "protocol_sha256": protocol_hash,
            "source_root_path": root_path,
            "source_tree_sha256": tree_hash,
        },
        "freeze_identity": {
            "authorized_manifest_sha256": authorized_hash,
            "static_archive_root_path": static_archive_root,
            "static_archive_source_snapshot": static_archive_snapshot,
            "static_archive_source_tree_sha256": static_archive_tree_hash,
            "freeze_manifest": freeze_manifest,
            "archived_freeze_manifest": archived_freeze_manifest,
            "execution_schedule": execution_schedule,
            "freeze_record_log": freeze_record_log,
            "archived_freeze_record_log": archived_freeze_record_log,
        },
        "source_snapshot": snapshot,
        "cells": cells,
    }


def validate_selected_attempt(value: Any) -> FirstValidSelectedAttempt:
    item = _exact_object(
        value,
        {
            "schema_version",
            "protocol_sha256",
            "cell",
            "policy",
            "attempts",
            "selected_attempt_index",
            "selected_eligibility_sha256",
            "selected_origin",
        },
        "quota_selected_attempt_invalid",
    )
    if item["schema_version"] != SELECTED_ATTEMPT_SCHEMA:
        raise QuotaArtifactError("quota_selected_attempt_schema_invalid")
    protocol_hash = _sha256(item["protocol_sha256"], "quota_selected_protocol_invalid")
    cell = _cell(item["cell"])
    if item["policy"] != _selection_policy():
        raise QuotaArtifactError("quota_selected_policy_invalid")
    selected_index = _attempt_index(item["selected_attempt_index"])
    attempts_raw = item["attempts"]
    if not isinstance(attempts_raw, list) or len(attempts_raw) != selected_index:
        raise QuotaArtifactError("quota_selected_attempt_sequence_invalid")
    attempts: list[dict[str, Any]] = []
    for expected_index, raw in enumerate(attempts_raw, start=1):
        record = _exact_object(
            raw,
            {"eligibility_sha256", "eligibility"},
            "quota_selected_attempt_record_invalid",
        )
        eligibility = validate_attempt_eligibility(record["eligibility"])
        if eligibility["cell"] != cell or eligibility["attempt_index"] != expected_index:
            raise QuotaArtifactError("quota_selected_attempt_identity_mismatch")
        eligibility_hash = _sha256(
            record["eligibility_sha256"], "quota_selected_eligibility_hash_invalid"
        )
        if eligibility_hash != document_sha256(eligibility):
            raise QuotaArtifactError("quota_selected_eligibility_hash_mismatch")
        if expected_index < selected_index and eligibility["eligible"]:
            raise QuotaArtifactError("quota_selected_not_first_valid")
        if (
            expected_index < selected_index
            and eligibility["failure"] is not None
            and eligibility["failure"]["disposition"] == "block_study"
        ):
            raise QuotaArtifactError("quota_selected_follows_blocking_attempt")
        if expected_index == selected_index and not eligibility["eligible"]:
            raise QuotaArtifactError("quota_selected_attempt_not_eligible")
        attempts.append(
            {"eligibility_sha256": eligibility_hash, "eligibility": eligibility}
        )
    selected = attempts[-1]
    selected_hash = _sha256(
        item["selected_eligibility_sha256"], "quota_selected_final_hash_invalid"
    )
    selected_origin = _origin(item["selected_origin"])
    if selected_hash != selected["eligibility_sha256"]:
        raise QuotaArtifactError("quota_selected_final_hash_mismatch")
    if selected_origin != selected["eligibility"]["origin"]:
        raise QuotaArtifactError("quota_selected_origin_mismatch")
    return {
        "schema_version": SELECTED_ATTEMPT_SCHEMA,
        "protocol_sha256": protocol_hash,
        "cell": cell,
        "policy": _selection_policy(),
        "attempts": attempts,
        "selected_attempt_index": selected_index,
        "selected_eligibility_sha256": selected_hash,
        "selected_origin": selected_origin,
    }


def _blocked_cell(value: Any, protocol_sha256: str) -> BlockedCell:
    item = _exact_object(
        value,
        {
            "cell",
            "reason",
            "evidence_code",
            "final_attempt_index",
            "eligibility_sha256",
            "eligibility",
            "source_tree_sha256",
        },
        "quota_blocked_cell_invalid",
    )
    cell = _cell(item["cell"])
    reason = item["reason"]
    if reason not in BLOCK_REASONS:
        raise QuotaArtifactError("quota_blocked_reason_invalid")
    evidence_code = item["evidence_code"]
    if not isinstance(evidence_code, str) or _FAILURE_CODE.fullmatch(evidence_code) is None:
        raise QuotaArtifactError("quota_blocked_evidence_code_invalid")
    if set(evidence_code.split("_")) & _FORBIDDEN_FAILURE_CODE_TOKENS:
        raise QuotaArtifactError("quota_blocked_evidence_code_uses_excluded_input")
    final_attempt_index = _attempt_index(item["final_attempt_index"])
    source_tree_sha256 = _sha256(
        item["source_tree_sha256"], "quota_blocked_source_tree_hash_invalid"
    )

    raw_eligibility = item["eligibility"]
    raw_eligibility_hash = item["eligibility_sha256"]
    if (raw_eligibility is None) is not (raw_eligibility_hash is None):
        raise QuotaArtifactError("quota_blocked_eligibility_partial")
    eligibility: AttemptEligibility | None
    eligibility_sha256: str | None
    source_mismatch = False
    if raw_eligibility is None:
        eligibility = None
        eligibility_sha256 = None
        if reason == "attempt_cap_exhausted":
            raise QuotaArtifactError("quota_blocked_cap_eligibility_missing")
    else:
        eligibility = validate_attempt_eligibility(raw_eligibility)
        eligibility_sha256 = _sha256(
            raw_eligibility_hash, "quota_blocked_eligibility_hash_invalid"
        )
        if eligibility_sha256 != document_sha256(eligibility):
            raise QuotaArtifactError("quota_blocked_eligibility_hash_mismatch")
        if eligibility["cell"] != cell or eligibility["attempt_index"] != final_attempt_index:
            raise QuotaArtifactError("quota_blocked_eligibility_identity_mismatch")
        if (
            eligibility["origin"]["source_kind"] == "v0.6-retry"
            and eligibility["origin"]["protocol_sha256"] != protocol_sha256
        ):
            raise QuotaArtifactError("quota_blocked_eligibility_protocol_mismatch")
        source_mismatch = (
            eligibility["source_snapshot"]["tree_sha256"] != source_tree_sha256
            or eligibility["origin"]["source_tree_sha256"] != source_tree_sha256
        )

    if reason == "attempt_cap_exhausted":
        assert eligibility is not None
        failure = eligibility["failure"]
        if (
            final_attempt_index != MAX_ATTEMPTS_PER_CELL
            or eligibility["eligible"]
            or failure is None
            or not failure["retryable"]
            or failure["disposition"] != "retry_whole_roundtrip"
            or source_mismatch
        ):
            raise QuotaArtifactError("quota_blocked_cap_evidence_invalid")
    elif reason == "provenance_failure":
        # A provenance stop can be evidenced either by an explicit blocking
        # eligibility record, by drift between that record and the observed
        # source tree, or by a source tree that could not produce a valid
        # eligibility record at all.
        explicit_provenance = (
            eligibility is not None
            and eligibility["failure"] is not None
            and not eligibility["failure"]["retryable"]
            and eligibility["failure"]["disposition"] == "block_study"
            and eligibility["failure"]["failure_class"] == "provenance"
        )
        if eligibility is not None and not (explicit_provenance or source_mismatch):
            raise QuotaArtifactError("quota_blocked_provenance_evidence_invalid")
    else:
        # Infrastructure can prevent creation of an eligibility record.  If a
        # final record does exist it must not be an otherwise selectable
        # success; local infrastructure never overrides a valid raw pair.
        if eligibility is not None and eligibility["eligible"]:
            raise QuotaArtifactError("quota_blocked_infrastructure_evidence_invalid")

    return {
        "cell": cell,
        "reason": reason,
        "evidence_code": evidence_code,
        "final_attempt_index": final_attempt_index,
        "eligibility_sha256": eligibility_sha256,
        "eligibility": eligibility,
        "source_tree_sha256": source_tree_sha256,
    }


def _blocked_counts(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_run = {str(run): 0 for run in RUN_INDICES}
    by_reason = {reason: 0 for reason in BLOCK_REASONS}
    for record in cells:
        by_run[str(record["cell"]["run_index"])] += 1
        by_reason[str(record["reason"])] += 1
    return {
        "blocked_cells": len(cells),
        "quota_satisfied": False,
        "by_run": by_run,
        "by_reason": by_reason,
    }


def validate_quota_blocked(value: Any) -> QuotaBlockedReceipt:
    """Validate an outcome-free, write-once terminal study-block receipt."""

    item = _exact_object(
        value,
        {
            "schema_version",
            "protocol_sha256",
            "policy",
            "status",
            "blocked_at_utc",
            "primary_reason",
            "blocked_cells",
            "counts",
        },
        "quota_blocked_invalid",
    )
    if item["schema_version"] != QUOTA_BLOCKED_SCHEMA:
        raise QuotaArtifactError("quota_blocked_schema_invalid")
    protocol_sha256 = _sha256(
        item["protocol_sha256"], "quota_blocked_protocol_hash_invalid"
    )
    if item["policy"] != _selection_policy():
        raise QuotaArtifactError("quota_blocked_policy_invalid")
    if item["status"] != "blocked":
        raise QuotaArtifactError("quota_blocked_status_invalid")
    blocked_at_utc = _utc_milliseconds(
        item["blocked_at_utc"], "quota_blocked_timestamp_invalid"
    )
    raw_cells = item["blocked_cells"]
    if not isinstance(raw_cells, list) or not raw_cells:
        raise QuotaArtifactError("quota_blocked_cells_invalid")
    cells = [_blocked_cell(record, protocol_sha256) for record in raw_cells]
    cell_keys = [_cell_key(record["cell"]) for record in cells]
    if cell_keys != sorted(set(cell_keys)):
        raise QuotaArtifactError("quota_blocked_cells_not_canonical")
    reasons = {record["reason"] for record in cells}
    primary_reason = next(reason for reason in BLOCK_REASON_PRIORITY if reason in reasons)
    if item["primary_reason"] != primary_reason:
        raise QuotaArtifactError("quota_blocked_primary_reason_invalid")
    counts = _blocked_counts(cells)
    if item["counts"] != counts:
        raise QuotaArtifactError("quota_blocked_counts_invalid")
    return {
        "schema_version": QUOTA_BLOCKED_SCHEMA,
        "protocol_sha256": protocol_sha256,
        "policy": _selection_policy(),
        "status": "blocked",
        "blocked_at_utc": blocked_at_utc,
        "primary_reason": primary_reason,
        "blocked_cells": cells,
        "counts": counts,
    }


def quota_blocked_document(
    *,
    protocol_sha256: str,
    blocked_at_utc: str,
    blocked_cells: Sequence[Mapping[str, Any]],
) -> QuotaBlockedReceipt:
    """Build the canonical blocked receipt body from independently bound cells."""

    protocol_hash = _sha256(
        protocol_sha256, "quota_blocked_protocol_hash_invalid"
    )
    cells = sorted(
        (_blocked_cell(record, protocol_hash) for record in blocked_cells),
        key=lambda record: _cell_key(record["cell"]),
    )
    if not cells:
        raise QuotaArtifactError("quota_blocked_cells_invalid")
    reasons = {record["reason"] for record in cells}
    primary_reason = next(reason for reason in BLOCK_REASON_PRIORITY if reason in reasons)
    return validate_quota_blocked(
        {
            "schema_version": QUOTA_BLOCKED_SCHEMA,
            "protocol_sha256": protocol_hash,
            "policy": _selection_policy(),
            "status": "blocked",
            "blocked_at_utc": blocked_at_utc,
            "primary_reason": primary_reason,
            "blocked_cells": cells,
            "counts": _blocked_counts(cells),
        }
    )


def validate_quota_complete(value: Any) -> QuotaCompleteReceipt:
    item = _exact_object(
        value,
        {
            "schema_version",
            "protocol_sha256",
            "policy",
            "counts",
            "legacy_inventory_identity",
            "attempt_counts",
            "selected_view",
            "selections",
        },
        "quota_complete_invalid",
    )
    if item["schema_version"] != QUOTA_COMPLETE_SCHEMA:
        raise QuotaArtifactError("quota_complete_schema_invalid")
    protocol_hash = _sha256(item["protocol_sha256"], "quota_complete_protocol_invalid")
    if item["policy"] != _selection_policy():
        raise QuotaArtifactError("quota_complete_policy_invalid")
    counts = _exact_object(
        item["counts"],
        {"runs", "methods_per_run", "required_cells", "selected_cells", "quota_satisfied"},
        "quota_complete_counts_invalid",
    )
    if counts != {
        "runs": 3,
        "methods_per_run": 50,
        "required_cells": EXPECTED_CELL_COUNT,
        "selected_cells": EXPECTED_CELL_COUNT,
        "quota_satisfied": True,
    }:
        raise QuotaArtifactError("quota_complete_counts_mismatch")
    legacy_item = _exact_object(
        item["legacy_inventory_identity"],
        {
            "path",
            "bytes",
            "sha256",
            "source_tree_sha256",
            "authorized_manifest_sha256",
        },
        "quota_complete_legacy_identity_invalid",
    )
    legacy_path = _safe_path(legacy_item["path"], "quota_complete_legacy_path_invalid")
    legacy_bytes = legacy_item["bytes"]
    if (
        isinstance(legacy_bytes, bool)
        or not isinstance(legacy_bytes, int)
        or not 0 < legacy_bytes <= _MAX_FILE_BYTES
    ):
        raise QuotaArtifactError("quota_complete_legacy_bytes_invalid")
    legacy_identity = {
        "path": legacy_path,
        "bytes": legacy_bytes,
        "sha256": _sha256(legacy_item["sha256"], "quota_complete_legacy_hash_invalid"),
        "source_tree_sha256": _sha256(
            legacy_item["source_tree_sha256"], "quota_complete_legacy_tree_hash_invalid"
        ),
        "authorized_manifest_sha256": _sha256(
            legacy_item["authorized_manifest_sha256"],
            "quota_complete_legacy_authorized_hash_invalid",
        ),
    }
    raw_selections = item["selections"]
    if not isinstance(raw_selections, list) or len(raw_selections) != EXPECTED_CELL_COUNT:
        raise QuotaArtifactError("quota_complete_selection_count_invalid")
    expected_cells = [(run, method) for run in RUN_INDICES for method in METHOD_IDS]
    selections: list[dict[str, Any]] = []
    attempt_histogram = {str(index): 0 for index in range(1, MAX_ATTEMPTS_PER_CELL + 1)}
    rejected_by_stage: Counter[str] = Counter()
    rejected_by_class: Counter[str] = Counter()
    rejected_by_code: Counter[str] = Counter()
    rejected_by_source_terminal_stage: Counter[str] = Counter()
    rejected_by_source_terminal_class: Counter[str] = Counter()
    rejected_by_source_terminal_code: Counter[str] = Counter()
    source_terminal_unreadable = 0
    selected_by_origin = {kind: 0 for kind in SOURCE_KINDS}
    total_attempts = 0
    rejected_attempts = 0
    per_run = {
        str(run): {
            "total_retained_attempts": 0,
            "rejected_attempts": 0,
            "attempts_to_success_histogram": {
                str(index): 0 for index in range(1, MAX_ATTEMPTS_PER_CELL + 1)
            },
            "rejected_by_stage": Counter(),
            "rejected_by_class": Counter(),
            "rejected_by_code": Counter(),
            "source_terminal_unreadable": 0,
            "rejected_by_source_terminal_stage": Counter(),
            "rejected_by_source_terminal_class": Counter(),
            "rejected_by_source_terminal_code": Counter(),
            "selected_by_origin": {kind: 0 for kind in SOURCE_KINDS},
        }
        for run in RUN_INDICES
    }
    for expected, raw in zip(expected_cells, raw_selections, strict=True):
        record = _exact_object(
            raw, {"selection_sha256", "selection"}, "quota_complete_selection_record_invalid"
        )
        selection = validate_selected_attempt(record["selection"])
        if _cell_key(selection["cell"]) != expected:
            raise QuotaArtifactError("quota_complete_selections_not_canonical")
        if selection["protocol_sha256"] != protocol_hash:
            raise QuotaArtifactError("quota_complete_selection_protocol_mismatch")
        selection_hash = _sha256(
            record["selection_sha256"], "quota_complete_selection_hash_invalid"
        )
        if selection_hash != document_sha256(selection):
            raise QuotaArtifactError("quota_complete_selection_hash_mismatch")
        selected_index = selection["selected_attempt_index"]
        run_summary = per_run[str(selection["cell"]["run_index"])]
        attempt_histogram[str(selected_index)] += 1
        total_attempts += len(selection["attempts"])
        rejected_attempts += len(selection["attempts"]) - 1
        selected_by_origin[selection["selected_origin"]["source_kind"]] += 1
        run_summary["total_retained_attempts"] += len(selection["attempts"])
        run_summary["rejected_attempts"] += len(selection["attempts"]) - 1
        run_summary["attempts_to_success_histogram"][str(selected_index)] += 1
        run_summary["selected_by_origin"][selection["selected_origin"]["source_kind"]] += 1
        for attempt_record in selection["attempts"][:-1]:
            failure = attempt_record["eligibility"]["failure"]
            if failure is None:
                raise QuotaArtifactError("quota_complete_rejected_failure_missing")
            rejected_by_stage[failure["stage"]] += 1
            rejected_by_class[failure["failure_class"]] += 1
            rejected_by_code[failure["code"]] += 1
            run_summary["rejected_by_stage"][failure["stage"]] += 1
            run_summary["rejected_by_class"][failure["failure_class"]] += 1
            run_summary["rejected_by_code"][failure["code"]] += 1
            if failure["source_terminal_stage"] is None:
                source_terminal_unreadable += 1
                run_summary["source_terminal_unreadable"] += 1
            else:
                rejected_by_source_terminal_stage[failure["source_terminal_stage"]] += 1
                rejected_by_source_terminal_class[failure["source_terminal_class"]] += 1
                rejected_by_source_terminal_code[failure["source_terminal_code"]] += 1
                run_summary["rejected_by_source_terminal_stage"][
                    failure["source_terminal_stage"]
                ] += 1
                run_summary["rejected_by_source_terminal_class"][
                    failure["source_terminal_class"]
                ] += 1
                run_summary["rejected_by_source_terminal_code"][
                    failure["source_terminal_code"]
                ] += 1
        selections.append({"selection_sha256": selection_hash, "selection": selection})
    expected_attempt_counts = {
        "total_retained_attempts": total_attempts,
        "rejected_attempts": rejected_attempts,
        "selected_by_attempt_index": attempt_histogram,
        "attempts_to_success_histogram": attempt_histogram,
        "rejected_by_stage": dict(sorted(rejected_by_stage.items())),
        "rejected_by_class": dict(sorted(rejected_by_class.items())),
        "rejected_by_code": dict(sorted(rejected_by_code.items())),
        "source_terminal_unreadable": source_terminal_unreadable,
        "rejected_by_source_terminal_stage": dict(
            sorted(rejected_by_source_terminal_stage.items())
        ),
        "rejected_by_source_terminal_class": dict(
            sorted(rejected_by_source_terminal_class.items())
        ),
        "rejected_by_source_terminal_code": dict(
            sorted(rejected_by_source_terminal_code.items())
        ),
        "selected_by_origin": selected_by_origin,
        "by_run": {
            run: {
                "total_retained_attempts": summary["total_retained_attempts"],
                "rejected_attempts": summary["rejected_attempts"],
                "attempts_to_success_histogram": summary[
                    "attempts_to_success_histogram"
                ],
                "rejected_by_stage": dict(sorted(summary["rejected_by_stage"].items())),
                "rejected_by_class": dict(sorted(summary["rejected_by_class"].items())),
                "rejected_by_code": dict(sorted(summary["rejected_by_code"].items())),
                "source_terminal_unreadable": summary["source_terminal_unreadable"],
                "rejected_by_source_terminal_stage": dict(
                    sorted(summary["rejected_by_source_terminal_stage"].items())
                ),
                "rejected_by_source_terminal_class": dict(
                    sorted(summary["rejected_by_source_terminal_class"].items())
                ),
                "rejected_by_source_terminal_code": dict(
                    sorted(summary["rejected_by_source_terminal_code"].items())
                ),
                "selected_by_origin": summary["selected_by_origin"],
            }
            for run, summary in per_run.items()
        },
    }
    if item["attempt_counts"] != expected_attempt_counts:
        raise QuotaArtifactError("quota_complete_attempt_counts_mismatch")
    if total_attempts < EXPECTED_CELL_COUNT:
        raise QuotaArtifactError("quota_complete_total_attempts_invalid")

    view_item = _exact_object(
        item["selected_view"],
        {"root_path", "source_snapshot", "cells"},
        "quota_complete_selected_view_invalid",
    )
    view_root = _safe_path(view_item["root_path"], "quota_complete_selected_view_root_invalid")
    view_snapshot = validate_source_tree_snapshot(view_item["source_snapshot"])
    raw_view_cells = view_item["cells"]
    if not isinstance(raw_view_cells, list) or len(raw_view_cells) != EXPECTED_CELL_COUNT:
        raise QuotaArtifactError("quota_complete_selected_view_cell_count_invalid")
    snapshot_files = {record["path"]: record for record in view_snapshot["files"]}
    view_cells: list[dict[str, Any]] = []
    for expected, selection_record, raw_view in zip(
        expected_cells, selections, raw_view_cells, strict=True
    ):
        view = _exact_object(
            raw_view,
            {"cell", "selection_sha256", "binding_file"},
            "quota_complete_selected_view_cell_invalid",
        )
        cell = _cell(view["cell"])
        if _cell_key(cell) != expected:
            raise QuotaArtifactError("quota_complete_selected_view_cells_not_canonical")
        selection_hash = _sha256(
            view["selection_sha256"], "quota_complete_selected_view_selection_hash_invalid"
        )
        if selection_hash != selection_record["selection_sha256"]:
            raise QuotaArtifactError("quota_complete_selected_view_selection_mismatch")
        binding = _file_identity(
            view["binding_file"], "quota_complete_selected_view_binding_invalid"
        )
        expected_binding_path = f"run-{cell['run_index']}/{cell['method_id']}/selected-attempt.json"
        if binding["path"] != expected_binding_path:
            raise QuotaArtifactError("quota_complete_selected_view_binding_path_mismatch")
        if binding["sha256"] != selection_hash:
            raise QuotaArtifactError("quota_complete_selected_view_binding_hash_mismatch")
        if binding["bytes"] != len(canonical_json_bytes(selection_record["selection"])):
            raise QuotaArtifactError("quota_complete_selected_view_binding_bytes_mismatch")
        if snapshot_files.get(binding["path"]) != binding:
            raise QuotaArtifactError("quota_complete_selected_view_snapshot_mismatch")
        view_cells.append(
            {"cell": cell, "selection_sha256": selection_hash, "binding_file": binding}
        )
    return {
        "schema_version": QUOTA_COMPLETE_SCHEMA,
        "protocol_sha256": protocol_hash,
        "policy": _selection_policy(),
        "counts": dict(counts),
        "legacy_inventory_identity": legacy_identity,
        "attempt_counts": expected_attempt_counts,
        "selected_view": {
            "root_path": view_root,
            "source_snapshot": view_snapshot,
            "cells": view_cells,
        },
        "selections": selections,
    }
