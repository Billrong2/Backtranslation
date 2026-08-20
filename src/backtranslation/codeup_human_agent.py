"""CODE-UP review-request human-versus-agent paired study."""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from backtranslation.codeup_stage1 import (
    Stage1Error,
    canonical_json_bytes,
    code_from_output,
    codex_call,
    fragment_tokens,
    load_json,
    sha256_bytes,
    write_bytes_once,
    write_json_once,
)


SCHEMA_VERSION = "codeup.human-agent.v1"
GENERATION_MODEL = "deepseek-v4-flash"
INTENT_MODEL = "deepseek-v4-pro"
MAX_ATTEMPTS = 100
CODEX_PROFILES = {
    "flash": ("codex-flash.toml", "codex-home-flash"),
    "pro": ("codex-pro.toml", "codex-home-pro"),
}


class ProviderUnavailableError(Stage1Error):
    """A systemic provider condition that must not consume format retries."""


def prepare_codex_home(
    project_root: Path, output_root: Path, profile: str
) -> Path:
    try:
        template_name, directory_name = CODEX_PROFILES[profile]
    except KeyError as exc:
        raise Stage1Error("human_agent_codex_profile_invalid") from exc
    template = project_root / "config" / template_name
    if not template.is_file():
        raise Stage1Error("human_agent_codex_profile_missing")
    home = output_root / directory_name
    write_bytes_once(home / "config.toml", template.read_bytes())
    return home


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


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise Stage1Error("codeup_timestamp_invalid") from exc


def _hours(later: str, earlier: str) -> float:
    return (_parse_time(later) - _parse_time(earlier)).total_seconds() / 3600.0


def _dataset_rows(dataset_path: Path) -> dict[str, dict[str, str]]:
    with dataset_path.open(encoding="utf-8-sig", newline="") as stream:
        return {str(row["key"]): dict(row) for row in csv.DictReader(stream, delimiter="\t")}


def _review_event(document: Mapping[str, Any], review_id: str) -> Mapping[str, Any]:
    matches = [
        item
        for item in document.get("timeline", [])
        if isinstance(item, Mapping)
        and item.get("type") == "inlineReview"
        and item.get("id") == review_id
    ]
    if len(matches) != 1:
        raise Stage1Error("target_review_event_not_unique")
    return matches[0]


def _same_file_revision(event: Mapping[str, Any], path: str) -> tuple[str, str] | None:
    revision = event.get("revised_code")
    if not isinstance(revision, Mapping) or not isinstance(revision.get("commit"), str):
        return None
    chunks = revision.get("changed_code")
    if not isinstance(chunks, list):
        return None
    code: list[str] = []
    for item in chunks:
        if not isinstance(item, Mapping):
            continue
        header = item.get("header")
        chunk = item.get("chunk")
        if not isinstance(header, str) or not isinstance(chunk, str) or f"b/{path}" not in header:
            continue
        fragment = diff_new_side(chunk)
        if fragment:
            code.append(fragment)
    if not code:
        return None
    return str(revision["commit"]), "\n\n".join(code)


