"""Intent, similarity, static-feature, and report stages for CODE-UP human vs agent."""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Callable, Mapping, Sequence

from backtranslation.codeup_human_agent import (
    INTENT_MODEL,
    ProviderUnavailableError,
    SCHEMA_VERSION,
    _call_retained,
    _json_object,
    prepare_codex_home,
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
from backtranslation.scoring import bleu_score, load_pinned_codebert, rouge_scores


INTENT_MAX_ATTEMPTS = 100
INTENT_STAGES = (
    "reference",
    "human-original",
    "human-roundtrip",
    "agent-original",
    "agent-roundtrip",
)
STATUS_VALUES = {"preserved", "changed", "lost"}


def reference_intent_prompt(review_request: str, path: str) -> str:
    return (
        "You are extracting the implementation intent of a Java code-review request written "
        "before the requested revision. Do not use tools or inspect files. Split the request "
        "into atomic, testable requirements without adding requirements or implementation "
        "details. Return exactly one JSON object and no Markdown: "
        '{"intents":["atomic intent","..."]}. Use 1 to 40 concise nonempty intents.\n\n'
        f"FILE PATH:\n{path}\n\nREVIEW REQUEST:\n{review_request}"
    )


def code_intent_prompt(reference: Sequence[str], code: str, label: str) -> str:
    return (
        "You are independently evaluating one Java fragment against implementation intents "
        "that were recorded before the revision. Do not use tools or inspect files. First list "
        "the atomic behavioral intents actually expressed by the code. Then label every "
        "zero-based reference intent preserved when fully present, changed when partially "
        "present or materially altered, and lost when absent or contradicted. Finally list "
        "indices of code intents that add meaningful behavior with no reference counterpart. "
        "Equivalent implementations count as preserved; formatting and syntax alone do not. "
        "Return exactly one JSON object and no Markdown: "
        '{"code_intents":["..."],"reference_statuses":["preserved"],'
        '"added_code_intent_indices":[0]}. The reference_statuses length must exactly equal '
        f"{len(reference)} and added indices must be valid.\n\nARM/STAGE:\n{label}\n\n"
        "REFERENCE INTENTS:\n"
        + json.dumps(list(reference), ensure_ascii=False)
        + "\n\nJAVA FRAGMENT:\n"
        + code
    )


def validate_reference(value: Mapping[str, Any]) -> list[str]:
    intents = value.get("intents")
    if not isinstance(intents, list) or not 1 <= len(intents) <= 40:
        raise Stage1Error("human_agent_reference_intents_invalid")
    normalized: list[str] = []
    for item in intents:
        if not isinstance(item, str) or not item.strip() or len(item) > 4000:
            raise Stage1Error("human_agent_reference_intent_invalid")
        normalized.append(item.strip())
    return normalized


def validate_code_judgment(
    value: Mapping[str, Any], reference_count: int
) -> dict[str, Any]:
    code_intents = value.get("code_intents")
    statuses = value.get("reference_statuses")
    added = value.get("added_code_intent_indices")
    if not isinstance(code_intents, list) or not 0 <= len(code_intents) <= 80:
        raise Stage1Error("human_agent_code_intents_invalid")
    normalized: list[str] = []
    for item in code_intents:
        if not isinstance(item, str) or not item.strip() or len(item) > 4000:
            raise Stage1Error("human_agent_code_intent_invalid")
        normalized.append(item.strip())
    if (
        not isinstance(statuses, list)
        or len(statuses) != reference_count
        or any(status not in STATUS_VALUES for status in statuses)
    ):
        raise Stage1Error("human_agent_reference_statuses_invalid")
    if not isinstance(added, list):
        raise Stage1Error("human_agent_added_intents_invalid")
    added_indices: list[int] = []
    for index in added:
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= len(normalized)
            or index in added_indices
        ):
            raise Stage1Error("human_agent_added_intent_index_invalid")
        added_indices.append(index)
    return {
        "code_intents": normalized,
        "reference_statuses": list(statuses),
        "added_code_intent_indices": sorted(added_indices),
    }


def intent_metrics(judgment: Mapping[str, Any]) -> dict[str, Any]:
    statuses = list(judgment["reference_statuses"])
    count = len(statuses)
    preserved = statuses.count("preserved")
    changed = statuses.count("changed")
    lost = statuses.count("lost")
    added = len(judgment["added_code_intent_indices"])
    code_count = len(judgment["code_intents"])
    weighted = (preserved + 0.5 * changed) / count
    addition_rate = 0.0 if code_count == 0 else added / code_count
    precision = 1.0 - addition_rate
    fidelity = 0.0 if weighted + precision == 0 else 2 * weighted * precision / (weighted + precision)
    return {
        "reference_intent_count": count,
        "code_intent_count": code_count,
        "preserved_count": preserved,
        "changed_count": changed,
        "lost_count": lost,
        "added_count": added,
        "strict_preservation_rate": preserved / count,
        "weighted_preservation_rate": weighted,
        "change_rate": changed / count,
        "loss_rate": lost / count,
        "addition_rate": addition_rate,
        "intent_fidelity_f1": fidelity,
    }


def selected_generation(output_root: Path, case_id: str) -> dict[str, Any]:
    selected_path = output_root / "runs" / case_id / "selected.json"
    selected = load_json(selected_path)
    attempt_path = output_root / selected["attempt_path"]
    raw = attempt_path.read_bytes()
    if sha256_bytes(raw) != selected.get("attempt_sha256"):
        raise Stage1Error("human_agent_generation_selected_hash_mismatch")
    attempt = load_json(attempt_path)
    if attempt.get("status") != "valid" or attempt.get("case_id") != case_id:
        raise Stage1Error("human_agent_generation_selected_invalid")
    return {"selected": selected, "attempt": attempt, "attempt_path": attempt_path}


