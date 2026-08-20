"""CODE-UP Stage 1 cohort, Codex round-trip runner, scoring, and reporting."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "codeup.stage1.v1"
MODEL = "deepseek-v4-flash"
REPETITIONS = 3
TARGET_CASES = 120
TARGET_PER_LABEL = 60
MAX_WORKERS = 64
MAX_ATTEMPTS_PER_CELL = 100
CODEX_PATH = Path(os.environ.get("CODEX_PATH") or shutil.which("codex") or "codex")
PR_BASE = "https://delanohelio.github.io/code_reviews"
SMELL_QUOTAS = {
    "Bad identifier": 18,
    "Complex, long, or inadequate logic": 16,
    "Unnecessary Code": 12,
    "Inconsistent or disrupted formatting": 8,
    "Missing constant usage": 2,
    "Wrong, missing, or inadequate string expression or literal": 2,
    "Inadequate logging and monitoring": 1,
    "Incomplete or inadequate code documentation": 1,
}


class Stage1Error(RuntimeError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_key(*values: str) -> str:
    return sha256_bytes("\x1f".join(values).encode("utf-8"))


def write_bytes_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError:
        if path.read_bytes() != data:
            raise Stage1Error(f"write_once_conflict:{path}")
        return
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_once(path: Path, value: Any) -> None:
    write_bytes_once(path, canonical_json_bytes(value) + b"\n")


def load_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage1Error(f"json_unreadable:{path}") from exc
    return value


def codeup_pr_url(project: str, pr_number: int) -> str:
    return f"{PR_BASE}/{project.replace('/', '_')}/pr_{pr_number}.json"


def cache_path(cache_root: Path, project: str, pr_number: int) -> Path:
    return cache_root / project.replace("/", "_") / f"pr_{pr_number}.json"


def download_pr(cache_root: Path, project: str, pr_number: int) -> Path:
    path = cache_path(cache_root, project, pr_number)
    if path.exists():
        load_json(path)
        return path
    url = codeup_pr_url(project, pr_number)
    last_error: Exception | None = None
    for retry in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "CODE-UP-Stage1/1.0"})
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise Stage1Error("codeup_pr_json_too_large")
            json.loads(raw.decode("utf-8"))
            write_bytes_once(path, raw)
            return path
        except (OSError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.25 * (2**retry))
    raise Stage1Error(f"codeup_pr_download_failed:{project}:{pr_number}") from last_error


def walk_objects(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_objects(child)


def find_review(document: Any, review_id: str) -> Mapping[str, Any] | None:
    candidates = [
        item
        for item in walk_objects(document)
        if item.get("id") == review_id
        and isinstance(item.get("diffHunk"), str)
        and isinstance(item.get("path"), str)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (len(item), canonical_json_bytes(item)))
    return candidates[-1]


def added_side_of_diff_hunk(diff_hunk: str) -> str:
    lines = diff_hunk.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].startswith("@@"):
        lines = lines[1:]
    output: list[str] = []
    for line in lines:
        if line.startswith("-") or line.startswith("\\ No newline"):
            continue
        if line.startswith("+") or line.startswith(" "):
            line = line[1:]
        output.append(line.rstrip())
    while output and not output[0].strip():
        output.pop(0)
    while output and not output[-1].strip():
        output.pop()
    return "\n".join(output)


_APPROX_TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_$][A-Za-z0-9_$]*|'
    r"\d+(?:\.\d+)?|==|!=|<=|>=|&&|\|\||->|::|\+\+|--|\S"
)


def approximate_tokens(source: str) -> list[str]:
    return _APPROX_TOKEN.findall(source)


def row_candidate(row: Mapping[str, str], document: Any, raw_sha256: str) -> dict[str, Any] | None:
    review = find_review(document, row["id"])
    if review is None:
        return None
    path = review.get("path")
    diff_hunk = review.get("diffHunk")
    commit = review.get("originalCommit")
    if (
        not isinstance(path, str)
        or not path.lower().endswith(".java")
        or not isinstance(diff_hunk, str)
        or not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None
    ):
        return None
    code = added_side_of_diff_hunk(diff_hunk)
    tokens = approximate_tokens(code)
    if len(tokens) < 12 or len(tokens) > 900 or len(code.encode("utf-8")) > 24_000:
        return None
    return {
        "project": row["project"],
        "pr_number": int(row["pr_number"]),
        "review_id": row["id"],
        "codeup_key": int(row["key"]),
        "codeup_url": row["url"],
        "human_understandability": row["RQ1 - Understandability?"].lower(),
        "review_scope": row["RQ1 - Scope"],
        "understandability_smell": row["RQ2 - Understandability smells"] or None,
        "path": path,
        "original_commit": commit.lower(),
        "pr_json_url": codeup_pr_url(row["project"], int(row["pr_number"])),
        "pr_json_sha256": raw_sha256,
        "diff_hunk_sha256": sha256_bytes(diff_hunk.encode("utf-8")),
        "code_1": code,
        "code_1_sha256": sha256_bytes(code.encode("utf-8")),
        "code_1_bytes": len(code.encode("utf-8")),
        "code_1_lines": code.count("\n") + 1,
        "code_1_approx_tokens": len(tokens),
    }


def prepare_cohort(project_root: Path, stage_root: Path, *, download_workers: int = 32) -> dict[str, Any]:
    artifact_root = project_root / "artifacts" / "codeUp"
    dataset_path = artifact_root / "csv" / "dataset.tsv"
    with dataset_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    yes = [
        row
        for row in rows
        if row["RQ1 - Understandability?"] == "Yes"
        and row["RQ1 - Scope"] == "Code"
        and row["selected-sample"].upper() == "TRUE"
    ]
    yes_projects = {row["project"] for row in yes}
    no_by_project: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if (
            row["RQ1 - Understandability?"] == "No"
            and row["RQ1 - Scope"] == "Code"
            and row["project"] in yes_projects
        ):
            no_by_project[row["project"]].append(row)
    yes = [row for row in yes if row["project"] in no_by_project]
    for values in no_by_project.values():
        values.sort(key=lambda row: stable_key("codeup-stage1-no", row["key"]))

    # Fetch every eligible positive and negative PR in matching projects. This
    # permits exact project matching with the closest available code-size match
    # instead of allowing an arbitrary candidate cap to distort the comparison.
    candidate_rows = list(yes)
    for project in sorted({row["project"] for row in yes}):
        candidate_rows.extend(no_by_project[project])
    unique_prs = sorted({(row["project"], int(row["pr_number"])) for row in candidate_rows})
    cache_root = stage_root / "source" / "pr-json"
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=download_workers) as pool:
        future_map = {
            pool.submit(download_pr, cache_root, project, pr_number): (project, pr_number)
            for project, pr_number in unique_prs
        }
        for future in as_completed(future_map):
            project, pr_number = future_map[future]
            try:
                future.result()
            except Exception:
                failures.append(f"{project}#{pr_number}")

    valid: list[dict[str, Any]] = []
    for row in candidate_rows:
        path = cache_path(cache_root, row["project"], int(row["pr_number"]))
        if not path.exists():
            continue
        raw = path.read_bytes()
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        candidate = row_candidate(row, document, sha256_bytes(raw))
        if candidate is not None:
            valid.append(candidate)

    valid_yes = [value for value in valid if value["human_understandability"] == "yes"]
    valid_no_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in valid:
        if value["human_understandability"] == "no":
            valid_no_by_project[value["project"]].append(value)
    for values in valid_no_by_project.values():
        values.sort(key=lambda value: stable_key("valid-no", str(value["codeup_key"])))

    selected_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used_projects: set[str] = set()
    used_yes: set[int] = set()

    def choose(values: Sequence[dict[str, Any]], needed: int) -> None:
        candidates = sorted(
            values,
            key=lambda value: stable_key(
                "codeup-stage1-yes", value["project"], str(value["codeup_key"])
            ),
        )
        for positive in candidates:
            if len(selected_pairs) >= TARGET_PER_LABEL or needed <= 0:
                break
            project = positive["project"]
            if project in used_projects or not valid_no_by_project.get(project):
                continue
            negative = min(
                valid_no_by_project[project],
                key=lambda value: (
                    abs(
                        math.log1p(value["code_1_approx_tokens"])
                        - math.log1p(positive["code_1_approx_tokens"])
                    ),
                    stable_key("matched-no", str(value["codeup_key"])),
                ),
            )
            selected_pairs.append((positive, negative))
            used_projects.add(project)
            used_yes.add(positive["codeup_key"])
            needed -= 1

    for smell, quota in SMELL_QUOTAS.items():
        choose([value for value in valid_yes if value["understandability_smell"] == smell], quota)
    if len(selected_pairs) < TARGET_PER_LABEL:
        choose([value for value in valid_yes if value["codeup_key"] not in used_yes], TARGET_PER_LABEL - len(selected_pairs))
    if len(selected_pairs) != TARGET_PER_LABEL:
        raise Stage1Error(f"cohort_pair_count_not_60:{len(selected_pairs)}")

    cases: list[dict[str, Any]] = []
    for pair_index, (positive, negative) in enumerate(selected_pairs, start=1):
        pair_id = f"pair-{pair_index:03d}"
        for candidate in (positive, negative):
            case = dict(candidate)
            case["pair_id"] = pair_id
            case["case_id"] = f"{pair_id}-{candidate['human_understandability']}"
            cases.append(case)
    cases.sort(key=lambda value: value["case_id"])
    dataset_bytes = dataset_path.read_bytes()
    cohort = {
        "schema_version": f"{SCHEMA_VERSION}.cohort",
        "design": {
            "cases": TARGET_CASES,
            "labels": {"yes": TARGET_PER_LABEL, "no": TARGET_PER_LABEL},
            "project_matched_pairs": TARGET_PER_LABEL,
            "distinct_projects": TARGET_PER_LABEL,
            "repetitions_per_case": REPETITIONS,
            "planned_round_trips": TARGET_CASES * REPETITIONS,
            "model_input_excludes_review_comment_and_human_label": True,
            "code_1_definition": "added/current side of the inline review diff hunk",
            "selection_seed": "codeup-stage1-v1",
            "positive_smell_target_quotas": SMELL_QUOTAS,
        },
        "source": {
            "artifact_commit": subprocess.check_output(
                ["git", "-C", str(artifact_root), "rev-parse", "HEAD"], text=True
            ).strip(),
            "dataset_path": str(dataset_path.relative_to(project_root)),
            "dataset_bytes": len(dataset_bytes),
            "dataset_sha256": sha256_bytes(dataset_bytes),
            "download_failures": sorted(failures),
        },
        "cases": cases,
    }
    write_json_once(stage_root / "cohort.json", cohort)
    return cohort


def prepare_half_cohort(
    project_root: Path, stage_root: Path, *, download_workers: int = 32
) -> dict[str, Any]:
    """Build 600 project-matched Yes/No pairs: 1,200 unique dataset rows."""

    artifact_root = project_root / "artifacts" / "codeUp"
    dataset_path = artifact_root / "csv" / "dataset.tsv"
    with dataset_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    candidate_rows = [
        row for row in rows if row["RQ1 - Understandability?"] in {"Yes", "No"}
    ]
    unique_prs = sorted(
        {(row["project"], int(row["pr_number"])) for row in candidate_rows}
    )
    cache_root = stage_root / "source" / "pr-json"
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=download_workers) as pool:
        future_map = {
            pool.submit(download_pr, cache_root, project, pr_number): (project, pr_number)
            for project, pr_number in unique_prs
        }
        for future in as_completed(future_map):
            project, pr_number = future_map[future]
            try:
                future.result()
            except Exception:
                failures.append(f"{project}#{pr_number}")

    valid: list[dict[str, Any]] = []
    for row in candidate_rows:
        path = cache_path(cache_root, row["project"], int(row["pr_number"]))
        if not path.exists():
            continue
        raw = path.read_bytes()
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        candidate = row_candidate(row, document, sha256_bytes(raw))
        if candidate is not None:
            valid.append(candidate)

    by_project_label: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"yes": [], "no": []}
    )
    for candidate in valid:
        by_project_label[candidate["project"]][
            candidate["human_understandability"]
        ].append(candidate)

    available_pairs: list[tuple[float, str, dict[str, Any], dict[str, Any]]] = []
    for project in sorted(by_project_label):
        positives = sorted(
            by_project_label[project]["yes"],
            key=lambda value: stable_key("half-yes", str(value["codeup_key"])),
        )
        negatives = list(by_project_label[project]["no"])
        while positives and negatives:
            positive = positives.pop(0)
            negative = min(
                negatives,
                key=lambda value: (
                    abs(
                        math.log1p(value["code_1_approx_tokens"])
                        - math.log1p(positive["code_1_approx_tokens"])
                    ),
                    stable_key("half-no", str(value["codeup_key"])),
                ),
            )
            negatives.remove(negative)
            size_distance = abs(
                math.log1p(negative["code_1_approx_tokens"])
                - math.log1p(positive["code_1_approx_tokens"])
            )
            available_pairs.append(
                (
                    size_distance,
                    stable_key(
                        "codeup-stage1-half-pair",
                        project,
                        str(positive["codeup_key"]),
                        str(negative["codeup_key"]),
                    ),
                    positive,
                    negative,
                )
            )
    available_pairs.sort(key=lambda value: (value[0], value[1]))
    selected_pairs = available_pairs[:600]
    if len(selected_pairs) != 600:
        raise Stage1Error(f"half_cohort_pair_count_not_600:{len(selected_pairs)}")
    selected_pairs.sort(key=lambda value: value[1])

    cases: list[dict[str, Any]] = []
    for pair_index, (_, _, positive, negative) in enumerate(selected_pairs, start=1):
        pair_id = f"half-pair-{pair_index:04d}"
        for candidate in (positive, negative):
            case = dict(candidate)
            case["pair_id"] = pair_id
            case["case_id"] = f"{pair_id}-{candidate['human_understandability']}"
            cases.append(case)
    cases.sort(key=lambda value: value["case_id"])
    dataset_bytes = dataset_path.read_bytes()
    cohort = {
        "schema_version": f"{SCHEMA_VERSION}.half-cohort",
        "design": {
            "dataset_rows": len(rows),
            "cases": 1200,
            "dataset_fraction": 1200 / len(rows),
            "labels": {"yes": 600, "no": 600},
            "project_matched_pairs": 600,
            "distinct_projects": len({case["project"] for case in cases}),
            "repetitions_per_case": REPETITIONS,
            "planned_round_trips": 1200 * REPETITIONS,
            "model_input_excludes_review_comment_and_human_label": True,
            "code_1_definition": "added/current side of the inline review diff hunk",
            "selection_seed": "codeup-stage1-50pct-v1",
            "selection": "600 closest-code-size pairs among valid within-project Yes/No pairs",
        },
        "source": {
            "artifact_commit": subprocess.check_output(
                ["git", "-C", str(artifact_root), "rev-parse", "HEAD"], text=True
            ).strip(),
            "dataset_path": str(dataset_path.relative_to(project_root)),
            "dataset_bytes": len(dataset_bytes),
            "dataset_sha256": sha256_bytes(dataset_bytes),
            "download_failures": sorted(failures),
            "binary_labeled_rows": len(candidate_rows),
            "valid_candidate_rows": len(valid),
            "valid_project_matched_pair_capacity": len(available_pairs),
        },
        "cases": cases,
    }
    write_json_once(stage_root / "cohort.json", cohort)
    return cohort


def extraction_prompt(case: Mapping[str, Any]) -> str:
    return (
        "You are participating in a controlled code round-trip study. Do not use tools or "
        "inspect files. Read only the Java fragment below. Describe everything needed to "
        "reconstruct its behavior and structure: identifiers, calls, literals, expressions, "
        "conditions, ordering, and formatting-relevant structure. Do not critique, improve, "
        "or simplify it. Return exactly one JSON object with this schema: "
        '{"directions":["nonempty prose step", "..."]}. Use 1 to 20 concise directions. '
        "Do not include Markdown fences.\n\nJAVA FRAGMENT:\n" + str(case["code_1"])
    )


def regeneration_prompt(case: Mapping[str, Any], directions: Sequence[str]) -> str:
    return (
        "You are participating in a controlled code round-trip study. Do not use tools or "
        "inspect files. Reconstruct the Java fragment solely from the natural-language "
        "directions below. Preserve the named identifiers, literals, behavior, ordering, and "
        "fragment granularity described. Do not add explanations or Markdown fences. Return "
        'exactly one JSON object with this schema: {"code":"nonempty Java fragment"}.\n\n'
        f"ORIGINAL FILE PATH (context only): {case['path']}\n"
        "DIRECTIONS:\n"
        + json.dumps(list(directions), ensure_ascii=False)
    )


def validate_directions(value: Any) -> list[str]:
    if not isinstance(value, Mapping) or "directions" not in value:
        raise Stage1Error("directions_schema_invalid")
    directions = value["directions"]
    if not isinstance(directions, list):
        raise Stage1Error("directions_value_invalid")
    usable = [
        item.strip()
        for item in directions
        if isinstance(item, str) and item.strip() and len(item) <= 5000
    ]
    if not usable or len(usable) > 200:
        raise Stage1Error("directions_value_invalid")
    return usable


def validate_code(value: Any) -> str:
    if not isinstance(value, Mapping) or "code" not in value:
        raise Stage1Error("code_schema_invalid")
    code = value["code"]
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 64_000:
        raise Stage1Error("code_value_invalid")
    code = code.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(approximate_tokens(code)) < 3:
        raise Stage1Error("code_too_short")
    return code


def directions_from_output(raw: bytes) -> list[str]:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise Stage1Error("directions_not_utf8") from exc
    try:
        return validate_directions(json.loads(text))
    except (json.JSONDecodeError, Stage1Error):
        # V4 Flash occasionally leaves quotation marks inside an otherwise
        # useful JSON-mode directions array unescaped. Formatting is not an
        # experimental outcome here, so retain the complete nonempty natural-
        # language response as one direction instead of discarding its content.
        if not text or len(text.encode("utf-8")) > 128_000:
            raise Stage1Error("directions_text_invalid")
        return [text]


def code_from_output(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise Stage1Error("code_not_utf8") from exc
    try:
        return validate_code(json.loads(text))
    except (json.JSONDecodeError, Stage1Error):
        candidate = text
        if candidate.startswith("```") and candidate.endswith("```"):
            lines = candidate.splitlines()
            candidate = "\n".join(lines[1:-1])
        match = re.match(r'^\s*\{\s*"code"\s*:\s*"', candidate, flags=re.DOTALL)
        if match is not None:
            candidate = candidate[match.end() :]
            candidate = re.sub(r'"\s*\}\s*$', "", candidate, flags=re.DOTALL)
            candidate = (
                candidate.replace(r"\r\n", "\n")
                .replace(r"\n", "\n")
                .replace(r"\t", "\t")
                .replace(r'\"', '"')
                .replace(r"\\", "\\")
            )
        return validate_code({"code": candidate})


async def codex_call(
    prompt: str,
    *,
    project_root: Path,
    codex_home: Path,
    timeout_seconds: int,
) -> tuple[bytes, bytes, int, int]:
    started = time.monotonic()
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    env["CODEUP_PROXY_TOKEN"] = "loopback-only"
    process = await asyncio.create_subprocess_exec(
        str(CODEX_PATH),
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        str(project_root),
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8")), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise Stage1Error("codex_timeout")
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return stdout, stderr, int(process.returncode), elapsed_ms


async def run_cell(
    case: Mapping[str, Any],
    repeat_index: int,
    *,
    project_root: Path,
    stage_root: Path,
    codex_home: Path,
    semaphore: asyncio.Semaphore,
    timeout_seconds: int,
) -> dict[str, Any]:
    cell_root = stage_root / "runs" / str(case["case_id"]) / f"repeat-{repeat_index}"
    selected_path = cell_root / "selected.json"
    if selected_path.exists():
        return load_json(selected_path)
    existing_directories = sorted(cell_root.glob("attempt-*")) if cell_root.exists() else []
    existing_indices: list[int] = []
    for directory in existing_directories:
        try:
            existing_indices.append(int(directory.name.removeprefix("attempt-")))
        except ValueError as exc:
            raise Stage1Error(f"attempt_directory_name_invalid:{directory}") from exc
    start_attempt = max(existing_indices, default=0) + 1
    for attempt_index in range(start_attempt, MAX_ATTEMPTS_PER_CELL + 1):
        attempt_root = cell_root / f"attempt-{attempt_index:02d}"
        extraction = extraction_prompt(case)
        try:
            async with semaphore:
                out1, err1, return1, elapsed1 = await codex_call(
                    extraction,
                    project_root=project_root,
                    codex_home=codex_home,
                    timeout_seconds=timeout_seconds,
                )
            write_bytes_once(attempt_root / "extraction.stdout", out1)
            write_bytes_once(attempt_root / "extraction.stderr", err1)
            if return1 != 0:
                raise Stage1Error(f"extraction_codex_exit_{return1}")
            directions = directions_from_output(out1)
            regeneration = regeneration_prompt(case, directions)
            async with semaphore:
                out2, err2, return2, elapsed2 = await codex_call(
                    regeneration,
                    project_root=project_root,
                    codex_home=codex_home,
                    timeout_seconds=timeout_seconds,
                )
            write_bytes_once(attempt_root / "regeneration.stdout", out2)
            write_bytes_once(attempt_root / "regeneration.stderr", err2)
            if return2 != 0:
                raise Stage1Error(f"regeneration_codex_exit_{return2}")
            code_2 = code_from_output(out2)
            value = {
                "schema_version": f"{SCHEMA_VERSION}.attempt",
                "status": "valid",
                "case_id": case["case_id"],
                "repeat_index": repeat_index,
                "attempt_index": attempt_index,
                "model": MODEL,
                "thinking": "disabled",
                "code_1_sha256": case["code_1_sha256"],
                "extraction_prompt_sha256": sha256_bytes(extraction.encode("utf-8")),
                "directions": directions,
                "directions_sha256": sha256_bytes(canonical_json_bytes(directions)),
                "regeneration_prompt_sha256": sha256_bytes(regeneration.encode("utf-8")),
                "code_2": code_2,
                "code_2_sha256": sha256_bytes(code_2.encode("utf-8")),
                "elapsed_milliseconds": {
                    "extraction": elapsed1,
                    "regeneration": elapsed2,
                    "total": elapsed1 + elapsed2,
                },
            }
            write_json_once(attempt_root / "attempt.json", value)
            selected = {
                "schema_version": f"{SCHEMA_VERSION}.selected",
                "case_id": case["case_id"],
                "repeat_index": repeat_index,
                "attempt_index": attempt_index,
                "attempt_path": str((attempt_root / "attempt.json").relative_to(stage_root)),
                "attempt_sha256": sha256_bytes((attempt_root / "attempt.json").read_bytes()),
                "code_1_sha256": case["code_1_sha256"],
                "code_2_sha256": value["code_2_sha256"],
            }
            write_json_once(selected_path, selected)
            return selected
        except (Stage1Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failure = {
                "schema_version": f"{SCHEMA_VERSION}.attempt",
                "status": "invalid",
                "case_id": case["case_id"],
                "repeat_index": repeat_index,
                "attempt_index": attempt_index,
                "model": MODEL,
                "failure_code": str(exc),
                "code_1_sha256": case["code_1_sha256"],
            }
            write_json_once(attempt_root / "attempt.json", failure)
    raise Stage1Error(f"cell_attempt_cap_exhausted:{case['case_id']}:{repeat_index}")


async def run_round_trips(
    project_root: Path,
    stage_root: Path,
    *,
    workers: int = MAX_WORKERS,
    timeout_seconds: int = 360,
) -> dict[str, Any]:
    if workers < 1 or workers > MAX_WORKERS:
        raise Stage1Error("workers_out_of_range")
    cohort = load_json(stage_root / "cohort.json")
    cases = cohort.get("cases")
    if not isinstance(cases, list) or not cases:
        raise Stage1Error("cohort_empty_or_invalid")
    repetitions = cohort.get("design", {}).get("repetitions_per_case")
    if repetitions != REPETITIONS:
        raise Stage1Error("cohort_repetitions_invalid")
    planned_cells = len(cases) * repetitions
    semaphore = asyncio.Semaphore(workers)
    tasks = [
        asyncio.create_task(
            run_cell(
                case,
                repeat,
                project_root=project_root,
                stage_root=stage_root,
                codex_home=stage_root / "codex-home",
                semaphore=semaphore,
                timeout_seconds=timeout_seconds,
            )
        )
        for case in cases
        for repeat in range(repetitions)
    ]
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for task in asyncio.as_completed(tasks):
        try:
            results.append(await task)
        except Exception as exc:
            errors.append(str(exc))
        progress = {
            "schema_version": f"{SCHEMA_VERSION}.progress",
            "planned_cells": planned_cells,
            "selected_valid_cells": len(results),
            "terminal_errors": len(errors),
            "remaining_cells": len(tasks) - len(results) - len(errors),
            "workers": workers,
            "model": MODEL,
        }
        progress_path = stage_root / "progress.json"
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = progress_path.with_suffix(".json.tmp")
        temporary.write_bytes(canonical_json_bytes(progress) + b"\n")
        os.replace(temporary, progress_path)
    summary = {
        "schema_version": f"{SCHEMA_VERSION}.generation-summary",
        "planned_cells": planned_cells,
        "selected_valid_cells": len(results),
        "terminal_errors": sorted(errors),
        "workers": workers,
        "model": MODEL,
        "thinking": "disabled",
        "complete": len(results) == planned_cells and not errors,
    }
    write_json_once(stage_root / "generation-summary.json", summary)
    if not summary["complete"]:
        raise Stage1Error("generation_incomplete")
    return summary


def stage_status(stage_root: Path) -> dict[str, Any]:
    cohort_path = stage_root / "cohort.json"
    cases = 0
    repetitions = REPETITIONS
    if cohort_path.exists():
        cohort = load_json(cohort_path)
        cases = len(cohort.get("cases", []))
        repetitions = int(
            cohort.get("design", {}).get("repetitions_per_case", REPETITIONS)
        )
    selected = list((stage_root / "runs").glob("*/repeat-*/selected.json")) if (stage_root / "runs").exists() else []
    attempts = list((stage_root / "runs").glob("*/repeat-*/attempt-*/attempt.json")) if (stage_root / "runs").exists() else []
    invalid = 0
    for path in attempts:
        try:
            if load_json(path).get("status") == "invalid":
                invalid += 1
        except Stage1Error:
            invalid += 1
    return {
        "schema_version": f"{SCHEMA_VERSION}.status",
        "cohort_cases": cases,
        "planned_cells": cases * repetitions,
        "selected_valid_cells": len(selected),
        "attempts": len(attempts),
        "invalid_attempts": invalid,
        "generation_complete": cases > 0 and len(selected) == cases * repetitions,
        "scoring_complete": (stage_root / "scores.json").exists(),
        "report_complete": (stage_root / "report.json").exists(),
    }


def fragment_tokens(source: str) -> tuple[str, ...]:
    """Tokenize every CODE-UP review scope, including comments and Javadocs.

    CODE-UP contains comment-only review fragments.  The Java parser's lexical
    view intentionally discards those comments, which made valid round trips
    appear empty.  The study therefore uses one deterministic, language-aware
    approximate tokenization for every source and reconstruction so all review
    scopes are compared on the same basis.
    """

    tokens = tuple(approximate_tokens(source))
    if not tokens:
        raise Stage1Error("fragment_tokens_empty")
    return tokens


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and values[order[end]] == values[order[position]]:
            end += 1
        rank = (position + 1 + end) / 2.0
        for cursor in range(position, end):
            ranks[order[cursor]] = rank
        position = end
    return ranks


def pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise Stage1Error("correlation_shape_invalid")
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right)
    )
    if denominator == 0:
        return float("nan")
    return numerator / denominator


def spearman(left: Sequence[float], right: Sequence[float]) -> float:
    return pearson(_rank(left), _rank(right))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def codebert_batch_similarities(
    pairs: Sequence[tuple[tuple[str, ...], tuple[str, ...]]],
    *,
    tokenizer: Any,
    model: Any,
) -> tuple[list[float], str]:
    """Embed all unique fragments in CUDA batches, with a CPU fallback."""

    import torch

    from backtranslation.scoring import cosine_similarity, normalized_scoring_view

    torch.use_deterministic_algorithms(True, warn_only=False)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.to(device=device, dtype=torch.float32)
    model.eval()
    if hasattr(model, "requires_grad_"):
        model.requires_grad_(False)

    token_sets: dict[str, tuple[str, ...]] = {}
    pair_keys: list[tuple[str, str]] = []
    for left, right in pairs:
        left_key = sha256_bytes(canonical_json_bytes(left))
        right_key = sha256_bytes(canonical_json_bytes(right))
        token_sets[left_key] = left
        token_sets[right_key] = right
        pair_keys.append((left_key, right_key))

    bos = tokenizer.bos_token_id
    eos = tokenizer.eos_token_id
    pad = tokenizer.pad_token_id
    if not all(isinstance(value, int) for value in (bos, eos, pad)):
        raise Stage1Error("codebert_special_tokens_invalid")
    chunks: list[tuple[str, list[int]]] = []
    content_counts: dict[str, int] = {}
    for key in sorted(token_sets):
        normalized = normalized_scoring_view(token_sets[key])
        encoded = tokenizer(
            normalized,
            add_special_tokens=False,
            padding=False,
            truncation=False,
            return_attention_mask=False,
            return_token_type_ids=False,
            verbose=False,
        )["input_ids"]
        if not encoded:
            raise Stage1Error("codebert_fragment_subtokens_empty")
        content_counts[key] = len(encoded)
        for start in range(0, len(encoded), 510):
            chunks.append((key, list(encoded[start : start + 510])))

    sums: dict[str, Any] = {}
    if device.type == "cpu":
        torch.set_num_threads(min(16, os.cpu_count() or 1))
    batch_size = 16
    with torch.inference_mode():
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            widths = [len(content) + 2 for _, content in batch]
            width = max(widths)
            input_rows: list[list[int]] = []
            masks: list[list[int]] = []
            for (_, content), row_width in zip(batch, widths):
                wrapped = [bos, *content, eos]
                input_rows.append(wrapped + [pad] * (width - row_width))
                masks.append([1] * row_width + [0] * (width - row_width))
            input_ids = torch.tensor(input_rows, dtype=torch.long, device=device)
            attention_mask = torch.tensor(masks, dtype=torch.long, device=device)
            hidden = model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                return_dict=True,
            ).last_hidden_state
            for index, ((key, content), row_width) in enumerate(zip(batch, widths)):
                vector_sum = hidden[index, 1 : row_width - 1, :].to("cpu", dtype=torch.float64).sum(dim=0)
                sums[key] = vector_sum if key not in sums else sums[key] + vector_sum

    vectors = {
        key: tuple(float(value) for value in (sums[key] / content_counts[key]).tolist())
        for key in token_sets
    }
    similarities = [cosine_similarity(vectors[left], vectors[right]) for left, right in pair_keys]
    device_label = torch.cuda.get_device_name(device) if device.type == "cuda" else "cpu"
    model.to("cpu")
    if device.type == "cuda":
        torch.cuda.empty_cache()
    return similarities, device_label


def score_stage(project_root: Path, stage_root: Path) -> dict[str, Any]:
    from backtranslation.scoring import (
        bleu_score,
        load_pinned_codebert,
        rouge_scores,
    )

    cohort = load_json(stage_root / "cohort.json")
    cases = cohort["cases"]
    if not cases:
        raise Stage1Error("cohort_empty")
    repetitions = int(cohort["design"]["repetitions_per_case"])
    tokenizer, model = load_pinned_codebert(
        project_root / "models" / "codebert-base",
        project_root / "config" / "codebert-base-revision.json",
    )
    run_rows: list[dict[str, Any]] = []
    token_pairs: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for case in cases:
        code1_tokens = fragment_tokens(case["code_1"])
        for repeat in range(repetitions):
            selected = load_json(
                stage_root / "runs" / case["case_id"] / f"repeat-{repeat}" / "selected.json"
            )
            attempt_path = stage_root / selected["attempt_path"]
            if sha256_bytes(attempt_path.read_bytes()) != selected["attempt_sha256"]:
                raise Stage1Error("selected_attempt_hash_mismatch")
            attempt = load_json(attempt_path)
            code2 = attempt["code_2"]
            code2_tokens = fragment_tokens(code2)
            bleu = bleu_score(code1_tokens, code2_tokens)
            rouge = rouge_scores(code1_tokens, code2_tokens)
            token_pairs.append((code1_tokens, code2_tokens))
            run_rows.append(
                {
                    "case_id": case["case_id"],
                    "pair_id": case["pair_id"],
                    "project": case["project"],
                    "human_understandability": case["human_understandability"],
                    "understandability_smell": case["understandability_smell"],
                    "repeat_index": repeat,
                    "selected_attempt_index": selected["attempt_index"],
                    "code_1_sha256": case["code_1_sha256"],
                    "code_2_sha256": attempt["code_2_sha256"],
                    "code_1_tokens": len(code1_tokens),
                    "code_2_tokens": len(code2_tokens),
                    "token_length_ratio": len(code2_tokens) / len(code1_tokens),
                    "bleu": bleu.score,
                    "rouge_1_f1": rouge.rouge1.f1,
                    "rouge_2_f1": rouge.rouge2.f1,
                    "rouge_l_f1": rouge.rouge_l.f1,
                }
            )
    similarities, codebert_device = codebert_batch_similarities(
        token_pairs, tokenizer=tokenizer, model=model
    )
    for row, similarity in zip(run_rows, similarities, strict=True):
        row["codebert"] = similarity
    case_rows: list[dict[str, Any]] = []
    for case in cases:
        rows = [row for row in run_rows if row["case_id"] == case["case_id"]]
        if len(rows) != repetitions:
            raise Stage1Error("case_repeat_count_mismatch")
        case_rows.append(
            {
                "case_id": case["case_id"],
                "pair_id": case["pair_id"],
                "project": case["project"],
                "human_understandability": case["human_understandability"],
                "understandability_smell": case["understandability_smell"],
                "repetitions": repetitions,
                **{
                    metric: _mean([float(row[metric]) for row in rows])
                    for metric in (
                        "token_length_ratio",
                        "bleu",
                        "rouge_1_f1",
                        "rouge_2_f1",
                        "rouge_l_f1",
                        "codebert",
                    )
                },
            }
        )
    labels = [1.0 if row["human_understandability"] == "yes" else 0.0 for row in case_rows]
    metrics: dict[str, Any] = {}
    for metric in ("bleu", "codebert", "rouge_1_f1", "rouge_2_f1", "rouge_l_f1", "token_length_ratio"):
        values = [float(row[metric]) for row in case_rows]
        yes_values = [value for value, label in zip(values, labels) if label == 1.0]
        no_values = [value for value, label in zip(values, labels) if label == 0.0]
        pair_differences: list[float] = []
        for pair_id in sorted({row["pair_id"] for row in case_rows}):
            positive = next(row for row in case_rows if row["pair_id"] == pair_id and row["human_understandability"] == "yes")
            negative = next(row for row in case_rows if row["pair_id"] == pair_id and row["human_understandability"] == "no")
            pair_differences.append(float(positive[metric]) - float(negative[metric]))
        metrics[metric] = {
            "spearman_rho": spearman(values, labels),
            "point_biserial_r": pearson(values, labels),
            "yes_mean": _mean(yes_values),
            "no_mean": _mean(no_values),
            "unmatched_mean_difference_yes_minus_no": _mean(yes_values) - _mean(no_values),
            "matched_pair_mean_difference_yes_minus_no": _mean(pair_differences),
            "matched_pairs_positive_difference": sum(value > 0 for value in pair_differences),
            "matched_pairs_tied": sum(value == 0 for value in pair_differences),
            "matched_pairs_negative_difference": sum(value < 0 for value in pair_differences),
        }
    result = {
        "schema_version": f"{SCHEMA_VERSION}.scores",
        "design": {
            "case_count": len(case_rows),
            "run_count": len(run_rows),
            "repetitions_averaged_per_case": repetitions,
            "project_matched_pairs": cohort["design"]["project_matched_pairs"],
            "distinct_projects": cohort["design"]["distinct_projects"],
            "human_label_encoding": {"yes": 1, "no": 0},
            "comparison": "higher similarity means Code2 is more like Code1",
            "tokenization": "codeup-approximate-v1 (retains comments and Javadocs)",
            "codebert_device": codebert_device,
            "codebert_batch_size": 16,
        },
        "metric_statistics": metrics,
        "case_scores": case_rows,
        "run_scores": run_rows,
    }
    write_json_once(stage_root / "scores.json", result)
    return result


def _tex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in value)


def build_report(stage_root: Path, report_root: Path) -> dict[str, Any]:
    from scipy.stats import spearmanr, wilcoxon

    scores = load_json(stage_root / "scores.json")
    generation = load_json(stage_root / "generation-summary.json")
    statistics = scores["metric_statistics"]
    display = [
        ("CodeBERT cosine", "codebert"),
        ("BLEU-4", "bleu"),
        ("ROUGE-1 F1", "rouge_1_f1"),
        ("ROUGE-2 F1", "rouge_2_f1"),
        ("ROUGE-L F1", "rouge_l_f1"),
        ("Token length ratio", "token_length_ratio"),
    ]
    rows = []
    for label, key in display:
        value = statistics[key]
        rows.append(
            {
                "metric": label,
                "yes_mean": value["yes_mean"],
                "no_mean": value["no_mean"],
                "difference": value["matched_pair_mean_difference_yes_minus_no"],
                "spearman": value["spearman_rho"],
                "point_biserial": value["point_biserial_r"],
            }
        )
    strongest = max(rows[:-1], key=lambda row: abs(row["spearman"]))
    interpretation = (
        f"The largest absolute rank correlation among the code-similarity measures is "
        f"{strongest['metric']} (Spearman rho={strongest['spearman']:.3f}). "
        "Positive values mean fragments labeled as understandability-related tended to retain "
        "more similarity after Code1→NL→Code2; negative values mean they tended to retain less."
    )
    case_scores = scores["case_scores"]
    case_count = len(case_scores)
    pair_ids = sorted({row["pair_id"] for row in case_scores})
    pair_count = len(pair_ids)
    distinct_projects = int(scores["design"].get("distinct_projects", 0))
    labels = [1 if row["human_understandability"] == "yes" else 0 for row in case_scores]
    rank_p_values: list[float] = []
    matched_p_values: list[float] = []
    for _, metric in display[:-1]:
        values = [float(row[metric]) for row in case_scores]
        rank_p_values.append(float(spearmanr(values, labels).pvalue))
        differences: list[float] = []
        for pair_id in pair_ids:
            positive = next(
                row
                for row in case_scores
                if row["pair_id"] == pair_id and row["human_understandability"] == "yes"
            )
            negative = next(
                row
                for row in case_scores
                if row["pair_id"] == pair_id and row["human_understandability"] == "no"
            )
            differences.append(float(positive[metric]) - float(negative[metric]))
        matched_p_values.append(float(wilcoxon(differences, zero_method="wilcox").pvalue))
    minimum_rank_p = min(rank_p_values)
    minimum_matched_p = min(matched_p_values)
    if minimum_rank_p >= 0.05 and minimum_matched_p >= 0.05:
        significance = (
            "None of the five code-similarity associations was statistically distinguishable "
            f"from zero (two-sided Spearman p >= {minimum_rank_p:.3f}; matched-pair "
            f"Wilcoxon p >= {minimum_matched_p:.3f})."
        )
    else:
        significance = (
            "At least one code-similarity association met the conventional 0.05 threshold "
            f"(minimum two-sided Spearman p = {minimum_rank_p:.3g}; minimum matched-pair "
            f"Wilcoxon p = {minimum_matched_p:.3g})."
        )
    md_lines = [
        "# CODE-UP Stage 1: Round-Trip Code Similarity",
        "",
        "**Report date:** August 12, 2026  ",
        f"**Execution model:** `{MODEL}` through the Codex CLI Responses harness  ",
        f"**Cohort:** {case_count:,} Java inline-review fragments in {pair_count:,} project-matched pairs across {distinct_projects:,} projects  ",
        f"**Round trips:** {generation['selected_valid_cells']} valid runs; three independent runs averaged per case",
        "",
        "## Question",
        "",
        "How much does a Java fragment change after Code1 → natural-language reconstruction directions → Code2, and is retained similarity associated with whether the original review comment was labeled as understandability-related in CODE-UP?",
        "",
        "## Overall results",
        "",
        "| Metric | Understandability mean | Other-review mean | Matched difference | Spearman ρ | Point-biserial r |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        md_lines.append(
            f"| {row['metric']} | {row['yes_mean']:.4f} | {row['no_mean']:.4f} | "
            f"{row['difference']:+.4f} | {row['spearman']:+.4f} | {row['point_biserial']:+.4f} |"
        )
    md_lines.extend(
        [
            "",
            "## Interpretation",
            "",
            interpretation,
            "",
            significance,
            "",
            "Each displayed case-level score is the arithmetic mean of its three independently generated Code2 fragments. Similarity is measured against the exact Code1 fragment shown to the first model turn. The human review text and label were never included in either model prompt.",
            "",
            "## What changed in the round trip",
            "",
            "CodeBERT captures semantic/representation similarity, BLEU emphasizes exact local token sequences, ROUGE measures token overlap, and the token-length ratio shows expansion or compression. A single deterministic tokenizer retains comment and Javadoc text as well as Java code so every CODE-UP review scope is represented. Taken together, the table distinguishes a semantically similar rewrite from a nearly exact reconstruction.",
            "",
            "## Reproducibility artifacts",
            "",
            f"The complete cohort, all selected generations, per-run scores, provider request audit metadata, and this report are under `artifacts/{stage_root.name}/`. Raw CODE-UP review text was retained only in the downloaded source cache; it was excluded from prompts.",
            "",
        ]
    )
    markdown = "\n".join(md_lines)

    tex_rows = "\n".join(
        f"{_tex_escape(row['metric'])} & {row['yes_mean']:.4f} & {row['no_mean']:.4f} & "
        f"{row['difference']:+.4f} & {row['spearman']:+.4f} & {row['point_biserial']:+.4f} \\\\"
        for row in rows
    )
    tex_interpretation = _tex_escape(interpretation).replace(
        "Code1→NL→Code2", r"Code1 $\rightarrow$ NL $\rightarrow$ Code2"
    )
    tex = rf"""\documentclass[11pt]{{article}}
