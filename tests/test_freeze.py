from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtranslation.freeze import (
    Finding,
    FreezeError,
    append_freeze_record,
    audit_artifact_tree,
    build_manifest,
    canonical_json_bytes,
    create_freeze_manifest,
    manifest_sha256,
    prepare_clean_rerun,
    scan_secret_material,
    snapshot_tree,
    verify_manifest,
    verify_runtime_lock,
    verify_tree_snapshot,
    validate_pin_inventory,
)


ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _resolved_spec(root: Path) -> dict[str, object]:
    protocol = root / "protocol" / "PROTOCOL.frozen.md"
    model = root / "pins" / "ruse.bin"
    _write(protocol, b"# Frozen protocol\n\nStatus: frozen and reviewed.\n")
    _write(model, b"verified metric fixture\n")
    model_manifest = root / "pins" / "ruse-manifest.json"
    _write(model_manifest, b'{"fixture":"verified"}\n')
    return {
        "schema_version": "backtranslation.freeze-spec.v1",
        "protocol_path": "protocol/PROTOCOL.frozen.md",
        "marker_policy": "standard-v1",
        "include_files": [
            "protocol/PROTOCOL.frozen.md",
            "pins/ruse-manifest.json",
        ],
        "include_trees": [],
        "exclude_patterns": [],
        "pin_inventory": {
            "required_input_ids": ["ruse-end-to-end"],
            "inputs": [
                {
                    "id": "ruse-end-to-end",
                    "status": "pinned",
                    "pin_type": "manifest",
                    "manifest_path": "pins/ruse-manifest.json",
                    "manifest_sha256": __import__("hashlib").sha256(
                        model_manifest.read_bytes()
                    ).hexdigest(),
                }
            ],
        },
    }


def test_manifest_is_deterministic_sorted_and_detects_tampering(tmp_path: Path) -> None:
    _write(tmp_path / "z.txt", b"z")
    _write(tmp_path / "a.txt", b"alpha")
    first = build_manifest(tmp_path, ["z.txt", "a.txt", "z.txt"])
    second = build_manifest(tmp_path, ["a.txt", "z.txt"])
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert [item["path"] for item in first["files"]] == ["a.txt", "z.txt"]
    digest = manifest_sha256(first)
    assert verify_manifest(tmp_path, first, expected_sha256=digest) == digest

    _write(tmp_path / "a.txt", b"changed")
    with pytest.raises(FreezeError) as caught:
        verify_manifest(tmp_path, first, expected_sha256=digest)
    assert {item.code for item in caught.value.findings} >= {
        "manifest_hash_mismatch",
        "manifest_size_mismatch",
    }


def test_unresolved_ruse_blocks_before_candidate_paths_are_opened(tmp_path: Path) -> None:
    spec = {
        "schema_version": "backtranslation.freeze-spec.v1",
        "protocol_path": "protocol/PROTOCOL.frozen.md",
        "forbidden_markers": [],
        "include_files": ["does-not-exist"],
        "include_trees": [],
        "exclude_patterns": [],
        "pin_inventory": {
            "required_input_ids": ["ruse-end-to-end"],
            "inputs": [
                {
                    "id": "ruse-end-to-end",
                    "status": "unresolved",
                    "pin_type": "manifest",
                }
            ],
        },
    }
    with pytest.raises(FreezeError) as caught:
        create_freeze_manifest(tmp_path, spec)
    assert caught.value.code == "freeze_preflight_failed"
    assert [item.code for item in caught.value.findings] == ["input_unresolved"]
    assert caught.value.findings[0].path == "ruse-end-to-end"


def test_unpinned_hash_and_protocol_marker_block_freeze(tmp_path: Path) -> None:
    spec = _resolved_spec(tmp_path)
    pin = spec["pin_inventory"]["inputs"][0]  # type: ignore[index]
    pin["manifest_sha256"] = "latest"  # type: ignore[index]
    with pytest.raises(FreezeError) as caught:
        create_freeze_manifest(tmp_path, spec)
    assert "pin_manifest_hash_unpinned" in {item.code for item in caught.value.findings}

    pin["manifest_sha256"] = __import__("hashlib").sha256(  # type: ignore[index]
        (tmp_path / "pins" / "ruse-manifest.json").read_bytes()
    ).hexdigest()
    marker = "DRA" + "FT"
    _write(tmp_path / "protocol" / "PROTOCOL.frozen.md", f"Status: {marker}\n".encode())
    with pytest.raises(FreezeError) as caught:
        create_freeze_manifest(tmp_path, spec)
    assert "unresolved_marker" in {item.code for item in caught.value.findings}


def test_successful_synthetic_freeze_has_no_timestamp(tmp_path: Path) -> None:
    manifest = create_freeze_manifest(tmp_path, _resolved_spec(tmp_path))
    assert set(manifest) == {"schema_version", "files"}
    assert manifest_sha256(manifest) == manifest_sha256(manifest)


