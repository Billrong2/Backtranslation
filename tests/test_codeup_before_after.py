from __future__ import annotations

import json

from backtranslation.codeup_before_after import (
    REPORT_METRICS,
    aligned_revision,
    diff_new_side,
    diff_old_side,
    source_intents_prompt,
    source_roundtrip_intent_prompt,
    render_reports,
)


CHUNK = """@@ -1,4 +1,4 @@
 public int size() {
-    return items.size() + 0;
+    return items.size();
 }
"""


def test_diff_sides_are_aligned() -> None:
    assert diff_old_side(CHUNK) == (
        "public int size() {\n    return items.size() + 0;\n}"
    )
    assert diff_new_side(CHUNK) == (
        "public int size() {\n    return items.size();\n}"
    )


def test_aligned_revision_selects_only_target_file() -> None:
    event = {
        "revised_code": {
            "commit": "abc123",
            "changed_code": [
                {"header": "--- a/A.java\n+++ b/A.java", "chunk": CHUNK},
                {
                    "header": "--- a/B.java\n+++ b/B.java",
                    "chunk": CHUNK.replace("items", "other"),
                },
            ],
        }
    }
    commit, before, after = aligned_revision(event, "A.java")
    assert commit == "abc123"
    assert "items.size() + 0" in before
    assert "items.size();" in after
    assert "other" not in before + after


def test_intent_prompts_exclude_review_and_generation_inputs() -> None:
    source_only = source_intents_prompt("return x;", "before")
    paired = source_roundtrip_intent_prompt("return x;", "return x;", "before")
    for prompt in (source_only, paired):
        assert "review comment" in prompt
        assert "generated directions" in prompt
        assert "REVIEW REQUEST" not in prompt
        assert "DIRECTIONS:" not in prompt
    contract = json.loads(paired.split("shape: ", 1)[1].split(". Use 1", 1)[0])
    assert contract["source_intents"] == ["atomic intent", "..."]
    assert contract["judgment"]["reference_statuses"] == [
        "preserved|changed|lost"
    ]


def test_report_adds_before_and_renames_revision_columns() -> None:
    statistic = {
        "paired_wilcoxon_p_value": 0.02,
        "roc_auc_human_as_positive": 0.4,
        "roc_auc_separation": 0.6,
    }
    comparison = {
        "before": {"mean": 0.7},
        "human": {"mean": 0.8},
        "agent": {"mean": 0.75},
        "before_vs_human": statistic,
        "human_vs_agent": statistic,
    }
    markdown, latex = render_reports(
        {
            "design": {
                "cases": 503,
                "agent_input_context_exact_matches_aligned_before": 1,
            },
            "comparisons": {
                key: comparison
                for key in (*REPORT_METRICS, "ccn_proxy_original", "smell_count_original")
            },
        }
    )
    assert "| Measure | Before revision | Human revision | Agent revision |" in markdown
    assert "**0.8000**" in markdown
    assert "- **Before revision:**" in markdown
    assert "Human mean" not in markdown
    assert "Agent mean" not in markdown
    assert "Only 1 of 503" in markdown
    assert "Code-smell count (source, round-trip)" in markdown
    assert "Smell-count change after round trip" not in markdown
    assert "(0.7000, 1.4000)" in markdown
    assert "Cyclomatic-complexity proxy (source, round-trip)" in markdown
    assert "CCN-proxy change after round trip" not in markdown
    assert "**Round-trip CodeBERT similarity**" in markdown
    assert "\\textbf{Round-trip CodeBERT similarity}" in latex
    assert "Before revision & Human revision & Agent revision" in latex
    assert "\\textbf{0.8000}" in latex
