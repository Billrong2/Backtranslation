"""Outcome-blind construction of the 50 round-trip case payloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class CaseValidationError(ValueError):
    """A stable-code case or isolation validation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StudyCase:
    method_id: str
    project: str
    dataset_loc: int
    code_1: str
    code_1_sha256: str
    target_declaration: str
    type_context: dict[str, Any]
    type_context_sha256: str


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CaseValidationError("case_not_canonical_json") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaseValidationError("case_json_read_failed") from exc
    if not isinstance(value, Mapping):
        raise CaseValidationError("case_json_not_object")
    return value


def _trim_leading_comments(prefix: str) -> str:
    """Remove attached leading Java comments, retaining annotations/declaration."""
    position = 0
    length = len(prefix)
    while True:
        while position < length and prefix[position].isspace():
            position += 1
        if prefix.startswith("//", position):
            newline = prefix.find("\n", position + 2)
            if newline < 0:
                raise CaseValidationError("target_declaration_only_comment")
            position = newline + 1
            continue
        if prefix.startswith("/*", position):
            end = prefix.find("*/", position + 2)
            if end < 0:
                raise CaseValidationError("target_declaration_unterminated_comment")
            position = end + 2
            continue
        break
    declaration = prefix[position:].strip()
    if not declaration:
        raise CaseValidationError("target_declaration_empty")
    return declaration


def _target_declaration(snippet: bytes, body_start: int) -> str:
    if body_start < 1 or body_start >= len(snippet):
        raise CaseValidationError("target_body_start_not_open_brace")
    before_body = snippet[:body_start].rstrip()
    if not before_body.endswith(b"{"):
        raise CaseValidationError("target_body_start_not_open_brace")
    try:
        prefix = before_body[:-1].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CaseValidationError("target_declaration_not_utf8") from exc
    return _trim_leading_comments(prefix)


def _type_context(source: Mapping[str, Any]) -> dict[str, Any]:
    if source.get("schema_version") != "tse-java-context-v1":
        raise CaseValidationError("source_context_schema_version")
    imports = source.get("imports")
    type_header = source.get("enclosing_type_header")
    package = source.get("package_declaration")
    import_count = source.get("source_file_import_count")
    if not isinstance(imports, list) or not all(isinstance(item, str) and item for item in imports):
        raise CaseValidationError("source_context_imports")
    if not isinstance(type_header, str) or not type_header:
        raise CaseValidationError("source_context_type_header")
    if not isinstance(package, str):
        raise CaseValidationError("source_context_package")
    if (
        source.get("import_selection") != "all_source_file_imports_in_source_order"
        or not isinstance(import_count, int)
        or isinstance(import_count, bool)
        or import_count != len(imports)
        or source.get("retained_import_count") != len(imports)
    ):
        raise CaseValidationError("source_context_import_policy")
    if source.get("member_stub_selection") != "uniformly_excluded_no_symbol_resolution_claim":
        raise CaseValidationError("source_context_member_policy")
    if source.get("enclosing_type_depth") != 0:
        raise CaseValidationError("source_context_enclosing_type_chain_unavailable")
    # The retained, outcome-blind TSE policy exposes the exact package, every
    # source-file import in source order, and the exact (depth-zero) type
    # header. Member declarations/initializers/bodies are uniformly excluded;
    # the empty arrays below do not claim that symbol resolution was performed.
    return {
        "schema_version": "type-context-v1",
        "language": "java",
        "java_language_level": None,
        "package_declaration": package or None,
        "imports": imports,
        "enclosing_type_headers": [type_header],
        "referenced_fields": [],
        "referenced_callables": [],
        "referenced_enum_constants": [],
    }