\usepackage[margin=1in]{{geometry}}
\usepackage{{booktabs}}
\usepackage{{longtable}}
\usepackage[T1]{{fontenc}}
\title{{CODE-UP Stage 1: Round-Trip Code Similarity}}
\date{{August 12, 2026}}
\begin{{document}}
\maketitle

\section{{Question}}
How much does a Java fragment change after Code1 $\rightarrow$ natural-language reconstruction directions $\rightarrow$ Code2, and is retained similarity associated with whether the original review comment was labeled as understandability-related in CODE-UP?

\section{{Design}}
The cohort contains {case_count} Java inline-review fragments in {pair_count} project-matched pairs across {distinct_projects} projects. Each case has three independent round trips, for {generation['selected_valid_cells']} valid runs. The model was \texttt{{deepseek-v4-flash}} through the Codex CLI Responses harness. Human review text and labels were excluded from model prompts. Case-level statistics average the three repetitions.

\section{{Overall Results}}
\begin{{longtable}}{{lrrrrr}}
\toprule
Metric & Understand. & Other & Matched $\Delta$ & Spearman $\rho$ & Point-biserial $r$ \\
\midrule
{tex_rows}
\bottomrule
\end{{longtable}}

\section{{Interpretation}}
{tex_interpretation}

{_tex_escape(significance)}

CodeBERT captures semantic/representation similarity, BLEU emphasizes exact local token sequences, ROUGE measures token overlap, and the token-length ratio shows expansion or compression. A single deterministic tokenizer retains comment and Javadoc text as well as Java code so every CODE-UP review scope is represented. Together they distinguish a semantically similar rewrite from a nearly exact reconstruction.

\end{{document}}
"""
    report_stem = (
        "2026-08-12-codeup-stage1-50pct"
        if case_count == 1200
        else "2026-08-12-codeup-stage1"
    )
    markdown_path = report_root / f"{report_stem}.md"
    tex_path = report_root / f"{report_stem}.tex"
    write_bytes_once(markdown_path, markdown.encode("utf-8"))
    write_bytes_once(tex_path, tex.encode("utf-8"))
    receipt = {
        "schema_version": f"{SCHEMA_VERSION}.report",
        "markdown_path": str(markdown_path),
        "markdown_sha256": sha256_bytes(markdown.encode("utf-8")),
        "tex_path": str(tex_path),
        "tex_sha256": sha256_bytes(tex.encode("utf-8")),
        "table": rows,
    }
    write_json_once(stage_root / "report.json", receipt)
    return receipt
