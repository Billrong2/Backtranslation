from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "tse"
TOOL_PATH = ROOT / "tools" / "recover_tse_dataset.py"
SPEC = importlib.util.spec_from_file_location("recover_tse_dataset", TOOL_PATH)
assert SPEC and SPEC.loader
RECOVERY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RECOVERY
SPEC.loader.exec_module(RECOVERY)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest() -> list[dict[str, object]]:
    return [json.loads(line) for line in (DATA / "source_manifest.jsonl").read_text().splitlines() if line]


def test_retained_artifact_validates_offline() -> None:
    RECOVERY.validate_artifact(DATA)


def test_expanded_tse_identity_and_source_ranges() -> None:
    rows = manifest()
    assert len(rows) == 50
    assert len({row["dataset_signature"] for row in rows}) == 50
    assert len({(row["source_repository_url"], row["source_revision"], row["source_path"], row["source_start_byte_utf8"], row["source_end_byte_utf8_exclusive"]) for row in rows}) == 50
    assert Counter(row["system_name"] for row in rows) == Counter({repo.system_name: 5 for repo in RECOVERY.REPOS})
    assert Counter(row["evaluation_rows"] for row in rows) == Counter({9: 44, 8: 6})
    assert [row["snippet_id"] for row in rows] == [f"tse-{number:03d}" for number in range(1, 51)]
    assert [row["dataset_signature"] for row in rows] == sorted(row["dataset_signature"] for row in rows)
    for row in rows:
        assert row["resolution_status"] == "validated"
        assert row["signature_validation"] == "fq_package_class_method_and_normalized_parameter_types_exact"
        assert row["dataset_LOC_range_validation"] == "exact"
        assert row["dataset_LOC"] == row["source_range_physical_lines"]
        assert len(row["parent_revision"]) == 40
        assert len(row["source_revision"]) == 40
        assert len(row["source_blob_oid_sha1"]) == 40
        assert sha256(DATA / row["snippet_path"]) == row["snippet_sha256"]


def test_context_is_mechanical_and_outcome_free() -> None:
    forbidden_keys = {"AU", "PBU", "TNPU", "TAU", "ABU50", "BD50"}
    for row in manifest():
        context = json.loads((DATA / row["context_path"]).read_text())
        assert context["policy_status"] == "proposal_for_protocol_freeze"
        assert context["schema_version"] == "tse-java-context-v1"
        assert context["retained_import_count"] == len(context["imports"])
        assert context["retained_import_count"] == context["source_file_import_count"]
        assert context["import_selection"] == "all_source_file_imports_in_source_order"
        assert (
            context["member_stub_selection"]
            == "uniformly_excluded_no_symbol_resolution_claim"
        )
        assert context["method_source_sha256"] == row["snippet_sha256"]
        assert forbidden_keys.isdisjoint(context)
        assert "questions" not in context


def test_raw_inputs_are_hash_pinned_but_not_redistributed() -> None:
    provenance = json.loads((DATA / "provenance.json").read_text())
    assert provenance["validated_structure"]["evaluation_rows"] == 444
    assert provenance["validated_structure"]["participants"] == 63
    assert provenance["validated_structure"]["snippets"] == 50
    assert provenance["raw_license_status"] == "unresolved_no_license_statement_found_on_authoritative_download_page"
    assert provenance["raw_retention_policy"] == "hash_pinned_cache_only_not_copied_into_artifact"
    retained_names = {path.name for path in DATA.rglob("*") if path.is_file()}
    assert "understandability.csv" not in retained_names
    assert "verification_questions.txt" not in retained_names
    assert "systems.csv" not in retained_names
    assert "RQ2.zip" not in retained_names
    assert not any(path.name == ".git" for path in DATA.rglob(".git"))


def test_file_level_license_overrides_are_retained() -> None:
    overrides = [row for row in manifest() if row["license_basis"] == "file_header_override"]
    assert {row["snippet_id"] for row in overrides} == {"tse-010", "tse-042", "tse-043"}
    for row in overrides:
        assert row["license_expression"] == "Apache-2.0"
        assert len(row["license_evidence_files"]) == 1
        evidence = row["license_evidence_files"][0]
        assert evidence["role"] == "verbatim_source_file_license_header"
        assert sha256(DATA / evidence["path"]) == evidence["sha256"]


def test_java_extractor_ignores_literals_and_excludes_section_comment() -> None:
    source = r'''
package x;
class Demo {
    // section heading, not owned by the method
    /** Attached docs with a misleading brace: } */
    @Deprecated
    public int f(java.util.List<String> xs, int[] values) {
        String text = "not a closing brace: }";
        char brace = '}';
        return xs.size() + values.length;
    }

    public int f(int value) { return value; }
}
'''
    signature = "x.Demo.f(java.util.List<java.lang.String>,int[])"
    recovered = RECOVERY.extract_method(source, signature)
    selected = source[recovered.start : recovered.end]
    assert selected.startswith("/** Attached docs")
    assert "section heading" not in selected
    assert selected.rstrip().endswith("}")
    assert RECOVERY.normalized_type("java.util.List<String>") == "List<String>"