def _selected_intent_stage(intent_root: Path, case_id: str, stage: str) -> Mapping[str, Any]:
    selected = load_json(intent_root / "runs" / case_id / stage / "selected.json")
    if selected.get("case_id") != case_id or selected.get("stage") != stage:
        raise Stage1Error("human_agent_intent_selected_identity_mismatch")
    attempt_path = intent_root / selected["attempt_path"]
    if sha256_bytes(attempt_path.read_bytes()) != selected.get("attempt_sha256"):
        raise Stage1Error("human_agent_intent_selected_hash_mismatch")
    attempt = load_json(attempt_path)
    if (
        attempt.get("status") != "valid"
        or attempt.get("case_id") != case_id
        or attempt.get("stage") != stage
        or attempt.get("model") != INTENT_MODEL
    ):
        raise Stage1Error("human_agent_intent_selected_invalid")
    return attempt


def verified_intent_case(
    output_root: Path, intent_root: Path, case_id: str
) -> Mapping[str, Any]:
    source = selected_generation(output_root, case_id)
    reference = _selected_intent_stage(intent_root, case_id, "reference")["value"]
    validate_reference({"intents": reference})
    judgments: dict[str, Any] = {}
    for stage in INTENT_STAGES[1:]:
        value = _selected_intent_stage(intent_root, case_id, stage)["value"]
        validated = validate_code_judgment(value, len(reference))
        if value != validated:
            raise Stage1Error("human_agent_intent_judgment_not_normalized")
        judgments[stage] = {"value": value, "metrics": intent_metrics(value)}
    expected = {
        "schema_version": f"{SCHEMA_VERSION}.intent-case",
        "case_id": case_id,
        "generation_attempt_path": str(source["attempt_path"].relative_to(output_root)),
        "generation_attempt_sha256": sha256_bytes(source["attempt_path"].read_bytes()),
        "reference_intents": reference,
        "reference_intents_sha256": sha256_bytes(canonical_json_bytes(reference)),
        "model": INTENT_MODEL,
        "judgments": judgments,
    }
    actual = load_json(intent_root / "runs" / case_id / "complete.json")
    if actual != expected:
        raise Stage1Error("human_agent_intent_case_mismatch")
    return actual


