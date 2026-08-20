#!/usr/bin/env python3
"""Run the aligned CODE-UP human before/after backtranslation study."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from backtranslation.codeup_before_after import (
    build_aligned_cohort,
    build_results,
    generation_status,
    intent_status,
    write_reports,
    run_before_generation,
    run_intent_analysis,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("cohort", "run", "status", "intent", "intent-status", "score", "report")
    )
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--retained-cohort", type=Path)
    parser.add_argument("--pr-json-root", type=Path)
    parser.add_argument("--existing-root", type=Path)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--report-root", type=Path)
    args = parser.parse_args()
    project = args.project_root.resolve()
    output = args.output_root.resolve()
    if args.command == "cohort":
        if args.retained_cohort is None or args.pr_json_root is None:
            parser.error("cohort requires --retained-cohort and --pr-json-root")
        value = build_aligned_cohort(
            retained_cohort_path=args.retained_cohort.resolve(),
            pr_json_root=args.pr_json_root.resolve(),
            output_root=output,
        )
    elif args.command == "run":
        value = asyncio.run(run_before_generation(project, output, workers=args.workers))
    elif args.command == "status":
        value = generation_status(output)
    elif args.command == "intent":
        if args.existing_root is None:
            parser.error("intent requires --existing-root")
        value = asyncio.run(
            run_intent_analysis(
                project, output, args.existing_root.resolve(), workers=args.workers
            )
        )
    elif args.command == "intent-status":
        value = intent_status(output)
    elif args.command == "score":
        if args.existing_root is None:
            parser.error("score requires --existing-root")
        value = build_results(project, output, args.existing_root.resolve())
    else:
        if args.report_root is None:
            parser.error("report requires --report-root")
        markdown, latex = write_reports(output / "results.json", args.report_root.resolve())
        value = {"markdown": str(markdown), "latex": str(latex)}
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
