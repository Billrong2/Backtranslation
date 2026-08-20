from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from backtranslation.quota import (
    ATTEMPT_ELIGIBILITY_SCHEMA,
    DESCRIPTIVE_ATTEMPT_FILES,
    EXPECTED_CELL_COUNT,
    LEGACY_INVENTORY_SCHEMA,
    METHOD_IDS,
    QUOTA_BLOCKED_SCHEMA,
    QUOTA_COMPLETE_SCHEMA,
    RUN_BARRIER_SCHEMA,
    RUN_INDICES,
    SELECTED_ATTEMPT_SCHEMA,
    SELECTION_POLICY,
    QuotaArtifactError,
    canonical_json_bytes,
    document_sha256,
    load_canonical_json,
    quota_blocked_document,
    run_barrier_witness_document,
    run_barrier_witness_sha256,
    selection_evidence_snapshot,
    snapshot_source_tree,
    validate_attempt_eligibility,
    validate_legacy_attempt_inventory,
    validate_quota_blocked,
    validate_quota_complete,
    validate_run_barrier_witness,
    validate_selected_attempt,
    verify_selection_evidence_snapshot,
    verify_source_tree_snapshot,
)


HASH = hashlib.sha256(b"fixture").hexdigest()
V06_HASH = hashlib.sha256(b"v0.6").hexdigest()


