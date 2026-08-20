#!/usr/bin/env python3
"""Recover and validate the expanded Scalabrino et al. TSE dataset.

This tool deliberately separates two kinds of material:

* the authors' response CSV, systems CSV, and verification questions are
  downloaded into a caller-provided cache and hash-checked, but are not copied
  into the study artifact because their download page states no reuse license;
* the 50 selected Java declarations/bodies are reconstructed from the exact
  git revisions and retained with their upstream open-source license notices.

No generated predictor and no outcome association is computed here.  The only
checks involving AU/PBU are schema/domain/missingness checks needed to identify
the correct expanded dataset.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DATASET_PAGE = "https://dibt-research.unimol.it/report/understandability-tse/"
PAPER_URL = "https://sscalabrino.github.io/files/2019/TSE2019AutomaticallyAssessingCode.pdf"
ASSETS = {
    "understandability.csv": {
        "url": DATASET_PAGE + "files/RQ1/understandability.csv",
        "sha256": "77704e6a39ded74a4542d61aaf737432950905fe9b886a2dd822132f75395ca1",
    },
    "systems.csv": {
        "url": DATASET_PAGE + "files/systems.csv",
        "sha256": "912a204ac80e72a0302aa52f6647a775df3ebe2d894d73ceb796109fdca7bb26",
    },
    "verification_questions.txt": {
        "url": DATASET_PAGE + "files/verification_questions.txt",
        "sha256": "43e8ce494dc171717a2e13e1526a0bc915d92020941a5a835fc43d873210dc56",
    },
    "RQ2.zip": {
        "url": DATASET_PAGE + "files/RQ2/RQ2.zip",
        "sha256": "4dff1f0da9937a900255e9dc4f2e1cc57f615f6dc510c77c9d211840996804a2",
    },
}


@dataclass(frozen=True)
class RepoSpec:
    system_name: str
    alias: str
    url: str
    commit: str
    license_expression: str
    license_paths: tuple[str, ...]


REPOS = (
    RepoSpec("opencms-core", "opencms-core", "https://github.com/alkacon/opencms-core.git", "6dfbbb0c9fa27fd6abe53214cde737d66b78d360", "LGPL-2.1-or-later", ("license.txt",)),
    RepoSpec("pom", "jenkins", "https://github.com/jenkinsci/jenkins.git", "f8b26a3bab7cc361664ec3c52de7f955626d63ee", "MIT", ("LICENSE.txt",)),
    RepoSpec("spring-batch", "spring-batch", "https://github.com/spring-projects/spring-batch.git", "4012f8d25178dcef3cfe21d218b80cf52c1ba8d2", "Apache-2.0", ("src/assembly/license.txt", "src/assembly/notice.txt")),
    RepoSpec("hibernate-orm", "hibernate-orm", "https://github.com/hibernate/hibernate-orm.git", "b9ddc063cd720b33d13581ceffdc9b6a0850a5cb", "LGPL-2.1-or-later", ("lgpl.txt",)),
    RepoSpec("weka-dev", "weka", "https://github.com/bnjmn/weka.git", "ee380afff49fe840b31d6d659be117f54029d040", "GPL-3.0-or-later", ("wekadocs/COPYING",)),
    RepoSpec("antlr4-master", "antlr4", "https://github.com/antlr/antlr4.git", "628aa8ff02a1d2cff673ce28ca3da6114112eba8", "BSD-3-Clause", ("LICENSE.txt",)),
    RepoSpec("phoenix", "phoenix", "https://github.com/apache/phoenix.git", "41d6349bd50de1ef13a52a8b146c01b504e60aeb", "Apache-2.0", ("LICENSE", "NOTICE")),
    RepoSpec("MyExpenses", "MyExpenses", "https://github.com/mtotschnig/MyExpenses.git", "28738e8399921b3511176281261551931c5aede0", "GPL-3.0-or-later", ("LICENSE",)),
    RepoSpec("k-9", "k-9", "https://github.com/k9mail/k-9.git", "0387b253ba934504f975c46d6a23b575f6bc245b", "Apache-2.0", ("LICENSE", "NOTICE")),
    RepoSpec("car-report", "car-report", "https://bitbucket.org/frigus02/car-report.git", "3d95bde502088f87b2955e2b249446a16e0493e8", "Apache-2.0", ("COPYING",)),
)
REPO_BY_SYSTEM = {repo.system_name: repo for repo in REPOS}
STICKY_REPO = RepoSpec(
    "MyExpenses",
    "StickyListHeaders",
    "https://github.com/mtotschnig/StickyListHeaders.git",
    "7c5d2e834789d17862e6d75c36fef186096b5774",
    "Apache-2.0",
    ("LICENSE",),
)
STICKY_SIGNATURE = "se.emilsjolander.stickylistheaders.StickyListHeadersListView.updateOrClearHeader(int)"
SYNC_ADAPTER_PREFIX = "org.totschnig.myexpenses.sync.SyncAdapter."
JENKINS_ANT_SIGNATURE = "jenkins.util.AntClassLoader.loadClass(java.lang.String,boolean)"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def run_git(repo_dir: Path, *args: str, text: bool = False) -> bytes | str:
    command = ["git", "-C", str(repo_dir), *args]
    return subprocess.check_output(command, text=text, stderr=subprocess.PIPE)


def download_assets(cache_dir: Path) -> None:
    raw_dir = cache_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name, metadata in ASSETS.items():
        destination = raw_dir / name
        if destination.exists() and sha256_file(destination) == metadata["sha256"]:
            continue
        request = urllib.request.Request(metadata["url"], headers={"User-Agent": "tse-recovery/1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
        actual = sha256_bytes(payload)
        if actual != metadata["sha256"]:
            raise ValueError(f"hash mismatch for {name}: expected {metadata['sha256']}, got {actual}")
        atomic_write(destination, payload)


def fetch_repo(repo: RepoSpec, source_root: Path) -> Path:
    destination = source_root / repo.alias
    destination.mkdir(parents=True, exist_ok=True)
    if not (destination / ".git").exists():
        subprocess.run(["git", "init", "--quiet", str(destination)], check=True)
    try:
        existing_url = run_git(destination, "remote", "get-url", "origin", text=True).strip()
    except subprocess.CalledProcessError:
        run_git(destination, "remote", "add", "origin", repo.url)
    else:
        if existing_url != repo.url:
            raise ValueError(f"unexpected origin for {repo.alias}: {existing_url}")
    try:
        run_git(destination, "cat-file", "-e", repo.commit + "^{commit}")
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "-C", str(destination), "fetch", "--quiet", "--depth", "1", "origin", repo.commit],
            check=True,
        )
    resolved = run_git(destination, "rev-parse", repo.commit + "^{commit}", text=True).strip()
    if resolved != repo.commit:
        raise ValueError(f"revision mismatch for {repo.alias}: {resolved}")
    run_git(destination, "update-ref", "refs/tse/pinned", repo.commit)
    return destination


def fetch_sources(cache_dir: Path) -> None:
    source_root = cache_dir / "sources"
    source_root.mkdir(parents=True, exist_ok=True)
    for repo in (*REPOS, STICKY_REPO):
        fetch_repo(repo, source_root)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def validate_raw(cache_dir: Path) -> dict[str, object]:
    raw_dir = cache_dir / "raw"
    for name, metadata in ASSETS.items():
        path = raw_dir / name
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = sha256_file(path)
        if actual != metadata["sha256"]:
            raise ValueError(f"hash mismatch for {name}: {actual}")

    rows = read_csv(raw_dir / "understandability.csv")
    if len(rows) != 444:
        raise ValueError(f"expected 444 evaluation rows, found {len(rows)}")
    required = {"participant_id", "system_name", "snippet_signature", "developer_position", "LOC", "PBU", "AU"}
    missing_columns = required - set(rows[0])
    if missing_columns:
        raise ValueError(f"missing columns: {sorted(missing_columns)}")
    participants = {row["participant_id"] for row in rows}
    signatures = {row["snippet_signature"] for row in rows}
    systems = {row["system_name"] for row in rows}
    if len(participants) != 63 or len(signatures) != 50 or len(systems) != 10:
        raise ValueError(
            f"expanded TSE identity failed: participants={len(participants)}, "
            f"signatures={len(signatures)}, systems={len(systems)}"
        )
    if systems != set(REPO_BY_SYSTEM):
        raise ValueError(f"unexpected system names: {sorted(systems)}")
    signature_counts = Counter(row["snippet_signature"] for row in rows)
    if Counter(signature_counts.values()) != Counter({9: 44, 8: 6}):
        raise ValueError(f"unexpected evaluations per method: {Counter(signature_counts.values())}")
    signatures_per_system = defaultdict(set)
    for row in rows:
        signatures_per_system[row["system_name"]].add(row["snippet_signature"])
    if any(len(value) != 5 for value in signatures_per_system.values()):
        raise ValueError("expected exactly five signatures per system")
    if {row["PBU"] for row in rows} != {"0", "1"}:
        raise ValueError("PBU must be complete and binary")
    if {row["AU"] for row in rows} != {"0", "0.333333333333333", "0.666666666666667", "1"}:
        raise ValueError("unexpected AU domain")
    for outcome in ("PBU", "AU", "LOC"):
        if any(row[outcome] in {"", "NA"} for row in rows):
            raise ValueError(f"unexpected missing {outcome}")
    missing_by_column = {
        column: sum(row[column] in {"", "NA", "NaN", "null", "NULL"} for row in rows)
        for column in rows[0]
    }
    missing_by_column = {column: count for column, count in missing_by_column.items() if count}
    for signature in signatures:
        if len({row["LOC"] for row in rows if row["snippet_signature"] == signature}) != 1:
            raise ValueError(f"LOC varies within signature: {signature}")

    unique_participant_groups: dict[str, str] = {}
    for row in rows:
        previous = unique_participant_groups.setdefault(row["participant_id"], row["developer_position"])
        if previous != row["developer_position"]:
            raise ValueError(f"participant group varies for {row['participant_id']}")
    expected_groups = {
        "bachelor student": 38,
        "master student": 9,
        "phd student": 3,
        "professional developer": 13,
    }
    if Counter(unique_participant_groups.values()) != Counter(expected_groups):
        raise ValueError("unexpected participant group counts")

    systems_rows = read_csv(raw_dir / "systems.csv")
    observed_systems = {(row["GitHub repository"], row["Commit"]) for row in systems_rows}
    expected_systems = {(repo.url, repo.commit) for repo in REPOS}
    if observed_systems != expected_systems:
        raise ValueError("systems.csv does not match the ten predeclared pinned revisions")

    question_lines = (raw_dir / "verification_questions.txt").read_text(encoding="utf-8").splitlines()
    missing_questions = [signature for signature in signatures if question_lines.count(signature) != 1]
    if missing_questions:
        raise ValueError(f"verification-question signature mismatch: {missing_questions}")

    return {
        "evaluation_rows": 444,
        "participants": 63,
        "snippets": 50,
        "systems": 10,
        "evaluations_per_snippet": {"8": 6, "9": 44},
        "participants_by_group": expected_groups,
        "PBU_domain": [0, 1],
        "AU_domain": [0, 1 / 3, 2 / 3, 1],
        "primary_fields_missing": {"PBU": 0, "AU": 0, "LOC": 0},
        "all_nonzero_missing_counts": missing_by_column,
    }


def java_mask(source: str) -> str:
    """Mask comments and literals while preserving indexes and newlines."""
    output = list(source)
    state = "code"
    i = 0
    while i < len(source):
        char = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                output[i] = output[i + 1] = " "
                state = "line_comment"
                i += 2
                continue
            if char == "/" and nxt == "*":
                output[i] = output[i + 1] = " "
                state = "block_comment"
                i += 2
                continue
            if char == '"':
                output[i] = " "
                state = "string"
            elif char == "'":
                output[i] = " "
                state = "char"
        elif state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                output[i] = " "
        elif state == "block_comment":
            if char == "*" and nxt == "/":
                output[i] = output[i + 1] = " "
                state = "code"
                i += 2
                continue
            if char not in "\r\n":
                output[i] = " "
        else:
            if char == "\\":
                output[i] = " "
                if i + 1 < len(source):
                    if source[i + 1] not in "\r\n":
                        output[i + 1] = " "
                    i += 2
                    continue
            if (state == "string" and char == '"') or (state == "char" and char == "'"):
                output[i] = " "
                state = "code"
            elif char not in "\r\n":
                output[i] = " "
        i += 1
    return "".join(output)


def find_matching(mask: str, opening: int, open_char: str, close_char: str) -> int:
    if opening >= len(mask) or mask[opening] != open_char:
        raise ValueError(f"expected {open_char!r} at {opening}")
    depth = 0
    for index in range(opening, len(mask)):
        if mask[index] == open_char:
            depth += 1
        elif mask[index] == close_char:
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"unterminated {open_char}{close_char} at {opening}")


def split_top_level(text: str) -> list[str]:
    if not text.strip():
        return []
    result: list[str] = []
    start = 0
    levels = {"<": 0, "(": 0, "[": 0, "{": 0}
    pairs = {">": "<", ")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(text):
        if char in levels:
            levels[char] += 1
        elif char in pairs and levels[pairs[char]]:
            levels[pairs[char]] -= 1
        elif char == "," and not any(levels.values()):
            result.append(text[start:index])
            start = index + 1
    result.append(text[start:])
    return result


def target_parts(signature: str) -> tuple[str, str, list[str]]:
    declaration, parameters = signature.rsplit("(", 1)
    parameters = parameters[:-1]
    class_name, method_name = declaration.rsplit(".", 1)
    return class_name, method_name, split_top_level(parameters)


def normalized_type(type_text: str) -> str:
    text = re.sub(r"@\w+(?:\.\w+)*(?:\s*\([^)]*\))?\s*", "", type_text)
    text = re.sub(r"\bfinal\b\s*", "", text)
    text = text.replace("...", "[]")
    text = re.sub(r"\s+", "", text)
    # Fully-qualified and simple spellings compare by their terminal name.
    text = re.sub(r"(?:[A-Za-z_$][\w$]*\.)+([A-Za-z_$][\w$]*)", r"\1", text)
    return text


def declared_parameter_types(parameter_text: str) -> list[str]:
    result: list[str] = []
    for parameter in split_top_level(parameter_text):
        clean = re.sub(r"@\w+(?:\.\w+)*(?:\s*\([^)]*\))?\s*", "", parameter).strip()
        clean = re.sub(r"\bfinal\b\s*", "", clean).strip()
        match = re.match(r"(?s)(.+?)\s+([A-Za-z_$][\w$]*)(\s*(?:\[\s*\])*)\s*$", clean)
        if not match:
            raise ValueError(f"cannot parse parameter declaration: {parameter!r}")
        result.append(match.group(1) + match.group(3).replace(" ", ""))
    return result


@dataclass(frozen=True)
class MethodSlice:
    start: int
    end: int
    open_brace: int
    close_brace: int
    parameter_types: tuple[str, ...]


def extract_method(source: str, signature: str) -> MethodSlice:
    class_name, method_name, expected_types = target_parts(signature)
    mask = java_mask(source)
    depth_before = [0] * (len(mask) + 1)
    depth = 0
    for index, char in enumerate(mask):
        depth_before[index] = depth
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if depth < 0:
            raise ValueError("unbalanced source braces")
    depth_before[len(mask)] = depth

    candidates: list[MethodSlice] = []
    pattern = re.compile(r"\b" + re.escape(method_name) + r"\s*\(")
    for match in pattern.finditer(mask):
        open_paren = mask.find("(", match.start())
        close_paren = find_matching(mask, open_paren, "(", ")")
        index = close_paren + 1
        while index < len(mask) and mask[index].isspace():
            index += 1
        if mask.startswith("throws", index) and (index + 6 == len(mask) or not mask[index + 6].isalnum()):
            brace = mask.find("{", index + 6)
            semicolon = mask.find(";", index + 6, brace if brace >= 0 else None)
            if brace < 0 or semicolon >= 0:
                continue
            index = brace
        if index >= len(mask) or mask[index] != "{":
            continue
        if depth_before[match.start()] != depth_before[index]:
            continue
        raw_types = declared_parameter_types(source[open_paren + 1 : close_paren])
        if len(raw_types) != len(expected_types):
            continue
        if [normalized_type(value) for value in raw_types] != [normalized_type(value) for value in expected_types]:
            continue
        close_brace = find_matching(mask, index, "{", "}")
        target_depth = depth_before[match.start()]
        boundary = 0
        for previous in range(match.start() - 1, -1, -1):
            char = mask[previous]
            if char == ";" and depth_before[previous] == target_depth:
                boundary = previous + 1
                break
            if char == "}" and depth_before[previous] == target_depth + 1:
                boundary = previous + 1
                break
            if char == "{" and depth_before[previous] == target_depth - 1:
                boundary = previous + 1
                break
        # JDT-style method ranges used by the replication data include an
        # attached Javadoc, but not an earlier free-standing section comment.
        # First locate the declaration/annotations in masked source, then add
        # only the immediately preceding /** ... */ block when one exists.
        start_match = re.search(r"\S", mask[boundary : match.start()])
        if not start_match:
            raise ValueError(f"cannot locate declaration start for {signature}")
        code_start = boundary + start_match.start()
        start = code_start
        prefix_end = code_start
        while prefix_end > boundary and source[prefix_end - 1].isspace():
            prefix_end -= 1
        if source[max(boundary, prefix_end - 2) : prefix_end] == "*/":
            comment_start = source.rfind("/*", boundary, prefix_end - 1)
            if comment_start >= boundary and source.startswith("/**", comment_start):
                start = comment_start
        candidates.append(MethodSlice(start, close_brace + 1, index, close_brace, tuple(raw_types)))
    if len(candidates) != 1:
        raise ValueError(f"expected one exact declaration for {signature}, found {len(candidates)}")
    return candidates[0]


def package_name(source: str) -> str:
    match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", java_mask(source))
    if not match:
        raise ValueError("Java source lacks package declaration")
    return match.group(1)


def exact_context(source: str, signature: str, method: MethodSlice) -> dict[str, object]:
    mask = java_mask(source)
    package_match = re.search(r"(?m)^\s*package\s+[\w.]+\s*;", mask)
    if not package_match:
        raise ValueError("missing package declaration")
    all_imports = [source[match.start() : match.end()].strip() for match in re.finditer(r"(?m)^\s*import\s+(?:static\s+)?[\w.*]+\s*;", mask)]
    fq_class, _, _ = target_parts(signature)
    simple_class = fq_class.rsplit(".", 1)[-1]
    type_pattern = re.compile(r"\b(?:class|interface|enum)\s+" + re.escape(simple_class) + r"\b")
    type_candidates = []
    for match in type_pattern.finditer(mask):
        opening = mask.find("{", match.end())
        if opening < 0:
            continue
        closing = find_matching(mask, opening, "{", "}")
        if opening < method.start < closing:
            type_candidates.append((match, opening, closing))
    if len(type_candidates) != 1:
        raise ValueError(f"expected one enclosing type for {signature}, found {len(type_candidates)}")
    match, opening, _ = type_candidates[0]
    type_depth = 0
    for char in mask[: match.start()]:
        if char == "{":
            type_depth += 1
        elif char == "}":
            type_depth -= 1
    boundary = package_match.end()
    for import_match in re.finditer(r"(?m)^\s*import\s+(?:static\s+)?[\w.*]+\s*;", mask[: match.start()]):
        boundary = import_match.end()
    start_match = re.search(r"\S", mask[boundary : match.start()])
    header_start = boundary + start_match.start() if start_match else match.start()
    enclosing_type_header = source[header_start : opening + 1].strip()
    return {
        "schema_version": "tse-java-context-v1",
        "policy_status": "proposal_for_protocol_freeze",
        "policy": (
            "Exact package declaration; every exact source-file import in source order; "
            "the exact enclosing type header through its opening brace; and the separately "
            "stored exact selected declaration/body. Member stubs are uniformly excluded: "
            "no fields, initializers, sibling methods, invoked source, comments outside the "
            "declaration, outcomes, or verification questions."
        ),
        "package_declaration": source[package_match.start() : package_match.end()].strip(),
        "imports": all_imports,
        "import_selection": "all_source_file_imports_in_source_order",
        "member_stub_selection": "uniformly_excluded_no_symbol_resolution_claim",
        "source_file_import_count": len(all_imports),
        "retained_import_count": len(all_imports),
        "enclosing_type_header": enclosing_type_header,
        "enclosing_type_depth": type_depth,
    }


def git_tree_paths(repo_dir: Path, commit: str) -> list[str]:
    return run_git(repo_dir, "ls-tree", "-r", "--name-only", commit, text=True).splitlines()


def git_blob(repo_dir: Path, commit: str, path: str) -> bytes:
    return run_git(repo_dir, "show", f"{commit}:{path}")


def discover_source(repo: RepoSpec, source_root: Path, signature: str) -> tuple[str, bytes, MethodSlice]:
    fq_class, _, _ = target_parts(signature)
    simple_class = fq_class.rsplit(".", 1)[-1]
    expected_package = fq_class.rsplit(".", 1)[0]
    repo_dir = source_root / repo.alias
    candidates = [path for path in git_tree_paths(repo_dir, repo.commit) if path.endswith("/" + simple_class + ".java") or path == simple_class + ".java"]
    matches: list[tuple[str, bytes, MethodSlice]] = []
    for path in candidates:
        blob = git_blob(repo_dir, repo.commit, path)
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            if package_name(text) != expected_package:
                continue
            method = extract_method(text, signature)
        except ValueError:
            continue
        matches.append((path, blob, method))
    if len(matches) != 1:
        raise ValueError(f"expected one source file for {signature}, found {[item[0] for item in matches]}")
    return matches[0]


def line_column(source: str, offset: int) -> tuple[int, int]:
    return source.count("\n", 0, offset) + 1, offset - source.rfind("\n", 0, offset)


def source_web_url(repo: RepoSpec, path: str, start_line: int, end_line: int) -> str:
    if repo.url.startswith("https://github.com/"):
        base = repo.url.removesuffix(".git")
        return f"{base}/blob/{repo.commit}/{path}#L{start_line}-L{end_line}"
    base = repo.url.removesuffix(".git")
    return f"{base}/src/{repo.commit}/{path}#lines-{start_line}:{end_line}"


def safe_filename(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path)


def apache_file_header(source: str) -> str:
    first_import = re.search(r"(?m)^\s*import\s", source)
    prefix = source[: first_import.start() if first_import else min(len(source), 5000)]
    for match in re.finditer(r"(?s)/\*.*?\*/", prefix):
        block = match.group(0)
        if "Licensed under the Apache License" in block or "licenses this file to You under the Apache License" in block:
            return block.rstrip() + "\n"
    raise ValueError("expected an Apache-2.0 source-file header")


def materialize(cache_dir: Path, output_dir: Path) -> None:
    summary = validate_raw(cache_dir)
    raw_dir = cache_dir / "raw"
    source_root = cache_dir / "sources"
    rows = read_csv(raw_dir / "understandability.csv")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["snippet_signature"]].append(row)

    for repo in (*REPOS, STICKY_REPO):
        repo_dir = source_root / repo.alias
        resolved = run_git(repo_dir, "rev-parse", repo.commit + "^{commit}", text=True).strip()
        if resolved != repo.commit:
            raise ValueError(f"missing pinned source: {repo.alias}@{repo.commit}")

    snippets_dir = output_dir / "snippets"
    contexts_dir = output_dir / "contexts"
    licenses_dir = output_dir / "licenses"
    snippets_dir.mkdir(parents=True, exist_ok=True)
    contexts_dir.mkdir(parents=True, exist_ok=True)
    licenses_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []

    copied_licenses: dict[tuple[str, str], dict[str, str]] = {}
    for repo in (*REPOS, STICKY_REPO):
        repo_dir = source_root / repo.alias
        for license_path in repo.license_paths:
            payload = git_blob(repo_dir, repo.commit, license_path)
            relative = Path("licenses") / repo.alias / safe_filename(license_path)
            atomic_write(output_dir / relative, payload)
            copied_licenses[(repo.alias, license_path)] = {
                "path": relative.as_posix(),
                "sha256": sha256_bytes(payload),
            }

    for number, signature in enumerate(sorted(grouped), 1):
        snippet_id = f"tse-{number:03d}"
        system_name = grouped[signature][0]["system_name"]
        parent_repo = REPO_BY_SYSTEM[system_name]
        source_repo = STICKY_REPO if signature == STICKY_SIGNATURE else parent_repo
        path, full_blob, method = discover_source(source_repo, source_root, signature)
        source_text = full_blob.decode("utf-8")
        method_text = source_text[method.start : method.end]
        method_payload = method_text.encode("utf-8")
        body_text = source_text[method.open_brace + 1 : method.close_brace]
        body_payload = body_text.encode("utf-8")
        snippet_body_start = len(source_text[method.start : method.open_brace + 1].encode("utf-8"))
        snippet_body_end = len(source_text[method.start : method.close_brace].encode("utf-8"))
        snippet_relpath = Path("snippets") / f"{snippet_id}.java"
        atomic_write(output_dir / snippet_relpath, method_payload)

        context = exact_context(source_text, signature, method)
        context.update({
            "snippet_id": snippet_id,
            "dataset_signature": signature,
            "method_source": snippet_relpath.as_posix(),
            "method_source_sha256": sha256_bytes(method_payload),
        })
        context_payload = (json.dumps(context, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        context_relpath = Path("contexts") / f"{snippet_id}.context.json"
        atomic_write(output_dir / context_relpath, context_payload)

        start_line, start_column = line_column(source_text, method.start)
        end_line, end_column = line_column(source_text, method.end - 1)
        blob_oid = run_git(source_root / source_repo.alias, "rev-parse", f"{source_repo.commit}:{path}", text=True).strip()
        license_expression = source_repo.license_expression
        license_basis = "repository"
        file_license_override = signature.startswith(SYNC_ADAPTER_PREFIX) or signature == JENKINS_ANT_SIGNATURE
        if file_license_override:
            license_expression = "Apache-2.0"
            license_basis = "file_header_override"
        license_refs = [copied_licenses[(source_repo.alias, item)] for item in source_repo.license_paths]
        license_evidence_files = []
        if signature.startswith(SYNC_ADAPTER_PREFIX):
            # These two source files carry an Apache-2.0 header even though the
            # containing application is GPL-3.0-or-later.  Retain a verbatim
            # Apache-2.0 license text from the pinned bundled dependency too.
            license_refs.append(copied_licenses[(STICKY_REPO.alias, "LICENSE")])
        elif signature == JENKINS_ANT_SIGNATURE:
            # AntClassLoader is an Apache-licensed file inside the MIT Jenkins
            # repository. Retain an Apache-2.0 text already present in another
            # pinned study project, in addition to Jenkins' repository license.
            license_refs.append(copied_licenses[(REPO_BY_SYSTEM["spring-batch"].alias, "src/assembly/license.txt")])
        if file_license_override:
            header_payload = apache_file_header(source_text).encode("utf-8")
            header_relative = Path("licenses") / "file-headers" / f"{snippet_id}.txt"
            atomic_write(output_dir / header_relative, header_payload)
            license_evidence_files.append({
                "path": header_relative.as_posix(),
                "sha256": sha256_bytes(header_payload),
                "role": "verbatim_source_file_license_header",
            })
        parent_gitlink = None
        if signature == STICKY_SIGNATURE:
            parent_gitlink = run_git(source_root / parent_repo.alias, "ls-tree", parent_repo.commit, "StickyListHeaders", text=True).strip()
            expected_gitlink = f"160000 commit {source_repo.commit}\tStickyListHeaders"
            if parent_gitlink != expected_gitlink:
                raise ValueError(f"MyExpenses submodule gitlink mismatch: {parent_gitlink}")

        dataset_loc = {row["LOC"] for row in grouped[signature]}
        dataset_loc_value = int(dataset_loc.pop())
        physical_line_count = method_text.count("\n") + 1
        if physical_line_count != dataset_loc_value:
            raise ValueError(
                f"source range/replication LOC mismatch for {signature}: "
                f"source={physical_line_count}, dataset={dataset_loc_value}"
            )
        manifest.append({
            "snippet_id": snippet_id,
            "dataset_signature": signature,
            "system_name": system_name,
            "evaluation_rows": len(grouped[signature]),
            "dataset_LOC": dataset_loc_value,
            "source_range_physical_lines": physical_line_count,
            "dataset_LOC_range_validation": "exact",
            "parent_repository_url": parent_repo.url,
            "parent_revision": parent_repo.commit,
            "parent_submodule_gitlink": parent_gitlink,
            "source_repository_url": source_repo.url,
            "source_revision": source_repo.commit,
            "source_path": path,
            "source_blob_oid_sha1": blob_oid,
            "source_file_sha256": sha256_bytes(full_blob),
            "source_start_byte_utf8": len(source_text[: method.start].encode("utf-8")),
            "source_end_byte_utf8_exclusive": len(source_text[: method.end].encode("utf-8")),
            "source_body_start_byte_utf8": len(source_text[: method.open_brace + 1].encode("utf-8")),
            "source_body_end_byte_utf8_exclusive": len(source_text[: method.close_brace].encode("utf-8")),
            "source_start_line": start_line,
            "source_start_column": start_column,
            "source_end_line": end_line,
            "source_end_column": end_column,
            "source_url": source_web_url(source_repo, path, start_line, end_line),
            "declared_parameter_types": list(method.parameter_types),
            "signature_validation": "fq_package_class_method_and_normalized_parameter_types_exact",
            "snippet_path": snippet_relpath.as_posix(),
            "snippet_sha256": sha256_bytes(method_payload),
            "snippet_body_start_byte_utf8": snippet_body_start,
            "snippet_body_end_byte_utf8_exclusive": snippet_body_end,
            "body_sha256": sha256_bytes(body_payload),
            "context_path": context_relpath.as_posix(),
            "context_sha256": sha256_bytes(context_payload),
            "license_expression": license_expression,
            "license_basis": license_basis,
            "license_files": license_refs,
            "license_evidence_files": license_evidence_files,
            "resolution_status": "validated",
        })

    if len(manifest) != 50:
        raise ValueError(f"expected 50 materialized snippets, found {len(manifest)}")
    manifest_payload = b"".join(
        (json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8") for item in manifest
    )
    atomic_write(output_dir / "source_manifest.jsonl", manifest_payload)

    index_rows = []
    for item in manifest:
        index_rows.append({
            "snippet_id": item["snippet_id"],
            "system_name": item["system_name"],
            "dataset_signature": item["dataset_signature"],
            "evaluation_rows": item["evaluation_rows"],
            "dataset_LOC": item["dataset_LOC"],
        })
    index_path = output_dir / "snippet_index.csv"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=index_path.parent, prefix="snippet_index.") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(index_rows[0]))
        writer.writeheader()
        writer.writerows(index_rows)
        temporary_index = stream.name
    os.replace(temporary_index, index_path)

    provenance = {
        "schema_version": "tse-provenance-v1",
        "dataset": "Scalabrino et al., Automatically Assessing Code Understandability, expanded TSE version",
        "dataset_page": DATASET_PAGE,
        "paper": PAPER_URL,
        "raw_assets": ASSETS,
        "validated_structure": summary,
        "raw_license_status": "unresolved_no_license_statement_found_on_authoritative_download_page",
        "raw_retention_policy": "hash_pinned_cache_only_not_copied_into_artifact",
        "source_snippet_retention": "retained_under_each_upstream_open_source_license_with_notices",
        "manifest_sha256": sha256_bytes(manifest_payload),
        "snippet_index_sha256": sha256_file(index_path),
    }
    atomic_write(
        output_dir / "provenance.json",
        (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    validate_artifact(output_dir)


def validate_artifact(output_dir: Path) -> None:
    manifest_path = output_dir / "source_manifest.jsonl"
    manifest = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line]
    if len(manifest) != 50:
        raise ValueError(f"artifact contains {len(manifest)} snippets, expected 50")
    if len({item["snippet_id"] for item in manifest}) != 50:
        raise ValueError("duplicate snippet IDs")
    if len({item["dataset_signature"] for item in manifest}) != 50:
        raise ValueError("duplicate signatures")
    if Counter(item["system_name"] for item in manifest) != Counter({name: 5 for name in REPO_BY_SYSTEM}):
        raise ValueError("artifact does not contain five snippets per source project")
    for item in manifest:
        if item["resolution_status"] != "validated":
            raise ValueError(f"unresolved source: {item['snippet_id']}")
        if item["dataset_LOC_range_validation"] != "exact" or item["dataset_LOC"] != item["source_range_physical_lines"]:
            raise ValueError(f"dataset/source LOC mismatch: {item['snippet_id']}")
        snippet_path = output_dir / item["snippet_path"]
        context_path = output_dir / item["context_path"]
        if sha256_file(snippet_path) != item["snippet_sha256"]:
            raise ValueError(f"snippet hash mismatch: {item['snippet_id']}")
        snippet_payload = snippet_path.read_bytes()
        body_payload = snippet_payload[item["snippet_body_start_byte_utf8"] : item["snippet_body_end_byte_utf8_exclusive"]]
        if sha256_bytes(body_payload) != item["body_sha256"]:
            raise ValueError(f"method body hash mismatch: {item['snippet_id']}")
        if sha256_file(context_path) != item["context_sha256"]:
            raise ValueError(f"context hash mismatch: {item['snippet_id']}")
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if context["method_source_sha256"] != item["snippet_sha256"]:
            raise ValueError(f"context/source mismatch: {item['snippet_id']}")
        for license_file in item["license_files"]:
            if sha256_file(output_dir / license_file["path"]) != license_file["sha256"]:
                raise ValueError(f"license hash mismatch: {license_file['path']}")
        for evidence_file in item["license_evidence_files"]:
            if sha256_file(output_dir / evidence_file["path"]) != evidence_file["sha256"]:
                raise ValueError(f"license evidence hash mismatch: {evidence_file['path']}")
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    manifest_hash = sha256_file(manifest_path)
    if provenance["manifest_sha256"] != manifest_hash:
        raise ValueError("provenance manifest hash mismatch")
    if provenance["raw_license_status"] != "unresolved_no_license_statement_found_on_authoritative_download_page":
        raise ValueError("raw licensing caveat is missing")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("download", "fetch-sources", "validate-raw"):
        child = subparsers.add_parser(command)
        child.add_argument("--cache-dir", type=Path, required=True)
    materialize_parser = subparsers.add_parser("materialize")
    materialize_parser.add_argument("--cache-dir", type=Path, required=True)
    materialize_parser.add_argument("--output-dir", type=Path, required=True)
    validate_parser = subparsers.add_parser("validate-artifact")
    validate_parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download":
        download_assets(args.cache_dir)
    elif args.command == "fetch-sources":
        fetch_sources(args.cache_dir)
    elif args.command == "validate-raw":
        print(json.dumps(validate_raw(args.cache_dir), indent=2, sort_keys=True))
    elif args.command == "materialize":
        materialize(args.cache_dir, args.output_dir)
    elif args.command == "validate-artifact":
        validate_artifact(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
