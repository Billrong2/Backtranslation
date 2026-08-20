"""Pinned Java normalization, lexing, and standalone-method validation.

The study scores generated methods even when they do not parse.  Consequently,
lexical normalization and structural validity are represented separately: an
``ERROR``/missing node makes ``parse_success`` false, but every non-whitespace,
non-comment spelling remains in ``tokens`` for the textual/model scorers.

Tree-sitter parses a method as a class-body declaration, which is also how Java
constructors become unambiguous.  The synthetic wrapper used here never enters
the returned source, hashes, token stream, NCLOC, or complexity value.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence


TREE_SITTER_VERSION = "0.25.2"
TREE_SITTER_JAVA_VERSION = "0.23.5"
TREE_SITTER_JAVA_REVISION = "94703d5a6bed02b98e438d7cad1136c01a60ba2c"
TREE_SITTER_LANGUAGE_ABI = 14
TREE_SITTER_NODE_KIND_COUNT = 321
TREE_SITTER_PARSE_STATE_COUNT = 1385
JAVA_NORMALIZATION_VERSION = "java-normalization-v1"

_COMMENT_TYPES = frozenset({"line_comment", "block_comment"})
_CALLABLE_TYPES = frozenset(
    {"method_declaration", "constructor_declaration", "compact_constructor_declaration"}
)
_TYPE_DECLARATION_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)
_ATOMIC_TOKEN_TYPES = frozenset({"string_literal", "character_literal"})
_JAVA_WHITESPACE_BYTES = frozenset({0x09, 0x0A, 0x0C, 0x0D, 0x20})
_SYNTHETIC_CLASS_NAME = "__BacktranslationValidationEnvelope"
_SIMPLE_JAVA_IDENTIFIER = re.compile(r"[^\W\d]\w*\Z", flags=re.UNICODE)


class JavaValidationError(ValueError):
    """Stable-code configuration, input, or frozen-target failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ByteSpan:
    start_byte: int
    end_byte: int

    def __post_init__(self) -> None:
        if self.start_byte < 0 or self.end_byte <= self.start_byte:
            raise ValueError("invalid_byte_span")


@dataclass(frozen=True)
class JavaToken:
    spelling: str
    start_byte: int
    end_byte: int
    from_error_gap: bool = False


@dataclass(frozen=True)
class ParseIssue:
    kind: str
    node_type: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class JavaLexResult:
    normalization_version: str
    raw_sha256: str
    canonical_source_sha256: str
    normalized_sha256: str
    raw_utf8_bytes: int
    canonical_source_utf8_bytes: int
    normalized_utf8_bytes: int
    normalized_source: str
    commentless_source: str
    tokens: tuple[str, ...]
    token_details: tuple[JavaToken, ...]
    comment_spans: tuple[ByteSpan, ...]
    parse_issues: tuple[ParseIssue, ...]
    parse_success: bool

    @property
    def normalized_scoring_view(self) -> str:
        return " ".join(self.tokens)