def _review_metadata(
    document: Mapping[str, Any], event: Mapping[str, Any], row: Mapping[str, str]
) -> dict[str, Any]:
    timeline = [item for item in document.get("timeline", []) if isinstance(item, Mapping)]
    reviews = [item for item in timeline if item.get("type") == "inlineReview"]
    commits = [item for item in timeline if item.get("type") in {"commit", "forcePushed"}]
    created = document.get("pr_createdAt")
    reviewed = event.get("createdAt")
    if not isinstance(created, str) or not isinstance(reviewed, str):
        raise Stage1Error("review_timing_missing")
    later_commits = sorted(
        item["committedDate"]
        for item in commits
        if isinstance(item.get("committedDate"), str)
        and _parse_time(item["committedDate"]) >= _parse_time(reviewed)
    )
    replies = event.get("replies")
    return {
        "pr_created_at_utc": created,
        "target_review_created_at_utc": reviewed,
        "hours_pr_open_to_target_review": _hours(reviewed, created),
        "hours_target_review_to_next_commit": (
            _hours(later_commits[0], reviewed) if later_commits else None
        ),
        "inline_review_comment_count": len(reviews),
        "distinct_inline_reviewer_count": len(
            {item.get("author") for item in reviews if isinstance(item.get("author"), str)}
        ),
        "commit_and_force_push_count": len(commits),
        "target_review_reply_count": len(replies) if isinstance(replies, list) else 0,
        "merge_commit_recorded": isinstance(document.get("merge_commit"), str),
        "rq2_understandability_smell": row.get("RQ2 - Understandability smells") or None,
        "rq2_where": row.get("RQ2 - Where") or None,
        "rq3_acceptability": row.get("RQ3 - Acceptability") or None,
        "rq4_improvement": row.get("RQ4 - Improvement") or None,
        "rq5_patch_merged": row.get("RQ5 - Patch Merged") or None,
        "rq5_patch_in_last_file_version": row.get("RQ5 - Patch in the last version of the file") or None,
        "rq6_spotbugs": row.get("RQ6 - Spotbugs") or None,
        "rq6_pmd": row.get("RQ6 - PMD") or None,
        "rq6_sonarqube": row.get("RQ6 - SonarQube") or None,
        "rq6_checkstyle": row.get("RQ6 - Checkstyle") or None,
    }


def build_human_agent_cohort(project_root: Path, stage_root: Path, output_root: Path) -> dict[str, Any]:
    source_cohort_path = stage_root / "cohort.json"
    source_cohort = load_json(source_cohort_path)
    configured_dataset = os.environ.get("CODEUP_DATASET_DIR")
    dataset_root = (
        Path(configured_dataset).expanduser().resolve()
        if configured_dataset
        else project_root / ".cache" / "codeUp"
    )
    legacy_dataset_root = project_root / "artifacts" / "codeUp"
    if not dataset_root.is_dir() and legacy_dataset_root.is_dir():
        dataset_root = legacy_dataset_root
    rows = _dataset_rows(dataset_root / "csv" / "dataset.tsv")
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for source in source_cohort["cases"]:
        pr_path = (
            stage_root
            / "source"
            / "pr-json"
            / str(source["project"]).replace("/", "_")
            / f"pr_{source['pr_number']}.json"
        )
        raw = pr_path.read_bytes()
        if sha256_bytes(raw) != source["pr_json_sha256"]:
            raise Stage1Error("source_pr_json_hash_mismatch")
        document = json.loads(raw.decode("utf-8"))
        event = _review_event(document, str(source["review_id"]))
        revision = _same_file_revision(event, str(source["path"]))
        if revision is None:
            reason = (
                "no_recorded_revision"
                if not isinstance(event.get("revised_code"), Mapping)
                else "same_file_nonempty_revision_unavailable"
            )
            excluded.append({"case_id": str(source["case_id"]), "reason": reason})
            continue
        review_text = event.get("bodyText")
        diff_hunk = event.get("diffHunk")
        if not isinstance(review_text, str) or not review_text.strip() or not isinstance(diff_hunk, str):
            excluded.append({"case_id": str(source["case_id"]), "reason": "review_request_invalid"})
            continue
        human_commit, human_code = revision
        if not fragment_tokens(human_code):
            excluded.append({"case_id": str(source["case_id"]), "reason": "human_revision_tokens_empty"})
            continue
        row = rows.get(str(source["codeup_key"]))
        if row is None:
            raise Stage1Error("codeup_dataset_join_missing")
        included.append(
            {
                "case_id": source["case_id"],
                "pair_id": source["pair_id"],
                "project": source["project"],
                "pr_number": source["pr_number"],
                "path": source["path"],
                "review_id": source["review_id"],
                "codeup_key": source["codeup_key"],
                "human_understandability": source["human_understandability"],
                "review_request": review_text.strip(),
                "review_request_sha256": sha256_bytes(review_text.strip().encode("utf-8")),
                "pre_review_diff_hunk": diff_hunk,
                "pre_review_diff_hunk_sha256": sha256_bytes(diff_hunk.encode("utf-8")),
                "pre_review_code": source["code_1"],
                "pre_review_code_sha256": source["code_1_sha256"],
                "human_revision_commit": human_commit,
                "human_revision_code": human_code,
                "human_revision_code_sha256": sha256_bytes(human_code.encode("utf-8")),
                "source_pr_json_path": str(pr_path.relative_to(project_root)),
                "source_pr_json_sha256": source["pr_json_sha256"],
                "metadata": _review_metadata(document, event, row),
            }
        )
    included.sort(key=lambda item: str(item["case_id"]))
    excluded.sort(key=lambda item: item["case_id"])
    cohort = {
        "schema_version": f"{SCHEMA_VERSION}.cohort",
        "design": {
            "task_definition": "review_request_plus_pre_review_fragment_to_revised_fragment",
            "human_arm": "recorded_same_file_revision_after_review_request",
            "agent_arm": "independent_generation_from_review_request_and_pre_review_context_only",
            "generation_model": GENERATION_MODEL,
            "intent_model": INTENT_MODEL,
            "planned_cases": len(included),
            "excluded_cases": len(excluded),
            "human_revision_hidden_from_agent": True,
        },
        "source": {
            "cohort_path": str(source_cohort_path.relative_to(project_root)),
            "cohort_sha256": sha256_bytes(source_cohort_path.read_bytes()),
        },
        "cases": included,
        "exclusions": excluded,
    }
    write_json_once(output_root / "cohort.json", cohort)
    return cohort


