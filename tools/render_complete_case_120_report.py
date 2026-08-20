#!/usr/bin/env python3
"""Render the August 12 complete-case result as Markdown and LaTeX."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import statistics
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
OUTPUT_MD = PROJECT / "reports" / "complete-case-120-correlation-2026-08-12.md"
OUTPUT_TEX = PROJECT / "reports" / "complete-case-120-correlation-2026-08-12.tex"
SCORE_DIGEST = "63be0044a56484c6021e8d995d28a94994c3338ab4afa1e911535e38692ab5ac"
SCORE_PATH = PROJECT / "artifacts" / "complete-case-120" / SCORE_DIGEST / "scores.json"
METRICS = (
    ("ruby", "RUBY-Java"),
    ("codebert", "CodeBERT"),
    ("rouge_1", "ROUGE-1"),
    ("rouge_2", "ROUGE-2"),
    ("rouge_l", "ROUGE-L"),
    ("bleu", "BLEU-4"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    return parser


def _number(value, digits: int = 3) -> str:
    if value is None:
        return "NA"
    number = float(value)
    if not math.isfinite(number):
        return "NA"
    return f"{number:.{digits}f}"


def _p(value) -> str:
    if value is None:
        return "NA"
    number = float(value)
    return "<0.001" if number < 0.001 else f"{number:.3f}"


def _cells(result: dict, outcome: str) -> list[tuple[str, ...]]:
    family = (
        result["holm_families"]["supporting_au_fidelity"]
        if outcome == "au"
        else result["holm_families"]["pbu_fidelity"]
    )
    rows: list[tuple[str, ...]] = []
    for key, label in METRICS:
        association = result["associations"][outcome][key]
        raw = association["raw_spearman"]
        partial = association["partial_spearman_loc"]
        interval = partial["bootstrap_95"]
        rows.append(
            (
                label,
                _number(raw["estimate"]),
                _p(raw.get("asymptotic_two_sided_p")),
                _number(partial["estimate"]),
                f"[{_number(interval['lower'])}, {_number(interval['upper'])}]",
                _p(partial["freedman_lane"]["two_sided_p"]),
                _p(family.get(key, {}).get("holm_adjusted_p")),
            )
        )
    return rows


def _markdown_table(result: dict, outcome: str) -> list[str]:
    lines = [
        "| Metric | Raw ρ | Raw p | LOC-adjusted ρ | Bootstrap 95% CI | Permutation p | Holm p |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in _cells(result, outcome))
    return lines


def _latex_table(result: dict, outcome: str, caption: str, label: str) -> list[str]:
    lines = [
        r"\begin{table*}[htbp]",
        r"\centering",
        rf"\caption{{{caption}}}",
        rf"\label{{{label}}}",
        r"\small",
        r"\begin{tabular}{lrrrrrr}",
        r"\toprule",
        r"Metric & Raw $\rho$ & Raw $p$ & LOC-adjusted $\rho$ & Bootstrap 95\% CI & Permutation $p$ & Holm $p$ \\",
        r"\midrule",
    ]
    for row in _cells(result, outcome):
        escaped = tuple(value.replace("<", r"$<$") for value in row)
        lines.append(" & ".join(escaped) + r" \\")
    lines.extend((r"\bottomrule", r"\end{tabular}", r"\end{table*}"))
    return lines


def _interpret_ruby(result: dict) -> str:
    primary = result["associations"]["au"]["ruby"]["partial_spearman_loc"]
    rho = primary["estimate"]
    interval = primary["bootstrap_95"]
    p_value = primary["freedman_lane"]["two_sided_p"]
    if rho is None:
        return "The primary RUBY–AU estimate was undefined."
    direction = "positive" if rho > 0 else "negative" if rho < 0 else "zero"
    excludes = (
        interval["lower"] is not None
        and interval["upper"] is not None
        and (interval["lower"] > 0 or interval["upper"] < 0)
    )
    return (
        f"The LOC-adjusted RUBY–AU association was {direction} "
        f"(partial Spearman ρ={rho:.3f}, 95% bootstrap CI "
        f"[{interval['lower']:.3f}, {interval['upper']:.3f}], "
        f"two-sided within-project permutation p={p_value:.3f}). "
        f"The interval {'excludes' if excludes else 'includes'} zero."
    )


def _similarity_summary() -> list[tuple[str, ...]]:
    score_bundle = json.loads(SCORE_PATH.read_text(encoding="utf-8"))
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in score_bundle["records"]:
        grouped[record["method_id"]].append(record["scores"])
    rows: list[tuple[str, ...]] = []
    for key, label in METRICS:
        method_means = [
            sum(float(run[key]) for run in runs) / len(runs)
            for runs in grouped.values()
        ]
        rows.append((
            label,
            _number(statistics.mean(method_means)),
            _number(statistics.median(method_means)),
            _number(statistics.stdev(method_means)),
            _number(min(method_means)),
            _number(max(method_means)),
        ))
    return rows


def _markdown_similarity_table() -> list[str]:
    return [
        "| Metric | Mean similarity | Median | SD | Minimum | Maximum |",
        "|---|---:|---:|---:|---:|---:|",
        *("| " + " | ".join(row) + " |" for row in _similarity_summary()),
    ]


def _latex_similarity_table() -> list[str]:
    lines = [
        r"\begin{table}[htbp]", r"\centering",
        r"\caption{Similarity between original Code1 and round-trip Code2 ($n=49$ method means).}",
        r"\label{tab:similarity}", r"\small", r"\begin{tabular}{lrrrrr}", r"\toprule",
        r"Metric & Mean & Median & SD & Minimum & Maximum \\", r"\midrule",
    ]
    lines.extend(" & ".join(row) + r" \\" for row in _similarity_summary())
    lines.extend((r"\bottomrule", r"\end{tabular}", r"\end{table}"))
    return lines


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise ValueError("complete_case_report_changed")
    if not path.exists():
        path.write_bytes(payload)


def main() -> int:
    args = _parser().parse_args()
    analysis = args.analysis if args.analysis.is_absolute() else PROJECT / args.analysis
    try:
        value = json.loads(analysis.read_text(encoding="utf-8"))
        _similarity_summary()
        analysis_digest = value["analysis_manifest_sha256"]
        interpretation = _interpret_ruby(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        print(json.dumps({"status": "blocked", "code": "complete_case_analysis_unavailable"}), file=sys.stderr)
        return 2

    md = [
        "# Complete-Case Round-Trip Similarity and Understandability",
        "",
        "**Report date:** August 12, 2026",
        "",
        "## Executive result",
        "",
        interpretation,
        "",
        "This analysis is descriptive and validity-conditioned; it has no GO/NO-GO gate.",
        "",
        "## How similar Code1 and Code2 are",
        "",
        "Scores closer to 1 mean Code2 is more like Code1. Each row summarizes 49 method-level values. If a method has one valid run, that run is used; if it has two or three, those valid runs are averaged first.",
        "",
        *_markdown_similarity_table(),
        "",
        "## Actual Understandability (AU)",
        "",
        *_markdown_table(value, "au"),
        "",
        "RUBY-Java is the primary ordered result. Holm adjustment applies to the five supporting AU metrics, not RUBY.",
        "",
        "## Perceived Understandability (PBU)",
        "",
        *_markdown_table(value, "pbu"),
        "",
        "The six PBU metrics form a separate Holm-adjusted supporting family.",
        "",
        "## Reproducibility",
        "",
        f"- Analysis manifest: `{analysis_digest}`",
        f"- Score manifest: `{SCORE_DIGEST}`",
        f"- Machine-readable analysis: `{analysis.relative_to(PROJECT).as_posix()}`",
        "- Frozen protocols: `protocol/PROTOCOL.complete-case-120.frozen.md` and `protocol/PROTOCOL.complete-case-120.outcome-amendment.frozen.md`",
        "",
    ]

    latex_interpretation = (
        interpretation.replace("ρ", r"$\rho$")
        .replace("%", r"\%")
        .replace("–", "--")
    )
    tex = [
        r"\documentclass[11pt]{article}",
        r"\usepackage[margin=1in]{geometry}",
        r"\usepackage{booktabs}",
        r"\usepackage{hyperref}",
        r"\title{Complete-Case Round-Trip Similarity and Understandability}",
        r"\author{Backtranslation Pilot}",
        r"\date{August 12, 2026}",
        r"\begin{document}",
        r"\maketitle",
        r"\section{Executive Result}",
        latex_interpretation,
        r"This analysis is descriptive and validity-conditioned; it has no GO/NO-GO gate.",
        r"\section{How Similar Code1 and Code2 Are}",
        r"Scores closer to one mean Code2 is more like Code1. Each row summarizes 49 method-level values. One valid run is used directly; two or three valid runs are averaged within method before this summary.",
        *_latex_similarity_table(),
        r"\section{Actual Understandability (AU)}",
        *_latex_table(value, "au", "Round-trip similarity associations with AU ($n=49$ methods).", "tab:au"),
        r"RUBY-Java is the primary ordered result. Holm adjustment applies to the five supporting AU metrics, not RUBY.",
        r"\section{Perceived Understandability (PBU)}",
        *_latex_table(value, "pbu", "Round-trip similarity associations with PBU ($n=49$ methods).", "tab:pbu"),
        r"The six PBU metrics form a separate Holm-adjusted supporting family.",
        r"\section{Reproducibility}",
        rf"Analysis manifest: \texttt{{{analysis_digest}}}.\\",
        rf"Score manifest: \texttt{{{SCORE_DIGEST}}}.\\",
        r"The machine-readable analysis and frozen protocol accompany this report.",
        r"\end{document}",
        "",
    ]
    try:
        _write_exact(OUTPUT_MD, "\n".join(md).encode("utf-8"))
        _write_exact(OUTPUT_TEX, "\n".join(tex).encode("utf-8"))
    except (OSError, ValueError):
        print(json.dumps({"status": "blocked", "code": "complete_case_report_changed"}), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "written",
        "markdown": OUTPUT_MD.relative_to(PROJECT).as_posix(),
        "latex": OUTPUT_TEX.relative_to(PROJECT).as_posix(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
