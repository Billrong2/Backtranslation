"""Human before/after revision backtranslation study for CODE-UP.

The study uses only CODE-UP reviews labeled as understandability-related.  It
reconstructs byte-aligned old/new fragments from the same human revision diff,
round-trips the old fragment, and reuses the already retained round trips of
the human and agent revisions.  Intent references are extracted independently
from each source fragment; the review request is never an intent reference.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import json
import math
from pathlib import Path
from typing import Any

from backtranslation.codeup_human_agent import (
    INTENT_MODEL,
    MAX_ATTEMPTS,
    ProviderUnavailableError,
    _call_retained,
    _code_output,
    _directions_output,
    _json_object,
    _review_event,
    backtranslation_code_prompt,
    backtranslation_directions_prompt,
    prepare_codex_home,
)
from backtranslation.codeup_human_agent_analysis import (
    _descriptive,
    _paired_statistic,
    _token_metrics,
    intent_metrics,
    static_features,
    validate_code_judgment,
    validate_reference,
)
from backtranslation.codeup_stage1 import (
    Stage1Error,
    canonical_json_bytes,
    codebert_batch_similarities,
    fragment_tokens,
    load_json,
    sha256_bytes,
    write_bytes_once,
    write_json_once,
)
from backtranslation.scoring import load_pinned_codebert


SCHEMA_VERSION = "codeup.before-after.v1"
GENERATION_MODEL = "deepseek-v4-flash"
ARMS = ("before", "human", "agent")

REPORT_METRICS = {
    "roundtrip_codebert": "Round-trip CodeBERT similarity",
    "roundtrip_bleu": "Round-trip BLEU",
    "roundtrip_rouge_l": "Round-trip ROUGE-L F1",
    "intent_fidelity": "Code-derived intent fidelity",
    "strict_preservation_rate": "Strict intent-preservation rate",
    "intent_count_original": "Number of source-code intents",
    "roundtrip_change_intent_count": "Intent-count change after round trip",
    "roundtrip_change_ccn_proxy": "Cyclomatic-complexity proxy (source, round-trip)",
    "roundtrip_change_smell_count": "Code-smell count (source, round-trip)",
}


def diff_old_side(chunk: str) -> str:
    """Return the context/removal side of one unified-diff chunk."""

    lines: list[str] = []
    for line in chunk.splitlines():
        if line.startswith(("@@", "diff --git ", "index ", "--- ", "+++ ")):
            continue
        if line == "\\ No newline at end of file" or line.startswith("+"):
            continue
        lines.append(line[1:] if line.startswith(("-", " ")) else line)
    return "\n".join(lines).strip()


def diff_new_side(chunk: str) -> str:
    """Return the context/addition side of one unified-diff chunk."""

    lines: list[str] = []
    for line in chunk.splitlines():
        if line.startswith(("@@", "diff --git ", "index ", "--- ", "+++ ")):
            continue
        if line == "\\ No newline at end of file" or line.startswith("-"):
            continue
        lines.append(line[1:] if line.startswith(("+", " ")) else line)
    return "\n".join(lines).strip()


def aligned_revision(event: Mapping[str, Any], path: str) -> tuple[str, str, str]:
    """Return commit plus aligned old/new sides for the target file."""

    revision = event.get("revised_code")
    if not isinstance(revision, Mapping) or not isinstance(revision.get("commit"), str):
        raise Stage1Error("before_after_revision_missing")
    chunks = revision.get("changed_code")
    if not isinstance(chunks, list):
        raise Stage1Error("before_after_changed_code_missing")
    old_fragments: list[str] = []
    new_fragments: list[str] = []
    for item in chunks:
        if not isinstance(item, Mapping):
            continue
        header = item.get("header")
        chunk = item.get("chunk")
        if not isinstance(header, str) or not isinstance(chunk, str) or f"b/{path}" not in header:
            continue
        old = diff_old_side(chunk)
        new = diff_new_side(chunk)
        if old:
            old_fragments.append(old)
        if new:
            new_fragments.append(new)
    before = "\n\n".join(old_fragments)
    after = "\n\n".join(new_fragments)
    if not fragment_tokens(before) or not fragment_tokens(after) or before == after:
        raise Stage1Error("before_after_aligned_fragment_invalid")
    return str(revision["commit"]), before, after


def build_aligned_cohort(
    *, retained_cohort_path: Path, pr_json_root: Path, output_root: Path
) -> dict[str, Any]:
    retained = load_json(retained_cohort_path)
    cases: list[dict[str, Any]] = []
    for source in retained["cases"]:
        if source.get("human_understandability") != "yes":
            continue
        document_path = (
            pr_json_root
            / str(source["project"]).replace("/", "_")
            / f"pr_{source['pr_number']}.json"
        )
        payload = document_path.read_bytes()
        if sha256_bytes(payload) != source["source_pr_json_sha256"]:
            raise Stage1Error("before_after_pr_json_hash_mismatch")
        document = json.loads(payload)
        event = _review_event(document, str(source["review_id"]))
        commit, before, human = aligned_revision(event, str(source["path"]))
        if commit != source["human_revision_commit"] or human != source["human_revision_code"]:
            raise Stage1Error("before_after_human_revision_mismatch")
        cases.append(
            {
                "case_id": source["case_id"],
                "project": source["project"],
                "pr_number": source["pr_number"],
                "path": source["path"],
                "review_id": source["review_id"],
                "revision_commit": commit,
                "before_code": before,
                "before_code_sha256": sha256_bytes(before.encode("utf-8")),
                "human_revision_code": human,
                "human_revision_code_sha256": sha256_bytes(human.encode("utf-8")),
                "source_pr_json_sha256": source["source_pr_json_sha256"],
            }
        )
    cases.sort(key=lambda item: str(item["case_id"]))
    if len(cases) != 503:
        raise Stage1Error(f"before_after_case_count_invalid:{len(cases)}")
    value = {
        "schema_version": f"{SCHEMA_VERSION}.cohort",
        "design": {
            "cases": len(cases),
            "selection": "CODE-UP understandability=yes with retained nonempty same-file human revision",
            "alignment": "old and new sides of the exact same revision diff chunks",
            "review_request_used_as_intent_reference": False,
            "source_authorship": {"before": "human", "human": "human", "agent": "model"},
        },
        "source_cohort_sha256": sha256_bytes(retained_cohort_path.read_bytes()),
        "cases": cases,
    }
    write_json_once(output_root / "cohort.json", value)
    return value


def _selected_attempt(root: Path, case_id: str) -> Mapping[str, Any]:
    selected_path = root / "runs" / case_id / "selected.json"
    selected = load_json(selected_path)
    attempt_path = root / str(selected["attempt_path"])
    if sha256_bytes(attempt_path.read_bytes()) != selected["attempt_sha256"]:
        raise Stage1Error("before_after_selected_attempt_hash_mismatch")
    attempt = load_json(attempt_path)
    if attempt.get("status") != "valid" or attempt.get("case_id") != case_id:
        raise Stage1Error("before_after_selected_attempt_invalid")
    return attempt


async def _generate_before_case(
    case: Mapping[str, Any], *, project_root: Path, output_root: Path,
    codex_home: Path, semaphore: asyncio.Semaphore, timeout_seconds: int,
    abort: asyncio.Event,
) -> Mapping[str, Any]:
    cell = output_root / "runs" / str(case["case_id"])
    selected_path = cell / "selected.json"
    if selected_path.exists():
        return load_json(selected_path)
    existing = [
        int(path.name.removeprefix("attempt-"))
        for path in cell.glob("attempt-*")
        if path.is_dir() and path.name.removeprefix("attempt-").isdigit()
    ] if cell.exists() else []
    for attempt_index in range(max(existing, default=0) + 1, MAX_ATTEMPTS + 1):
        attempt_root = cell / f"attempt-{attempt_index:03d}"
        try:
            directions = _directions_output(
                await _call_retained(
                    "before-directions",
                    backtranslation_directions_prompt(str(case["before_code"])),
                    attempt_root=attempt_root, project_root=project_root,
                    codex_home=codex_home, semaphore=semaphore,
                    timeout_seconds=timeout_seconds, abort=abort,
                )
            )
            reconstructed = _code_output(
                await _call_retained(
                    "before-reconstruction", backtranslation_code_prompt(directions),
                    attempt_root=attempt_root, project_root=project_root,
                    codex_home=codex_home, semaphore=semaphore,
                    timeout_seconds=timeout_seconds, abort=abort,
                )
            )
            attempt = {
                "schema_version": f"{SCHEMA_VERSION}.generation-attempt",
                "status": "valid", "case_id": case["case_id"],
                "attempt_index": attempt_index, "model": GENERATION_MODEL,
                "before_code_sha256": case["before_code_sha256"],
                "directions": directions,
                "directions_sha256": sha256_bytes(canonical_json_bytes(directions)),
                "reconstructed_code": reconstructed,
                "reconstructed_code_sha256": sha256_bytes(reconstructed.encode("utf-8")),
            }
            write_json_once(attempt_root / "attempt.json", attempt)
            attempt_path = attempt_root / "attempt.json"
            selected = {
                "schema_version": f"{SCHEMA_VERSION}.generation-selected",
                "case_id": case["case_id"], "attempt_index": attempt_index,
                "attempt_path": str(attempt_path.relative_to(output_root)),
                "attempt_sha256": sha256_bytes(attempt_path.read_bytes()),
            }
            write_json_once(selected_path, selected)
            return selected
        except ProviderUnavailableError:
            abort.set()
            raise
        except (Stage1Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            write_json_once(
                attempt_root / "attempt.json",
                {"schema_version": f"{SCHEMA_VERSION}.generation-attempt",
                 "status": "invalid", "case_id": case["case_id"],
                 "attempt_index": attempt_index, "model": GENERATION_MODEL,
                 "failure_code": str(exc)},
            )
    raise Stage1Error(f"before_after_attempt_cap_exhausted:{case['case_id']}")


async def run_before_generation(
    project_root: Path, output_root: Path, *, workers: int = 64,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    cohort = load_json(output_root / "cohort.json")
    semaphore = asyncio.Semaphore(workers)
    abort = asyncio.Event()
    codex_home = prepare_codex_home(project_root, output_root, "flash")
    tasks = [
        asyncio.create_task(
            _generate_before_case(
                case, project_root=project_root, output_root=output_root,
                codex_home=codex_home, semaphore=semaphore,
                timeout_seconds=timeout_seconds, abort=abort,
            )
        )
        for case in cohort["cases"]
        if not (output_root / "runs" / str(case["case_id"]) / "selected.json").exists()
    ]
    errors: list[str] = []
    for future in asyncio.as_completed(tasks):
        try:
            await future
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")
    selected = len(list((output_root / "runs").glob("*/selected.json")))
    status = {"schema_version": f"{SCHEMA_VERSION}.generation-status",
              "planned": len(cohort["cases"]), "selected": selected,
              "pending": len(cohort["cases"]) - selected, "errors": errors[:20],
              "complete": selected == len(cohort["cases"]) and not errors}
    if not status["complete"]:
        raise Stage1Error(f"before_after_generation_incomplete:{selected}:{len(cohort['cases'])}")
    write_json_once(output_root / "generation-summary.json", status)
    return status


def generation_status(output_root: Path) -> dict[str, Any]:
    cohort = load_json(output_root / "cohort.json")
    selected = len(list((output_root / "runs").glob("*/selected.json")))
    return {"schema_version": f"{SCHEMA_VERSION}.generation-status",
            "planned": len(cohort["cases"]), "selected": selected,
            "pending": len(cohort["cases"]) - selected,
            "complete": selected == len(cohort["cases"])}


def source_intents_prompt(code: str, label: str) -> str:
    return (
        "Independently extract the implementation intent of this Java fragment. Do not use "
        "tools, external context, a review comment, or generated directions. List atomic, "
        "testable behavioral and structural requirements expressed by the code without adding "
        "requirements. Return exactly one JSON object and no Markdown: "
        '{"intents":["atomic intent","..."]}. Use 1 to 40 concise nonempty intents.\n\n'
        f"VERSION:\n{label}\n\nJAVA FRAGMENT:\n{code}"
    )


def source_roundtrip_intent_prompt(original: str, reconstructed: str, label: str) -> str:
    """Request an independent code-derived intent reference and its preservation judgment."""

    return (
        "Independently extract the implementation intent of the SOURCE Java fragment, then "
        "judge whether its ROUND-TRIP RECONSTRUCTION preserves each extracted intent. Do not "
        "use tools, external context, a review comment, or generated directions. Extract only "
        "atomic, testable behavioral and structural requirements expressed by the source code; "
        "do not add requirements. Return exactly one JSON object and no Markdown with this "
        "shape: {\"source_intents\":[\"atomic intent\",\"...\"],\"judgment\":{"
        "\"code_intents\":[\"intent visible in reconstruction\",\"...\"],"
        "\"reference_statuses\":[\"preserved|changed|lost\"],"
        "\"added_code_intent_indices\":[0]}}. "
        "Use 1 to 40 concise nonempty source intents. Include exactly one assessment for every "
        "source intent, in index order. Added indices must point to code_intents that express "
        "behavior introduced by the reconstruction and absent from source_intents.\n\n"
        f"VERSION:\n{label}\n\nSOURCE JAVA FRAGMENT:\n{original}\n\n"
        f"ROUND-TRIP RECONSTRUCTION:\n{reconstructed}"
    )


async def _intent_case(
    case: Mapping[str, Any], *, project_root: Path, output_root: Path,
    existing_root: Path, codex_home: Path, semaphore: asyncio.Semaphore,
    timeout_seconds: int, abort: asyncio.Event,
) -> Mapping[str, Any]:
    cell = output_root / "intent-runs" / str(case["case_id"])
    selected_path = cell / "selected.json"
    if selected_path.exists():
        return load_json(selected_path)
    before_attempt = _selected_attempt(output_root, str(case["case_id"]))
    existing = _selected_attempt(existing_root, str(case["case_id"]))
    sources = {
        "before": (str(case["before_code"]), str(before_attempt["reconstructed_code"])),
        "human": (str(existing["arms"]["human"]["original_code"]), str(existing["arms"]["human"]["reconstructed_code"])),
        "agent": (str(existing["arms"]["agent"]["original_code"]), str(existing["arms"]["agent"]["reconstructed_code"])),
    }
    if sources["human"][0] != case["human_revision_code"]:
        raise Stage1Error("before_after_retained_human_mismatch")
    existing_indices = [
        int(path.name.removeprefix("attempt-")) for path in cell.glob("attempt-*")
        if path.is_dir() and path.name.removeprefix("attempt-").isdigit()
    ] if cell.exists() else []
    for attempt_index in range(max(existing_indices, default=0) + 1, MAX_ATTEMPTS + 1):
        attempt_root = cell / f"attempt-{attempt_index:03d}"
        try:
            arms: dict[str, Any] = {}
            for arm, (original, reconstructed) in sources.items():
                response = _json_object(await _call_retained(
                    f"{arm}-source-roundtrip-intent",
                    source_roundtrip_intent_prompt(original, reconstructed, arm),
                    attempt_root=attempt_root, project_root=project_root,
                    codex_home=codex_home, semaphore=semaphore,
                    timeout_seconds=timeout_seconds, abort=abort,
                ))
                reference = validate_reference({"intents": response.get("source_intents")})
                judgment_value = response.get("judgment")
                if not isinstance(judgment_value, Mapping):
                    raise Stage1Error("before_after_intent_judgment_missing")
                judgment = validate_code_judgment(judgment_value, len(reference))
                arms[arm] = {"source_intents": reference,
                             "source_intents_sha256": sha256_bytes(canonical_json_bytes(reference)),
                             "judgment": judgment, "metrics": intent_metrics(judgment)}
            attempt = {"schema_version": f"{SCHEMA_VERSION}.intent-attempt",
                       "status": "valid", "case_id": case["case_id"],
                       "attempt_index": attempt_index, "model": INTENT_MODEL,
                       "review_request_used": False, "arms": arms}
            write_json_once(attempt_root / "attempt.json", attempt)
            attempt_path = attempt_root / "attempt.json"
            selected = {"schema_version": f"{SCHEMA_VERSION}.intent-selected",
                        "case_id": case["case_id"], "attempt_index": attempt_index,
                        "attempt_path": str(attempt_path.relative_to(output_root)),
                        "attempt_sha256": sha256_bytes(attempt_path.read_bytes())}
            write_json_once(selected_path, selected)
            return selected
        except ProviderUnavailableError:
            abort.set()
            raise
        except (Stage1Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            write_json_once(attempt_root / "attempt.json",
                            {"schema_version": f"{SCHEMA_VERSION}.intent-attempt",
                             "status": "invalid", "case_id": case["case_id"],
                             "attempt_index": attempt_index, "model": INTENT_MODEL,
                             "failure_code": str(exc)})
    raise Stage1Error(f"before_after_intent_cap_exhausted:{case['case_id']}")


async def run_intent_analysis(
    project_root: Path, output_root: Path, existing_root: Path, *, workers: int = 64,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    cohort = load_json(output_root / "cohort.json")
    semaphore = asyncio.Semaphore(workers)
    abort = asyncio.Event()
    codex_home = prepare_codex_home(project_root, output_root, "pro")
    tasks = [asyncio.create_task(_intent_case(
        case, project_root=project_root, output_root=output_root,
        existing_root=existing_root, codex_home=codex_home,
        semaphore=semaphore, timeout_seconds=timeout_seconds, abort=abort,
    )) for case in cohort["cases"]
        if not (output_root / "intent-runs" / str(case["case_id"]) / "selected.json").exists()]
    errors: list[str] = []
    for future in asyncio.as_completed(tasks):
        try:
            await future
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")
    selected = len(list((output_root / "intent-runs").glob("*/selected.json")))
    status = {"schema_version": f"{SCHEMA_VERSION}.intent-status",
              "planned": len(cohort["cases"]), "selected": selected,
              "pending": len(cohort["cases"]) - selected, "errors": errors[:20],
              "complete": selected == len(cohort["cases"]) and not errors}
    if not status["complete"]:
        raise Stage1Error(f"before_after_intent_incomplete:{selected}:{len(cohort['cases'])}")
    write_json_once(output_root / "intent-summary.json", status)
    return status


def intent_status(output_root: Path) -> dict[str, Any]:
    cohort = load_json(output_root / "cohort.json")
    selected = len(list((output_root / "intent-runs").glob("*/selected.json")))
    return {"schema_version": f"{SCHEMA_VERSION}.intent-status",
            "planned": len(cohort["cases"]), "selected": selected,
            "pending": len(cohort["cases"]) - selected,
            "complete": selected == len(cohort["cases"])}


def build_results(project_root: Path, output_root: Path, existing_root: Path) -> dict[str, Any]:
    cohort = load_json(output_root / "cohort.json")
    existing_cohort = load_json(existing_root / "cohort.json")
    existing_cases = {str(case["case_id"]): case for case in existing_cohort["cases"]}
    if not load_json(output_root / "generation-summary.json").get("complete"):
        raise Stage1Error("before_after_generation_not_complete")
    if not load_json(output_root / "intent-summary.json").get("complete"):
        raise Stage1Error("before_after_intent_not_complete")
    rows: list[dict[str, Any]] = []
    codebert_pairs: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    exact_agent_input_matches = 0
    for case in cohort["cases"]:
        case_id = str(case["case_id"])
        existing_case = existing_cases.get(case_id)
        if not isinstance(existing_case, Mapping):
            raise Stage1Error("before_after_existing_case_missing")
        exact_agent_input_matches += int(
            str(existing_case["pre_review_code"]) == str(case["before_code"])
        )
        before_attempt = _selected_attempt(output_root, case_id)
        existing = _selected_attempt(existing_root, case_id)
        intent_selected = load_json(output_root / "intent-runs" / case_id / "selected.json")
        intent_path = output_root / str(intent_selected["attempt_path"])
        if sha256_bytes(intent_path.read_bytes()) != intent_selected["attempt_sha256"]:
            raise Stage1Error("before_after_intent_selected_hash_mismatch")
        intent_attempt = load_json(intent_path)
        if (
            intent_attempt.get("status") != "valid"
            or intent_attempt.get("case_id") != case_id
            or set(intent_attempt.get("arms", {})) != set(ARMS)
        ):
            raise Stage1Error("before_after_intent_selected_invalid")
        sources = {
            "before": (str(case["before_code"]), str(before_attempt["reconstructed_code"])),
            "human": (str(existing["arms"]["human"]["original_code"]), str(existing["arms"]["human"]["reconstructed_code"])),
            "agent": (str(existing["arms"]["agent"]["original_code"]), str(existing["arms"]["agent"]["reconstructed_code"])),
        }
        arm_rows: dict[str, Any] = {}
        for arm, (original, reconstructed) in sources.items():
            original_tokens = fragment_tokens(original)
            reconstructed_tokens = fragment_tokens(reconstructed)
            codebert_pairs.append((original_tokens, reconstructed_tokens))
            token = _token_metrics(original, reconstructed)
            arm_rows[arm] = {
                "roundtrip_bleu": token["bleu"],
                "roundtrip_rouge_l": token["rouge_l_f1"],
                "intent_fidelity": intent_attempt["arms"][arm]["metrics"]["intent_fidelity_f1"],
                "strict_preservation_rate": intent_attempt["arms"][arm]["metrics"]["strict_preservation_rate"],
                "intent_count_original": len(intent_attempt["arms"][arm]["source_intents"]),
                "intent_count_roundtrip": intent_attempt["arms"][arm]["metrics"]["code_intent_count"],
                "static_original": static_features(original),
                "static_roundtrip": static_features(reconstructed),
            }
        rows.append({"case_id": case_id, "arms": arm_rows})
    tokenizer, model = load_pinned_codebert(
        project_root / "models" / "codebert-base",
        project_root / "config" / "codebert-base-revision.json",
    )
    similarities, device = codebert_batch_similarities(codebert_pairs, tokenizer=tokenizer, model=model)
    cursor = 0
    for row in rows:
        for arm in ARMS:
            row["arms"][arm]["roundtrip_codebert"] = similarities[cursor]
            cursor += 1
    extractors = {
        "roundtrip_codebert": lambda v: float(v["roundtrip_codebert"]),
        "roundtrip_bleu": lambda v: float(v["roundtrip_bleu"]),
        "roundtrip_rouge_l": lambda v: float(v["roundtrip_rouge_l"]),
        "intent_fidelity": lambda v: float(v["intent_fidelity"]),
        "strict_preservation_rate": lambda v: float(v["strict_preservation_rate"]),
        "intent_count_original": lambda v: float(v["intent_count_original"]),
        "ccn_proxy_original": lambda v: float(v["static_original"]["cyclomatic_complexity_proxy"]),
        "smell_count_original": lambda v: float(v["static_original"]["smell_count"]),
        "token_count_original": lambda v: float(v["static_original"]["token_count"]),
        "roundtrip_change_intent_count": lambda v: float(v["intent_count_roundtrip"] - v["intent_count_original"]),
        "roundtrip_change_ccn_proxy": lambda v: float(v["static_roundtrip"]["cyclomatic_complexity_proxy"] - v["static_original"]["cyclomatic_complexity_proxy"]),
        "roundtrip_change_smell_count": lambda v: float(v["static_roundtrip"]["smell_count"] - v["static_original"]["smell_count"]),
    }
    comparisons: dict[str, Any] = {}
    for metric, extractor in extractors.items():
        values = {arm: [extractor(row["arms"][arm]) for row in rows] for arm in ARMS}
        comparisons[metric] = {
            **{arm: _descriptive(values[arm]) for arm in ARMS},
            "before_vs_human": _paired_statistic(values["before"], values["human"]),
            "human_vs_agent": _paired_statistic(values["human"], values["agent"]),
        }
    result = {
        "schema_version": f"{SCHEMA_VERSION}.results",
        "design": {"cases": len(rows), "selection": cohort["design"]["selection"],
                   "aligned_before_after": True, "review_request_used_as_intent_reference": False,
                   "agent_same_review_event": True,
                   "agent_input_context_exact_matches_aligned_before": exact_agent_input_matches,
                   "agent_input_context_exact_match_rate": exact_agent_input_matches / len(rows),
                   "generation_model": GENERATION_MODEL, "intent_model": INTENT_MODEL,
                   "codebert_device": device},
        "comparisons": comparisons,
        "case_rows": rows,
    }
    write_json_once(output_root / "results.json", result)
    return result


def _fmt(value: float) -> str:
    return "NA" if not math.isfinite(value) else f"{value:.4f}"


def _fmt_p(value: float) -> str:
    if not math.isfinite(value):
        return "NA"
    return f"{value:.2e}" if value < 0.0001 else f"{value:.4f}"


def _bold_max(values: Mapping[str, float], *, latex: bool = False) -> dict[str, str]:
    maximum = max(values.values())
    result: dict[str, str] = {}
    for key, value in values.items():
        formatted = _fmt(value)
        if value == maximum and sum(candidate == maximum for candidate in values.values()) == 1:
            formatted = rf"\textbf{{{formatted}}}" if latex else f"**{formatted}**"
        result[key] = formatted
    return result


def render_reports(results: Mapping[str, Any]) -> tuple[str, str]:
    """Render the August 20 Markdown and LaTeX reports from aggregate results."""

    comparisons = results["comparisons"]
    cases = int(results["design"]["cases"])
    required_metrics = set(REPORT_METRICS) | {"ccn_proxy_original", "smell_count_original"}
    if cases != 503 or not required_metrics.issubset(comparisons):
        raise Stage1Error("before_after_report_contract_invalid")
    markdown = [
        "# CODE-UP Before/After Revision and Round-Trip Study",
        "",
        "**Report date:** August 20, 2026<br>",
        f"**Aligned paired cases:** {cases:,}<br>",
        "**Backtranslation model:** `deepseek-v4-flash` through separate Codex instances<br>",
        "**Code-derived intent extraction/judging:** `deepseek-v4-pro` through separate Codex instances",
        "",
        "## Study design",
        "",
        "For each understandability-related CODE-UP revision, the old and new fragments come from the two sides of the same human-authored revision diff. The old human code is the **Before revision** arm; the new human code is the **Human revision** arm. The **Agent revision** arm is the retained independent model revision for the same review event, generated from CODE-UP's review-target context; that context is not generally byte-identical to the aligned old diff fragment. Each arm is separately translated from code to natural-language directions and back to code. The review comment is used to identify the revision but is not used as the intent reference. Instead, Pro extracts intent independently from each source fragment and judges only that source against its own reconstruction.",
        "",
        "## Overall statistics",
        "",
        "The largest numeric arm mean in each row is bolded; bold does not imply that a larger value is desirable for complexity, smell, size, or change measures.",
        "",
        "- **Measure:** the code similarity, code-derived intent, size, complexity, smell, or round-trip-change statistic.",
        "- **Before revision:** mean for the human-authored old side of the aligned revision diff.",
        "- **Human revision:** mean for the human-authored new side of that same diff.",
        "- **Agent revision:** mean for the retained model revision from the same CODE-UP review event; its review-target input scope usually differs from the exact old diff fragment.",
        "- **Wilcoxon p:** paired two-sided Wilcoxon signed-rank p-value for Before revision versus Human revision; the Agent revision is not part of this column.",
        "- **AUC:** descriptive one-variable AUC for separating Before revision (positive class) from Human revision. Values near 0.5 mean little separation; direction is visible from the means.",
        "- **Separation:** `max(AUC, 1-AUC)`, an unsigned effect-separation summary from 0.5 to 1.0.",
        "- **Code-smell pair:** in the `(A, B)` row, `A` is the mean smell count in the source fragment and `B` is the mean smell count after round-trip reconstruction. Its Wilcoxon, AUC, and separation values compare the per-case changes `B - A` between arms.",
        "- **Cyclomatic-complexity pair:** in the `(A, B)` row, `A` is the mean source CCN proxy and `B` is the mean reconstructed CCN proxy. Its Wilcoxon, AUC, and separation values compare the per-case changes `B - A` between arms.",
        "",
        "| Measure | Before revision | Human revision | Agent revision | Wilcoxon p | AUC | Separation |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    latex_rows: list[str] = []
    human_agent_rows: list[str] = []
    latex_human_agent_rows: list[str] = []
    for key, label in REPORT_METRICS.items():
        item = comparisons[key]
        means = {arm: float(item[arm]["mean"]) for arm in ARMS}
        pair_source_key = {
            "roundtrip_change_ccn_proxy": "ccn_proxy_original",
            "roundtrip_change_smell_count": "smell_count_original",
        }.get(key)
        if pair_source_key is not None:
            source_item = comparisons[pair_source_key]
            cells = {
                arm: f"({_fmt(float(source_item[arm]['mean']))}, "
                f"{_fmt(float(source_item[arm]['mean']) + means[arm])})"
                for arm in ARMS
            }
        else:
            cells = _bold_max(means)
        before_human = item["before_vs_human"]
        markdown.append(
            f"| {label} | {cells['before']} | {cells['human']} | {cells['agent']} | "
            f"{_fmt_p(float(before_human['paired_wilcoxon_p_value']))} | "
            f"{_fmt(float(before_human['roc_auc_human_as_positive']))} | "
            f"{_fmt(float(before_human['roc_auc_separation']))} |"
        )
        if pair_source_key is not None:
            source_item = comparisons[pair_source_key]
            latex_cells = {
                arm: f"({_fmt(float(source_item[arm]['mean']))}, "
                f"{_fmt(float(source_item[arm]['mean']) + means[arm])})"
                for arm in ARMS
            }
        else:
            latex_cells = _bold_max(means, latex=True)
        safe_label = label.replace("&", r"\&").replace("%", r"\%").replace("_", r"\_")
        latex_rows.append(
            f"{safe_label} & {latex_cells['before']} & {latex_cells['human']} & "
            f"{latex_cells['agent']} & {_fmt_p(float(before_human['paired_wilcoxon_p_value']))} & "
            f"{_fmt(float(before_human['roc_auc_human_as_positive']))} & "
            f"{_fmt(float(before_human['roc_auc_separation']))} \\\\"
        )
        human_agent = item["human_vs_agent"]
        human_agent_p = float(human_agent["paired_wilcoxon_p_value"])
        human_agent_auc = float(human_agent["roc_auc_human_as_positive"])
        human_agent_separation = float(human_agent["roc_auc_separation"])
        major_difference = human_agent_p < 0.05 and human_agent_separation >= 0.55
        secondary_label = f"**{label}**" if major_difference else label
        secondary_p = _fmt_p(human_agent_p)
        secondary_auc = _fmt(human_agent_auc)
        secondary_separation = _fmt(human_agent_separation)
        if major_difference:
            secondary_p = f"**{secondary_p}**"
            secondary_auc = f"**{secondary_auc}**"
            secondary_separation = f"**{secondary_separation}**"
        human_agent_rows.append(
            f"| {secondary_label} | {secondary_p} | {secondary_auc} | "
            f"{secondary_separation} |"
        )
        latex_secondary_label = rf"\textbf{{{safe_label}}}" if major_difference else safe_label
        latex_secondary_p = _fmt_p(human_agent_p)
        latex_secondary_auc = _fmt(human_agent_auc)
        latex_secondary_separation = _fmt(human_agent_separation)
        if major_difference:
            latex_secondary_p = rf"\textbf{{{latex_secondary_p}}}"
            latex_secondary_auc = rf"\textbf{{{latex_secondary_auc}}}"
            latex_secondary_separation = rf"\textbf{{{latex_secondary_separation}}}"
        latex_human_agent_rows.append(
            f"{latex_secondary_label} & {latex_secondary_p} & {latex_secondary_auc} & "
            f"{latex_secondary_separation} \\\\"
        )
    markdown.extend([
        "",
        "## Human-versus-agent paired contrasts",
        "",
        "This secondary table keeps the Human revision versus Agent revision inferential comparison separate from the main before-versus-after question. Here AUC treats Human revision as the positive class. Because the retained agent input uses CODE-UP's review-target scope rather than the exact old diff side, this is a same-event comparison with a known granularity limitation.",
        "Bold marks a major difference under the report's display rule: `p < 0.05` and separation at least `0.55`.",
        "",
        "| Measure | Wilcoxon p | AUC | Separation |",
        "|---|---:|---:|---:|",
        *human_agent_rows,
        "",
        "## Main findings",
        "",
        f"- Mean round-trip CodeBERT similarity is {_fmt(float(comparisons['roundtrip_codebert']['before']['mean']))} before revision, {_fmt(float(comparisons['roundtrip_codebert']['human']['mean']))} after human revision, and {_fmt(float(comparisons['roundtrip_codebert']['agent']['mean']))} after agent revision.",
        f"- Mean code-derived intent fidelity is {_fmt(float(comparisons['intent_fidelity']['before']['mean']))} before revision, {_fmt(float(comparisons['intent_fidelity']['human']['mean']))} after human revision, and {_fmt(float(comparisons['intent_fidelity']['agent']['mean']))} after agent revision.",
        f"- Mean strict intent preservation is {_fmt(float(comparisons['strict_preservation_rate']['before']['mean']))} before revision, {_fmt(float(comparisons['strict_preservation_rate']['human']['mean']))} after human revision, and {_fmt(float(comparisons['strict_preservation_rate']['agent']['mean']))} after agent revision.",
        f"- The aligned human revision lowers the mean CCN proxy from {_fmt(float(comparisons['ccn_proxy_original']['before']['mean']))} to {_fmt(float(comparisons['ccn_proxy_original']['human']['mean']))}.",
        "- CodeBERT, BLEU, and ROUGE measure code-to-code resemblance. Intent fidelity is a separate Pro judgment derived only from each source fragment and its reconstruction, so high lexical similarity is not treated as proof of intent preservation.",
        "",
        "## Interpretation boundaries",
        "",
        "- Before revision versus Human revision is an exact aligned comparison over the same 503 diff chunks.",
        "- Before revision and Human revision are both human-authored. Agent revision is model-authored.",
        f"- Only {int(results['design'].get('agent_input_context_exact_matches_aligned_before', 0))} of {cases} retained agent input contexts is byte-identical to the aligned Before revision fragment; the Agent column is therefore secondary, not part of the core before-versus-after test.",
        "- The review comment does not define the measured intent and is never shown to the intent extractor/judge.",
        "- Results measure conditional preservation through this specific Flash backtranslation and Pro intent-analysis procedure, not functional equivalence established by compilation or tests.",
        "- The p-values are exploratory and unadjusted across metrics. AUC is descriptive separation, not held-out predictive performance.",
        "",
    ])
    latex = rf"""\documentclass[11pt]{{article}}