def agent_revision_prompt(case: Mapping[str, Any]) -> str:
    return (
        "You are implementing one Java code-review request. Do not use tools or inspect files. "
        "Use only the review request and the pre-review fragment below. Return the revised Java "
        "fragment that best satisfies the request while preserving unrelated behavior and the "
        "fragment's incomplete boundaries. Return exactly one JSON object and no Markdown: "
        '{"code":"revised Java fragment"}.\n\nREVIEW REQUEST:\n'
        + str(case["review_request"])
        + "\n\nFILE PATH:\n"
        + str(case["path"])
        + "\n\nPRE-REVIEW DIFF CONTEXT:\n"
        + str(case["pre_review_diff_hunk"])
        + "\n\nPRE-REVIEW CODE FRAGMENT:\n"
        + str(case["pre_review_code"])
    )


def backtranslation_directions_prompt(code: str) -> str:
    return (
        "Describe the supplied Java fragment as atomic reconstruction directions. Do not use "
        "tools, critique, improve, or mention authorship. Preserve purpose, behavior, ordering, "
        "calls, literals, comments, declarations, and incomplete boundaries. Summarize repeated "
        "patterns instead of quoting the code wholesale. Return 1 to 40 directions, each at most "
        "300 characters, in exactly one JSON object: "
        '{"directions":["atomic direction","..."]}.\n\nJAVA FRAGMENT:\n'
        + code
    )


def backtranslation_code_prompt(directions: Sequence[str]) -> str:
    return (
        "Reconstruct the Java fragment from these directions only. Do not use tools and do not "
        "add improvements. Preserve incomplete fragment boundaries. Return exactly one JSON "
        'object: {"code":"Java fragment"}.\n\nDIRECTIONS:\n'
        + json.dumps(list(directions), ensure_ascii=False)
    )


def _json_object(raw: bytes) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8").strip()
        if text.startswith("```") and text.endswith("```"):
            text = "\n".join(text.splitlines()[1:-1]).strip()
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage1Error("human_agent_response_not_json") from exc
    if not isinstance(value, Mapping):
        raise Stage1Error("human_agent_response_not_object")
    return value