def load_study_cases(data_directory: Path) -> tuple[StudyCase, ...]:
    """Load and revalidate retained source/context artifacts without outcomes."""
    manifest_path = data_directory / "source_manifest.jsonl"
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CaseValidationError("source_manifest_read_failed") from exc
    if len(lines) != 50:
        raise CaseValidationError("source_manifest_count_not_50")
    cases: list[StudyCase] = []
    seen: set[str] = set()
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseValidationError("source_manifest_not_jsonl") from exc
        if not isinstance(record, Mapping):
            raise CaseValidationError("source_manifest_record_not_object")
        method_id = record.get("snippet_id")
        if not isinstance(method_id, str) or method_id in seen:
            raise CaseValidationError("source_manifest_method_id")
        seen.add(method_id)
        snippet_path = data_directory / str(record.get("snippet_path"))
        context_path = data_directory / str(record.get("context_path"))
        try:
            snippet = snippet_path.read_bytes()
            code_1 = snippet.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            raise CaseValidationError("snippet_read_failed") from exc
        if sha256_bytes(snippet) != record.get("snippet_sha256"):
            raise CaseValidationError("snippet_hash_mismatch")
        try:
            context_raw_bytes = context_path.read_bytes()
        except OSError as exc:
            raise CaseValidationError("context_read_failed") from exc
        if sha256_bytes(context_raw_bytes) != record.get("context_sha256"):
            raise CaseValidationError("context_hash_mismatch")
        source_context = _read_json(context_path)
        if source_context.get("snippet_id") != method_id:
            raise CaseValidationError("context_method_id_mismatch")
        type_context = _type_context(source_context)
        type_context_bytes = canonical_json_bytes(type_context)
        body_start = record.get("snippet_body_start_byte_utf8")
        if not isinstance(body_start, int) or isinstance(body_start, bool):
            raise CaseValidationError("snippet_body_start_invalid")
        project = record.get("system_name")
        dataset_loc = record.get("dataset_LOC")
        if not isinstance(project, str) or not project:
            raise CaseValidationError("case_project_invalid")
        if not isinstance(dataset_loc, int) or isinstance(dataset_loc, bool) or dataset_loc < 1:
            raise CaseValidationError("case_loc_invalid")
        cases.append(
            StudyCase(
                method_id=method_id,
                project=project,
                dataset_loc=dataset_loc,
                code_1=code_1,
                code_1_sha256=sha256_bytes(snippet),
                target_declaration=_target_declaration(snippet, body_start),
                type_context=type_context,
                type_context_sha256=sha256_bytes(type_context_bytes),
            )
        )
    cases.sort(key=lambda case: case.method_id)
    expected = [f"tse-{number:03d}" for number in range(1, 51)]
    if [case.method_id for case in cases] != expected:
        raise CaseValidationError("case_method_id_sequence")
    return tuple(cases)


def extraction_input(case: StudyCase) -> dict[str, Any]:
    return {
        "schema_version": "extraction-input-v1",
        "type_context": case.type_context,
        "code_1": case.code_1,
    }


def regeneration_input(case: StudyCase, directions: Mapping[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": "regeneration-input-v1",
        "target_declaration": case.target_declaration,
        "type_context": case.type_context,
        "directions": dict(directions),
    }
    assert_regeneration_isolated(case, value)
    return value


def _mapping_keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _mapping_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _mapping_keys(nested)


def assert_regeneration_isolated(case: StudyCase, value: Mapping[str, Any]) -> None:
    serialized = canonical_json_bytes(value)
    code_bytes = case.code_1.encode("utf-8")
    if code_bytes in serialized:
        raise CaseValidationError("regeneration_contains_complete_code_1")
    if case.code_1_sha256.encode("ascii") in serialized:
        raise CaseValidationError("regeneration_contains_code_1_hash")
    forbidden = {
        "au",
        "pbu",
        "au_mean",
        "pbu_mean",
        "ruse",
        "ruby",
        "codebert",
        "rouge",
        "bleu",
        "fidelity",
        "score",
        "human_outcome",
        "verification_questions",
    }
    present = {key.casefold() for key in _mapping_keys(value)}
    if present.intersection(forbidden):
        raise CaseValidationError("regeneration_contains_forbidden_key")


def render_prompt(template: str, placeholder: str, payload: Mapping[str, Any]) -> str:
    marker = "{{" + placeholder + "}}"
    if template.count(marker) != 1:
        raise CaseValidationError("prompt_placeholder_count_not_one")
    rendered = template.replace(marker, canonical_json_bytes(payload).decode("utf-8"))
    if marker in rendered:
        raise CaseValidationError("prompt_placeholder_not_replaced")
    return rendered
