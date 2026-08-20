"""Outcome-blind protocol freeze and reproducibility primitives.

This module deliberately has no dependency on the study's outcome loader.  It
operates on paths, hashes, pin declarations, and artifact structure only.  A
candidate freeze is valid only when every declared input is pinned, every
candidate file is immutable and regular, the candidate bundle contains no
draft marker, and the artifact-safety scans pass.

The functions are library entry points for the command-line tools in ``tools``
and are intentionally written using only the Python standard library.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import sys
import sysconfig
import tomllib
import base64
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence


MANIFEST_SCHEMA = "backtranslation.freeze-manifest.v1"
RUNTIME_LOCK_SCHEMA = "backtranslation.runtime-lock.v1"
TREE_SNAPSHOT_SCHEMA = "backtranslation.tree-snapshot.v1"
RERUN_MARKER_SCHEMA = "backtranslation.clean-rerun.v1"
STANDARD_BLOCKER_MARKERS = (
    "TO" + "DO-",
    "DRA" + "FT",
    "UN" + "FROZEN",
    "NOT " + "FROZEN",
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_UNPINNED_WORD = re.compile(
    r"(?:^|[^a-z0-9])(?:draft|head|latest|main|master|todo|tbd|unknown|"
    r"unresolved|unpinned)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".csv",
    ".ini",
    ".java",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".rst",
    ".schema",
    ".sh",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
_SUSPICIOUS_FILENAME = re.compile(
    r"(?:^|[._-])(?:api[._-]?key|credential|credentials|private[._-]?key|"
    r"key|secret|token)(?:[._-]|$)",
    re.IGNORECASE,
)
_PROHIBITED_ARTIFACT_FILENAME = re.compile(
    r"(?:^|[._-])(?:au|pbu|human[._-]?(?:response|outcome)s?|outcomes?|"
    r"verification[._-]?answers?|answer[._-]?key|reasoning[._-]?content|"
    r"chain[._-]?of[._-]?thought)(?:[._-]|$)",
    re.IGNORECASE,
)
_SECRET_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "authorization_bearer",
        re.compile(rb"(?i)\bauthorization\s*:\s*bearer\s+[^\s\"']{8,}"),
    ),
    (
        "private_key_block",
        re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
    (
        "provider_secret_literal",
        re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"),
    ),
    (
        "assigned_secret",
        re.compile(
            rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret)"
            rb"\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{16,}"
        ),
    ),
)

_FORBIDDEN_ARTIFACT_KEYS = {
    "au": "human_outcome",
    "actualunderstandability": "human_outcome",
    "pbu": "human_outcome",
    "perceivedunderstandability": "human_outcome",
    "tnpu": "prohibited_human_measure",
    "tau": "prohibited_human_measure",
    "abu50": "prohibited_human_measure",
    "bd50": "prohibited_human_measure",
    "verificationanswers": "verification_answer",
    "answerkey": "verification_answer",
    "authorization": "credential_material",
    "authorizationheader": "credential_material",
    "apikey": "credential_material",
    "accesskey": "credential_material",
    "accesstoken": "credential_material",
    "clientsecret": "credential_material",
    "reasoningcontent": "hidden_reasoning",
    "chainofthought": "hidden_reasoning",
    "hiddenthinking": "hidden_reasoning",
}


@dataclass(frozen=True, order=True)
class Finding:
    """A sanitized audit finding; never includes offending file contents."""

    code: str
    path: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code}
        if self.path:
            result["path"] = self.path
        if self.detail:
            result["detail"] = self.detail
        return result


class FreezeError(RuntimeError):
    """Raised when a freeze/reproducibility invariant is not satisfied."""

    def __init__(self, code: str, findings: Sequence[Finding] = ()) -> None:
        self.code = code
        self.findings = tuple(sorted(findings))
        super().__init__(code)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the exact canonical byte sequence used for freeze hashes."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FreezeError("not_canonical_json") from exc


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise FreezeError("file_hash_failed", (Finding("file_hash_failed", str(path)),)) from exc
    return digest.hexdigest()


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FreezeError(
            "unsafe_relative_path", (Finding("unsafe_relative_path", value),)
        )
    return path


def _regular_file_beneath(root: Path, relative: PurePosixPath) -> Path:
    """Resolve a relative file without permitting symlink traversal."""

    root = root.resolve(strict=True)
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise FreezeError(
                "required_path_missing",
                (Finding("required_path_missing", relative.as_posix()),),
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise FreezeError(
                "symlink_prohibited", (Finding("symlink_prohibited", relative.as_posix()),)
            )
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise FreezeError(
                "path_component_not_directory",
                (Finding("path_component_not_directory", relative.as_posix()),),
            )
    if not stat.S_ISREG(current.lstat().st_mode):
        raise FreezeError(
            "regular_file_required", (Finding("regular_file_required", relative.as_posix()),)
        )
    return current


def file_record(root: Path, relative_path: str) -> dict[str, Any]:
    relative = _relative_path(relative_path)
    path = _regular_file_beneath(root, relative)
    metadata_before = path.stat()
    digest = sha256_file(path)
    metadata_after = path.stat()
    before = (
        metadata_before.st_dev,
        metadata_before.st_ino,
        metadata_before.st_size,
        metadata_before.st_mtime_ns,
    )
    after = (
        metadata_after.st_dev,
        metadata_after.st_ino,
        metadata_after.st_size,
        metadata_after.st_mtime_ns,
    )
    if before != after:
        raise FreezeError(
            "file_changed_while_hashing",
            (Finding("file_changed_while_hashing", relative.as_posix()),),
        )
    return {
        "path": relative.as_posix(),
        "bytes": metadata_after.st_size,
        "sha256": digest,
    }


def build_manifest(root: Path, relative_paths: Iterable[str]) -> dict[str, Any]:
    """Build a timestamp-free, deterministic manifest for explicit files."""

    normalized = sorted({_relative_path(value).as_posix() for value in relative_paths})
    if not normalized:
        raise FreezeError("manifest_has_no_files")
    records = [file_record(root, value) for value in normalized]
    return {"schema_version": MANIFEST_SCHEMA, "files": records}


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(manifest))


def verify_manifest(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    expected_sha256: str | None = None,
) -> str:
    findings: list[Finding] = []
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        findings.append(Finding("manifest_schema_invalid"))
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        findings.append(Finding("manifest_files_invalid"))
        raise FreezeError("manifest_verification_failed", findings)

    observed_paths: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            findings.append(Finding("manifest_record_invalid"))
            continue
        relative = record.get("path")
        expected_bytes = record.get("bytes")
        expected_file_hash = record.get("sha256")
        if not isinstance(relative, str):
            findings.append(Finding("manifest_record_path_invalid"))
            continue
        observed_paths.append(relative)
        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            findings.append(Finding("manifest_record_size_invalid", relative))
            continue
        if not isinstance(expected_file_hash, str) or not _SHA256.fullmatch(
            expected_file_hash
        ):
            findings.append(Finding("manifest_record_hash_invalid", relative))
            continue
        try:
            current = file_record(root, relative)
        except FreezeError as exc:
            findings.extend(exc.findings or (Finding(exc.code, relative),))
            continue
        if current["bytes"] != expected_bytes:
            findings.append(Finding("manifest_size_mismatch", relative))
        if current["sha256"] != expected_file_hash:
            findings.append(Finding("manifest_hash_mismatch", relative))

    if observed_paths != sorted(observed_paths):
        findings.append(Finding("manifest_paths_not_sorted"))
    if len(set(observed_paths)) != len(observed_paths):
        findings.append(Finding("manifest_path_duplicate"))

    digest = manifest_sha256(manifest)
    if expected_sha256 is not None:
        if not _SHA256.fullmatch(expected_sha256):
            findings.append(Finding("expected_manifest_hash_invalid"))
        elif digest != expected_sha256:
            findings.append(Finding("manifest_digest_mismatch"))
    if findings:
        raise FreezeError("manifest_verification_failed", findings)
    return digest


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreezeError("json_object_read_failed", (Finding("json_object_read_failed", str(path)),)) from exc
    if not isinstance(value, dict):
        raise FreezeError("json_object_required", (Finding("json_object_required", str(path)),))
    return value


def _placeholder_present(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or _UNPINNED_WORD.search(value) is not None
    if isinstance(value, Mapping):
        return any(_placeholder_present(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_placeholder_present(item) for item in value)
    return False


def validate_pin_inventory(root: Path, inventory: Mapping[str, Any]) -> tuple[Finding, ...]:
    """Validate that each required external input has an immutable identity."""

    findings: list[Finding] = []
    required = inventory.get("required_input_ids")
    inputs = inventory.get("inputs")
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        return (Finding("required_input_ids_invalid"),)
    if len(set(required)) != len(required):
        findings.append(Finding("required_input_id_duplicate"))
    if not isinstance(inputs, list):
        return tuple(sorted(findings + [Finding("pin_inputs_invalid")]))

    by_id: dict[str, Mapping[str, Any]] = {}
    for item in inputs:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            findings.append(Finding("pin_record_invalid"))
            continue
        identifier = str(item["id"])
        if identifier in by_id:
            findings.append(Finding("pin_id_duplicate", identifier))
        by_id[identifier] = item

    for identifier in sorted(set(required)):
        item = by_id.get(identifier)
        if item is None:
            findings.append(Finding("required_pin_missing", identifier))
            continue
        status_value = item.get("status")
        if status_value != "pinned":
            findings.append(
                Finding("input_unresolved", identifier, str(status_value or "missing_status"))
            )
            continue
        pin_type = item.get("pin_type")
        if pin_type == "file":
            _validate_file_pin(root, identifier, item, findings)
        elif pin_type == "git":
            source = item.get("source")
            revision = item.get("revision")
            if not isinstance(source, str) or _placeholder_present(source):
                findings.append(Finding("git_source_unpinned", identifier))
            if not isinstance(revision, str) or not _GIT_REVISION.fullmatch(revision):
                findings.append(Finding("git_revision_unpinned", identifier))
        elif pin_type == "package":
            requirement = item.get("requirement")
            if (
                not isinstance(requirement, str)
                or "==" not in requirement
                or _placeholder_present(requirement)
            ):
                findings.append(Finding("package_requirement_unpinned", identifier))
            source = item.get("source")
            if not isinstance(source, str) or _placeholder_present(source):
                findings.append(Finding("package_source_unpinned", identifier))
            _validate_manifest_backed_pin(root, identifier, item, findings)
        elif pin_type == "service":
            endpoint = item.get("endpoint")
            model = item.get("model")
            if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
                findings.append(Finding("service_endpoint_unpinned", identifier))
            if not isinstance(model, str) or _placeholder_present(model):
                findings.append(Finding("service_model_unpinned", identifier))
            _validate_manifest_backed_pin(root, identifier, item, findings)
            _validate_service_pin_semantics(root, identifier, item, findings)
        elif pin_type == "manifest":
            _validate_manifest_backed_pin(root, identifier, item, findings)
        else:
            findings.append(Finding("pin_type_invalid", identifier, str(pin_type)))

    extras = sorted(set(by_id) - set(required))
    for identifier in extras:
        findings.append(Finding("undeclared_pin_record", identifier))
    return tuple(sorted(findings))


def _validate_file_pin(
    root: Path,
    identifier: str,
    item: Mapping[str, Any],
    findings: list[Finding],
) -> None:
    source = item.get("source")
    expected_hash = item.get("sha256")
    expected_bytes = item.get("bytes")
    if not isinstance(source, str) or _placeholder_present(source):
        findings.append(Finding("file_source_unpinned", identifier))
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        findings.append(Finding("file_hash_unpinned", identifier))
    if not isinstance(expected_bytes, int) or expected_bytes < 0:
        findings.append(Finding("file_size_unpinned", identifier))
    local_path = item.get("local_path")
    if local_path is None:
        return
    if not isinstance(local_path, str):
        findings.append(Finding("file_local_path_invalid", identifier))
        return
    try:
        current = file_record(root, local_path)
    except FreezeError as exc:
        findings.extend(exc.findings or (Finding(exc.code, local_path),))
        return
    if isinstance(expected_hash, str) and current["sha256"] != expected_hash:
        findings.append(Finding("file_pin_hash_mismatch", identifier))
    if isinstance(expected_bytes, int) and current["bytes"] != expected_bytes:
        findings.append(Finding("file_pin_size_mismatch", identifier))


def _validate_manifest_backed_pin(
    root: Path,
    identifier: str,
    item: Mapping[str, Any],
    findings: list[Finding],
) -> None:
    relative = item.get("manifest_path")
    expected = item.get("manifest_sha256")
    if not isinstance(relative, str):
        findings.append(Finding("pin_manifest_path_invalid", identifier))
        return
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        findings.append(Finding("pin_manifest_hash_unpinned", identifier))
        return
    try:
        record = file_record(root, relative)
    except FreezeError as exc:
        findings.extend(exc.findings or (Finding(exc.code, relative),))
        return
    if record["sha256"] != expected:
        findings.append(Finding("pin_manifest_hash_mismatch", identifier))


def _validate_service_pin_semantics(
    root: Path,
    identifier: str,
    item: Mapping[str, Any],
    findings: list[Finding],
) -> None:
    """Bind declared service settings to the sanitized passed canary record."""

    relative = item.get("manifest_path")
    if not isinstance(relative, str):
        return
    try:
        path = _regular_file_beneath(root, _relative_path(relative))
        record = _read_json_object(path)
    except FreezeError as exc:
        findings.extend(exc.findings or (Finding(exc.code, relative),))
        return
    event = record.get("provider_event")
    request = event.get("request") if isinstance(event, Mapping) else None
    response = event.get("response") if isinstance(event, Mapping) else None
    if (
        record.get("status") != "passed"
        or not isinstance(request, Mapping)
        or not isinstance(response, Mapping)
    ):
        findings.append(Finding("service_canary_not_passed", identifier))
        return
    endpoint = item.get("endpoint")
    if isinstance(endpoint, str):
        expected_host_path = endpoint.removeprefix("https://").split("/", 1)
        expected_host = expected_host_path[0]
        expected_path = "/" + expected_host_path[1] if len(expected_host_path) == 2 else "/"
        if request.get("host") != expected_host or request.get("endpoint") != expected_path:
            findings.append(Finding("service_canary_endpoint_mismatch", identifier))
    if (
        request.get("model") != item.get("model")
        or response.get("returned_model") != item.get("model")
        or response.get("finish_reason") != "stop"
    ):
        findings.append(Finding("service_canary_model_mismatch", identifier))
    expected_max_tokens = item.get("max_tokens")
    if expected_max_tokens is not None:
        if (
            not isinstance(expected_max_tokens, int)
            or isinstance(expected_max_tokens, bool)
            or request.get("max_tokens") != expected_max_tokens
        ):
            findings.append(Finding("service_canary_max_tokens_mismatch", identifier))
    if (
        request.get("thinking") != "enabled"
        or request.get("reasoning_effort") != "high"
        or request.get("response_format") != "json_object"
        or request.get("stream") is not False
        or response.get("reasoning_content_retained") is not False
    ):
        findings.append(Finding("service_canary_settings_mismatch", identifier))


def _iter_tree_files(root: Path, relative_directory: str) -> Iterator[str]:
    relative = _relative_path(relative_directory)
    directory = root.resolve(strict=True) / Path(*relative.parts)
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise FreezeError(
            "required_tree_missing", (Finding("required_tree_missing", relative.as_posix()),)
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FreezeError(
            "regular_directory_required",
            (Finding("regular_directory_required", relative.as_posix()),),
        )
    for current, directory_names, file_names in os.walk(directory, followlinks=False):
        current_path = Path(current)
        for name in tuple(directory_names):
            candidate = current_path / name
            if candidate.is_symlink():
                rel = candidate.relative_to(root.resolve()).as_posix()
                raise FreezeError("symlink_prohibited", (Finding("symlink_prohibited", rel),))
        for name in sorted(file_names):
            candidate = current_path / name
            rel = candidate.relative_to(root.resolve()).as_posix()
            if candidate.is_symlink():
                raise FreezeError("symlink_prohibited", (Finding("symlink_prohibited", rel),))
            if not candidate.is_file():
                raise FreezeError("regular_file_required", (Finding("regular_file_required", rel),))
            yield rel


def expand_freeze_paths(root: Path, spec: Mapping[str, Any]) -> tuple[str, ...]:
    include_files = spec.get("include_files")
    include_trees = spec.get("include_trees")
    if not isinstance(include_files, list) or not all(
        isinstance(item, str) for item in include_files
    ):
        raise FreezeError("include_files_invalid")
    if not isinstance(include_trees, list) or not all(
        isinstance(item, str) for item in include_trees
    ):
        raise FreezeError("include_trees_invalid")
    paths = {_relative_path(item).as_posix() for item in include_files}
    for directory in include_trees:
        paths.update(_iter_tree_files(root, directory))
    excluded = spec.get("exclude_patterns", [])
    if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
        raise FreezeError("exclude_patterns_invalid")
    return tuple(sorted(path for path in paths if not _matches_any(path, excluded)))


def scan_markers(
    root: Path,
    relative_paths: Iterable[str],
    markers: Sequence[str],
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for relative in sorted(set(relative_paths)):
        path = _regular_file_beneath(root, _relative_path(relative))
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(Finding("candidate_text_not_utf8", relative))
            continue
        for marker in markers:
            if not isinstance(marker, str) or not marker:
                findings.append(Finding("marker_rule_invalid"))
                continue
            position = text.find(marker)
            if position >= 0:
                line = text.count("\n", 0, position) + 1
                findings.append(Finding("unresolved_marker", relative, f"line={line};marker={marker}"))
    return tuple(sorted(findings))


def preflight_freeze(root: Path, spec: Mapping[str, Any]) -> tuple[str, ...]:
    """Run all freeze blockers and return the candidate path set on success."""

    findings: list[Finding] = []
    if spec.get("schema_version") != "backtranslation.freeze-spec.v1":
        findings.append(Finding("freeze_spec_schema_invalid"))

    inventory = spec.get("pin_inventory")
    if not isinstance(inventory, Mapping):
        findings.append(Finding("pin_inventory_invalid"))
    else:
        findings.extend(validate_pin_inventory(root, inventory))

    # Refuse before opening any candidate data file when an external input is
    # unresolved. This keeps scientific-input blockers unmistakable and avoids
    # scanning a partially authorized candidate bundle.
    if findings:
        raise FreezeError("freeze_preflight_failed", findings)

    try:
        paths = expand_freeze_paths(root, spec)
    except FreezeError as exc:
        raise FreezeError("freeze_preflight_failed", exc.findings or (Finding(exc.code),)) from exc

    protocol_path = spec.get("protocol_path")
    if not isinstance(protocol_path, str) or not protocol_path.endswith(".frozen.md"):
        findings.append(Finding("frozen_protocol_path_invalid", str(protocol_path or "")))
    elif protocol_path not in paths:
        findings.append(Finding("frozen_protocol_not_manifested", protocol_path))

    marker_policy = spec.get("marker_policy")
    if marker_policy == "standard-v1":
        findings.extend(scan_markers(root, paths, STANDARD_BLOCKER_MARKERS))
    else:
        markers = spec.get("forbidden_markers")
        if not isinstance(markers, list) or not all(
            isinstance(item, str) for item in markers
        ):
            findings.append(Finding("forbidden_markers_invalid"))
        else:
            findings.extend(scan_markers(root, paths, markers))
    findings.extend(scan_secret_material(root, paths))
    if findings:
        raise FreezeError("freeze_preflight_failed", findings)
    return paths


def create_freeze_manifest(root: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    paths = preflight_freeze(root, spec)
    return build_manifest(root, paths)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _json_key_findings(value: Any, relative: str, location: str = "$") -> Iterator[Finding]:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            normalized = _normalized_key(key_text)
            category = _FORBIDDEN_ARTIFACT_KEYS.get(normalized)
            next_location = f"{location}.{key_text}"
            if category is not None:
                yield Finding("prohibited_artifact_key", relative, f"{category}:{next_location}")
            yield from _json_key_findings(nested, relative, next_location)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _json_key_findings(nested, relative, f"{location}[{index}]")


def _scan_csv_header(path: Path, relative: str) -> Iterator[Finding]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            sample = stream.read(8192)
    except (OSError, UnicodeDecodeError):
        yield Finding("artifact_text_read_failed", relative)
        return
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        rows = csv.reader(sample.splitlines(), dialect)
        header = next(rows, [])
    except csv.Error:
        yield Finding("artifact_delimited_header_invalid", relative)
        return
    for column in header:
        category = _FORBIDDEN_ARTIFACT_KEYS.get(_normalized_key(column))
        if category is not None:
            yield Finding("prohibited_artifact_column", relative, f"{category}:{column}")


def _safe_content_scan(path: Path, relative: str, *, maximum_bytes: int) -> tuple[Finding, ...]:
    try:
        size = path.stat().st_size
    except OSError:
        return (Finding("artifact_stat_failed", relative),)
    if size > maximum_bytes:
        return (Finding("artifact_scan_size_limit", relative, f"bytes={size}"),)
    try:
        payload = path.read_bytes()
    except OSError:
        return (Finding("artifact_read_failed", relative),)
    findings = [
        Finding("secret_material_detected", relative, code)
        for code, pattern in _SECRET_CONTENT_PATTERNS
        if pattern.search(payload)
    ]
    return tuple(findings)


def scan_secret_material(
    root: Path,
    relative_paths: Iterable[str],
    *,
    maximum_bytes: int = 64 * 1024 * 1024,
) -> tuple[Finding, ...]:
    """Scan candidate files for high-confidence secrets without echoing them."""

    findings: list[Finding] = []
    for relative in sorted(set(relative_paths)):
        path = _regular_file_beneath(root, _relative_path(relative))
        if _SUSPICIOUS_FILENAME.search(path.name) or path.suffix.lower() in {".key", ".pem"}:
            # Do not open likely credential files.  The path alone is a blocker.
            findings.append(Finding("credential_file_prohibited", relative))
            continue
        findings.extend(_safe_content_scan(path, relative, maximum_bytes=maximum_bytes))
    return tuple(sorted(findings))


def audit_artifact_tree(
    root: Path,
    *,
    maximum_bytes: int = 64 * 1024 * 1024,
    allowed_empty_operational_files: Sequence[str] = (),
) -> tuple[Finding, ...]:
    """Reject outcomes, answers, credentials, and hidden reasoning in artifacts.

    ``allowed_empty_operational_files`` is an exact, root-relative allowlist for
    control files which intentionally have no parseable artifact format (for
    example the executor's flock inode).  An allowlisted file is accepted only
    when it is a regular, non-symlink, zero-byte file at that exact relative
    path.  This deliberately does not create a suffix- or basename-wide
    exemption.
    """

    if root.is_symlink():
        return (Finding("artifact_root_symlink_prohibited", root.name),)
    root = root.resolve(strict=True)
    findings: list[Finding] = []
    allowed_operational: set[str] = set()
    for value in allowed_empty_operational_files:
        try:
            relative = _relative_path(value).as_posix()
        except FreezeError:
            findings.append(Finding("operational_file_allowlist_invalid", str(value)))
            continue
        allowed_operational.add(relative)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            findings.append(Finding("artifact_symlink_prohibited", relative))
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            findings.append(Finding("artifact_special_file_prohibited", relative))
            continue
        if relative in allowed_operational:
            if path.stat().st_size != 0:
                findings.append(Finding("operational_file_not_empty", relative))
            continue
        if _SUSPICIOUS_FILENAME.search(path.name) or path.suffix.lower() in {".key", ".pem"}:
            findings.append(Finding("credential_file_prohibited", relative))
            continue
        if _PROHIBITED_ARTIFACT_FILENAME.search(path.name):
            findings.append(Finding("prohibited_artifact_filename", relative))
            continue
        findings.extend(_safe_content_scan(path, relative, maximum_bytes=maximum_bytes))
        if path.stat().st_size > maximum_bytes:
            continue
        suffix = path.suffix.lower()
        if suffix not in _TEXT_SUFFIXES:
            findings.append(Finding("artifact_format_unscanned", relative, suffix or "no_suffix"))
            continue
        if suffix == ".json":
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                findings.append(Finding("artifact_json_invalid", relative))
            else:
                findings.extend(_json_key_findings(value, relative))
        elif suffix == ".jsonl":
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                findings.append(Finding("artifact_jsonl_read_failed", relative))
            else:
                for number, line in enumerate(lines, start=1):
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        findings.append(Finding("artifact_jsonl_invalid", relative, f"line={number}"))
                        continue
                    findings.extend(_json_key_findings(value, relative, f"$line[{number}]"))
        elif suffix in {".csv", ".tsv"}:
            findings.extend(_scan_csv_header(path, relative))
        elif suffix in _TEXT_SUFFIXES:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                findings.append(Finding("artifact_text_read_failed", relative))
                continue
            outcome_assignment = re.compile(
                r"(?im)^\s*(AU|PBU|TNPU|TAU|ABU50|BD50)\s*[:,=]"
            )
            if outcome_assignment.search(text):
                findings.append(Finding("prohibited_outcome_text", relative))
            # Exact provider task payloads are retained with a .txt suffix so
            # even a malformed test/transport payload remains auditable. In
            # production JSON mode they are objects; inspect their keys with
            # the same outcome/credential/reasoning policy as .json files.
            if path.name.endswith(".output.txt"):
                try:
                    output_value = json.loads(text)
                except json.JSONDecodeError:
                    pass
                else:
                    findings.extend(_json_key_findings(output_value, relative))
    return tuple(sorted(set(findings)))


def _matches_any(relative: str, patterns: Sequence[str]) -> bool:
    pure = PurePosixPath(relative)
    return any(pure.match(pattern) for pattern in patterns)


def snapshot_tree(root: Path, *, exclude: Sequence[str] = ()) -> dict[str, Any]:
    """Create an exact deterministic hash inventory of a regular-file tree."""

    if root.is_symlink():
        raise FreezeError("snapshot_root_symlink_prohibited")
    root = root.resolve(strict=True)
    excluded_patterns = sorted(set(exclude))
    if not all(isinstance(item, str) and item for item in excluded_patterns):
        raise FreezeError("snapshot_exclude_invalid")
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if _matches_any(relative, excluded_patterns):
            continue
        if path.is_symlink():
            raise FreezeError("snapshot_symlink_prohibited", (Finding("snapshot_symlink_prohibited", relative),))
        if path.is_dir():
            continue
        if not path.is_file():
            raise FreezeError("snapshot_special_file_prohibited", (Finding("snapshot_special_file_prohibited", relative),))
        metadata = path.stat()
        records.append({"path": relative, "bytes": metadata.st_size, "sha256": sha256_file(path)})
    return {
        "schema_version": TREE_SNAPSHOT_SCHEMA,
        "excluded_patterns": excluded_patterns,
        "files": records,
    }


def verify_tree_snapshot(root: Path, snapshot: Mapping[str, Any]) -> tuple[Finding, ...]:
    if snapshot.get("schema_version") != TREE_SNAPSHOT_SCHEMA:
        return (Finding("snapshot_schema_invalid"),)
    records = snapshot.get("files")
    excluded = snapshot.get("excluded_patterns")
    if not isinstance(records, list):
        return (Finding("snapshot_files_invalid"),)
    if not isinstance(excluded, list) or not all(
        isinstance(item, str) and item for item in excluded
    ):
        return (Finding("snapshot_exclude_invalid"),)
    findings: list[Finding] = []
    expected: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
            findings.append(Finding("snapshot_record_invalid"))
            continue
        relative = str(record["path"])
        try:
            _relative_path(relative)
        except FreezeError:
            findings.append(Finding("snapshot_path_invalid", relative))
            continue
        if (
            not isinstance(record.get("bytes"), int)
            or record["bytes"] < 0
            or not isinstance(record.get("sha256"), str)
            or _SHA256.fullmatch(str(record["sha256"])) is None
        ):
            findings.append(Finding("snapshot_record_identity_invalid", relative))
            continue
        if relative in expected:
            findings.append(Finding("snapshot_path_duplicate", relative))
        expected[relative] = record
    try:
        current = snapshot_tree(root, exclude=excluded)
    except FreezeError as exc:
        return tuple(sorted(findings + list(exc.findings or (Finding(exc.code),))))
    actual = {str(item["path"]): item for item in current["files"]}
    for relative in sorted(set(expected) - set(actual)):
        findings.append(Finding("rerun_file_missing", relative))
    for relative in sorted(set(actual) - set(expected)):
        findings.append(Finding("rerun_file_unexpected", relative))
    for relative in sorted(set(expected) & set(actual)):
        wanted = expected[relative]
        found = actual[relative]
        if wanted.get("bytes") != found.get("bytes"):
            findings.append(Finding("rerun_size_mismatch", relative))
        if wanted.get("sha256") != found.get("sha256"):
            findings.append(Finding("rerun_hash_mismatch", relative))
    return tuple(sorted(findings))


def prepare_clean_rerun(directory: Path, freeze_hash: str, label: str) -> Path:
    """Create a new rerun directory; never delete or reuse an old run."""

    if not _SHA256.fullmatch(freeze_hash):
        raise FreezeError("rerun_freeze_hash_invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", label):
        raise FreezeError("rerun_label_invalid")
    parent = directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise FreezeError("rerun_parent_symlink_prohibited")
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise FreezeError("rerun_directory_exists") from exc
    marker = {
        "schema_version": RERUN_MARKER_SCHEMA,
        "freeze_manifest_sha256": freeze_hash,
        "label": label,
    }
    marker_path = directory / "rerun.json"
    descriptor = os.open(
        marker_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        payload = canonical_json_bytes(marker) + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise FreezeError("rerun_marker_short_write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return marker_path


def append_freeze_record(
    record_path: Path,
    *,
    manifest_path: str,
    manifest_hash: str,
    reviewer: str,
    frozen_at_utc: str | None = None,
) -> dict[str, str]:
    """Append one canonical freeze record after explicit reviewer approval.

    The caller must separately verify the manifest immediately before calling
    this function.  ``O_APPEND`` and one write ensure records are never
    rewritten in place.
    """

    if not _SHA256.fullmatch(manifest_hash):
        raise FreezeError("freeze_record_hash_invalid")
    if not reviewer.strip() or len(reviewer) > 200 or any(
        character in reviewer for character in "\r\n\x00"
    ):
        raise FreezeError("freeze_reviewer_invalid")
    relative = _relative_path(manifest_path).as_posix()
    if frozen_at_utc is None:
        frozen_at_utc = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", frozen_at_utc):
        raise FreezeError("freeze_timestamp_invalid")
    record = {
        "schema_version": "backtranslation.freeze-record.v1",
        "frozen_at_utc": frozen_at_utc,
        "manifest_path": relative,
        "manifest_sha256": manifest_hash,
        "reviewer": reviewer.strip(),
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    if record_path.is_symlink():
        raise FreezeError("freeze_record_symlink_prohibited")
    descriptor = os.open(
        record_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0),
        0o644,
    )
    try:
        payload = canonical_json_bytes(record) + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise FreezeError("freeze_record_short_write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record


def _metadata_file_hash(distribution: importlib.metadata.Distribution, name: str) -> str | None:
    candidates = [
        item
        for item in (distribution.files or ())
        if item.name == name
        and len(item.parts) == 2
        and item.parent.name.endswith((".dist-info", ".egg-info"))
    ]
    if len(candidates) != 1:
        return None
    path = Path(distribution.locate_file(candidates[0]))
    if not path.is_file():
        return None
    return sha256_file(path)


def _verify_distribution_payloads(
    distribution: importlib.metadata.Distribution,
) -> tuple[int, int, str]:
    """Verify installer hashes and bind their canonical path/hash inventory."""

    verified: list[dict[str, str | int]] = []
    unhashed = 0
    for package_path in distribution.files or ():
        expected = package_path.hash
        if expected is None:
            unhashed += 1
            continue
        # Bytecode caches are interpreter-generated after installation and can
        # legitimately differ from a wheel RECORD. They are excluded from the
        # reproducibility identity; source/module payloads remain verified.
        if "__pycache__" in package_path.parts or package_path.suffix in {
            ".pyc",
            ".pyo",
        }:
            unhashed += 1
            continue
        algorithm = expected.mode.casefold()
        try:
            constructor = getattr(hashlib, algorithm)
        except AttributeError as exc:
            raise FreezeError("distribution_hash_algorithm_unsupported") from exc
        installed_path = Path(distribution.locate_file(package_path))
        if not installed_path.is_file():
            raise FreezeError(
                "distribution_payload_missing",
                (Finding("distribution_payload_missing", package_path.as_posix()),),
            )
        digest = constructor()
        try:
            with installed_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise FreezeError("distribution_payload_read_failed") from exc
        expected_bytes = expected.value + "=" * (-len(expected.value) % 4)
        try:
            wanted = base64.urlsafe_b64decode(expected_bytes)
        except (ValueError, TypeError) as exc:
            raise FreezeError("distribution_record_hash_invalid") from exc
        if digest.digest() != wanted:
            raise FreezeError(
                "distribution_payload_hash_mismatch",
                (Finding("distribution_payload_hash_mismatch", package_path.as_posix()),),
            )
        verified.append(
            {
                "path": package_path.as_posix(),
                "hash": f"{algorithm}={expected.value}",
                "bytes": installed_path.stat().st_size,
            }
        )
    verified.sort(key=lambda item: str(item["path"]))
    return len(verified), unhashed, sha256_bytes(canonical_json_bytes(verified))


def build_runtime_lock(project_root: Path) -> dict[str, Any]:
    """Inventory the exact interpreter and installed distribution metadata.

    Wheel ``RECORD`` files contain installer-supplied hashes for wheel payloads;
    hashing RECORD plus METADATA and WHEEL gives a compact deterministic audit
    identity without importing packages or executing arbitrary package code.
    """

    pyproject_path = project_root / "pyproject.toml"
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise FreezeError("pyproject_read_failed") from exc
    project = pyproject.get("project", {})
    declared = project.get("dependencies", []) if isinstance(project, dict) else []
    build = pyproject.get("build-system", {})
    build_requires = build.get("requires", []) if isinstance(build, dict) else []
    if not isinstance(declared, list) or not isinstance(build_requires, list):
        raise FreezeError("pyproject_dependencies_invalid")
    direct_requirements = sorted(str(item) for item in [*declared, *build_requires])
    for requirement in direct_requirements:
        if "==" not in requirement or _placeholder_present(requirement):
            raise FreezeError(
                "direct_dependency_unpinned",
                (Finding("direct_dependency_unpinned", "pyproject.toml", requirement),),
            )

    distributions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not name or not version:
            raise FreezeError("distribution_identity_missing")
        normalized = re.sub(r"[-_.]+", "-", name).casefold()
        if normalized in seen:
            raise FreezeError("distribution_duplicate", (Finding("distribution_duplicate", normalized),))
        seen.add(normalized)
        metadata_hash = _metadata_file_hash(distribution, "METADATA")
        wheel_hash = _metadata_file_hash(distribution, "WHEEL")
        record_hash = _metadata_file_hash(distribution, "RECORD")
        if metadata_hash is None or record_hash is None:
            raise FreezeError(
                "distribution_metadata_unhashable",
                (Finding("distribution_metadata_unhashable", normalized),),
            )
        files = distribution.files or ()
        hashed_entries, unhashed_entries, payload_inventory_hash = (
            _verify_distribution_payloads(distribution)
        )
        item: dict[str, Any] = {
            "name": normalized,
            "version": version,
            "metadata_sha256": metadata_hash,
            "record_sha256": record_hash,
            "record_entries": len(files),
            "record_entries_hashed": hashed_entries,
            "record_entries_unhashed": unhashed_entries,
            "verified_payload_inventory_sha256": payload_inventory_hash,
        }
        if wheel_hash is not None:
            item["wheel_metadata_sha256"] = wheel_hash
        direct_url_hash = _metadata_file_hash(distribution, "direct_url.json")
        if direct_url_hash is not None:
            item["direct_url_sha256"] = direct_url_hash
        distributions.append(item)
    distributions.sort(key=lambda item: (item["name"], item["version"]))

    executable = Path(sys.executable).resolve(strict=True)
    lock = {
        "schema_version": RUNTIME_LOCK_SCHEMA,
        "project_requirements": direct_requirements,
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "abi": sysconfig.get_config_var("SOABI"),
            "executable_sha256": sha256_file(executable),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "libc": list(platform.libc_ver()),
        },
        "distributions": distributions,
    }
    lock["inventory_sha256"] = sha256_bytes(canonical_json_bytes(lock))
    return lock


def verify_runtime_lock(project_root: Path, lock: Mapping[str, Any]) -> tuple[Finding, ...]:
    if lock.get("schema_version") != RUNTIME_LOCK_SCHEMA:
        return (Finding("runtime_lock_schema_invalid"),)
    supplied = dict(lock)
    supplied_hash = supplied.pop("inventory_sha256", None)
    if not isinstance(supplied_hash, str) or not _SHA256.fullmatch(supplied_hash):
        return (Finding("runtime_lock_inventory_hash_invalid"),)
    if sha256_bytes(canonical_json_bytes(supplied)) != supplied_hash:
        return (Finding("runtime_lock_inventory_hash_mismatch"),)
    try:
        current = build_runtime_lock(project_root)
    except FreezeError as exc:
        return exc.findings or (Finding(exc.code),)
    if canonical_json_bytes(current) != canonical_json_bytes(lock):
        return (Finding("runtime_environment_mismatch"),)
    return ()