def _snapshot(payload: bytes = b"artifact\n") -> dict[str, object]:
    body = {
        "schema_version": "backtranslation.quota-source-tree.v1",
        "directories": [],
        "files": [
            {
                "path": "regeneration.output.txt",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    return {**body, "tree_sha256": document_sha256(body)}


def _barrier(run: int, protocol_hash: str) -> dict[str, object]:
    predecessors = (
        []
        if run == 0
        else [
            {
                "cell": {"run_index": run - 1, "method_id": method_id},
                "selection_sha256": hashlib.sha256(
                    f"{protocol_hash}:{run - 1}:{method_id}".encode()
                ).hexdigest(),
            }
            for method_id in METHOD_IDS
        ]
    )
    return run_barrier_witness_document(
        protocol_sha256=protocol_hash,
        target_run_index=run,
        predecessor_selections=predecessors,
    )


def _attempt(
    run: int,
    method_id: str,
    index: int,
    *,
    eligible: bool,
    source_kind: str = "v0.6-retry",
    protocol_hash: str = V06_HASH,
    root: str | None = None,
) -> dict[str, object]:
    snapshot = _snapshot(f"{run}:{method_id}:{index}\n".encode())
    root = root or f"artifacts/runs/{protocol_hash}/attempts"
    if source_kind == "legacy-v0.5":
        attempt_path = f"{root}/run-{run}/{method_id}"
    else:
        attempt_path = f"{root}/run-{run}/{method_id}/attempt-{index:04d}"
    checks = {
        name: eligible or name in {
            "cell_identity_valid",
            "artifact_hashes_valid",
            "request_reconstruction_valid",
        }
        for name in SELECTION_POLICY["selection_inputs"]
    }
    java = {
        "performed": eligible,
        "analyzer_id": "analyze_java_method-v1" if eligible else None,
        "analyzer_version": "tree-sitter-java-0.23.5" if eligible else None,
        "validation_policy_sha256": HASH if eligible else None,
        "artifact_path": "regeneration.output.txt" if eligible else None,
        "artifact_sha256": snapshot["files"][0]["sha256"] if eligible else None,
        "structurally_valid": eligible,
    }
    barrier = None if source_kind == "legacy-v0.5" else _barrier(run, protocol_hash)
    return {
        "schema_version": ATTEMPT_ELIGIBILITY_SCHEMA,
        "cell": {"run_index": run, "method_id": method_id},
        "attempt_index": index,
        "origin": {
            "source_kind": source_kind,
            "protocol_sha256": protocol_hash,
            "source_root_path": root,
            "attempt_path": attempt_path,
            "source_tree_sha256": snapshot["tree_sha256"],
        },
        "run_barrier_witness": barrier,
        "run_barrier_witness_sha256": (
            run_barrier_witness_sha256(barrier) if barrier is not None else None
        ),
        "source_snapshot": snapshot,
        "predicate": copy.deepcopy(SELECTION_POLICY),
        "checks": checks,
        "java_validation": java,
        "eligible": eligible,
        "rejection_codes": []
        if eligible
        else [f"check_failed_{name}" for name, passed in checks.items() if not passed],
        "failure": None
        if eligible
        else {
            "primary_check": "provider_extraction_completed",
            "stage": "extraction_provider",
            "failure_class": "provider",
            "code": "provider_finish_reason_not_stop",
            "retryable": True,
            "disposition": "retry_whole_roundtrip",
            "source_terminal_stage": "extraction_api",
            "source_terminal_class": "provider",
            "source_terminal_code": "provider_finish_reason_not_stop",
        },
    }


def _selection(run: int, method_id: str, selected_index: int = 1) -> dict[str, object]:
    attempts = [
        _attempt(run, method_id, index, eligible=index == selected_index)
        for index in range(1, selected_index + 1)
    ]
    records = [
        {"eligibility_sha256": document_sha256(attempt), "eligibility": attempt}
        for attempt in attempts
    ]
    return {
        "schema_version": SELECTED_ATTEMPT_SCHEMA,
        "protocol_sha256": V06_HASH,
        "cell": {"run_index": run, "method_id": method_id},
        "policy": copy.deepcopy(SELECTION_POLICY),
        "attempts": records,
        "selected_attempt_index": selected_index,
        "selected_eligibility_sha256": records[-1]["eligibility_sha256"],
        "selected_origin": attempts[-1]["origin"],
    }


def test_source_snapshot_is_deterministic_secure_and_detects_drift(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "nested" / "empty").mkdir(parents=True)
    (root / "z.txt").write_bytes(b"z")
    (root / "nested" / "a.txt").write_bytes(b"alpha")
    snapshot = snapshot_source_tree(root)
    assert snapshot["directories"] == ["nested", "nested/empty"]
    assert [record["path"] for record in snapshot["files"]] == ["nested/a.txt", "z.txt"]
    assert verify_source_tree_snapshot(root, snapshot) == snapshot["tree_sha256"]

    (root / "nested" / "a.txt").write_bytes(b"changed")
    with pytest.raises(QuotaArtifactError, match="quota_source_tree_snapshot_mismatch"):
        verify_source_tree_snapshot(root, snapshot)

    (root / "nested" / "a.txt").unlink()
    (root / "nested" / "link").symlink_to(root / "z.txt")
    with pytest.raises(QuotaArtifactError, match="quota_source_symlink_prohibited"):
        snapshot_source_tree(root)


def test_selection_evidence_projection_excludes_only_descriptive_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    (root / "attempt.claim.json").write_bytes(b"claim\n")
    (root / "extraction.output.txt").write_bytes(b"directions\n")
    for name in DESCRIPTIVE_ATTEMPT_FILES:
        (root / name).write_bytes(f"descriptive:{name}\n".encode())
    projected = selection_evidence_snapshot(snapshot_source_tree(root))
    assert [record["path"] for record in projected["files"]] == [
        "attempt.claim.json",
        "extraction.output.txt",
    ]
    assert verify_selection_evidence_snapshot(root, projected) == projected["tree_sha256"]

    (root / "status.json").write_bytes(b"changed but still descriptive\n")
    assert verify_selection_evidence_snapshot(root, projected) == projected["tree_sha256"]
    (root / "extraction.output.txt").write_bytes(b"changed raw evidence\n")
    with pytest.raises(
        QuotaArtifactError, match="quota_selection_evidence_snapshot_mismatch"
    ):
        verify_selection_evidence_snapshot(root, projected)


def test_canonical_loader_rejects_noncanonical_json(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    value = {"b": 2, "a": 1}
    path.write_bytes(canonical_json_bytes(value))
    assert load_canonical_json(path) == value
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(QuotaArtifactError, match="quota_json_not_canonical"):
        load_canonical_json(path)


def test_run_barrier_witness_is_exact_genesis_or_fifty_predecessor_selections() -> None:
    genesis = run_barrier_witness_document(
        protocol_sha256=V06_HASH, target_run_index=0
    )
    assert genesis == {
        "schema_version": RUN_BARRIER_SCHEMA,
        "protocol_sha256": V06_HASH,
        "policy_id": "first-valid-roundtrip-v1",
        "target_run_index": 0,
        "predecessor_run_index": None,
        "predecessor_selection_count": 0,
        "predecessor_selections": [],
    }
    assert run_barrier_witness_sha256(genesis) == document_sha256(genesis)

    # Builder canonicalizes discovery order; validator requires the exact
    # predecessor run and all 50 opaque methods.
    run_two = _barrier(2, V06_HASH)
    reversed_records = list(reversed(run_two["predecessor_selections"]))
    rebuilt = run_barrier_witness_document(
        protocol_sha256=V06_HASH,
        target_run_index=2,
        predecessor_selections=reversed_records,
    )
    assert rebuilt == run_two
    assert rebuilt["predecessor_selection_count"] == 50

    invalid = copy.deepcopy(run_two)
    invalid["predecessor_selections"] = invalid["predecessor_selections"][:-1]
    invalid["predecessor_selection_count"] = 49
    with pytest.raises(
        QuotaArtifactError, match="quota_run_barrier_selection_count_invalid"
    ):
        validate_run_barrier_witness(invalid)

    invalid = copy.deepcopy(run_two)
    invalid["predecessor_selections"][0]["cell"]["run_index"] = 0
    with pytest.raises(QuotaArtifactError, match="quota_run_barrier_selection_run_invalid"):
        validate_run_barrier_witness(invalid)


def test_attempt_requires_native_barrier_and_legacy_forbids_retroactive_barrier() -> None:
    native = _attempt(1, "tse-001", 2, eligible=False)
    assert validate_attempt_eligibility(native)["run_barrier_witness"] == _barrier(
        1, V06_HASH
    )

    invalid = copy.deepcopy(native)
    invalid["run_barrier_witness_sha256"] = HASH
    with pytest.raises(
        QuotaArtifactError, match="quota_native_attempt_barrier_hash_mismatch"
    ):
        validate_attempt_eligibility(invalid)

    invalid = copy.deepcopy(native)
    invalid["run_barrier_witness"] = _barrier(0, V06_HASH)
    invalid["run_barrier_witness_sha256"] = run_barrier_witness_sha256(
        invalid["run_barrier_witness"]
    )
    with pytest.raises(
        QuotaArtifactError, match="quota_native_attempt_barrier_identity_mismatch"
    ):
        validate_attempt_eligibility(invalid)

    legacy_hash = hashlib.sha256(b"legacy").hexdigest()
    legacy = _attempt(
        0,
        "tse-001",
        1,
        eligible=False,
        source_kind="legacy-v0.5",
        protocol_hash=legacy_hash,
        root=f"artifacts/runs/{legacy_hash}",
    )
    assert validate_attempt_eligibility(legacy)["run_barrier_witness"] is None
    legacy["run_barrier_witness"] = _barrier(0, legacy_hash)
    legacy["run_barrier_witness_sha256"] = run_barrier_witness_sha256(
        legacy["run_barrier_witness"]
    )
    with pytest.raises(QuotaArtifactError, match="quota_legacy_attempt_barrier_present"):
        validate_attempt_eligibility(legacy)


def test_attempt_requires_structural_validity_and_exact_outcome_blind_policy() -> None:
    accepted = _attempt(0, "tse-001", 1, eligible=True)
    assert validate_attempt_eligibility(accepted)["eligible"] is True

    invalid = copy.deepcopy(accepted)
    invalid["predicate"]["excluded_inputs"].remove("actual_understandability")
    with pytest.raises(QuotaArtifactError, match="quota_attempt_predicate_invalid"):
        validate_attempt_eligibility(invalid)

    invalid = copy.deepcopy(accepted)
    invalid["java_validation"]["structurally_valid"] = False
    with pytest.raises(QuotaArtifactError, match="quota_attempt_java_result_inconsistent"):
        validate_attempt_eligibility(invalid)

    invalid = copy.deepcopy(accepted)
    invalid["attempt_index"] = 11
    with pytest.raises(QuotaArtifactError, match="quota_attempt_index_invalid"):
        validate_attempt_eligibility(invalid)


def test_terminal_success_is_recomputed_from_raw_pair_not_local_status() -> None:
    complete_pair = _attempt(0, "tse-001", 2, eligible=True)
    validated = validate_attempt_eligibility(complete_pair)
    assert validated["checks"]["terminal_success"] is True
    assert validated["eligible"] is True

    invalid = copy.deepcopy(complete_pair)
    invalid["checks"]["terminal_success"] = False
    invalid["eligible"] = False
    invalid["rejection_codes"] = ["check_failed_terminal_success"]
    invalid["failure"] = {
        "primary_check": "terminal_success",
        "stage": "terminal",
        "failure_class": "operational",
        "code": "terminal_status_interrupted",
        "retryable": True,
        "disposition": "retry_whole_roundtrip",
        "source_terminal_stage": "terminal",
        "source_terminal_class": "operational",
        "source_terminal_code": "terminal_status_interrupted",
    }
    with pytest.raises(QuotaArtifactError, match="quota_attempt_terminal_success_not_derived"):
        validate_attempt_eligibility(invalid)


def test_failure_diagnostic_is_sanitized_primary_and_provenance_blocks() -> None:
    rejected = _attempt(0, "tse-001", 2, eligible=False)
    rejected["failure"]["code"] = "timeout after leaking arbitrary text"
    with pytest.raises(QuotaArtifactError, match="quota_attempt_failure_code_invalid"):
        validate_attempt_eligibility(rejected)

    rejected = _attempt(0, "tse-001", 2, eligible=False)
    rejected["failure"]["code"] = "low_au_score"
    with pytest.raises(
        QuotaArtifactError, match="quota_attempt_failure_code_uses_excluded_input"
    ):
        validate_attempt_eligibility(rejected)

    unreadable = _attempt(0, "tse-001", 2, eligible=False)
    unreadable["failure"]["source_terminal_stage"] = None
    unreadable["failure"]["source_terminal_class"] = None
    unreadable["failure"]["source_terminal_code"] = None
    assert validate_attempt_eligibility(unreadable)["failure"]["source_terminal_code"] is None

    partial = copy.deepcopy(unreadable)
    partial["failure"]["source_terminal_stage"] = "infrastructure"
    with pytest.raises(QuotaArtifactError, match="quota_attempt_source_terminal_partial"):
        validate_attempt_eligibility(partial)

    invalid_source = _attempt(0, "tse-001", 2, eligible=False)
    invalid_source["failure"]["source_terminal_stage"] = "arbitrary_stage"
    with pytest.raises(
        QuotaArtifactError, match="quota_attempt_source_terminal_stage_invalid"
    ):
        validate_attempt_eligibility(invalid_source)

    blocked = _attempt(0, "tse-001", 2, eligible=True)
    blocked["checks"] = {name: True for name in SELECTION_POLICY["selection_inputs"]}
    blocked["checks"]["cell_identity_valid"] = False
    blocked["eligible"] = False
    blocked["rejection_codes"] = ["check_failed_cell_identity_valid"]
    blocked["failure"] = {
        "primary_check": "cell_identity_valid",
        "stage": "identity",
        "failure_class": "provenance",
        "code": "cell_identity_mismatch",
        "retryable": False,
        "disposition": "block_study",
        "source_terminal_stage": "infrastructure",
        "source_terminal_class": "unexpected_runtime",
        "source_terminal_code": "synthetic_wrapper_parse_failed",
    }
    assert validate_attempt_eligibility(blocked)["failure"]["disposition"] == "block_study"

    blocked["failure"]["retryable"] = True
    with pytest.raises(QuotaArtifactError, match="quota_attempt_failure_retryability_invalid"):
        validate_attempt_eligibility(blocked)

    mixed = _attempt(0, "tse-001", 2, eligible=False)
    mixed["checks"]["cell_identity_valid"] = False
    mixed["rejection_codes"] = [
        f"check_failed_{name}" for name, passed in mixed["checks"].items() if not passed
    ]
    mixed["failure"] = {
        "primary_check": "cell_identity_valid",
        "stage": "identity",
        "failure_class": "provenance",
        "code": "cell_identity_mismatch",
        "retryable": False,
        "disposition": "block_study",
        "source_terminal_stage": "infrastructure",
        "source_terminal_class": "unexpected_runtime",
        "source_terminal_code": "synthetic_wrapper_parse_failed",
    }
    assert validate_attempt_eligibility(mixed)["failure"]["disposition"] == "block_study"

    mixed["failure"] = {
        "primary_check": "provider_extraction_completed",
        "stage": "extraction_provider",
        "failure_class": "provider",
        "code": "provider_failure",
        "retryable": True,
        "disposition": "retry_whole_roundtrip",
        "source_terminal_stage": "infrastructure",
        "source_terminal_class": "unexpected_runtime",
        "source_terminal_code": "synthetic_wrapper_parse_failed",
    }
    with pytest.raises(QuotaArtifactError, match="quota_attempt_primary_failure_not_first"):
        validate_attempt_eligibility(mixed)


def test_selection_is_first_valid_has_every_prior_rejection_and_no_later_attempt() -> None:
    selected = _selection(1, "tse-019", selected_index=3)
    result = validate_selected_attempt(selected)
    assert result["selected_attempt_index"] == 3
    assert len(result["attempts"]) == 3

    invalid = copy.deepcopy(selected)
    invalid["attempts"][0]["eligibility"] = _attempt(1, "tse-019", 1, eligible=True)
    invalid["attempts"][0]["eligibility_sha256"] = document_sha256(
        invalid["attempts"][0]["eligibility"]
    )
    with pytest.raises(QuotaArtifactError, match="quota_selected_not_first_valid"):
        validate_selected_attempt(invalid)

    invalid = copy.deepcopy(selected)
    invalid["attempts"].append(
        {
            "eligibility_sha256": document_sha256(_attempt(1, "tse-019", 4, eligible=False)),
            "eligibility": _attempt(1, "tse-019", 4, eligible=False),
        }
    )
    with pytest.raises(QuotaArtifactError, match="quota_selected_attempt_sequence_invalid"):
        validate_selected_attempt(invalid)


def test_blocked_receipt_binds_cap_and_infrastructure_evidence_canonically() -> None:
    cap = _attempt(1, "tse-019", 10, eligible=False)
    cap_record = {
        "cell": cap["cell"],
        "reason": "attempt_cap_exhausted",
        "evidence_code": "attempt_cap_exhausted",
        "final_attempt_index": 10,
        "eligibility_sha256": document_sha256(cap),
        "eligibility": cap,
        "source_tree_sha256": cap["source_snapshot"]["tree_sha256"],
    }
    infrastructure_record = {
        "cell": {"run_index": 0, "method_id": "tse-002"},
        "reason": "java_infrastructure_failure",
        "evidence_code": "java_parser_dependency_mismatch",
        "final_attempt_index": 3,
        "eligibility_sha256": None,
        "eligibility": None,
        "source_tree_sha256": HASH,
    }
    receipt = quota_blocked_document(
        protocol_sha256=V06_HASH,
        blocked_at_utc="2026-08-12T12:34:56.789Z",
        # The builder canonicalizes an operationally discovered order.
        blocked_cells=[cap_record, infrastructure_record],
    )
    assert receipt["schema_version"] == QUOTA_BLOCKED_SCHEMA
    assert receipt["status"] == "blocked"
    assert receipt["primary_reason"] == "java_infrastructure_failure"
    assert [record["cell"] for record in receipt["blocked_cells"]] == [
        {"run_index": 0, "method_id": "tse-002"},
        {"run_index": 1, "method_id": "tse-019"},
    ]
    assert receipt["counts"] == {
        "blocked_cells": 2,
        "quota_satisfied": False,
        "by_run": {"0": 1, "1": 1, "2": 0},
        "by_reason": {
            "attempt_cap_exhausted": 1,
            "provenance_failure": 0,
            "java_infrastructure_failure": 1,
        },
    }
    assert validate_quota_blocked(receipt) == receipt


def test_blocked_receipt_rejects_ambiguous_or_outcome_tainted_evidence() -> None:
    cap = _attempt(0, "tse-001", 10, eligible=False)
    record = {
        "cell": cap["cell"],
        "reason": "attempt_cap_exhausted",
        "evidence_code": "attempt_cap_exhausted",
        "final_attempt_index": 10,
        "eligibility_sha256": document_sha256(cap),
        "eligibility": cap,
        "source_tree_sha256": cap["source_snapshot"]["tree_sha256"],
    }
    receipt = quota_blocked_document(
        protocol_sha256=V06_HASH,
        blocked_at_utc="2026-08-12T12:34:56.789Z",
        blocked_cells=[record],
    )

    invalid = copy.deepcopy(receipt)
    invalid["blocked_at_utc"] = "2026-08-12T12:34:56Z"
    with pytest.raises(QuotaArtifactError, match="quota_blocked_timestamp_invalid"):
        validate_quota_blocked(invalid)

    invalid = copy.deepcopy(record)
    invalid["final_attempt_index"] = 9
    invalid["eligibility"] = _attempt(0, "tse-001", 9, eligible=False)
    invalid["eligibility_sha256"] = document_sha256(invalid["eligibility"])
    invalid["source_tree_sha256"] = invalid["eligibility"]["source_snapshot"][
        "tree_sha256"
    ]
    with pytest.raises(QuotaArtifactError, match="quota_blocked_cap_evidence_invalid"):
        quota_blocked_document(
            protocol_sha256=V06_HASH,
            blocked_at_utc="2026-08-12T12:34:56.789Z",
            blocked_cells=[invalid],
        )

    invalid = copy.deepcopy(record)
    invalid["evidence_code"] = "low_au_score"
    with pytest.raises(
        QuotaArtifactError, match="quota_blocked_evidence_code_uses_excluded_input"
    ):
        quota_blocked_document(
            protocol_sha256=V06_HASH,
            blocked_at_utc="2026-08-12T12:34:56.789Z",
            blocked_cells=[invalid],
        )

    invalid = copy.deepcopy(receipt)
    invalid["blocked_cells"].append(copy.deepcopy(invalid["blocked_cells"][0]))
    invalid["counts"]["blocked_cells"] = 2
    invalid["counts"]["by_run"]["0"] = 2
    invalid["counts"]["by_reason"]["attempt_cap_exhausted"] = 2
    with pytest.raises(QuotaArtifactError, match="quota_blocked_cells_not_canonical"):
        validate_quota_blocked(invalid)


def test_blocked_receipt_accepts_explicit_or_observed_provenance_failure() -> None:
    blocked = _attempt(2, "tse-050", 4, eligible=True)
    blocked["checks"]["cell_identity_valid"] = False
    blocked["eligible"] = False
    blocked["rejection_codes"] = ["check_failed_cell_identity_valid"]
    blocked["failure"] = {
        "primary_check": "cell_identity_valid",
        "stage": "identity",
        "failure_class": "provenance",
        "code": "cell_identity_mismatch",
        "retryable": False,
        "disposition": "block_study",
        "source_terminal_stage": None,
        "source_terminal_class": None,
        "source_terminal_code": None,
    }
    record = {
        "cell": blocked["cell"],
        "reason": "provenance_failure",
        "evidence_code": "cell_identity_mismatch",
        "final_attempt_index": 4,
        "eligibility_sha256": document_sha256(blocked),
        "eligibility": blocked,
        "source_tree_sha256": blocked["source_snapshot"]["tree_sha256"],
    }
    receipt = quota_blocked_document(
        protocol_sha256=V06_HASH,
        blocked_at_utc="2026-08-12T12:34:56.789Z",
        blocked_cells=[record],
    )
    assert receipt["primary_reason"] == "provenance_failure"

    # A previously valid ledger record can also prove provenance failure when
    # the independently observed raw source tree no longer matches its binding.
    drifted = _attempt(0, "tse-003", 2, eligible=False)
    drift_record = {
        "cell": drifted["cell"],
        "reason": "provenance_failure",
        "evidence_code": "source_tree_snapshot_mismatch",
        "final_attempt_index": 2,
        "eligibility_sha256": document_sha256(drifted),
        "eligibility": drifted,
        "source_tree_sha256": HASH,
    }
    quota_blocked_document(
        protocol_sha256=V06_HASH,
        blocked_at_utc="2026-08-12T12:34:56.789Z",
        blocked_cells=[drift_record],
    )


def test_legacy_inventory_requires_exact_three_by_fifty_attempt_one_cells() -> None:
    legacy_protocol = hashlib.sha256(b"v0.5").hexdigest()
    root = f"artifacts/runs/{legacy_protocol}"
    cells = []
    root_files = []
    for run in RUN_INDICES:
        for method_id in METHOD_IDS:
            attempt = _attempt(
                run,
                method_id,
                1,
                eligible=False,
                source_kind="legacy-v0.5",
                protocol_hash=legacy_protocol,
                root=root,
            )
            cells.append(
                {"cell": attempt["cell"], "eligibility_sha256": document_sha256(attempt), "eligibility": attempt}
            )
            cell_file = attempt["source_snapshot"]["files"][0]
            root_files.append(
                {
                    **cell_file,
                    "path": f"run-{run}/{method_id}/{cell_file['path']}",
                }
            )
    schedule_payload = b"schedule\n"
    root_files.append(
        {
            "path": "schedule.json",
            "bytes": len(schedule_payload),
            "sha256": hashlib.sha256(schedule_payload).hexdigest(),
        }
    )
    root_files.sort(key=lambda item: item["path"])
    root_body = {
        "schema_version": "backtranslation.quota-source-tree.v1",
        "directories": [
            *[f"run-{run}" for run in RUN_INDICES],
            *[f"run-{run}/{method_id}" for run in RUN_INDICES for method_id in METHOD_IDS],
        ],
        "files": root_files,
    }
    root_body["directories"] = sorted(root_body["directories"])
    root_snapshot = {**root_body, "tree_sha256": document_sha256(root_body)}
    manifest_identity = {
        "path": "protocol/freeze-manifest-v1.json",
        "bytes": 100,
        "sha256": HASH,
    }
    record_identity = {
        "path": "protocol/freeze-record.jsonl",
        "bytes": 200,
        "sha256": HASH,
    }
    archive_body = {
        "schema_version": "backtranslation.quota-source-tree.v1",
        "directories": ["protocol"],
        "files": [dict(manifest_identity), dict(record_identity)],
    }
    archive_snapshot = {**archive_body, "tree_sha256": document_sha256(archive_body)}
    inventory = {
        "schema_version": LEGACY_INVENTORY_SCHEMA,
        "inventoried_at_utc": "2026-08-12T12:34:56.789Z",
        "origin": {
            "source_kind": "legacy-v0.5",
            "protocol_sha256": legacy_protocol,
            "source_root_path": root,
            "source_tree_sha256": root_snapshot["tree_sha256"],
        },
        "freeze_identity": {
            "authorized_manifest_sha256": legacy_protocol,
            "static_archive_root_path": "artifacts/provenance/v0.5-static",
            "static_archive_source_snapshot": archive_snapshot,
            "static_archive_source_tree_sha256": archive_snapshot["tree_sha256"],
            "freeze_manifest": dict(manifest_identity),
            "archived_freeze_manifest": {
                **manifest_identity,
                "path": "artifacts/provenance/v0.5-static/protocol/freeze-manifest-v1.json",
            },
            "execution_schedule": {
                "path": f"{root}/schedule.json",
                "bytes": len(schedule_payload),
                "sha256": hashlib.sha256(schedule_payload).hexdigest(),
            },
            "freeze_record_log": dict(record_identity),
            "archived_freeze_record_log": {
                **record_identity,
                "path": "artifacts/provenance/v0.5-static/protocol/freeze-record.jsonl",
            },
        },
        "source_snapshot": root_snapshot,
        "cells": cells,
    }
    result = validate_legacy_attempt_inventory(inventory)
    assert len(result["cells"]) == EXPECTED_CELL_COUNT

    invalid = copy.deepcopy(inventory)
    invalid["cells"] = invalid["cells"][:-1]
    with pytest.raises(QuotaArtifactError, match="quota_legacy_cell_count_invalid"):
        validate_legacy_attempt_inventory(invalid)

    invalid = copy.deepcopy(inventory)
    invalid["inventoried_at_utc"] = "2026-08-12T12:34:56Z"
    with pytest.raises(QuotaArtifactError, match="quota_legacy_inventory_timestamp_invalid"):
        validate_legacy_attempt_inventory(invalid)

    invalid = copy.deepcopy(inventory)
    invalid["freeze_identity"]["execution_schedule"]["sha256"] = HASH
    with pytest.raises(QuotaArtifactError, match="quota_legacy_schedule_snapshot_mismatch"):
        validate_legacy_attempt_inventory(invalid)

    invalid = copy.deepcopy(inventory)
    invalid["freeze_identity"]["freeze_record_log"]["path"] = "protocol/other.jsonl"
    with pytest.raises(QuotaArtifactError, match="quota_legacy_freeze_record_path_mismatch"):
        validate_legacy_attempt_inventory(invalid)

    invalid = copy.deepcopy(inventory)
    invalid["freeze_identity"]["archived_freeze_manifest"]["sha256"] = hashlib.sha256(
        b"different"
    ).hexdigest()
    with pytest.raises(QuotaArtifactError, match="quota_legacy_static_archive_original_mismatch"):
        validate_legacy_attempt_inventory(invalid)

    invalid = copy.deepcopy(inventory)
    invalid["freeze_identity"]["static_archive_root_path"] = "artifacts/provenance/live"
    with pytest.raises(QuotaArtifactError, match="quota_legacy_static_archive_root_mismatch"):
        validate_legacy_attempt_inventory(invalid)


def test_quota_complete_is_exactly_150_first_valid_cells_and_hash_bound() -> None:
    records = []
    view_cells = []
    view_files = []
    for run in RUN_INDICES:
        for method_id in METHOD_IDS:
            selection = _selection(run, method_id, 1)
            selection_hash = document_sha256(selection)
            records.append(
                {"selection_sha256": selection_hash, "selection": selection}
            )
            binding = {
                "path": f"run-{run}/{method_id}/selected-attempt.json",
                "bytes": len(canonical_json_bytes(selection)),
                "sha256": selection_hash,
            }
            view_files.append(binding)
            view_cells.append(
                {
                    "cell": {"run_index": run, "method_id": method_id},
                    "selection_sha256": selection_hash,
                    "binding_file": binding,
                }
            )
    view_body = {
        "schema_version": "backtranslation.quota-source-tree.v1",
        "directories": sorted(
            [
                *[f"run-{run}" for run in RUN_INDICES],
                *[f"run-{run}/{method_id}" for run in RUN_INDICES for method_id in METHOD_IDS],
            ]
        ),
        "files": view_files,
    }
    view_snapshot = {**view_body, "tree_sha256": document_sha256(view_body)}
    receipt = {
        "schema_version": QUOTA_COMPLETE_SCHEMA,
        "protocol_sha256": V06_HASH,
        "policy": copy.deepcopy(SELECTION_POLICY),
        "counts": {
            "runs": 3,
            "methods_per_run": 50,
            "required_cells": 150,
            "selected_cells": 150,
            "quota_satisfied": True,
        },
        "legacy_inventory_identity": {
            "path": "artifacts/provenance/legacy-attempt-inventory-v0.5.json",
            "bytes": 12345,
            "sha256": HASH,
            "source_tree_sha256": HASH,
            "authorized_manifest_sha256": HASH,
        },
        "attempt_counts": {
            "total_retained_attempts": 150,
            "rejected_attempts": 0,
            "selected_by_attempt_index": {
                **{str(index): 0 for index in range(1, 11)},
                "1": 150,
            },
            "attempts_to_success_histogram": {
                **{str(index): 0 for index in range(1, 11)},
                "1": 150,
            },
            "rejected_by_stage": {},
            "rejected_by_class": {},
            "rejected_by_code": {},
            "source_terminal_unreadable": 0,
            "rejected_by_source_terminal_stage": {},
            "rejected_by_source_terminal_class": {},
            "rejected_by_source_terminal_code": {},
            "selected_by_origin": {"legacy-v0.5": 0, "v0.6-retry": 150},
            "by_run": {
                str(run): {
                    "total_retained_attempts": 50,
                    "rejected_attempts": 0,
                    "attempts_to_success_histogram": {
                        **{str(index): 0 for index in range(1, 11)},
                        "1": 50,
                    },
                    "rejected_by_stage": {},
                    "rejected_by_class": {},
                    "rejected_by_code": {},
                    "source_terminal_unreadable": 0,
                    "rejected_by_source_terminal_stage": {},
                    "rejected_by_source_terminal_class": {},
                    "rejected_by_source_terminal_code": {},
                    "selected_by_origin": {"legacy-v0.5": 0, "v0.6-retry": 50},
                }
                for run in RUN_INDICES
            },
        },
        "selected_view": {
            "root_path": f"artifacts/runs/{V06_HASH}/selected-view",
            "source_snapshot": view_snapshot,
            "cells": view_cells,
        },
        "selections": records,
    }
    result = validate_quota_complete(receipt)
    assert len(result["selections"]) == 150

    invalid = copy.deepcopy(receipt)
    invalid["selections"][0]["selection_sha256"] = HASH
    with pytest.raises(QuotaArtifactError, match="quota_complete_selection_hash_mismatch"):
        validate_quota_complete(invalid)

    invalid = copy.deepcopy(receipt)
    invalid["counts"]["selected_cells"] = 149
    with pytest.raises(QuotaArtifactError, match="quota_complete_counts_mismatch"):
        validate_quota_complete(invalid)

    invalid = copy.deepcopy(receipt)
    invalid["attempt_counts"]["total_retained_attempts"] = 151
    with pytest.raises(QuotaArtifactError, match="quota_complete_attempt_counts_mismatch"):
        validate_quota_complete(invalid)

    invalid = copy.deepcopy(receipt)
    invalid["selected_view"]["cells"][0]["binding_file"] = {
        **invalid["selected_view"]["cells"][0]["binding_file"],
        "sha256": HASH,
    }
    with pytest.raises(QuotaArtifactError, match="quota_complete_selected_view_binding_hash_mismatch"):
        validate_quota_complete(invalid)

    invalid = copy.deepcopy(receipt)
    invalid["legacy_inventory_identity"]["bytes"] = 0
    with pytest.raises(QuotaArtifactError, match="quota_complete_legacy_bytes_invalid"):
        validate_quota_complete(invalid)

    with_rejection = copy.deepcopy(receipt)
    replacement = _selection(0, "tse-001", 2)
    replacement_hash = document_sha256(replacement)
    with_rejection["selections"][0] = {
        "selection_sha256": replacement_hash,
        "selection": replacement,
    }
    replacement_binding = {
        "path": "run-0/tse-001/selected-attempt.json",
        "bytes": len(canonical_json_bytes(replacement)),
        "sha256": replacement_hash,
    }
    with_rejection["selected_view"]["cells"][0] = {
        "cell": {"run_index": 0, "method_id": "tse-001"},
        "selection_sha256": replacement_hash,
        "binding_file": replacement_binding,
    }
    with_rejection["selected_view"]["source_snapshot"]["files"][0] = replacement_binding
    source_body = {
        key: value
        for key, value in with_rejection["selected_view"]["source_snapshot"].items()
        if key != "tree_sha256"
    }
    with_rejection["selected_view"]["source_snapshot"]["tree_sha256"] = document_sha256(
        source_body
    )
    with_rejection["attempt_counts"] = {
        "total_retained_attempts": 151,
        "rejected_attempts": 1,
        "selected_by_attempt_index": {
            **{str(index): 0 for index in range(1, 11)},
            "1": 149,
            "2": 1,
        },
        "attempts_to_success_histogram": {
            **{str(index): 0 for index in range(1, 11)},
            "1": 149,
            "2": 1,
        },
        "rejected_by_stage": {"extraction_provider": 1},
        "rejected_by_class": {"provider": 1},
        "rejected_by_code": {"provider_finish_reason_not_stop": 1},
        "source_terminal_unreadable": 0,
        "rejected_by_source_terminal_stage": {"extraction_api": 1},
        "rejected_by_source_terminal_class": {"provider": 1},
        "rejected_by_source_terminal_code": {"provider_finish_reason_not_stop": 1},
        "selected_by_origin": {"legacy-v0.5": 0, "v0.6-retry": 150},
        "by_run": copy.deepcopy(receipt["attempt_counts"]["by_run"]),
    }
    with_rejection["attempt_counts"]["by_run"]["0"] = {
        "total_retained_attempts": 51,
        "rejected_attempts": 1,
        "attempts_to_success_histogram": {
            **{str(index): 0 for index in range(1, 11)},
            "1": 49,
            "2": 1,
        },
        "rejected_by_stage": {"extraction_provider": 1},
        "rejected_by_class": {"provider": 1},
        "rejected_by_code": {"provider_finish_reason_not_stop": 1},
        "source_terminal_unreadable": 0,
        "rejected_by_source_terminal_stage": {"extraction_api": 1},
        "rejected_by_source_terminal_class": {"provider": 1},
        "rejected_by_source_terminal_code": {"provider_finish_reason_not_stop": 1},
        "selected_by_origin": {"legacy-v0.5": 0, "v0.6-retry": 50},
    }
    validate_quota_complete(with_rejection)
    with_rejection["attempt_counts"]["rejected_by_source_terminal_stage"] = {
        "regeneration_api": 1
    }
    with pytest.raises(QuotaArtifactError, match="quota_complete_attempt_counts_mismatch"):
        validate_quota_complete(with_rejection)