def _code_output(raw: bytes) -> str:
    try:
        value = _json_object(raw)
        code = value.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 200_000:
            raise Stage1Error("human_agent_code_invalid")
        code = code.strip()
    except Stage1Error:
        # Flash sometimes emits a complete JSON code string followed by one
        # redundant quote/brace or a prose suffix.  The established CODE-UP
        # parser recovers only the unambiguous code payload and still rejects
        # empty/too-short output; this is formatting recovery, not selection
        # on code content.
        try:
            text = raw.decode("utf-8").strip()
            first, _end = json.JSONDecoder().raw_decode(text)
            first_code = first.get("code") if isinstance(first, Mapping) else None
            if not isinstance(first_code, str) or not first_code.strip():
                raise Stage1Error("human_agent_first_code_object_invalid")
            code = first_code.strip()
        except (UnicodeDecodeError, json.JSONDecodeError, Stage1Error):
            code = code_from_output(raw)
    if not fragment_tokens(code):
        raise Stage1Error("human_agent_code_tokens_empty")
    return code


def _directions_output(raw: bytes) -> list[str]:
    value = _json_object(raw)
    directions = value.get("directions")
    if not isinstance(directions, list) or not 1 <= len(directions) <= 120:
        raise Stage1Error("human_agent_directions_invalid")
    result: list[str] = []
    for item in directions:
        if not isinstance(item, str) or not item.strip() or len(item) > 1000:
            raise Stage1Error("human_agent_direction_invalid")
        result.append(item.strip())
    return result


async def _call_retained(
    name: str,
    prompt: str,
    *,
    attempt_root: Path,
    project_root: Path,
    codex_home: Path,
    semaphore: asyncio.Semaphore,
    timeout_seconds: int,
    abort: asyncio.Event | None = None,
) -> bytes:
    stdout_path = attempt_root / f"{name}.stdout"
    if stdout_path.exists():
        return stdout_path.read_bytes()
    if abort is not None and abort.is_set():
        raise ProviderUnavailableError("human_agent_generation_aborted")
    async with semaphore:
        if abort is not None and abort.is_set():
            raise ProviderUnavailableError("human_agent_generation_aborted")
        stdout, stderr, return_code, _elapsed = await codex_call(
            prompt,
            project_root=project_root,
            codex_home=codex_home,
            timeout_seconds=timeout_seconds,
        )
    write_bytes_once(stdout_path, stdout)
    write_bytes_once(attempt_root / f"{name}.stderr", stderr)
    if return_code != 0:
        if any(
            marker in stderr
            for marker in (b"deepseek_http_401", b"deepseek_http_402", b"deepseek_http_403")
        ):
            if abort is not None:
                abort.set()
            raise ProviderUnavailableError(f"{name}_provider_account_unavailable")
        raise Stage1Error(f"{name}_codex_exit_{return_code}")
    return stdout


