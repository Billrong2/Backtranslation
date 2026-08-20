#!/usr/bin/env python3
"""Analyze the frozen score bundle after the 444-row loader correction."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from backtranslation.artifacts import canonical_json_bytes, write_bytes_once  # noqa: E402
from backtranslation.complete_case_120 import (  # noqa: E402
    CompleteCaseError,
    SCORE_SCHEMA,
    aggregate_method_scores,
    analyze_score_bundle,
)
from backtranslation.execution import ExecutionError, verify_freeze_authorization  # noqa: E402


SCORE_FREEZE_DIGEST = "63be0044a56484c6021e8d995d28a94994c3338ab4afa1e911535e38692ab5ac"
SCORE_SHA256 = "7025cb1278cf40fff004770e74eb8ed26dfc927b451be5fcf9a6f169d065668c"
SCORE_PATH = PROJECT / "artifacts" / "complete-case-120" / SCORE_FREEZE_DIGEST / "scores.json"
MANIFEST = PROJECT / "protocol" / "freeze-manifest-complete-case-120-outcomes.json"
RECORD = PROJECT / "protocol" / "freeze-record-complete-case-120-outcomes.jsonl"
OUTCOME = PROJECT / ".cache" / "tse" / "raw" / "understandability.csv"
SOURCE_MANIFEST = PROJECT / "data" / "tse" / "source_manifest.jsonl"


def main() -> int:
    try:
        authorization = verify_freeze_authorization(
            project_directory=PROJECT,
            manifest_path=MANIFEST,
            freeze_record_path=RECORD,
        )
        payload = SCORE_PATH.read_bytes()
        import hashlib

        if hashlib.sha256(payload).hexdigest() != SCORE_SHA256:
            raise CompleteCaseError("complete_case_score_bundle_hash_mismatch")
        score_bundle = json.loads(payload)
        if (
            not isinstance(score_bundle, dict)
            or score_bundle.get("schema_version") != SCORE_SCHEMA
            or canonical_json_bytes(score_bundle) != payload
        ):
            raise CompleteCaseError("complete_case_score_bundle_invalid")
        aggregate_method_scores(score_bundle)
        result = analyze_score_bundle(
            score_bundle=score_bundle,
            outcome_path=OUTCOME,
            source_manifest_path=SOURCE_MANIFEST,
            analysis_manifest_sha256=authorization.manifest_sha256,
            parallel_workers=12,
        )
        output = PROJECT / "artifacts" / "complete-case-120" / authorization.manifest_sha256
        path = output / "analysis.json"
        encoded = canonical_json_bytes(result)
        if path.exists():
            if path.read_bytes() != encoded:
                raise CompleteCaseError("complete_case_analysis_changed")
        else:
            write_bytes_once(path, encoded)
        print(json.dumps({
            "status": "analyzed",
            "analysis_manifest_sha256": authorization.manifest_sha256,
            "score_manifest_sha256": SCORE_FREEZE_DIGEST,
            "valid_cells": 120,
            "methods": 49,
            "analysis_path": path.relative_to(PROJECT).as_posix(),
        }, sort_keys=True))
        return 0
    except (CompleteCaseError, ExecutionError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "blocked",
            "code": getattr(exc, "code", "complete_case_outcome_analysis_failed"),
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
