#!/usr/bin/env python3
"""Build and run the CODE-UP human-versus-agent paired study."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from backtranslation.codeup_human_agent import (
    build_human_agent_cohort,
    generation_status,
    run_generation,
)
from backtranslation.codeup_human_agent_analysis import (
    build_reports,
    build_results,
    intent_status,
    run_intent_analysis,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "cohort",
            "run",
            "status",
            "intent",
            "intent-status",
            "score",
            "report",
        ),
    )
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--stage-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--workers", type=int, default=64)
    args = parser.parse_args()
    project = args.project_root.resolve()
    output = (args.output_root or project / "artifacts" / "codeup-human-agent").resolve()
    if args.command == "cohort":
        if args.stage_root is None:
            parser.error("--stage-root is required when rebuilding the cohort")
        stage = args.stage_root.resolve()
        value = build_human_agent_cohort(project, stage, output)
    elif args.command == "run":
        value = asyncio.run(run_generation(project, output, workers=args.workers))
    elif args.command == "status":
        cohort = json.loads((output / "cohort.json").read_text())
        value = generation_status(output, len(cohort["cases"]))
    elif args.command == "intent":
        value = asyncio.run(run_intent_analysis(project, output, workers=args.workers))
    elif args.command == "intent-status":
        cohort = json.loads((output / "cohort.json").read_text())
        value = intent_status(output / "intent-analysis", len(cohort["cases"]))
    elif args.command == "score":
        value = build_results(project, output)
    else:
        markdown, latex = build_reports(output, project / "reports")
        value = {"markdown": str(markdown), "latex": str(latex)}
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
