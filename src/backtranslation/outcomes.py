"""Licensed-local loading of the hash-pinned TSE participant outcomes.

This module is intentionally separate from generation and scoring.  Production
analysis calls it only after a freeze manifest has been verified; raw author
data stays in the ignored cache because its redistribution license is unclear.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any


TSE_OUTCOME_SHA256 = "77704e6a39ded74a4542d61aaf737432950905fe9b886a2dd822132f75395ca1"
TSE_OUTCOME_BYTES = 524_171
TSE_OUTCOME_ROWS = 444


class OutcomeLoadError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _secure_read_once(path: Path, *, maximum_bytes: int, failure_prefix: str) -> bytes:
    """Read exact regular-file bytes once and detect replacement during read."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise OutcomeLoadError(f"{failure_prefix}_read_failed") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > maximum_bytes
    ):
        raise OutcomeLoadError(f"{failure_prefix}_not_regular_or_too_large")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OutcomeLoadError(f"{failure_prefix}_read_failed") from exc
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened, after)
    }
    payload = b"".join(chunks)
    if len(identities) != 1 or len(payload) != after.st_size or len(payload) > maximum_bytes:
        raise OutcomeLoadError(f"{failure_prefix}_changed_during_read")
    return payload


def signature_to_method_id(source_manifest: Path) -> dict[str, str]:
    payload = _secure_read_once(
        source_manifest,
        maximum_bytes=16 * 1024 * 1024,
        failure_prefix="outcome_manifest",
    )
    return _signature_mapping_payload(payload)


def _signature_mapping_payload(payload: bytes) -> dict[str, str]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise OutcomeLoadError("outcome_manifest_read_failed") from exc
    mapping: dict[str, str] = {}
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OutcomeLoadError("outcome_manifest_not_jsonl") from exc
        if not isinstance(value, Mapping):
            raise OutcomeLoadError("outcome_manifest_record_not_object")
        signature = value.get("dataset_signature")
        method_id = value.get("snippet_id")
        if (
            not isinstance(signature, str)
            or not signature
            or not isinstance(method_id, str)
            or not method_id
            or signature in mapping
        ):
            raise OutcomeLoadError("outcome_manifest_mapping_invalid")
        mapping[signature] = method_id
    if len(mapping) != 50 or set(mapping.values()) != {
        f"tse-{index:03d}" for index in range(1, 51)
    }:
        raise OutcomeLoadError("outcome_manifest_count_mismatch")
    return mapping


def load_tse_evaluations(
    outcome_csv: Path,
    source_manifest: Path,
    *,
    expected_sha256: str = TSE_OUTCOME_SHA256,
    expected_bytes: int = TSE_OUTCOME_BYTES,
    expected_rows: int = TSE_OUTCOME_ROWS,
    expected_methods: int = 50,
    expected_participants: int = 63,
    expected_projects: int = 10,
    allow_repeated_participant_method: bool = False,
) -> list[dict[str, Any]]:
    """Load the exact TSE CSV into the normalized analysis interface."""
    payload = _secure_read_once(
        outcome_csv,
        maximum_bytes=64 * 1024 * 1024,
        failure_prefix="outcome_file",
    )
    if len(payload) != expected_bytes:
        raise OutcomeLoadError("outcome_file_size_mismatch")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise OutcomeLoadError("outcome_file_hash_mismatch")
    manifest_payload = _secure_read_once(
        source_manifest,
        maximum_bytes=16 * 1024 * 1024,
        failure_prefix="outcome_manifest",
    )
    method_ids = _signature_mapping_payload(manifest_payload)
    required = {
        "participant_id",
        "system_name",
        "snippet_signature",
        "developer_position",
        "Cyclomatic complexity",
        "LOC",
        "PBU",
        "AU",
    }
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    try:
        decoded = payload.decode("utf-8-sig")
        with io.StringIO(decoded, newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise OutcomeLoadError("outcome_columns_missing")
            for raw in reader:
                signature = raw["snippet_signature"]
                if signature not in method_ids:
                    raise OutcomeLoadError("outcome_signature_not_in_manifest")
                method_id = method_ids[signature]
                participant = raw["participant_id"]
                key = (participant, method_id)
                if key in seen and not allow_repeated_participant_method:
                    raise OutcomeLoadError("outcome_participant_method_duplicate")
                seen.add(key)
                records.append(
                    {
                        "evaluation_index": len(records),
                        "participant_id": participant,
                        "method_id": method_id,
                        "project": raw["system_name"],
                        "participant_group": raw["developer_position"],
                        "loc": raw["LOC"],
                        "cyclomatic_complexity": raw["Cyclomatic complexity"],
                        "pbu": raw["PBU"],
                        "au": raw["AU"],
                    }
                )
    except OutcomeLoadError:
        raise
    except (UnicodeDecodeError, csv.Error) as exc:
        raise OutcomeLoadError("outcome_csv_read_failed") from exc
    if len(records) != expected_rows:
        raise OutcomeLoadError("outcome_row_count_mismatch")
    if len({item["method_id"] for item in records}) != expected_methods:
        raise OutcomeLoadError("outcome_method_count_mismatch")
    if len({item["participant_id"] for item in records}) != expected_participants:
        raise OutcomeLoadError("outcome_participant_count_mismatch")
    if len({item["project"] for item in records}) != expected_projects:
        raise OutcomeLoadError("outcome_project_count_mismatch")
    return records
