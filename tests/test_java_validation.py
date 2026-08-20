from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backtranslation.cases import load_study_cases
from backtranslation.java_validation import (
    TREE_SITTER_JAVA_REVISION,
    TREE_SITTER_JAVA_VERSION,
    TREE_SITTER_VERSION,
    JavaValidationError,
    analyze_java_method,
)


PROJECT = Path(__file__).resolve().parents[1]


def test_parser_pin_manifest_matches_module_constants() -> None:
    pin = json.loads((PROJECT / "config" / "java-parser-revision.json").read_text())
    assert pin["runtime"]["version"] == TREE_SITTER_VERSION
    assert pin["java_grammar"]["version"] == TREE_SITTER_JAVA_VERSION
    assert pin["java_grammar"]["revision"] == TREE_SITTER_JAVA_REVISION
    for section, wheel_key in (
        (pin["runtime"], "cp311_linux_x86_64_wheel_sha256"),
        (pin["java_grammar"], "cp39_abi3_linux_x86_64_wheel_sha256"),
    ):
        assert len(section[wheel_key]) == 64
        int(section[wheel_key], 16)


def test_utf8_nfc_lf_comment_removal_and_literal_preservation() -> None:
    raw = (
        b'public String f() {\r\n'
        b'  int a = 1/* between */+2; // line\r\n'
        b'  String s = "/* literal */ // literal e'
        + "\u0301".encode("utf-8")
        + b'";\r'
        b'  char slash = \'/\'; /* tail */ return s;\r\n'
        b'}'
    )
    result = analyze_java_method(raw, "public String f()")
    assert result.structurally_valid
    assert "\r" not in result.lex.normalized_source
    assert "e\u0301" not in result.lex.normalized_source
    assert "é" in result.lex.normalized_source
    assert "between" not in result.lex.commentless_source
    assert "line" not in result.lex.commentless_source
    assert '"/* literal */ // literal é"' in result.lex.tokens
    assert "'/'" in result.lex.tokens
    plus = result.lex.tokens.index("+")
    assert result.lex.tokens[plus - 1 : plus + 2] == ("1", "+", "2")
    assert result.lex.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.lex.raw_sha256 != result.lex.normalized_sha256
    assert result.lex.normalized_sha256 == hashlib.sha256(
        result.lex.normalized_scoring_view.encode("utf-8")
    ).hexdigest()
    assert result.lex.canonical_source_sha256 == hashlib.sha256(
        result.lex.normalized_source.encode("utf-8")
    ).hexdigest()
    assert len(result.lex.comment_spans) == 3


def test_comment_replacement_preserves_newlines_and_separates_tokens() -> None:
    result = analyze_java_method(
        "void f() { int/**/value=1; /* first\nsecond */value++; }", "void f()"
    )
    assert result.structurally_valid
    assert result.lex.tokens[:6] == ("void", "f", "(", ")", "{", "int")
    assert result.lex.tokens.count("value") == 2
    assert result.lex.commentless_source.count("\n") == 1
    assert "int    value" in result.lex.commentless_source


def test_exact_declaration_comparison_is_token_based_not_character_whitespace() -> None:
    equivalent = analyze_java_method(
        "@Deprecated\npublic <T> T f ( final T value ) throws Exception { return value; }",
        "@Deprecated public <T> T f(final T value) throws Exception",
    )
    assert equivalent.declaration_matches
    assert equivalent.structurally_valid

    changed_name = analyze_java_method(
        "@Deprecated public <T> T g(final T value) throws Exception { return value; }",
        "@Deprecated public <T> T f(final T value) throws Exception",
    )
    assert not changed_name.declaration_matches
    assert changed_name.failure_codes == ("target_declaration_mismatch",)

    comment_in_declaration = analyze_java_method(
        "public/** harmless */void f(){return;}", "public void f()"
    )
    assert comment_in_declaration.structurally_valid


def test_constructor_is_disambiguated_by_target_name() -> None:
    result = analyze_java_method(
        "protected Widget(final int value) { this.value = value; }",
        "protected Widget(final int value)",
    )
    assert result.target_kind == "constructor_declaration"
    assert result.candidate_kind == "constructor_declaration"
    assert result.structurally_valid
    assert result.method_ncloc == 1
    assert result.cyclomatic_complexity == 1