@dataclass(frozen=True)
class JavaMethodAnalysis:
    lex: JavaLexResult
    target_kind: str
    candidate_kind: str | None
    target_declaration_tokens: tuple[str, ...]
    candidate_declaration_tokens: tuple[str, ...]
    exactly_one_target_callable: bool
    no_sibling_members: bool
    no_enclosing_type: bool
    declaration_matches: bool
    body_present: bool
    structurally_valid: bool
    failure_codes: tuple[str, ...]
    method_ncloc: int | None
    cyclomatic_complexity: int | None

    def as_metadata(self) -> dict[str, Any]:
        """Return outcome-free, source-free validation metadata for an artifact."""

        return {
            "schema_version": "backtranslation.java-method-analysis.v1",
            "normalization_version": self.lex.normalization_version,
            "parser": "tree-sitter-java",
            "parser_version": TREE_SITTER_JAVA_VERSION,
            "parser_revision": TREE_SITTER_JAVA_REVISION,
            "runtime_version": TREE_SITTER_VERSION,
            "raw_sha256": self.lex.raw_sha256,
            "canonical_source_sha256": self.lex.canonical_source_sha256,
            "normalized_sha256": self.lex.normalized_sha256,
            "raw_utf8_bytes": self.lex.raw_utf8_bytes,
            "canonical_source_utf8_bytes": self.lex.canonical_source_utf8_bytes,
            "normalized_utf8_bytes": self.lex.normalized_utf8_bytes,
            "token_count": len(self.lex.tokens),
            "comment_count": len(self.lex.comment_spans),
            "error_gap_token_count": sum(
                token.from_error_gap for token in self.lex.token_details
            ),
            "parse_success": self.lex.parse_success,
            "parse_issue_count": len(self.lex.parse_issues),
            "parse_issues": [
                {
                    "kind": issue.kind,
                    "node_type": issue.node_type,
                    "start_byte": issue.start_byte,
                    "end_byte": issue.end_byte,
                }
                for issue in self.lex.parse_issues
            ],
            "target_kind": self.target_kind,
            "candidate_kind": self.candidate_kind,
            "target_declaration_tokens_sha256": _token_sequence_sha256(
                self.target_declaration_tokens
            ),
            "candidate_declaration_tokens_sha256": (
                _token_sequence_sha256(self.candidate_declaration_tokens)
                if self.candidate_declaration_tokens
                else None
            ),
            "exactly_one_target_callable": self.exactly_one_target_callable,
            "no_sibling_members": self.no_sibling_members,
            "no_enclosing_type": self.no_enclosing_type,
            "declaration_matches": self.declaration_matches,
            "body_present": self.body_present,
            "structurally_valid": self.structurally_valid,
            "failure_codes": list(self.failure_codes),
            "method_ncloc": self.method_ncloc,
            "cyclomatic_complexity": self.cyclomatic_complexity,
        }


@dataclass(frozen=True)
class _CanonicalSource:
    raw_bytes: bytes
    normalized_bytes: bytes
    normalized_text: str


@dataclass(frozen=True)
class _WrappedParse:
    source: _CanonicalSource
    wrapper_bytes: bytes
    candidate_start: int
    candidate_end: int
    tree: Any
    class_body: Any
    members: tuple[Any, ...]
    lex: JavaLexResult


@dataclass(frozen=True)
class _TargetSpec:
    declaration_tokens: tuple[str, ...]
    kind: str
    wrapper_class_name: str