\usepackage[margin=1in]{{geometry}}
\usepackage{{booktabs,longtable,array,pdflscape}}
\title{{CODE-UP Before/After Revision and Round-Trip Study}}
\date{{August 20, 2026}}
\begin{{document}}
\maketitle

\textbf{{Aligned paired cases:}} {cases}. \textbf{{Backtranslation:}} deepseek-v4-flash through separate Codex instances. \textbf{{Code-derived intent analysis:}} deepseek-v4-pro through separate Codex instances.

\section*{{Study design}}
For each understandability-related CODE-UP revision, the old and new fragments are the two sides of the same human-authored revision diff. They form the Before revision and Human revision arms. Agent revision is the retained independent model revision from the same review event and uses CODE-UP's review-target context, which generally has a different fragment scope. Every arm is independently round-tripped. The review comment identifies the revision but is not an intent reference.

\section*{{Column definitions}}
\begin{{itemize}}
\item \textbf{{Measure:}} the reported similarity, intent, size, complexity, smell, or change statistic.
\item \textbf{{Before revision:}} mean for the human-authored old diff side.
\item \textbf{{Human revision:}} mean for the human-authored new diff side.
\item \textbf{{Agent revision:}} mean for the retained model revision from the same review event; its review-target input scope usually differs from the exact old diff fragment.
\item \textbf{{Wilcoxon p:}} paired two-sided Before-versus-Human signed-rank p-value.
\item \textbf{{AUC:}} Before-positive descriptive separation from Human; 0.5 indicates little separation.
\item \textbf{{Separation:}} $\max(\mathrm{{AUC}},1-\mathrm{{AUC}})$.
\item \textbf{{Code-smell pair:}} in the $(A,B)$ row, $A$ is mean source smell count and $B$ is mean reconstructed smell count. Its inferential columns compare per-case $B-A$ changes between arms.
\item \textbf{{Cyclomatic-complexity pair:}} in the $(A,B)$ row, $A$ is mean source CCN proxy and $B$ is mean reconstructed CCN proxy. Its inferential columns compare per-case $B-A$ changes between arms.
\end{{itemize}}
The largest numeric arm mean is bolded; larger is not necessarily better for complexity, smells, size, or changes.

