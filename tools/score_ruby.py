#!/usr/bin/env python3
"""Compute frozen outcome-blind RUBY-Java adaptation scores for pilot runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from backtranslation.artifacts import (  # noqa: E402
    ArtifactError,
    read_json_object,
    write_json_once,
)
from backtranslation.cases import load_study_cases  # noqa: E402
from backtranslation.directions import validate_regenerated_code  # noqa: E402
from backtranslation.execution import (  # noqa: E402
    ExecutionError,
    FROZEN_RUNS,
    initialize_schedule,
    verify_freeze_authorization,
)
from backtranslation.ruby_scoring import (  # noqa: E402
    RUBY_ARTIFACT,
    RUBY_FAILURE_ARTIFACT,
    RubyScoringError,
    ruby_failure_artifact,
    score_generated_run_ruby,
)


DEFAULT_MANIFEST = PROJECT / "protocol" / "freeze-manifest-v1.json"
DEFAULT_RECORD = PROJECT / "protocol" / "freeze-record.jsonl"
ARTIFACT_ROOT = PROJECT / "artifacts" / "runs"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "run"))
    return parser


def _counts(cases, digest: str) -> dict[str, int]:
    counts = {
        "generated_runs": 0,
        "not_generated": 0,
        "ruby_scores_complete": 0,
        "ruby_scores_failed": 0,
        "ruby_scores_pending": 0,
    }
    for run_index in FROZEN_RUNS:
        for case in cases:
            directory = (
                ARTIFACT_ROOT / digest / f"run-{run_index}" / case.method_id
            )
            terminal = directory / "status.json"
            if (
                not terminal.exists()
                or read_json_object(terminal).get("status") != "generated"
            ):
                counts["not_generated"] += 1
                continue
            counts["generated_runs"] += 1
            success = (directory / RUBY_ARTIFACT).exists()
            failure = (directory / RUBY_FAILURE_ARTIFACT).exists()
            if success and failure:
                raise RubyScoringError("ruby_score_success_and_failure")
            if success:
                counts["ruby_scores_complete"] += 1
            elif failure:
                counts["ruby_scores_failed"] += 1
            else:
                counts["ruby_scores_pending"] += 1
    return counts


def _code_2_identity(case, run_index: int, directory: Path) -> tuple[str, str]:
    try:
        regeneration = read_json_object(directory / "regeneration.result.json")
    except ArtifactError as exc:
        raise RubyScoringError(exc.code) from exc
    if (
        regeneration.get("method_id") != case.method_id
        or regeneration.get("run_index") != run_index
    ):
        raise RubyScoringError("ruby_regeneration_identity_invalid")
    try:
        regenerated = validate_regenerated_code(regeneration.get("output"))
    except ValueError as exc:
        raise RubyScoringError("ruby_regeneration_output_invalid") from exc
    digest = hashlib.sha256(regenerated.code.encode("utf-8")).hexdigest()
    if regeneration.get("code_2_sha256") != digest:
        raise RubyScoringError("ruby_regeneration_code_hash_mismatch")
    return regenerated.code, digest


def main() -> int:
    args = _parser().parse_args()
    try:
        authorization = verify_freeze_authorization(
            project_directory=PROJECT,
            manifest_path=DEFAULT_MANIFEST,
            freeze_record_path=DEFAULT_RECORD,
        )
        cases = load_study_cases(PROJECT / "data" / "tse")
        initialize_schedule(
            artifact_root=ARTIFACT_ROOT,
            cases=cases,
            authorization=authorization,
        )
        if args.command == "status":
            value = {
                "schema_version": "backtranslation.ruby_scoring_status.v1",
                "freeze_manifest_sha256": authorization.manifest_sha256,
                **_counts(cases, authorization.manifest_sha256),
            }
        else:
            attempted = 0
            for run_index in FROZEN_RUNS:
                for case in cases:
                    directory = (
                        ARTIFACT_ROOT
                        / authorization.manifest_sha256
                        / f"run-{run_index}"
                        / case.method_id
                    )
                    terminal = directory / "status.json"
                    if (
                        not terminal.exists()
                        or read_json_object(terminal).get("status") != "generated"
                        or (directory / RUBY_ARTIFACT).exists()
                        or (directory / RUBY_FAILURE_ARTIFACT).exists()
                    ):
                        continue
                    attempted += 1
                    _, code_2_sha256 = _code_2_identity(case, run_index, directory)
                    try:
                        score_generated_run_ruby(
                            case=case,
                            run_directory=directory,
                            freeze_manifest_sha256=authorization.manifest_sha256,
                        )
                    except RubyScoringError as exc:
                        failure = ruby_failure_artifact(
                            case=case,
                            run_index=run_index,
                            freeze_manifest_sha256=authorization.manifest_sha256,
                            code_2_sha256=code_2_sha256,
                            failure_code=exc.code,
                        )
                        try:
                            write_json_once(directory / RUBY_FAILURE_ARTIFACT, failure)
                        except ArtifactError as write_exc:
                            raise RubyScoringError(write_exc.code) from write_exc
            value = {
                "schema_version": "backtranslation.ruby_scoring_invocation.v1",
                "freeze_manifest_sha256": authorization.manifest_sha256,
                "attempted": attempted,
                **_counts(cases, authorization.manifest_sha256),
            }
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        ArtifactError,
        ExecutionError,
        OSError,
        RubyScoringError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", "ruby_scoring_failed")
        print(
            json.dumps({"status": "blocked", "code": code}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
