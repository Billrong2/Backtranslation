from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from backtranslation.scoring import (
    BLEU_DEFINITION,
    BLEU_MAX_ORDER,
    BLEU_SMOOTHING,
    CODEBERT_MAX_CONTENT_TOKENS,
    CODEBERT_MODEL_ID,
    CODEBERT_REQUIRED_FILES,
    CODEBERT_REVISION,
    ROUGE_SCORE_VERSION,
    ScoringError,
    bleu_score,
    codebert_embedding,
    codebert_similarity,
    cosine_similarity,
    normalized_scoring_view,
    rouge_scores,
    validate_scoring_tokens,
    verify_codebert_snapshot,
)


class FakeTokenizer:
    bos_token_id = 1_000
    eos_token_id = 2_000

    def __call__(self, text: str, **kwargs):
        assert kwargs == {
            "add_special_tokens": False,
            "padding": False,
            "truncation": False,
            "return_attention_mask": False,
            "return_token_type_ids": False,
            "verbose": False,
        }
        return {"input_ids": [int(item.removeprefix("t")) for item in text.split(" ")]}


class FakeModel(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.anchor = torch.nn.Parameter(torch.tensor(0.0, dtype=torch.float64))
        self.call_shapes: list[tuple[int, ...]] = []

    def forward(self, *, input_ids, attention_mask, return_dict):
        assert return_dict is True
        assert torch.equal(attention_mask, torch.ones_like(input_ids))
        self.call_shapes.append(tuple(input_ids.shape))
        values = input_ids.to(dtype=torch.float32)
        hidden = torch.stack((values, values.square()), dim=-1)
        return SimpleNamespace(last_hidden_state=hidden)


def test_exact_token_validation_and_normalized_view() -> None:
    tokens = ('String', 's', '=', '"two words"', ';')
    assert validate_scoring_tokens(tokens) == tokens
    assert normalized_scoring_view(tokens) == 'String s = "two words" ;'

    with pytest.raises(ScoringError, match="scoring_tokens_empty"):
        validate_scoring_tokens([])
    with pytest.raises(ScoringError, match="scoring_tokens_not_sequence"):
        validate_scoring_tokens("int x")
    with pytest.raises(ScoringError, match="scoring_token_not_nfc"):
        validate_scoring_tokens(["e\u0301"])
    with pytest.raises(ScoringError, match="scoring_token_is_whitespace"):
        validate_scoring_tokens([" \n"])
    # NBSP is not Java lexical whitespace; if it appears as a lexer error token
    # it must be retained rather than silently discarded.
    assert validate_scoring_tokens(["\u00a0"]) == ("\u00a0",)


def test_rouge_known_reference_candidate_orientation() -> None:
    scores = rouge_scores(["a", "b", "c"], ["a", "b"])
    assert scores.rouge1.precision == pytest.approx(1.0)
    assert scores.rouge1.recall == pytest.approx(2 / 3)
    assert scores.rouge1.f1 == pytest.approx(0.8)
    assert scores.rouge2.precision == pytest.approx(1.0)
    assert scores.rouge2.recall == pytest.approx(0.5)
    assert scores.rouge2.f1 == pytest.approx(2 / 3)
    assert scores.rouge_l == scores.rouge1
    assert scores.as_dict()["rougeL"]["f1"] == pytest.approx(0.8)


def test_rouge_is_case_sensitive_and_preserves_embedded_spaces_as_one_token() -> None:
    scores = rouge_scores(['"a b"', "Foo", ";"], ['"a b"', "foo", ";"])
    assert scores.rouge1.f1 == pytest.approx(2 / 3)
    assert scores.rouge2.f1 == pytest.approx(0.0)
    assert scores.rouge_l.f1 == pytest.approx(2 / 3)


def test_rouge_refuses_empty_stream_instead_of_fabricating_zero() -> None:
    with pytest.raises(ScoringError, match="scoring_tokens_empty"):
        rouge_scores(["return"], [])


def test_bleu_exact_match_and_serialized_definition() -> None:
    result = bleu_score(["a", "b", "c", "d"], ["a", "b", "c", "d"])
    assert result.score == pytest.approx(1.0)
    assert result.brevity_penalty == pytest.approx(1.0)
    assert result.length_ratio == pytest.approx(1.0)
    assert result.effective_order == BLEU_MAX_ORDER
    assert result.modified_precisions == pytest.approx((1.0, 1.0, 1.0, 1.0))
    assert result.matched_ngrams == (4, 3, 2, 1)
    assert result.candidate_ngrams == (4, 3, 2, 1)
    serialized = result.as_dict()
    assert serialized["definition"] == BLEU_DEFINITION
    assert serialized["score"] == pytest.approx(1.0)
    assert serialized["scale"] == "unit_interval"
    assert serialized["max_order"] == 4
    assert serialized["effective_order"] == 4
    assert serialized["smoothing"] == BLEU_SMOOTHING
    assert serialized["modified_precisions"] == pytest.approx([1.0] * 4)
    assert serialized["matched_ngrams"] == [4, 3, 2, 1]
    assert serialized["candidate_ngrams"] == [4, 3, 2, 1]


def test_bleu_clips_counts_and_pins_exponential_smoothing() -> None:
    result = bleu_score(["a", "a", "b", "c"], ["a", "a", "a", "a"])
    assert result.matched_ngrams == (2, 1, 0, 0)
    assert result.candidate_ngrams == (4, 3, 2, 1)
    assert result.modified_precisions == pytest.approx((1 / 2, 1 / 3, 1 / 4, 1 / 4))
    assert result.score == pytest.approx((1 / 96) ** 0.25)


def test_bleu_is_exact_zero_without_a_unigram_match() -> None:
    result = bleu_score(["a", "b", "c", "d"], ["w", "x", "y", "z"])
    assert result.matched_ngrams == (0, 0, 0, 0)
    assert result.modified_precisions == (0.0, 0.0, 0.0, 0.0)
    assert result.score == 0.0


def test_bleu_uses_code1_reference_and_effective_order_for_short_candidate() -> None:
    forward = bleu_score(["a", "b", "c", "d"], ["a", "b"])
    reverse = bleu_score(["a", "b"], ["a", "b", "c", "d"])
    assert forward.effective_order == 2
    assert forward.modified_precisions == pytest.approx((1.0, 1.0))
    assert forward.brevity_penalty == pytest.approx(math.exp(-1.0))
    assert forward.score == pytest.approx(math.exp(-1.0))
    assert reverse.effective_order == 4
    assert reverse.brevity_penalty == pytest.approx(1.0)
    assert reverse.score != pytest.approx(forward.score)


def test_bleu_uses_exact_case_sensitive_java_tokens_and_refuses_empty_streams() -> None:
    result = bleu_score(['"a b"', "Foo", ";", "}"], ['"a b"', "foo", ";", "}"])
    assert result.matched_ngrams == (3, 1, 0, 0)
    with pytest.raises(ScoringError, match="scoring_tokens_empty"):
        bleu_score(["return"], [])
    with pytest.raises(ScoringError, match="scoring_token_not_nfc"):
        bleu_score(["return"], ["e\u0301"])


def test_codebert_full_method_pooling_weights_chunks_by_content_count() -> None:
    tokenizer = FakeTokenizer()
    model = FakeModel()
    count = CODEBERT_MAX_CONTENT_TOKENS + 1
    embedding = codebert_embedding(
        [f"t{index}" for index in range(1, count + 1)],
        tokenizer=tokenizer,
        model=model,
        torch_module=torch,
    )
    assert embedding.content_subtoken_count == count
    assert embedding.chunk_count == 2
    assert model.call_shapes == [(1, CODEBERT_MAX_CONTENT_TOKENS + 2), (1, 3)]
    assert embedding.vector[0] == pytest.approx((count + 1) / 2)
    assert embedding.vector[1] == pytest.approx((count + 1) * (2 * count + 1) / 6)
    assert model.anchor.dtype == torch.float32
    assert model.training is False


def test_codebert_pair_score_uses_full_method_cosine_and_is_deterministic() -> None:
    tokenizer = FakeTokenizer()
    model = FakeModel()
    first = codebert_similarity(
        ["t1"], ["t2"], tokenizer=tokenizer, model=model, torch_module=torch
    )
    second = codebert_similarity(
        ["t1"], ["t2"], tokenizer=tokenizer, model=model, torch_module=torch
    )
    expected = 6 / math.sqrt(2 * 20)
    assert first.cosine_similarity == pytest.approx(expected)
    assert first == second
    assert first.reference_content_subtokens == 1
    assert first.candidate_content_subtokens == 1
    assert first.reference_chunks == first.candidate_chunks == 1


def test_cosine_rejects_invalid_vectors() -> None:
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)
    with pytest.raises(ScoringError, match="cosine_zero_vector"):
        cosine_similarity([0.0], [1.0])
    with pytest.raises(ScoringError, match="cosine_vector_shape_mismatch"):
        cosine_similarity([1.0], [1.0, 2.0])