async def generate_case(
    case: Mapping[str, Any],
    *,
    project_root: Path,
    output_root: Path,
    codex_home: Path,
    semaphore: asyncio.Semaphore,
    timeout_seconds: int,
    abort: asyncio.Event | None = None,
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
            agent_code = _code_output(
                await _call_retained(
                    "agent-revision",
                    agent_revision_prompt(case),
                    attempt_root=attempt_root,
                    project_root=project_root,
                    codex_home=codex_home,
                    semaphore=semaphore,
                    timeout_seconds=timeout_seconds,
                    abort=abort,
                )
            )
            arm_values: dict[str, Any] = {}
            for arm, original in (
                ("human", str(case["human_revision_code"])),
                ("agent", agent_code),
            ):
                directions = _directions_output(
                    await _call_retained(
                        f"{arm}-directions",
                        backtranslation_directions_prompt(original),
                        attempt_root=attempt_root,
                        project_root=project_root,
                        codex_home=codex_home,
                        semaphore=semaphore,
                        timeout_seconds=timeout_seconds,
                        abort=abort,
                    )
                )
                reconstructed = _code_output(
                    await _call_retained(
                        f"{arm}-reconstruction",
                        backtranslation_code_prompt(directions),
                        attempt_root=attempt_root,
                        project_root=project_root,
                        codex_home=codex_home,
                        semaphore=semaphore,
                        timeout_seconds=timeout_seconds,
                        abort=abort,
                    )
                )
                arm_values[arm] = {
                    "original_code": original,
                    "original_code_sha256": sha256_bytes(original.encode("utf-8")),
                    "directions": directions,
                    "directions_sha256": sha256_bytes(canonical_json_bytes(directions)),
                    "reconstructed_code": reconstructed,
                    "reconstructed_code_sha256": sha256_bytes(reconstructed.encode("utf-8")),
                }
            value = {
                "schema_version": f"{SCHEMA_VERSION}.generation-attempt",
                "status": "valid",
                "case_id": case["case_id"],
                "attempt_index": attempt_index,
                "model": GENERATION_MODEL,
                "agent_revision_input_excludes_human_revision": True,
                "agent_revision_code": agent_code,
                "agent_revision_code_sha256": sha256_bytes(agent_code.encode("utf-8")),
                "arms": arm_values,
            }
            write_json_once(attempt_root / "attempt.json", value)
            attempt_bytes = (attempt_root / "attempt.json").read_bytes()
            selected = {
                "schema_version": f"{SCHEMA_VERSION}.generation-selected",
                "case_id": case["case_id"],
                "attempt_index": attempt_index,
                "attempt_path": str((attempt_root / "attempt.json").relative_to(output_root)),
                "attempt_sha256": sha256_bytes(attempt_bytes),
            }
            write_json_once(selected_path, selected)
            return selected
        except ProviderUnavailableError as exc:
            write_json_once(
                attempt_root / "attempt.json",
                {
                    "schema_version": f"{SCHEMA_VERSION}.generation-attempt",
                    "status": "provider_unavailable",
                    "case_id": case["case_id"],
                    "attempt_index": attempt_index,
                    "model": GENERATION_MODEL,
                    "failure_code": str(exc),
                },
            )
            raise
        except (Stage1Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            write_json_once(
                attempt_root / "attempt.json",
                {
                    "schema_version": f"{SCHEMA_VERSION}.generation-attempt",
                    "status": "invalid",
                    "case_id": case["case_id"],
                    "attempt_index": attempt_index,
                    "model": GENERATION_MODEL,
                    "failure_code": str(exc),
                },
            )
    raise Stage1Error(f"human_agent_attempt_cap_exhausted:{case['case_id']}")


def generation_status(output_root: Path, planned: int) -> dict[str, Any]:
    selected = list(output_root.glob("runs/*/selected.json"))
    attempts = list(output_root.glob("runs/*/attempt-*/attempt.json"))
    invalid = 0
    for path in attempts:
        try:
            invalid += load_json(path).get("status") != "valid"
        except Stage1Error:
            invalid += 1
    return {
        "schema_version": f"{SCHEMA_VERSION}.generation-status",
        "planned_cases": planned,
        "selected_valid_cases": len(selected),
        "attempts": len(attempts),
        "invalid_attempts": invalid,
        "complete": len(selected) == planned,
    }


async def run_generation(
    project_root: Path,
    output_root: Path,
    *,
    workers: int = 64,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    cohort = load_json(output_root / "cohort.json")
    cases = cohort["cases"]
    semaphore = asyncio.Semaphore(workers)
    abort = asyncio.Event()
    codex_home = prepare_codex_home(project_root, output_root, "flash")
    tasks = [
        asyncio.create_task(
            generate_case(
                case,
                project_root=project_root,
                output_root=output_root,
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
    current = generation_status(output_root, len(cases))
    if errors or not current["complete"]:
        raise Stage1Error(f"human_agent_generation_incomplete:{current['selected_valid_cases']}:{len(cases)}")
    summary = {
        "schema_version": f"{SCHEMA_VERSION}.generation-summary",
        "complete": True,
        "model": GENERATION_MODEL,
        "workers": workers,
        "planned_cases": len(cases),
        "selected_valid_cases": len(cases),
        "invalid_attempts": current["invalid_attempts"],
    }
    write_json_once(output_root / "generation-summary.json", summary)
    return summary
