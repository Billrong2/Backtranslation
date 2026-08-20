from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from backtranslation.outcomes import (
    OutcomeLoadError,
    load_tse_evaluations,
    signature_to_method_id,
)


def manifest(path: Path) -> None:
    records = [
        {"dataset_signature": f"example.C.m{index}()", "snippet_id": f"tse-{index:03d}"}
        for index in range(1, 51)
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


def test_signature_mapping_is_exact(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    manifest(path)
    mapping = signature_to_method_id(path)
    assert mapping["example.C.m1()"] == "tse-001"
    assert len(mapping) == 50


def test_loader_maps_only_required_fields(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    manifest(manifest_path)
    csv_path = tmp_path / "outcomes.csv"
    fields = [
        "participant_id",
        "system_name",
        "snippet_signature",
        "developer_position",
        "Cyclomatic complexity",
        "LOC",
        "PBU",
        "AU",
        "ignored",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for method in range(1, 51):
            writer.writerow(
                {
                    "participant_id": str(method),
                    "system_name": f"project-{(method - 1) // 5}",
                    "snippet_signature": f"example.C.m{method}()",
                    "developer_position": "bachelor student",
                    "Cyclomatic complexity": "2",
                    "LOC": "10",
                    "PBU": "1",
                    "AU": "0.6666666666666666",
                    "ignored": "do not propagate",
                }
            )
    payload = csv_path.read_bytes()
    records = load_tse_evaluations(
        csv_path,
        manifest_path,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_bytes=len(payload),
        expected_rows=50,
        expected_methods=50,
        expected_participants=50,
        expected_projects=10,
    )
    assert len(records) == 50
    assert "ignored" not in records[0]
    assert records[0]["method_id"] == "tse-001"


def test_loader_can_retain_authoritative_repeated_evaluations(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    manifest(manifest_path)
    csv_path = tmp_path / "outcomes.csv"
    fields = [
        "participant_id", "system_name", "snippet_signature",
        "developer_position", "Cyclomatic complexity", "LOC", "PBU", "AU",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for method in range(1, 51):
            count = 2 if method == 1 else 1
            for _ in range(count):
                writer.writerow({
                    "participant_id": str(method),
                    "system_name": f"project-{(method - 1) // 5}",
                    "snippet_signature": f"example.C.m{method}()",
                    "developer_position": "bachelor student",
                    "Cyclomatic complexity": "2", "LOC": "10", "PBU": "1", "AU": "1",
                })
    payload = csv_path.read_bytes()
    records = load_tse_evaluations(
        csv_path, manifest_path,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_bytes=len(payload), expected_rows=51, expected_methods=50,
        expected_participants=50, expected_projects=10,
        allow_repeated_participant_method=True,
    )
    assert len(records) == 51
    assert records[0]["evaluation_index"] == 0
    assert records[1]["evaluation_index"] == 1


def test_loader_rejects_digest_mismatch(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    manifest(manifest_path)
    csv_path = tmp_path / "outcomes.csv"
    csv_path.write_text("not the expected file", encoding="utf-8")
    with pytest.raises(OutcomeLoadError, match="outcome_file_hash_mismatch"):
        load_tse_evaluations(
            csv_path,
            manifest_path,
            expected_sha256="0" * 64,
            expected_bytes=csv_path.stat().st_size,
            expected_rows=1,
        )


def test_loader_rejects_symlinked_outcome_and_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    manifest(manifest_path)
    manifest_link = tmp_path / "manifest-link.jsonl"
    manifest_link.symlink_to(manifest_path)
    with pytest.raises(OutcomeLoadError, match="outcome_manifest_not_regular_or_too_large"):
        signature_to_method_id(manifest_link)

    csv_path = tmp_path / "outcomes.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    csv_link = tmp_path / "outcomes-link.csv"
    csv_link.symlink_to(csv_path)
    with pytest.raises(OutcomeLoadError, match="outcome_file_not_regular_or_too_large"):
        load_tse_evaluations(
            csv_link,
            manifest_path,
            expected_sha256=hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            expected_bytes=csv_path.stat().st_size,
            expected_rows=1,
        )