def test_codebert_manifest_constants_and_file_verification(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    payload = b"pinned checkpoint fixture\n"
    for filename in CODEBERT_REQUIRED_FILES:
        (snapshot / filename).write_bytes(payload)
    manifest = {
        "schema_version": "backtranslation.codebert-artifacts.v1",
        "model_id": CODEBERT_MODEL_ID,
        "revision": CODEBERT_REVISION,
        "files": [
            {
                "path": filename,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for filename in sorted(CODEBERT_REQUIRED_FILES)
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_codebert_snapshot(snapshot, manifest_path)["pytorch_model.bin"] == {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }

    (snapshot / "pytorch_model.bin").write_bytes(b"tampered checkpoint fixture")
    with pytest.raises(ScoringError, match="codebert_snapshot_file_size_mismatch"):
        verify_codebert_snapshot(snapshot, manifest_path)


def test_checked_in_codebert_manifest_and_runtime_pins() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "config" / "codebert-base-revision.json").read_text(encoding="utf-8")
    )
    assert manifest["model_id"] == CODEBERT_MODEL_ID
    assert manifest["revision"] == CODEBERT_REVISION
    assert manifest["runtime"] == {
        "python": "3.11",
        "torch_dependency_pin": "2.9.1",
        "torch_runtime": "2.9.1+cpu",
        "transformers": "4.57.6",
        "tokenizers": "0.22.2",
        "device": "cpu",
        "transformer_output_dtype": "float32",
        "pooling_dtype": "float64",
        "cpu_threads": 1,
        "deterministic_algorithms": True,
        "float32_matmul_precision": "highest",
    }
    fixture = manifest["reference_fixture"]
    assert fixture["cosine_similarity"] == pytest.approx(0.998540899304726)
    assert fixture["absolute_tolerance"] == 1e-6
    assert ROUGE_SCORE_VERSION == "0.1.2"


def test_pinned_codebert_reference_fixture_when_checkpoint_is_present() -> None:
    root = Path(__file__).resolve().parents[1]
    snapshot = root / "models" / "codebert-base"
    if not (snapshot / "pytorch_model.bin").is_file():
        pytest.skip("pinned CodeBERT checkpoint is not present locally")

    from backtranslation.scoring import load_pinned_codebert

    manifest_path = root / "config" / "codebert-base-revision.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture = manifest["reference_fixture"]
    tokenizer, model = load_pinned_codebert(snapshot, manifest_path)
    result = codebert_similarity(
        fixture["reference_tokens"],
        fixture["candidate_tokens"],
        tokenizer=tokenizer,
        model=model,
        torch_module=torch,
    )
    assert result.reference_content_subtokens == fixture["reference_content_subtokens"]
    assert result.candidate_content_subtokens == fixture["candidate_content_subtokens"]
    assert result.cosine_similarity == pytest.approx(
        fixture["cosine_similarity"], abs=fixture["absolute_tolerance"]
    )
