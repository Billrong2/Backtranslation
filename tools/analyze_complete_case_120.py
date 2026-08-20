#!/usr/bin/env python3
"""Score and analyze the frozen 120-valid complete-case cohort."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from backtranslation.complete_case_120 import (  # noqa: E402
    CompleteCaseError,
    build_score_bundle,
    fixed_cohort,
    score_and_analyze,
)
from backtranslation.execution import ExecutionError, verify_freeze_authorization  # noqa: E402
from backtranslation.artifacts import ArtifactError, canonical_json_bytes, write_bytes_once  # noqa: E402


MANIFEST = PROJECT / "protocol" / "freeze-manifest-complete-case-120.json"
RECORD = PROJECT / "protocol" / "freeze-record-complete-case-120.jsonl"
INVENTORY = PROJECT / "artifacts" / "provenance" / "legacy-attempt-inventory-v0.5.json"
OUTCOME = PROJECT / ".cache" / "tse" / "raw" / "understandability.csv"
SOURCE_MANIFEST = PROJECT / "data" / "tse" / "source_manifest.jsonl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "score", "run"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        authorization = verify_freeze_authorization(
            project_directory=PROJECT,
            manifest_path=MANIFEST,
            freeze_record_path=RECORD,
        )
        output = PROJECT / "artifacts" / "complete-case-120" / authorization.manifest_sha256
        if args.command == "status":
            cohort = fixed_cohort(project_directory=PROJECT, inventory_path=INVENTORY)
            result = {
                "status": "analyzed" if (output / "analysis.json").is_file() else (
                    "scored" if (output / "scores.json").is_file() else "ready"
                ),
                "analysis_manifest_sha256": authorization.manifest_sha256,
                "valid_cells": cohort["valid_cells"],
                "methods_with_valid_runs": cohort["methods_with_valid_runs"],
                "missing_method_ids": cohort["missing_method_ids"],
            }
        elif args.command == "score":
            score = build_score_bundle(
                project_directory=PROJECT,
                inventory_path=INVENTORY,
                analysis_manifest_sha256=authorization.manifest_sha256,
            )
            path = output / "scores.json"
            payload = canonical_json_bytes(score)
            if path.exists():
                if path.read_bytes() != payload:
                    raise CompleteCaseError("complete_case_scores_changed")
            else:
                write_bytes_once(path, payload)
            result = {
                "status": "scored",
                "analysis_manifest_sha256": authorization.manifest_sha256,
                "valid_cells": len(score["records"]),
            }
        else:
            result = score_and_analyze(
                project_directory=PROJECT,
                inventory_path=INVENTORY,
                analysis_manifest_sha256=authorization.manifest_sha256,
                output_directory=output,
                outcome_path=OUTCOME,
                source_manifest_path=SOURCE_MANIFEST,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ArtifactError, CompleteCaseError, ExecutionError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "code": getattr(exc, "code", "complete_case_failed")},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