async def _intent_stage(
    *,
    case_id: str,
    stage: str,
    prompt: str,
    validator: Callable[[Mapping[str, Any]], Any],
    intent_root: Path,
    project_root: Path,
    codex_home: Path,
    semaphore: asyncio.Semaphore,
    timeout_seconds: int,
    abort: asyncio.Event,
) -> Mapping[str, Any]:
    stage_root = intent_root / "runs" / case_id / stage
    selected_path = stage_root / "selected.json"
    if selected_path.exists():
        selected = load_json(selected_path)
        attempt_path = intent_root / selected["attempt_path"]
        if sha256_bytes(attempt_path.read_bytes()) != selected.get("attempt_sha256"):
            raise Stage1Error("human_agent_intent_selected_hash_mismatch")
        attempt = load_json(attempt_path)
        if attempt.get("status") != "valid":
            raise Stage1Error("human_agent_intent_selected_invalid")
        return attempt
    existing = [
        int(path.name.removeprefix("attempt-"))
        for path in stage_root.glob("attempt-*")
        if path.is_dir() and path.name.removeprefix("attempt-").isdigit()
    ] if stage_root.exists() else []
    for attempt_index in range(max(existing, default=0) + 1, INTENT_MAX_ATTEMPTS + 1):
        attempt_root = stage_root / f"attempt-{attempt_index:03d}"
        try:
            raw = await _call_retained(
                "response",
                prompt,
                attempt_root=attempt_root,
                project_root=project_root,
                codex_home=codex_home,
                semaphore=semaphore,
                timeout_seconds=timeout_seconds,
                abort=abort,
            )
            parsed = validator(_json_object(raw))
            value = {
                "schema_version": f"{SCHEMA_VERSION}.intent-stage",
                "status": "valid",
                "case_id": case_id,
                "stage": stage,
                "attempt_index": attempt_index,
                "model": INTENT_MODEL,
                "value": parsed,
            }
            write_json_once(attempt_root / "attempt.json", value)
            selected = {
                "schema_version": f"{SCHEMA_VERSION}.intent-selected",
                "case_id": case_id,
                "stage": stage,
                "attempt_index": attempt_index,
                "attempt_path": str((attempt_root / "attempt.json").relative_to(intent_root)),
                "attempt_sha256": sha256_bytes((attempt_root / "attempt.json").read_bytes()),
            }
            write_json_once(selected_path, selected)
            return value
        except ProviderUnavailableError as exc:
            write_json_once(
                attempt_root / "attempt.json",
                {
                    "schema_version": f"{SCHEMA_VERSION}.intent-stage",
                    "status": "provider_unavailable",
                    "case_id": case_id,
                    "stage": stage,
                    "attempt_index": attempt_index,
                    "model": INTENT_MODEL,
                    "failure_code": str(exc),
                },
            )
            raise
        except (Stage1Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            write_json_once(
                attempt_root / "attempt.json",
                {
                    "schema_version": f"{SCHEMA_VERSION}.intent-stage",
                    "status": "invalid",
                    "case_id": case_id,
                    "stage": stage,
                    "attempt_index": attempt_index,
                    "model": INTENT_MODEL,
                    "failure_code": str(exc),
                },
            )
    raise Stage1Error(f"human_agent_intent_attempt_cap_exhausted:{case_id}:{stage}")


async def evaluate_intent_case(
    case: Mapping[str, Any],
    *,
    project_root: Path,
    output_root: Path,
    intent_root: Path,
    codex_home: Path,
    semaphore: asyncio.Semaphore,
    timeout_seconds: int,
    abort: asyncio.Event,
) -> Mapping[str, Any]:
    complete_path = intent_root / "runs" / str(case["case_id"]) / "complete.json"
    if complete_path.exists():
        return verified_intent_case(output_root, intent_root, str(case["case_id"]))
    source = selected_generation(output_root, str(case["case_id"]))
    attempt = source["attempt"]
    reference_stage = await _intent_stage(
        case_id=str(case["case_id"]),
        stage="reference",
        prompt=reference_intent_prompt(str(case["review_request"]), str(case["path"])),
        validator=validate_reference,
        intent_root=intent_root,
        project_root=project_root,
        codex_home=codex_home,
        semaphore=semaphore,
        timeout_seconds=timeout_seconds,
        abort=abort,
    )
    reference = reference_stage["value"]
    fragments = {
        "human-original": attempt["arms"]["human"]["original_code"],
        "human-roundtrip": attempt["arms"]["human"]["reconstructed_code"],
        "agent-original": attempt["arms"]["agent"]["original_code"],
        "agent-roundtrip": attempt["arms"]["agent"]["reconstructed_code"],
    }
    judgments: dict[str, Any] = {}
    for stage, code in fragments.items():
        result = await _intent_stage(
            case_id=str(case["case_id"]),
            stage=stage,
            prompt=code_intent_prompt(reference, str(code), stage),
            validator=lambda value, count=len(reference): validate_code_judgment(value, count),
            intent_root=intent_root,
            project_root=project_root,
            codex_home=codex_home,
            semaphore=semaphore,
            timeout_seconds=timeout_seconds,
            abort=abort,
        )
        judgments[stage] = {
            "value": result["value"],
            "metrics": intent_metrics(result["value"]),
        }
    complete = {
        "schema_version": f"{SCHEMA_VERSION}.intent-case",
        "case_id": case["case_id"],
        "generation_attempt_path": str(source["attempt_path"].relative_to(output_root)),
        "generation_attempt_sha256": sha256_bytes(source["attempt_path"].read_bytes()),
        "reference_intents": reference,
        "reference_intents_sha256": sha256_bytes(canonical_json_bytes(reference)),
        "model": INTENT_MODEL,
        "judgments": judgments,
    }
    write_json_once(complete_path, complete)
    return complete


def intent_status(intent_root: Path, planned: int) -> dict[str, Any]:
    completed = len(list(intent_root.glob("runs/*/complete.json")))
    selected = len(list(intent_root.glob("runs/*/*/selected.json")))
    attempts = list(intent_root.glob("runs/*/*/attempt-*/attempt.json"))
    invalid = sum(load_json(path).get("status") != "valid" for path in attempts)
    return {
        "schema_version": f"{SCHEMA_VERSION}.intent-status",
        "planned_cases": planned,
        "planned_model_calls": planned * len(INTENT_STAGES),
        "complete_cases": completed,
        "selected_valid_stages": selected,
        "attempts": len(attempts),
        "invalid_attempts": invalid,
        "complete": completed == planned,
    }


async def run_intent_analysis(
    project_root: Path,
    output_root: Path,
    *,
    workers: int = 64,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    cohort = load_json(output_root / "cohort.json")
    cases = cohort["cases"]
    if len(list(output_root.glob("runs/*/selected.json"))) != len(cases):
        raise Stage1Error("human_agent_generation_not_complete")
    intent_root = output_root / "intent-analysis"
    semaphore = asyncio.Semaphore(workers)
    abort = asyncio.Event()
    codex_home = prepare_codex_home(project_root, output_root, "pro")
    tasks = [
        asyncio.create_task(
            evaluate_intent_case(
                case,
                project_root=project_root,
                output_root=output_root,
                intent_root=intent_root,
                codex_home=codex_home,
                semaphore=semaphore,
                timeout_seconds=timeout_seconds,
                abort=abort,
            )
        )
        for case in cases
    ]
    errors: list[str] = []
    for future in asyncio.as_completed(tasks):
        try:
            await future
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")
    status = intent_status(intent_root, len(cases))
    if errors or not status["complete"]:
        raise Stage1Error(f"human_agent_intent_incomplete:{status['complete_cases']}:{len(cases)}")
    summary = {
        "schema_version": f"{SCHEMA_VERSION}.intent-summary",
        "complete": True,
        "model": INTENT_MODEL,
        "workers": workers,
        "planned_cases": len(cases),
        "valid_cases": len(cases),
        "valid_stages": len(cases) * len(INTENT_STAGES),
        "invalid_attempts": status["invalid_attempts"],
    }
    write_json_once(intent_root / "summary.json", summary)
    return summary


def static_features(code: str) -> dict[str, Any]:
    tokens = fragment_tokens(code)
    token_counter = Counter(tokens)
    ccn_terms = sum(token_counter[item] for item in ("if", "for", "while", "case", "catch"))
    ccn_terms += sum(token_counter[item] for item in ("&&", "||", "?"))
    depth = maximum = 0
    for token in tokens:
        if token == "{":
            depth += 1
            maximum = max(maximum, depth)
        elif token == "}":
            depth = max(0, depth - 1)
    lines = code.splitlines() or [code]
    rules = {
        "long_fragment": len(tokens) >= 100,
        "long_line": any(len(line) > 120 for line in lines),
        "deep_brace_nesting": maximum >= 5,
        "magic_number": bool(
            re.search(r"(?<![A-Za-z0-9_])(?:[2-9]|[1-9][0-9]+)(?:\.[0-9]+)?(?![A-Za-z0-9_])", code)
        ),
        "todo_or_fixme": bool(re.search(r"\b(?:TODO|FIXME)\b", code, re.IGNORECASE)),
        "empty_catch": bool(re.search(r"catch\s*\([^)]*\)\s*\{\s*\}", code, re.DOTALL)),
        "generic_exception_catch": bool(re.search(r"catch\s*\(\s*(?:Exception|Throwable)\b", code)),
        "print_stack_trace": ".printStackTrace(" in code,
        "system_output": bool(re.search(r"\bSystem\.(?:out|err)\.print", code)),
        "thread_sleep": bool(re.search(r"\bThread\.sleep\s*\(", code)),
        "commented_out_code": bool(re.search(r"(?m)^\s*//\s*(?:if|for|while|return|throw|new|[A-Za-z_$][\w$]*\s*[=(]).*[;{]", code)),
    }
    active = sorted(rule for rule, present in rules.items() if present)
    return {
        "token_count": len(tokens),
        "line_count": len(lines),
        "cyclomatic_complexity_proxy": 1 + ccn_terms,
        "maximum_brace_nesting": maximum,
        "smell_count": len(active),
        "smell_rules": active,
    }


def _auc(labels: Sequence[int], values: Sequence[float]) -> float:
    if len(labels) != len(values) or set(labels) != {0, 1}:
        raise Stage1Error("human_agent_auc_input_invalid")
    positives = [value for label, value in zip(labels, values) if label == 1]
    negatives = [value for label, value in zip(labels, values) if label == 0]
    wins = sum(
        1.0 if positive > negative else 0.5 if positive == negative else 0.0
        for positive in positives
        for negative in negatives
    )
    return wins / (len(positives) * len(negatives))


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values)


def _descriptive(values: Sequence[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    n = len(ordered)
    mean = _mean(ordered)
    sd = statistics.stdev(ordered) if n > 1 else 0.0
    margin = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
    return {
        "n": n,
        "mean": mean,
        "median": statistics.median(ordered),
        "standard_deviation": sd,
        "minimum": ordered[0],
        "maximum": ordered[-1],
        "mean_ci95_low": mean - margin,
        "mean_ci95_high": mean + margin,
    }


def _paired_statistic(human: Sequence[float], agent: Sequence[float]) -> dict[str, Any]:
    from scipy.stats import wilcoxon

    differences = [left - right for left, right in zip(human, agent)]
    try:
        p_value = (
            1.0
            if all(value == 0.0 for value in differences)
            else float(wilcoxon(differences, zero_method="wilcox").pvalue)
        )
    except ValueError:
        p_value = 1.0
    if not math.isfinite(p_value):
        p_value = 1.0
    labels = [1] * len(human) + [0] * len(agent)
    values = list(human) + list(agent)
    auc = _auc(labels, values)
    return {
        "human": _descriptive(human),
        "agent": _descriptive(agent),
        "paired_difference_human_minus_agent": _descriptive(differences),
        "paired_wilcoxon_p_value": p_value,
        "roc_auc_human_as_positive": auc,
        "roc_auc_separation": max(auc, 1.0 - auc),
        "direction_of_higher_values": "human" if auc > 0.5 else "agent" if auc < 0.5 else "tie",
    }


def _spearman_record(left: Sequence[float], right: Sequence[float]) -> dict[str, Any]:
    from scipy.stats import spearmanr

    if len(left) != len(right) or len(left) < 3:
        return {"n": len(left), "rho": None, "p_value": None}
    result = spearmanr(left, right)
    rho = float(result.statistic)
    p_value = float(result.pvalue)
    return {
        "n": len(left),
        "rho": rho if math.isfinite(rho) else None,
        "p_value": p_value if math.isfinite(p_value) else None,
    }


def _token_metrics(reference: str, candidate: str) -> dict[str, float]:
    reference_tokens = fragment_tokens(reference)
    candidate_tokens = fragment_tokens(candidate)
    bleu = bleu_score(reference_tokens, candidate_tokens)
    rouge = rouge_scores(reference_tokens, candidate_tokens)
    return {
        "bleu": bleu.score,
        "rouge_1_f1": rouge.rouge1.f1,
        "rouge_2_f1": rouge.rouge2.f1,
        "rouge_l_f1": rouge.rouge_l.f1,
    }


def build_results(project_root: Path, output_root: Path) -> dict[str, Any]:
    cohort = load_json(output_root / "cohort.json")
    cases = cohort["cases"]
    intent_root = output_root / "intent-analysis"
    if not load_json(intent_root / "summary.json").get("complete"):
        raise Stage1Error("human_agent_intent_not_complete")
    rows: list[dict[str, Any]] = []
    codebert_pairs: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for case in cases:
        source = selected_generation(output_root, str(case["case_id"]))["attempt"]
        intent = verified_intent_case(output_root, intent_root, str(case["case_id"]))
        fragments = {
            "human_original": source["arms"]["human"]["original_code"],
            "human_roundtrip": source["arms"]["human"]["reconstructed_code"],
            "agent_original": source["arms"]["agent"]["original_code"],
            "agent_roundtrip": source["arms"]["agent"]["reconstructed_code"],
        }
        pair_names = (
            ("human_roundtrip", "human_original", "human_roundtrip"),
            ("agent_roundtrip", "agent_original", "agent_roundtrip"),
            ("human_agent_original", "human_original", "agent_original"),
            ("pre_to_human", "pre_review", "human_original"),
            ("pre_to_agent", "pre_review", "agent_original"),
        )
        pair_fragments = {"pre_review": case["pre_review_code"], **fragments}
        token_scores = {
            name: _token_metrics(str(pair_fragments[left]), str(pair_fragments[right]))
            for name, left, right in pair_names
        }
        for _name, left, right in pair_names:
            codebert_pairs.append(
                (fragment_tokens(str(pair_fragments[left])), fragment_tokens(str(pair_fragments[right])))
            )
        row = {
            "case_id": case["case_id"],
            "project": case["project"],
            "pr_number": case["pr_number"],
            "path": case["path"],
            "review_metadata": case["metadata"],
            "token_similarity": token_scores,
            "static_features": {name: static_features(str(code)) for name, code in fragments.items()},
            "intent": {
                name.replace("-", "_"): intent["judgments"][name]["metrics"]
                for name in ("human-original", "human-roundtrip", "agent-original", "agent-roundtrip")
            },
        }
        rows.append(row)
    tokenizer, model = load_pinned_codebert(
        project_root / "models" / "codebert-base",
        project_root / "config" / "codebert-base-revision.json",
    )
    similarities, device = codebert_batch_similarities(codebert_pairs, tokenizer=tokenizer, model=model)
    pair_order = ("human_roundtrip", "agent_roundtrip", "human_agent_original", "pre_to_human", "pre_to_agent")
    cursor = 0
    for row in rows:
        row["codebert"] = {}
        for name in pair_order:
            row["codebert"][name] = similarities[cursor]
            cursor += 1

    metric_extractors: dict[str, Callable[[Mapping[str, Any], str], float]] = {
        "roundtrip_codebert": lambda row, arm: float(row["codebert"][f"{arm}_roundtrip"]),
        "roundtrip_bleu": lambda row, arm: float(row["token_similarity"][f"{arm}_roundtrip"]["bleu"]),
        "roundtrip_rouge_l": lambda row, arm: float(row["token_similarity"][f"{arm}_roundtrip"]["rouge_l_f1"]),
        "intent_fidelity_original": lambda row, arm: float(row["intent"][f"{arm}_original"]["intent_fidelity_f1"]),
        "intent_fidelity_roundtrip": lambda row, arm: float(row["intent"][f"{arm}_roundtrip"]["intent_fidelity_f1"]),
        "intent_count_original": lambda row, arm: float(row["intent"][f"{arm}_original"]["code_intent_count"]),
        "ccn_proxy_original": lambda row, arm: float(row["static_features"][f"{arm}_original"]["cyclomatic_complexity_proxy"]),
        "smell_count_original": lambda row, arm: float(row["static_features"][f"{arm}_original"]["smell_count"]),
        "token_count_original": lambda row, arm: float(row["static_features"][f"{arm}_original"]["token_count"]),
        "revision_codebert_from_pre_review": lambda row, arm: float(row["codebert"][f"pre_to_{arm}"]),
        "revision_bleu_from_pre_review": lambda row, arm: float(row["token_similarity"][f"pre_to_{arm}"]["bleu"]),
        "revision_rouge_l_from_pre_review": lambda row, arm: float(row["token_similarity"][f"pre_to_{arm}"]["rouge_l_f1"]),
        "roundtrip_change_intent_count": lambda row, arm: float(row["intent"][f"{arm}_roundtrip"]["code_intent_count"] - row["intent"][f"{arm}_original"]["code_intent_count"]),
        "roundtrip_change_ccn_proxy": lambda row, arm: float(row["static_features"][f"{arm}_roundtrip"]["cyclomatic_complexity_proxy"] - row["static_features"][f"{arm}_original"]["cyclomatic_complexity_proxy"]),
        "roundtrip_change_smell_count": lambda row, arm: float(row["static_features"][f"{arm}_roundtrip"]["smell_count"] - row["static_features"][f"{arm}_original"]["smell_count"]),
    }
    comparisons = {
        name: _paired_statistic(
            [extractor(row, "human") for row in rows],
            [extractor(row, "agent") for row in rows],
        )
        for name, extractor in metric_extractors.items()
    }
    for name in ("intent_fidelity", "strict_preservation_rate", "change_rate", "loss_rate", "addition_rate"):
        key = "intent_fidelity_f1" if name == "intent_fidelity" else name
        human_drift = [
            float(row["intent"]["human_original"][key]) - float(row["intent"]["human_roundtrip"][key])
            for row in rows
        ]
        agent_drift = [
            float(row["intent"]["agent_original"][key]) - float(row["intent"]["agent_roundtrip"][key])
            for row in rows
        ]
        comparisons[f"roundtrip_drift_{name}"] = _paired_statistic(human_drift, agent_drift)

    human_agent_similarity = {
        "codebert": _descriptive([float(row["codebert"]["human_agent_original"]) for row in rows]),
        "bleu": _descriptive([float(row["token_similarity"]["human_agent_original"]["bleu"]) for row in rows]),
        "rouge_1_f1": _descriptive([float(row["token_similarity"]["human_agent_original"]["rouge_1_f1"]) for row in rows]),
        "rouge_2_f1": _descriptive([float(row["token_similarity"]["human_agent_original"]["rouge_2_f1"]) for row in rows]),
        "rouge_l_f1": _descriptive([float(row["token_similarity"]["human_agent_original"]["rouge_l_f1"]) for row in rows]),
    }
    numeric_metadata = (
        "hours_pr_open_to_target_review",
        "hours_target_review_to_next_commit",
        "inline_review_comment_count",
        "distinct_inline_reviewer_count",
        "commit_and_force_push_count",
        "target_review_reply_count",
        "merge_commit_recorded",
    )
    categorical_metadata = (
        "rq2_understandability_smell",
        "rq2_where",
        "rq3_acceptability",
        "rq4_improvement",
        "rq5_patch_merged",
        "rq5_patch_in_last_file_version",
        "rq6_spotbugs",
        "rq6_pmd",
        "rq6_sonarqube",
        "rq6_checkstyle",
    )
    metadata_summary: dict[str, Any] = {"numeric": {}, "categorical": {}}
    metadata_correlations: dict[str, Any] = {}
    correlation_features = {
        "human_intent_fidelity": lambda row: float(row["intent"]["human_original"]["intent_fidelity_f1"]),
        "agent_intent_fidelity": lambda row: float(row["intent"]["agent_original"]["intent_fidelity_f1"]),
        "human_roundtrip_codebert": lambda row: float(row["codebert"]["human_roundtrip"]),
        "agent_roundtrip_codebert": lambda row: float(row["codebert"]["agent_roundtrip"]),
        "human_ccn_proxy": lambda row: float(row["static_features"]["human_original"]["cyclomatic_complexity_proxy"]),
        "agent_ccn_proxy": lambda row: float(row["static_features"]["agent_original"]["cyclomatic_complexity_proxy"]),
        "human_smell_count": lambda row: float(row["static_features"]["human_original"]["smell_count"]),
        "agent_smell_count": lambda row: float(row["static_features"]["agent_original"]["smell_count"]),
    }
    for name in numeric_metadata:
        available = [
            (row, row["review_metadata"].get(name))
            for row in rows
            if isinstance(row["review_metadata"].get(name), (int, float, bool))
        ]
        metadata_values = [float(value) for _row, value in available]
        metadata_summary["numeric"][name] = {
            "available": len(available),
            "missing": len(rows) - len(available),
            "values": _descriptive(metadata_values) if metadata_values else None,
        }
        metadata_correlations[name] = {
            feature: _spearman_record(
                metadata_values,
                [extractor(row) for row, _value in available],
            )
            for feature, extractor in correlation_features.items()
        }
    for name in categorical_metadata:
        values = [row["review_metadata"].get(name) for row in rows]
        present = [str(value) for value in values if value is not None]
        metadata_summary["categorical"][name] = {
            "available": len(present),
            "missing": len(rows) - len(present),
            "value_counts": dict(sorted(Counter(present).items())),
        }
    results = {
        "schema_version": f"{SCHEMA_VERSION}.results",
        "design": {
            "cases": len(rows),
            "human_source": "recorded same-file post-review revision",
            "agent_source": "independent revision from review request and pre-review context",
            "generation_model": "deepseek-v4-flash",
            "intent_model": INTENT_MODEL,
            "codebert_device": device,
            "auc_positive_class": "human",
            "ccn_definition": "fragment proxy: 1 + if/for/while/case/catch/&&/||/? token counts",
            "smell_definition": "same transparent eleven-rule fragment heuristic for both arms",
        },
        "comparisons": comparisons,
        "human_agent_original_similarity": human_agent_similarity,
        "review_metadata_summary": metadata_summary,
        "review_metadata_correlations": metadata_correlations,
        "case_rows": rows,
        "limitations": [
            "Review comments are pre-revision specifications, not issue reports that predate the original code.",
            "CCN is a fragment-level token proxy because many CODE-UP scopes are incomplete Java fragments.",
            "Smells are transparent uniform heuristics, not whole-project PMD/SonarQube executions.",
            "The 915 cases exclude 285 sampled reviews without a recoverable nonempty same-file revision.",
        ],
    }
    write_json_once(output_root / "results.json", results)
    return results


def _fmt(value: float) -> str:
    return "NA" if not math.isfinite(value) else f"{value:.4f}"


def _fmt_p(value: float) -> str:
    if not math.isfinite(value):
        return "NA"
    return f"{value:.2e}" if value < 0.0001 else f"{value:.4f}"


def build_reports(output_root: Path, report_root: Path) -> tuple[Path, Path]:
    results = load_json(output_root / "results.json")
    comparisons = results["comparisons"]
    labels = {
        "roundtrip_codebert": "Round-trip CodeBERT similarity",
        "roundtrip_bleu": "Round-trip BLEU",
        "roundtrip_rouge_l": "Round-trip ROUGE-L F1",
        "intent_fidelity_original": "Review-intent fidelity, original",
        "intent_fidelity_roundtrip": "Review-intent fidelity, round-trip",
        "intent_count_original": "Number of code intents, original",
        "ccn_proxy_original": "Cyclomatic-complexity proxy, original",
        "smell_count_original": "Code-smell/antipattern count, original",
        "token_count_original": "Token count, original",
        "revision_codebert_from_pre_review": "Pre-review→revision CodeBERT similarity",
        "revision_bleu_from_pre_review": "Pre-review→revision BLEU",
        "revision_rouge_l_from_pre_review": "Pre-review→revision ROUGE-L F1",
        "roundtrip_change_intent_count": "Intent-count change after round trip",
        "roundtrip_change_ccn_proxy": "CCN-proxy change after round trip",
        "roundtrip_change_smell_count": "Smell-count change after round trip",
        "roundtrip_drift_intent_fidelity": "Intent-fidelity loss after round trip",
        "roundtrip_drift_strict_preservation_rate": "Strict-preservation loss after round trip",
        "roundtrip_drift_change_rate": "Changed-intent-rate change after round trip",
        "roundtrip_drift_loss_rate": "Lost-intent-rate change after round trip",
        "roundtrip_drift_addition_rate": "Added-intent-rate change after round trip",
    }
    header = [
        "# CODE-UP Human-versus-Agent Revision and Round-Trip Study",
        "",
        "**Report date:** August 20, 2026  ",
        f"**Paired cases:** {results['design']['cases']:,}  ",
        "**Human arm:** recorded same-file revision after the review request  ",
        "**Agent arm:** independent revision from the same review request and pre-review context  ",
        "**Generation/backtranslation:** `deepseek-v4-flash` through separate Codex instances  ",
        "**Intent extraction/judging:** `deepseek-v4-pro` through separate Codex instances",
        "",
        "## Overall paired results",
        "",
        "AUC treats human code as the positive class. Separation is `max(AUC, 1-AUC)`; the direction column shows which arm tends to have larger values.",
        "",
        "| Measure | Human mean | Agent mean | Human − agent | Wilcoxon p | AUC | Separation | Higher |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for key in labels:
        item = comparisons[key]
        header.append(
            f"| {labels[key]} | {_fmt(item['human']['mean'])} | {_fmt(item['agent']['mean'])} | "
            f"{_fmt(item['paired_difference_human_minus_agent']['mean'])} | {_fmt_p(item['paired_wilcoxon_p_value'])} | "
            f"{_fmt(item['roc_auc_human_as_positive'])} | {_fmt(item['roc_auc_separation'])} | {item['direction_of_higher_values']} |"
        )
    header.extend(
        [
            "",
            "## Direct similarity of human and agent revisions",
            "",
            "These values compare the independently written human and agent revisions to each other, not either arm to its reconstruction.",
            "",
            "| Similarity | Mean | Median | 95% CI of mean |",
            "|---|---:|---:|---:|",
        ]
    )
    for key, label in (("codebert", "CodeBERT"), ("bleu", "BLEU"), ("rouge_1_f1", "ROUGE-1 F1"), ("rouge_2_f1", "ROUGE-2 F1"), ("rouge_l_f1", "ROUGE-L F1")):
        item = results["human_agent_original_similarity"][key]
        header.append(
            f"| {label} | {_fmt(item['mean'])} | {_fmt(item['median'])} | "
            f"[{_fmt(item['mean_ci95_low'])}, {_fmt(item['mean_ci95_high'])}] |"
        )
    header.extend(
        [
            "",
            "## Main findings",
            "",
            f"- **Round-trip reconstruction is highly similar for both arms.** Mean CodeBERT is {_fmt(comparisons['roundtrip_codebert']['human']['mean'])} for human code and {_fmt(comparisons['roundtrip_codebert']['agent']['mean'])} for agent code; mean BLEU is {_fmt(comparisons['roundtrip_bleu']['human']['mean'])} and {_fmt(comparisons['roundtrip_bleu']['agent']['mean'])}, respectively.",
            f"- **The round trip does not produce large intent drift in this cohort.** Mean intent-fidelity loss is {_fmt(comparisons['roundtrip_drift_intent_fidelity']['human']['mean'])} for human revisions and {_fmt(comparisons['roundtrip_drift_intent_fidelity']['agent']['mean'])} for agent revisions.",
            f"- **Absolute review-intent fidelity is low in both extracted code fragments.** Before round trip it is {_fmt(comparisons['intent_fidelity_original']['human']['mean'])} for human and {_fmt(comparisons['intent_fidelity_original']['agent']['mean'])} for agent revisions. This is distinct from round-trip drift and should not be described as intent lost by backtranslation.",
            f"- **Agent revisions are longer and trigger more structural heuristics.** Mean token count is {_fmt(comparisons['token_count_original']['human']['mean'])} versus {_fmt(comparisons['token_count_original']['agent']['mean'])}; mean CCN proxy is {_fmt(comparisons['ccn_proxy_original']['human']['mean'])} versus {_fmt(comparisons['ccn_proxy_original']['agent']['mean'])}; and mean smell/antipattern count is {_fmt(comparisons['smell_count_original']['human']['mean'])} versus {_fmt(comparisons['smell_count_original']['agent']['mean'])}.",
            f"- **Agent revisions stay much closer to the pre-review fragment.** Mean pre-review-to-revision CodeBERT is {_fmt(comparisons['revision_codebert_from_pre_review']['human']['mean'])} for human revisions and {_fmt(comparisons['revision_codebert_from_pre_review']['agent']['mean'])} for agent revisions. This may reflect conservative under-editing as well as fragment/extraction differences, so it is not automatically evidence of better revisions.",
            "",
            "## Interpretation",
            "",
            "CodeBERT, BLEU, and ROUGE quantify textual/representation similarity between each original revision and its reconstruction. The Pro intent measures separately test whether the pre-revision review request remains implemented before and after round-trip translation. A high code-similarity score therefore does not, by itself, prove intent preservation.",
            "",
            "## Review metadata retained",
            "",
            "Each machine-readable case row retains PR-open-to-review hours, review-to-next-commit hours when available, inline-review comment count, distinct inline reviewers, commit/force-push count, target-thread replies, merge-record presence, and sparse CODE-UP RQ2–RQ6 labels.",
            "",
            "| Metadata field | Available | Missing | Mean | Median |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for key, item in results["review_metadata_summary"]["numeric"].items():
        values = item["values"]
        header.append(
            f"| {key.replace('_', ' ')} | {item['available']} | {item['missing']} | "
            f"{_fmt(values['mean'])} | {_fmt(values['median'])} |"
        )
    header.extend(
        [
            "",
            f"Sparse CODE-UP RQ2/RQ3 labels are available for {results['review_metadata_summary']['categorical']['rq3_acceptability']['available']} cases; most RQ4–RQ6 fields are available for {results['review_metadata_summary']['categorical']['rq4_improvement']['available']} cases.",
            "",
            "## Definitions and limitations",
            "",
        ]
    )
    header.extend(f"- {item}" for item in results["limitations"])
    header.extend(
        [
           "- The agent never receives the recorded human revision.",
           "- Both arms use identical backtranslation prompts, similarity implementations, intent judge, CCN proxy, and smell rules.",
            "- Human revisions aggregate recoverable same-file changed fragments, whereas the agent responds to the target review fragment; this granularity difference can affect revision-from-pre-review similarity.",
            "- The p-values are exploratory and unadjusted across metrics. AUC is a descriptive one-variable separation statistic, not held-out predictive performance.",
           "- Full case-level values and provenance are in `artifacts/codeup-human-agent/results.json`.",
            "",
        ]
    )
    report_root.mkdir(parents=True, exist_ok=True)
    md_path = report_root / "2026-08-20-codeup-human-vs-agent.md"
    write_bytes_once(md_path, ("\n".join(header) + "\n").encode("utf-8"))

    latex_rows = []
    for key in labels:
        item = comparisons[key]
        label = (
            labels[key]
            .replace("%", r"\%")
            .replace("&", r"\&")
            .replace("_", r"\_")
            .replace("→", r"$\rightarrow$")
        )
        latex_rows.append(
            f"{label} & {_fmt(item['human']['mean'])} & {_fmt(item['agent']['mean'])} & "
            f"{_fmt(item['paired_difference_human_minus_agent']['mean'])} & {_fmt_p(item['paired_wilcoxon_p_value'])} & "
            f"{_fmt(item['roc_auc_human_as_positive'])} & {_fmt(item['roc_auc_separation'])} \\\\"
        )
    latex_similarity_rows = []
    for key, label in (("codebert", "CodeBERT"), ("bleu", "BLEU"), ("rouge_1_f1", "ROUGE-1 F1"), ("rouge_2_f1", "ROUGE-2 F1"), ("rouge_l_f1", "ROUGE-L F1")):
        item = results["human_agent_original_similarity"][key]
        latex_similarity_rows.append(
            f"{label} & {_fmt(item['mean'])} & {_fmt(item['median'])} & "
            f"[{_fmt(item['mean_ci95_low'])}, {_fmt(item['mean_ci95_high'])}] \\\\"
        )
    tex = r"""\documentclass{article}
\usepackage[margin=1in]{geometry}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage[T1]{fontenc}
\title{CODE-UP Human-versus-Agent Revision and Round-Trip Study}
\date{August 20, 2026}
\begin{document}
\maketitle
\section{Design}
We compare paired revisions for """ + f"{results['design']['cases']:,}" + r""" CODE-UP review requests. The human arm is the recorded same-file post-review revision. The agent arm is independently generated from only the review request and pre-review context. DeepSeek V4 Flash performs generation and backtranslation through separate Codex instances; DeepSeek V4 Pro extracts and judges intent.

\section{Overall paired results}
Human is the positive class for ROC--AUC. Separation is $\max(\mathrm{AUC},1-\mathrm{AUC})$.
\small
\begin{longtable}{p{5.2cm}rrrrrr}
\toprule
Measure & Human & Agent & $\Delta$ & $p$ & AUC & Separation \\
\midrule
\endhead
""" + "\n".join(latex_rows) + r"""
\bottomrule
\end{longtable}
\normalsize

\section{Direct similarity of human and agent revisions}
\begin{tabular}{lrrr}
\toprule
Similarity & Mean & Median & 95\% CI of mean \\
\midrule
""" + "\n".join(latex_similarity_rows) + r"""
\bottomrule
\end{tabular}

\section{Main findings}
Round-trip reconstruction is highly similar for both arms: mean CodeBERT is """ + _fmt(comparisons["roundtrip_codebert"]["human"]["mean"]) + " for human code and " + _fmt(comparisons["roundtrip_codebert"]["agent"]["mean"]) + r""" for agent code. Mean intent-fidelity loss after round trip is only """ + _fmt(comparisons["roundtrip_drift_intent_fidelity"]["human"]["mean"]) + " and " + _fmt(comparisons["roundtrip_drift_intent_fidelity"]["agent"]["mean"]) + r""", respectively, so this cohort does not show large intent drift caused by backtranslation. Absolute review-intent fidelity is low in both original extracted fragments (""" + _fmt(comparisons["intent_fidelity_original"]["human"]["mean"]) + " human; " + _fmt(comparisons["intent_fidelity_original"]["agent"]["mean"]) + r""" agent), but that is a different quantity. Agent revisions are longer, have higher CCN proxy and smell counts, and remain substantially closer to the pre-review fragment; the latter may indicate conservative under-editing or extraction-granularity effects rather than superior revision quality.

\section{Interpretation}
CodeBERT, BLEU, and ROUGE measure code similarity. The Pro intent measures separately test whether the prior review request remains implemented before and after round-trip translation. High code similarity is not by itself evidence of intent preservation.

\section{Limitations}
Review comments are pre-revision specifications rather than issue reports predating the original code. CCN is a fragment-level proxy. Smells use identical transparent heuristics rather than whole-project static analyzers. The study excludes sampled reviews without a recoverable nonempty same-file revision. The agent never receives the human revision. Human and agent revision fragments can differ in extraction granularity. Reported p-values are exploratory and unadjusted across metrics; AUC is descriptive separation, not held-out predictive performance.

\end{document}
"""
    tex_path = report_root / "2026-08-20-codeup-human-vs-agent.tex"
    write_bytes_once(tex_path, tex.encode("utf-8"))
    return md_path, tex_path