\begin{{landscape}}
\small
\setlength{{\tabcolsep}}{{4pt}}
\begin{{longtable}}{{p{{5.0cm}}rrrrrr}}
\toprule
Measure & Before revision & Human revision & Agent revision & Wilcoxon $p$ & AUC & Separation \\
\midrule
\endfirsthead
\toprule
Measure & Before revision & Human revision & Agent revision & Wilcoxon $p$ & AUC & Separation \\
\midrule
\endhead
{chr(10).join(latex_rows)}
\bottomrule
\end{{longtable}}
\end{{landscape}}

\section*{{Human-versus-agent paired contrasts}}
This secondary table separates Human revision versus Agent revision inference from the main before-versus-after question. AUC treats Human revision as the positive class. The retained agent input uses CODE-UP's review-target scope rather than the exact old diff side, so this is a same-event comparison with a known granularity limitation. Bold marks $p<0.05$ with separation at least $0.55$.

\begin{{longtable}}{{p{{8.5cm}}rrr}}
\toprule
Measure & Wilcoxon $p$ & AUC & Separation \\
\midrule
{chr(10).join(latex_human_agent_rows)}
\bottomrule
\end{{longtable}}

\section*{{Interpretation}}
CodeBERT, BLEU, and ROUGE measure code resemblance. The Pro intent analysis separately extracts intent from each source fragment and judges its own reconstruction without the review comment or generated directions. Before and Human are exact aligned diff sides; Agent is a secondary same-event arm with different source granularity. P-values are exploratory and unadjusted; AUC is descriptive rather than held-out predictive performance.

\end{{document}}
"""
    return "\n".join(markdown), latex


def write_reports(results_path: Path, report_root: Path) -> tuple[Path, Path]:
    markdown, latex = render_reports(load_json(results_path))
    markdown_path = report_root / "2026-08-20-codeup-human-vs-agent.md"
    latex_path = report_root / "2026-08-20-codeup-human-vs-agent.tex"
    write_json_once(
        report_root / "report-source.json",
        {"schema_version": f"{SCHEMA_VERSION}.report-source",
         "results_sha256": sha256_bytes(results_path.read_bytes())},
    )
    write_bytes_once(markdown_path, markdown.encode("utf-8"))
    write_bytes_once(latex_path, latex.encode("utf-8"))
    return markdown_path, latex_path