def test_service_cap_pin_validates_canary_semantics_and_hash(tmp_path: Path) -> None:
    canary = {
        "status": "passed",
        "provider_event": {
            "request": {
                "host": "api.deepseek.com",
                "endpoint": "/chat/completions",
                "model": "deepseek-v4-pro",
                "max_tokens": 16384,
                "thinking": "enabled",
                "reasoning_effort": "high",
                "response_format": "json_object",
                "stream": False,
            },
            "response": {
                "returned_model": "deepseek-v4-pro",
                "finish_reason": "stop",
                "reasoning_content_retained": False,
            },
        },
    }
    path = tmp_path / "canary.json"
    _write(path, canonical_json_bytes(canary) + b"\n")
    item = {
        "id": "cap",
        "status": "pinned",
        "pin_type": "service",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-v4-pro",
        "max_tokens": 16384,
        "manifest_path": "canary.json",
        "manifest_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
    }
    inventory = {"required_input_ids": ["cap"], "inputs": [item]}
    assert validate_pin_inventory(tmp_path, inventory) == ()
    item["max_tokens"] = 128
    assert "service_canary_max_tokens_mismatch" in {
        finding.code for finding in validate_pin_inventory(tmp_path, inventory)
    }


def test_secret_scanner_blocks_filename_without_opening_contents(tmp_path: Path) -> None:
    _write(tmp_path / "provider-key.txt", b"do not inspect this value")
    findings = scan_secret_material(tmp_path, ["provider-key.txt"])
    assert findings == (Finding("credential_file_prohibited", "provider-key.txt"),)


def test_artifact_scan_rejects_outcome_reasoning_and_authorization_keys(
    tmp_path: Path,
) -> None:
    payload = {
        "opaque_method_id": "m-001",
        "nested": {
            "AU": 1,
            "reasoning_content": "must never persist",
            "authorization": "redacted",
        },
    }
    _write(tmp_path / "run.json", json.dumps(payload).encode())
    _write(tmp_path / "scores.csv", b"method_id,PBU,score\nm-001,1,0.5\n")
    _write(tmp_path / "human-outcomes.parquet", b"opaque binary")
    findings = audit_artifact_tree(tmp_path)
    codes = {item.code for item in findings}
    assert "prohibited_artifact_key" in codes
    assert "prohibited_artifact_column" in codes
    assert "prohibited_artifact_filename" in codes
    assert all("must never persist" not in item.detail for item in findings)


def test_artifact_scan_operational_allowlist_is_exact_and_empty_only(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "execution.lock", b"")
    _write(tmp_path / "nested" / "execution.lock", b"")
    findings = audit_artifact_tree(
        tmp_path, allowed_empty_operational_files=("execution.lock",)
    )
    assert Finding(
        "artifact_format_unscanned", "nested/execution.lock", ".lock"
    ) in findings
    (tmp_path / "nested" / "execution.lock").unlink()
    assert audit_artifact_tree(
        tmp_path, allowed_empty_operational_files=("execution.lock",)
    ) == ()
    _write(tmp_path / "execution.lock", b"not empty")
    assert audit_artifact_tree(
        tmp_path, allowed_empty_operational_files=("execution.lock",)
    ) == (Finding("operational_file_not_empty", "execution.lock"),)


def test_clean_rerun_never_reuses_a_directory_and_snapshot_detects_drift(
    tmp_path: Path,
) -> None:
    freeze_hash = "a" * 64
    run = tmp_path / "rerun-01"
    marker = prepare_clean_rerun(run, freeze_hash, "independent-01")
    assert marker.is_file()
    with pytest.raises(FreezeError, match="rerun_directory_exists"):
        prepare_clean_rerun(run, freeze_hash, "independent-01")

    _write(run / "scores.json", b'{"score":1}\n')
    snapshot = snapshot_tree(run)
    assert snapshot["excluded_patterns"] == []
    assert verify_tree_snapshot(run, snapshot) == ()
    _write(run / "scores.json", b'{"score":2}\n')
    findings = verify_tree_snapshot(run, snapshot)
    assert "rerun_hash_mismatch" in {item.code for item in findings}


def test_snapshot_exclusions_are_reused_during_verification(tmp_path: Path) -> None:
    _write(tmp_path / "stable.json", b"{}\n")
    _write(tmp_path / "volatile" / "timing.json", b'{"elapsed":1}\n')
    snapshot = snapshot_tree(tmp_path, exclude=("volatile/**",))
    _write(tmp_path / "volatile" / "timing.json", b'{"elapsed":999}\n')
    assert verify_tree_snapshot(tmp_path, snapshot) == ()


def test_checked_in_runtime_lock_matches_current_environment() -> None:
    lock = json.loads((ROOT / "config" / "runtime-lock.json").read_text(encoding="utf-8"))
    assert verify_runtime_lock(ROOT, lock) == ()


def test_freeze_record_is_append_only_and_sanitizes_reviewer(tmp_path: Path) -> None:
    record_path = tmp_path / "freeze-record.jsonl"
    first = append_freeze_record(
        record_path,
        manifest_path="protocol/manifest-v1.json",
        manifest_hash="b" * 64,
        reviewer="reviewer-one",
        frozen_at_utc="2026-08-11T12:00:00Z",
    )
    second = append_freeze_record(
        record_path,
        manifest_path="protocol/manifest-v2.json",
        manifest_hash="c" * 64,
        reviewer="reviewer-two",
        frozen_at_utc="2026-08-11T12:01:00Z",
    )
    lines = [json.loads(line) for line in record_path.read_text().splitlines()]
    assert lines == [first, second]
    with pytest.raises(FreezeError, match="freeze_reviewer_invalid"):
        append_freeze_record(
            record_path,
            manifest_path="protocol/manifest-v3.json",
            manifest_hash="d" * 64,
            reviewer="name\nforged-record",
        )
