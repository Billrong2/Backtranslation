#!/usr/bin/env python3
"""Compute the user-requested overall complete-case statistics in parallel."""

from __future__ import annotations

import hashlib
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


SCORE_PATH = PROJECT / "artifacts/complete-case-120/63be0044a56484c6021e8d995d28a94994c3338ab4afa1e911535e38692ab5ac/scores.json"
SCORE_SHA256 = "7025cb1278cf40fff004770e74eb8ed26dfc927b451be5fcf9a6f169d065668c"
OUTPUT = PROJECT / "artifacts/complete-case-120/results-2026-08-12/analysis.json"


def main() -> int:
    try:
        payload = SCORE_PATH.read_bytes()
        if hashlib.sha256(payload).hexdigest() != SCORE_SHA256:
            raise CompleteCaseError("score_bundle_hash_mismatch")
        scores = json.loads(payload)
        if scores.get("schema_version") != SCORE_SCHEMA or canonical_json_bytes(scores) != payload:
            raise CompleteCaseError("score_bundle_invalid")
        aggregate_method_scores(scores)
        result = analyze_score_bundle(
            score_bundle=scores,
            outcome_path=PROJECT / ".cache/tse/raw/understandability.csv",
            source_manifest_path=PROJECT / "data/tse/source_manifest.jsonl",
            analysis_manifest_sha256="user-directed-results-2026-08-12",
            parallel_workers=12,
        )
        encoded = canonical_json_bytes(result)
        if OUTPUT.exists():
            if OUTPUT.read_bytes() != encoded:
                raise CompleteCaseError("analysis_result_changed")
        else:
            write_bytes_once(OUTPUT, encoded)
        print(json.dumps({"status": "analyzed", "path": OUTPUT.relative_to(PROJECT).as_posix()}))
        return 0
    except (CompleteCaseError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "code": getattr(exc, "code", "result_failed")}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