@pytest.mark.parametrize(
    ("candidate", "failures"),
    [
        (
            "class Wrapper { public int f(){return 1;} }",
            {
                "target_callable_count_not_one",
                "sibling_member_present",
                "enclosing_type_present",
                "target_body_missing",
                "target_declaration_mismatch",
            },
        ),
        (
            "public int f(){return 1;} public int g(){return 2;}",
            {
                "target_callable_count_not_one",
                "sibling_member_present",
                "target_body_missing",
                "target_declaration_mismatch",
            },
        ),
        (
            "int field; public int f(){return field;}",
            {"sibling_member_present"},
        ),
        (
            "; public int f(){return 1;}",
            {"sibling_member_present"},
        ),
    ],
)
def test_rejects_wrapper_sibling_callable_or_sibling_field(candidate, failures) -> None:
    result = analyze_java_method(candidate, "public int f()")
    assert not result.structurally_valid
    assert failures.issubset(result.failure_codes)
    assert result.method_ncloc is None
    assert result.cyclomatic_complexity is None


def test_parse_failure_keeps_nonempty_tokens_and_separate_status() -> None:
    candidate = "public int f() { String s = \"// not a comment\"; return (1 + ; }"
    result = analyze_java_method(candidate, "public int f()")
    assert not result.structurally_valid
    assert not result.lex.parse_success
    assert "java_parse_error" in result.failure_codes
    assert '"// not a comment"' in result.lex.tokens
    assert ";" in result.lex.tokens
    assert result.lex.parse_issues
    assert result.method_ncloc is None
    metadata = result.as_metadata()
    assert metadata["parse_success"] is False
    assert metadata["parse_issue_count"] == len(metadata["parse_issues"])
    assert metadata["target_declaration_tokens_sha256"]
    assert metadata["candidate_declaration_tokens_sha256"]
    assert "source" not in metadata

    non_java_whitespace = analyze_java_method(
        "public int f() { int\u00a0value = 1; return value; }", "public int f()"
    )
    assert not non_java_whitespace.lex.parse_success
    assert "\u00a0" in non_java_whitespace.lex.tokens


def test_ncloc_and_cyclomatic_definition() -> None:
    candidate = """public int f(int x, boolean a, boolean b) {
        // ignored physical line
        if (a && b || x > 0) {
            x++;
        }

        for (int i = 0; i < 2; i++) x += i;
        switch (x) {
            case 1:
            case 2: x += 2; break;
            default: x--;
        }
        try { assert x >= 0; } catch (RuntimeException ex) { x = a ? 1 : 2; }
        return x;
    }"""
    result = analyze_java_method(
        candidate, "public int f(int x, boolean a, boolean b)"
    )
    assert result.structurally_valid
    # Base 1; if; &&; ||; for; two non-default case labels; assert; catch;
    # ternary. Default labels do not add a path.
    assert result.cyclomatic_complexity == 10
    assert result.method_ncloc == 13


def test_nested_anonymous_method_is_not_counted_as_sibling_or_complexity() -> None:
    candidate = """public Runnable f() {
        return new Runnable() {
            @Override public void run() { if (true) { } }
        };
    }"""
    result = analyze_java_method(candidate, "public Runnable f()")
    assert result.structurally_valid
    assert result.exactly_one_target_callable
    assert result.no_sibling_members
    assert result.cyclomatic_complexity == 1

    lambda_result = analyze_java_method(
        "public Runnable f() { return () -> { if (true) { } }; }",
        "public Runnable f()",
    )
    assert lambda_result.structurally_valid
    assert lambda_result.cyclomatic_complexity == 1


def test_all_50_recovered_code1_methods_validate_without_outcomes() -> None:
    cases = load_study_cases(PROJECT / "data" / "tse")
    analyses = [
        analyze_java_method(case.code_1, case.target_declaration) for case in cases
    ]
    assert len(analyses) == 50
    assert all(analysis.structurally_valid for analysis in analyses)
    assert all(analysis.lex.tokens for analysis in analyses)
    assert all(analysis.method_ncloc and analysis.method_ncloc > 0 for analysis in analyses)
    assert all(
        analysis.cyclomatic_complexity and analysis.cyclomatic_complexity >= 1
        for analysis in analyses
    )


def test_invalid_target_and_invalid_utf8_are_hard_failures() -> None:
    with pytest.raises(JavaValidationError, match="java_source_not_utf8"):
        analyze_java_method(b"void f(){\xff}", "void f()")
    with pytest.raises(JavaValidationError, match="target_declaration_contains_brace"):
        analyze_java_method("void f(){}", "void f() {}")