def _token_sequence_sha256(tokens: Sequence[str]) -> str:
    encoded = json.dumps(
        tuple(tokens), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(encoded).hexdigest()


def _distribution_version(distribution: str, expected: str) -> None:
    try:
        actual = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise JavaValidationError(f"{distribution}_not_installed") from exc
    if actual != expected:
        raise JavaValidationError(f"{distribution}_version_not_pinned")


@lru_cache(maxsize=1)
def _java_language() -> Any:
    _distribution_version("tree-sitter", TREE_SITTER_VERSION)
    _distribution_version("tree-sitter-java", TREE_SITTER_JAVA_VERSION)
    try:
        from tree_sitter import Language
        import tree_sitter_java

        language = Language(tree_sitter_java.language())
    except (ImportError, TypeError, ValueError) as exc:
        raise JavaValidationError("java_parser_initialization_failed") from exc
    if (
        language.abi_version != TREE_SITTER_LANGUAGE_ABI
        or language.node_kind_count != TREE_SITTER_NODE_KIND_COUNT
        or language.parse_state_count != TREE_SITTER_PARSE_STATE_COUNT
    ):
        raise JavaValidationError("java_grammar_shape_not_pinned")
    return language


def _parser() -> Any:
    try:
        from tree_sitter import Parser

        return Parser(_java_language())
    except (ImportError, TypeError, ValueError) as exc:
        raise JavaValidationError("java_parser_initialization_failed") from exc


def _canonical_source(source: str | bytes) -> _CanonicalSource:
    if isinstance(source, bytes):
        raw = source
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise JavaValidationError("java_source_not_utf8") from exc
    elif isinstance(source, str):
        text = source
        try:
            raw = source.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise JavaValidationError("java_source_not_utf8") from exc
    else:
        raise JavaValidationError("java_source_not_text_or_bytes")

    # CRLF must be collapsed before lone CR.  NFC does not otherwise change
    # line geometry, and no Java token/literal spelling is case-folded.
    normalized_text = unicodedata.normalize(
        "NFC", text.replace("\r\n", "\n").replace("\r", "\n")
    )
    try:
        normalized = normalized_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:  # pragma: no cover - guarded above
        raise JavaValidationError("java_source_not_utf8") from exc
    return _CanonicalSource(raw, normalized, normalized_text)


def _walk(node: Any) -> Iterable[Any]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _collect_parser_spans(
    node: Any,
    *,
    region_start: int,
    region_end: int,
    token_spans: list[tuple[int, int]],
    comment_spans: list[tuple[int, int]],
) -> None:
    if node.end_byte <= region_start or node.start_byte >= region_end:
        return
    if node.type in _COMMENT_TYPES:
        if node.start_byte >= region_start and node.end_byte <= region_end:
            comment_spans.append((node.start_byte, node.end_byte))
        return
    if node.type in _ATOMIC_TOKEN_TYPES:
        if node.start_byte >= region_start and node.end_byte <= region_end:
            token_spans.append((node.start_byte, node.end_byte))
        return
    if node.child_count == 0:
        if (
            not node.is_missing
            and node.end_byte > node.start_byte
            and node.start_byte >= region_start
            and node.end_byte <= region_end
        ):
            token_spans.append((node.start_byte, node.end_byte))
        return
    for child in node.children:
        _collect_parser_spans(
            child,
            region_start=region_start,
            region_end=region_end,
            token_spans=token_spans,
            comment_spans=comment_spans,
        )


def _non_whitespace_gap_spans(data: bytes, start: int, end: int) -> Iterable[tuple[int, int]]:
    position = start
    while position < end:
        while position < end and data[position] in _JAVA_WHITESPACE_BYTES:
            position += 1
        token_start = position
        while position < end and data[position] not in _JAVA_WHITESPACE_BYTES:
            position += 1
        if token_start < position:
            yield token_start, position


def _parse_issues(root: Any, region_start: int, region_end: int) -> list[ParseIssue]:
    issues: list[ParseIssue] = []
    for node in _walk(root):
        kind = "missing" if node.is_missing else "error" if node.is_error else None
        if kind is None:
            continue
        if node.is_missing:
            intersects = region_start <= node.start_byte <= region_end
        else:
            intersects = node.end_byte > region_start and node.start_byte < region_end
        if not intersects:
            continue
        issues.append(
            ParseIssue(
                kind=kind,
                node_type=node.type,
                start_byte=max(0, node.start_byte - region_start),
                end_byte=max(0, min(node.end_byte, region_end) - region_start),
            )
        )
    issues.sort(key=lambda item: (item.start_byte, item.end_byte, item.kind, item.node_type))
    return issues


def _lex_region(
    canonical: _CanonicalSource,
    *,
    wrapper: bytes,
    tree: Any,
    region_start: int,
    region_end: int,
) -> JavaLexResult:
    raw_token_spans: list[tuple[int, int]] = []
    raw_comment_spans: list[tuple[int, int]] = []
    _collect_parser_spans(
        tree.root_node,
        region_start=region_start,
        region_end=region_end,
        token_spans=raw_token_spans,
        comment_spans=raw_comment_spans,
    )
    raw_token_spans = sorted(set(raw_token_spans))
    raw_comment_spans = sorted(set(raw_comment_spans))

    # Terminal nodes plus comment nodes should cover every non-whitespace byte.
    # On malformed input, Tree-sitter can leave bytes only in a composite ERROR
    # node.  Those maximal spellings are retained as explicit error-gap tokens.
    covered = sorted((*raw_token_spans, *raw_comment_spans))
    gap_tokens: list[tuple[int, int]] = []
    cursor = region_start
    for start, end in covered:
        if start < cursor:
            if end <= cursor:
                continue
            raise JavaValidationError("java_parser_spans_overlap")
        gap_tokens.extend(_non_whitespace_gap_spans(wrapper, cursor, start))
        cursor = end
    gap_tokens.extend(_non_whitespace_gap_spans(wrapper, cursor, region_end))

    gap_token_set = set(gap_tokens)
    all_token_spans = sorted((*raw_token_spans, *gap_tokens))
    details: list[JavaToken] = []
    for start, end in all_token_spans:
        try:
            spelling = wrapper[start:end].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:  # pragma: no cover - canonical source invariant
            raise JavaValidationError("java_token_not_utf8") from exc
        if not spelling:
            raise JavaValidationError("java_parser_emitted_empty_token")
        details.append(
            JavaToken(
                spelling=spelling,
                start_byte=start - region_start,
                end_byte=end - region_start,
                from_error_gap=(start, end) in gap_token_set,
            )
        )

    relative_comments = tuple(
        ByteSpan(start - region_start, end - region_start)
        for start, end in raw_comment_spans
    )
    commentless = bytearray(canonical.normalized_bytes)
    for span in relative_comments:
        # Replacing rather than deleting is lexically equivalent, always keeps
        # adjacent tokens separated, and preserves byte offsets and newlines.
        for position in range(span.start_byte, span.end_byte):
            if commentless[position] != 0x0A:
                commentless[position] = 0x20
    try:
        commentless_text = bytes(commentless).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:  # pragma: no cover - spaces/newlines are valid UTF-8
        raise JavaValidationError("comment_removal_not_utf8") from exc

    issues = _parse_issues(tree.root_node, region_start, region_end)
    for token in details:
        if token.from_error_gap:
            issues.append(
                ParseIssue(
                    kind="unparsed_bytes",
                    node_type="UNPARSED",
                    start_byte=token.start_byte,
                    end_byte=token.end_byte,
                )
            )
    issues.sort(key=lambda item: (item.start_byte, item.end_byte, item.kind, item.node_type))
    tokens = tuple(item.spelling for item in details)
    scoring_view_bytes = " ".join(tokens).encode("utf-8", errors="strict")
    return JavaLexResult(
        normalization_version=JAVA_NORMALIZATION_VERSION,
        raw_sha256=hashlib.sha256(canonical.raw_bytes).hexdigest(),
        canonical_source_sha256=hashlib.sha256(canonical.normalized_bytes).hexdigest(),
        normalized_sha256=hashlib.sha256(scoring_view_bytes).hexdigest(),
        raw_utf8_bytes=len(canonical.raw_bytes),
        canonical_source_utf8_bytes=len(canonical.normalized_bytes),
        normalized_utf8_bytes=len(scoring_view_bytes),
        normalized_source=canonical.normalized_text,
        commentless_source=commentless_text,
        tokens=tokens,
        token_details=tuple(details),
        comment_spans=relative_comments,
        parse_issues=tuple(issues),
        parse_success=not issues,
    )


def _wrapped_parse(source: str | bytes, class_name: str) -> _WrappedParse:
    if not _SIMPLE_JAVA_IDENTIFIER.fullmatch(class_name):
        raise JavaValidationError("synthetic_class_name_invalid")
    canonical = _canonical_source(source)
    prefix = f"class {class_name} {{\n".encode("utf-8")
    suffix = b"\n}\n"
    wrapped = prefix + canonical.normalized_bytes + suffix
    tree = _parser().parse(wrapped)
    root_members = [node for node in tree.root_node.named_children if node.type not in _COMMENT_TYPES]
    if len(root_members) != 1 or root_members[0].type != "class_declaration":
        raise JavaValidationError("synthetic_wrapper_parse_failed")
    class_body = root_members[0].child_by_field_name("body")
    if class_body is None:
        raise JavaValidationError("synthetic_wrapper_body_missing")
    members = tuple(node for node in class_body.named_children if node.type not in _COMMENT_TYPES)
    start = len(prefix)
    end = start + len(canonical.normalized_bytes)
    lex = _lex_region(
        canonical,
        wrapper=wrapped,
        tree=tree,
        region_start=start,
        region_end=end,
    )
    return _WrappedParse(
        source=canonical,
        wrapper_bytes=wrapped,
        candidate_start=start,
        candidate_end=end,
        tree=tree,
        class_body=class_body,
        members=members,
        lex=lex,
    )


def _declaration_tokens(parsed: _WrappedParse, callable_node: Any, body: Any) -> tuple[str, ...]:
    start = callable_node.start_byte - parsed.candidate_start
    end = body.start_byte - parsed.candidate_start
    if start < 0 or end <= start or end > parsed.lex.canonical_source_utf8_bytes:
        raise JavaValidationError("callable_declaration_span_invalid")
    return tuple(
        token.spelling
        for token in parsed.lex.token_details
        if token.start_byte >= start and token.end_byte <= end
    )


def _target_spec(target_declaration: str | bytes) -> _TargetSpec:
    canonical = _canonical_source(target_declaration)
    if not canonical.normalized_text.strip():
        raise JavaValidationError("target_declaration_empty")
    if "{" in canonical.normalized_text or "}" in canonical.normalized_text:
        raise JavaValidationError("target_declaration_contains_brace")
    complete = canonical.normalized_text + " {}"

    # Ordinary methods are accepted as standalone top-level declarations by
    # the pinned grammar.  Constructors appear as a method with a missing name;
    # its apparent return type is the constructor/class identifier.
    standalone_tree = _parser().parse(complete.encode("utf-8"))
    standalone_callables = [
        node
        for node in standalone_tree.root_node.named_children
        if node.type in _CALLABLE_TYPES
    ]
    if len(standalone_callables) != 1:
        raise JavaValidationError("target_declaration_not_callable")
    standalone = standalone_callables[0]
    name = standalone.child_by_field_name("name")
    if not standalone_tree.root_node.has_error and name is not None and not name.is_missing:
        wrapper_name = _SYNTHETIC_CLASS_NAME
        expected_kind = "method_declaration"
    else:
        apparent_type = standalone.child_by_field_name("type")
        if apparent_type is None or apparent_type.type != "type_identifier":
            raise JavaValidationError("target_declaration_parse_failed")
        constructor_name = complete.encode("utf-8")[
            apparent_type.start_byte : apparent_type.end_byte
        ].decode("utf-8")
        if not _SIMPLE_JAVA_IDENTIFIER.fullmatch(constructor_name):
            raise JavaValidationError("target_constructor_name_invalid")
        wrapper_name = constructor_name
        expected_kind = "constructor_declaration"

    parsed = _wrapped_parse(complete, wrapper_name)
    callables = [member for member in parsed.members if member.type in _CALLABLE_TYPES]
    if len(parsed.members) != 1 or len(callables) != 1 or not parsed.lex.parse_success:
        raise JavaValidationError("target_declaration_parse_failed")
    callable_node = callables[0]
    if callable_node.type != expected_kind:
        raise JavaValidationError("target_declaration_kind_mismatch")
    body = callable_node.child_by_field_name("body")
    if body is None or body.type not in {"block", "constructor_body"}:
        raise JavaValidationError("target_declaration_body_missing")
    tokens = _declaration_tokens(parsed, callable_node, body)
    if not tokens:
        raise JavaValidationError("target_declaration_tokens_empty")
    return _TargetSpec(tokens, expected_kind, wrapper_name)


def _method_ncloc(parsed: _WrappedParse, callable_node: Any) -> int:
    start = callable_node.start_byte - parsed.candidate_start
    end = callable_node.end_byte - parsed.candidate_start
    data = parsed.lex.commentless_source.encode("utf-8")[start:end]
    text = data.decode("utf-8", errors="strict")
    return sum(1 for line in text.split("\n") if line.strip())


_DECISION_NODES = frozenset(
    {
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "catch_clause",
        "ternary_expression",
        "assert_statement",
    }
)


def _cyclomatic_complexity(callable_node: Any) -> int:
    decisions = 0

    def visit(node: Any, *, root: bool = False) -> None:
        nonlocal decisions
        if not root and node.type in (
            _CALLABLE_TYPES | _TYPE_DECLARATION_TYPES | {"lambda_expression"}
        ):
            # Nested methods, types, and lambdas are separate callable units.
            return
        if node.type in _DECISION_NODES:
            decisions += 1
        elif node.type == "switch_label":
            if any(child.type == "case" for child in node.children):
                decisions += 1
        elif node.type == "binary_expression":
            decisions += sum(child.type in {"&&", "||"} for child in node.children)
        for child in node.children:
            visit(child)

    visit(callable_node, root=True)
    return 1 + decisions


def analyze_java_method(
    source: str | bytes, target_declaration: str | bytes
) -> JavaMethodAnalysis:
    """Normalize/lex one method and validate it against a frozen declaration.

    Invalid candidate syntax is data, not an exception.  Exceptions are
    reserved for undecodable input, a dependency-pin mismatch, or an invalid
    frozen target declaration.  Textual scorers may consume ``analysis.lex.tokens``
    regardless of structural validity, provided the token tuple is nonempty.
    """

    target = _target_spec(target_declaration)
    parsed = _wrapped_parse(source, target.wrapper_class_name)
    callables = tuple(member for member in parsed.members if member.type in _CALLABLE_TYPES)
    types = tuple(member for member in parsed.members if member.type in _TYPE_DECLARATION_TYPES)
    exactly_one = len(callables) == 1
    candidate = callables[0] if exactly_one else None
    siblings = tuple(member for member in parsed.members if member is not candidate)
    if candidate is None:
        extra_tokens_outside_candidate = bool(parsed.lex.tokens)
    else:
        candidate_start = candidate.start_byte - parsed.candidate_start
        candidate_end = candidate.end_byte - parsed.candidate_start
        extra_tokens_outside_candidate = any(
            token.start_byte < candidate_start or token.end_byte > candidate_end
            for token in parsed.lex.token_details
        )
    # Anonymous punctuation such as a standalone semicolon is not a named AST
    # member, so the token-span check is required in addition to named siblings.
    no_siblings = not siblings and not extra_tokens_outside_candidate
    no_enclosing_type = not types

    candidate_kind = candidate.type if candidate is not None else None
    body = candidate.child_by_field_name("body") if candidate is not None else None
    body_present = body is not None and body.type in {"block", "constructor_body"}
    candidate_tokens = (
        _declaration_tokens(parsed, candidate, body)
        if candidate is not None and body_present
        else ()
    )
    declaration_matches = (
        candidate_kind == target.kind and candidate_tokens == target.declaration_tokens
    )
    structurally_valid = all(
        (
            parsed.lex.parse_success,
            exactly_one,
            no_siblings,
            no_enclosing_type,
            body_present,
            declaration_matches,
        )
    )

    failures: list[str] = []
    if not parsed.lex.parse_success:
        failures.append("java_parse_error")
    if not exactly_one:
        failures.append("target_callable_count_not_one")
    if not no_siblings:
        failures.append("sibling_member_present")
    if not no_enclosing_type:
        failures.append("enclosing_type_present")
    if not body_present:
        failures.append("target_body_missing")
    if not declaration_matches:
        failures.append("target_declaration_mismatch")

    ncloc = _method_ncloc(parsed, candidate) if structurally_valid and candidate is not None else None
    cyclomatic = (
        _cyclomatic_complexity(candidate)
        if structurally_valid and candidate is not None
        else None
    )
    return JavaMethodAnalysis(
        lex=parsed.lex,
        target_kind=target.kind,
        candidate_kind=candidate_kind,
        target_declaration_tokens=target.declaration_tokens,
        candidate_declaration_tokens=candidate_tokens,
        exactly_one_target_callable=exactly_one,
        no_sibling_members=no_siblings,
        no_enclosing_type=no_enclosing_type,
        declaration_matches=declaration_matches,
        body_present=body_present,
        structurally_valid=structurally_valid,
        failure_codes=tuple(failures),
        method_ncloc=ncloc,
        cyclomatic_complexity=cyclomatic,
    )


def normalized_java_tokens(
    source: str | bytes, target_declaration: str | bytes
) -> tuple[str, ...]:
    """Common CodeBERT/ROUGE/BLEU and RUBY-Java lexical token view."""

    return analyze_java_method(source, target_declaration).lex.tokens
