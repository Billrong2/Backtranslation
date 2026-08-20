"""Outcome-blind supporting fidelity scores for reconstructed Java methods.

This module deliberately does not parse Java.  Its inputs are the exact token
spellings emitted by the separately pinned Java lexer after the common scoring
normalization.  Keeping token boundaries in the API matters: a Java string or
text-block token can itself contain whitespace, so splitting the normalized
display string would be lossy.

The score families implemented here are supporting metrics. None is RUSE or
RUBY, and none may be relabelled or used as a replacement for the study's
predeclared RUBY-Java gate. Official RUSE has no numerical study score.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CODEBERT_MODEL_ID = "microsoft/codebert-base"
CODEBERT_REVISION = "3b0952feddeffad0063f274080e3c23d75e7eb39"
CODEBERT_MAX_CONTENT_TOKENS = 510
CODEBERT_CPU_THREADS = 1
CODEBERT_EXPECTED_VOCAB_SIZE = 50_265
CODEBERT_EXPECTED_HIDDEN_SIZE = 768
CODEBERT_EXPECTED_LAYERS = 12
CODEBERT_REQUIRED_FILES = frozenset(
    {
        "config.json",
        "merges.txt",
        "pytorch_model.bin",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "vocab.json",
    }
)
ROUGE_SCORE_VERSION = "0.1.2"
TORCH_RUNTIME_VERSION = "2.9.1+cpu"
BLEU_DEFINITION = "segment-bleu-4-exp-v1"
BLEU_MAX_ORDER = 4
BLEU_SMOOTHING = "exponential-zero-match"


class ScoringError(ValueError):
    """A deterministic, outcome-safe scoring failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RougeMetric:
    precision: float
    recall: float
    f1: float

    def as_dict(self) -> dict[str, float]:
        return {
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


@dataclass(frozen=True)
class RougeScores:
    rouge1: RougeMetric
    rouge2: RougeMetric
    rouge_l: RougeMetric

    def as_dict(self) -> dict[str, dict[str, float]]:
        return {
            "rouge1": self.rouge1.as_dict(),
            "rouge2": self.rouge2.as_dict(),
            "rougeL": self.rouge_l.as_dict(),
        }


@dataclass(frozen=True)
class BleuScore:
    score: float
    brevity_penalty: float
    length_ratio: float
    reference_length: int
    candidate_length: int
    effective_order: int
    modified_precisions: tuple[float, ...]
    matched_ngrams: tuple[int, ...]
    candidate_ngrams: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "definition": BLEU_DEFINITION,
            "score": self.score,
            "scale": "unit_interval",
            "max_order": BLEU_MAX_ORDER,
            "effective_order": self.effective_order,
            "smoothing": BLEU_SMOOTHING,
            "brevity_penalty": self.brevity_penalty,
            "length_ratio": self.length_ratio,
            "reference_length": self.reference_length,
            "candidate_length": self.candidate_length,
            "modified_precisions": list(self.modified_precisions),
            "matched_ngrams": list(self.matched_ngrams),
            "candidate_ngrams": list(self.candidate_ngrams),
        }


@dataclass(frozen=True)
class CodeBertEmbedding:
    vector: tuple[float, ...]
    content_subtoken_count: int
    chunk_count: int
    normalized_utf8_bytes: int
    normalized_sha256: str


@dataclass(frozen=True)
class CodeBertScore:
    cosine_similarity: float
    reference_content_subtokens: int
    candidate_content_subtokens: int
    reference_chunks: int
    candidate_chunks: int
    reference_normalized_sha256: str
    candidate_normalized_sha256: str

    def as_dict(self) -> dict[str, int | float | str]:
        return {
            "cosine_similarity": self.cosine_similarity,
            "reference_content_subtokens": self.reference_content_subtokens,
            "candidate_content_subtokens": self.candidate_content_subtokens,
            "reference_chunks": self.reference_chunks,
            "candidate_chunks": self.candidate_chunks,
            "reference_normalized_sha256": self.reference_normalized_sha256,
            "candidate_normalized_sha256": self.candidate_normalized_sha256,
        }


def validate_scoring_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    """Validate and freeze a nonempty exact Java-token sequence.

    NFC and LF normalization happen before Java lexing in the protocol.  This
    function refuses to repair a noncanonical sequence, because repair here
    could change either the CodeBERT byte-level BPE input or ROUGE matches.
    Whitespace *inside* a token is retained for Java string/text-block tokens;
    a token consisting solely of lexer whitespace is rejected.
    """

    if isinstance(tokens, (str, bytes, bytearray)) or not isinstance(tokens, Sequence):
        raise ScoringError("scoring_tokens_not_sequence")
    frozen = tuple(tokens)
    if not frozen:
        raise ScoringError("scoring_tokens_empty")
    for token in frozen:
        if not isinstance(token, str):
            raise ScoringError("scoring_token_not_string")
        if not token:
            raise ScoringError("scoring_token_empty")
        if "\x00" in token:
            raise ScoringError("scoring_token_nul")
        if "\r" in token:
            raise ScoringError("scoring_token_non_lf_line_ending")
        # Java lexical whitespace is the ASCII space, horizontal tab, form
        # feed, and normalized LF set.  Unicode characters such as NBSP are
        # invalid Java input/error tokens, not lexer whitespace; retain them so
        # malformed Code 2 is still scored exactly instead of silently repaired.
        if all(character in {" ", "\t", "\n", "\f"} for character in token):
            raise ScoringError("scoring_token_is_whitespace")
        if unicodedata.normalize("NFC", token) != token:
            raise ScoringError("scoring_token_not_nfc")
    return frozen


def normalized_scoring_view(tokens: Sequence[str]) -> str:
    """Return the protocol's exact one-ASCII-space joined scoring view."""

    return " ".join(validate_scoring_tokens(tokens))


class _ExactTokenListTokenizer:
    """Transport exact lexer-token arrays through rouge-score's text API."""

    def tokenize(self, carrier: str) -> list[str]:
        try:
            value = json.loads(carrier)
        except json.JSONDecodeError as exc:  # pragma: no cover - internal invariant
            raise ScoringError("rouge_token_carrier_invalid_json") from exc
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ScoringError("rouge_token_carrier_invalid")
        return value


def _token_carrier(tokens: tuple[str, ...]) -> str:
    return json.dumps(tokens, ensure_ascii=False, separators=(",", ":"))


def _ngram_counts(tokens: tuple[str, ...], order: int) -> Counter[tuple[str, ...]]:
    return Counter(
        tuple(tokens[start : start + order])
        for start in range(len(tokens) - order + 1)
    )


def bleu_score(
    reference_tokens: Sequence[str], candidate_tokens: Sequence[str]
) -> BleuScore:
    """Compute the pinned single-reference, segment-level BLEU-4 score.

    Code 1 is the reference and Code 2 is the candidate. Modified n-gram
    precision clips candidate counts against the single reference. Orders are
    uniformly weighted through ``min(4, candidate_length)``; this effective-
    order rule avoids inventing unavailable n-grams for very short methods.

    If there is no unigram match, the score and precisions are exactly zero.
    Otherwise, for each present order with zero matches, exponential smoothing
    replaces the zero numerator with ``1 / 2**k``, where ``k`` is the one-based
    count of zero-match orders encountered so far. Positive counts are not
    smoothed. The brevity penalty is the standard
    ``exp(min(0, 1 - r/c))``. The returned score is in [0, 1], not the
    percentage scale sometimes used by BLEU tools.
    """

    reference = validate_scoring_tokens(reference_tokens)
    candidate = validate_scoring_tokens(candidate_tokens)
    reference_length = len(reference)
    candidate_length = len(candidate)
    effective_order = min(BLEU_MAX_ORDER, candidate_length)

    precisions: list[float] = []
    matches: list[int] = []
    totals: list[int] = []
    zero_match_order = 0
    for order in range(1, effective_order + 1):
        reference_counts = _ngram_counts(reference, order)
        candidate_counts = _ngram_counts(candidate, order)
        possible = sum(candidate_counts.values())
        # A present order always has at least one candidate n-gram. Keeping
        # this check makes a future change to effective-order handling fail
        # closed rather than divide by zero or silently alter the definition.
        if possible < 1:  # pragma: no cover - guarded by effective_order
            raise ScoringError("bleu_candidate_ngram_count_invalid")
        matched = sum(
            min(count, reference_counts.get(ngram, 0))
            for ngram, count in candidate_counts.items()
        )
        if matched:
            precision = matched / possible
        else:
            zero_match_order += 1
            precision = 1.0 / ((2**zero_match_order) * possible)
        matches.append(matched)
        totals.append(possible)
        precisions.append(precision)

    brevity_penalty = math.exp(min(0.0, 1.0 - reference_length / candidate_length))
    length_ratio = candidate_length / reference_length
    if matches[0] == 0:
        # BLEU has no evidence of any shared token. This explicit zero rule
        # matches conventional sentence-BLEU implementations and prevents
        # smoothing alone from manufacturing similarity.
        precisions = [0.0] * effective_order
        score = 0.0
    else:
        score = brevity_penalty * math.exp(
            math.fsum(math.log(precision) for precision in precisions) / effective_order
        )
    values = (score, brevity_penalty, length_ratio, *precisions)
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise ScoringError("bleu_score_invalid")
    if score > 1.0 or brevity_penalty > 1.0 or any(value > 1.0 for value in precisions):
        raise ScoringError("bleu_score_out_of_range")

    return BleuScore(
        score=score,
        brevity_penalty=brevity_penalty,
        length_ratio=length_ratio,
        reference_length=reference_length,
        candidate_length=candidate_length,
        effective_order=effective_order,
        modified_precisions=tuple(precisions),
        matched_ngrams=tuple(matches),
        candidate_ngrams=tuple(totals),
    )


def _require_distribution_version(distribution: str, expected: str) -> None:
    try:
        actual = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ScoringError(f"{distribution}_not_installed") from exc
    if actual != expected:
        raise ScoringError(f"{distribution}_version_not_pinned")


def rouge_scores(
    reference_tokens: Sequence[str], candidate_tokens: Sequence[str]
) -> RougeScores:
    """Compute exact-token ROUGE-1/2/L with Code 1 as the reference.

    The designated supporting statistic is ``rouge_l.f1``.  Empty sequences
    are generation/scoring failures and are never converted to fabricated zero
    scores.  A legitimate nonempty pair with no matches can, of course, score
    zero.
    """

    reference = validate_scoring_tokens(reference_tokens)
    candidate = validate_scoring_tokens(candidate_tokens)
    _require_distribution_version("rouge-score", ROUGE_SCORE_VERSION)
    try:
        from rouge_score import rouge_scorer
    except ImportError as exc:  # pragma: no cover - guarded by metadata check
        raise ScoringError("rouge_score_import_failed") from exc

    scorer = rouge_scorer.RougeScorer(
        ["rouge1", "rouge2", "rougeL"],
        use_stemmer=False,
        split_summaries=False,
        tokenizer=_ExactTokenListTokenizer(),
    )
    raw = scorer.score(_token_carrier(reference), _token_carrier(candidate))

    def convert(name: str) -> RougeMetric:
        score = raw[name]
        values = (float(score.precision), float(score.recall), float(score.fmeasure))
        if any(not math.isfinite(value) or value < 0.0 or value > 1.0 for value in values):
            raise ScoringError("rouge_score_out_of_range")
        return RougeMetric(precision=values[0], recall=values[1], f1=values[2])

    return RougeScores(
        rouge1=convert("rouge1"),
        rouge2=convert("rouge2"),
        rouge_l=convert("rougeL"),
    )


def configure_codebert_determinism(model: Any, torch_module: Any | None = None) -> Any:
    """Force the frozen CPU/float32/evaluation inference regime."""

    torch = torch_module
    if torch is None:
        _require_distribution_version("torch", TORCH_RUNTIME_VERSION)
        try:
            import torch as imported_torch
        except ImportError as exc:  # pragma: no cover - guarded by metadata check
            raise ScoringError("torch_import_failed") from exc
        torch = imported_torch

    torch.use_deterministic_algorithms(True, warn_only=False)
    torch.set_num_threads(CODEBERT_CPU_THREADS)
    if hasattr(torch, "set_float32_matmul_precision"):
        torch.set_float32_matmul_precision("highest")
    try:
        model.to(device=torch.device("cpu"), dtype=torch.float32)
        model.eval()
        if hasattr(model, "requires_grad_"):
            model.requires_grad_(False)
    except (AttributeError, RuntimeError, TypeError) as exc:
        raise ScoringError("codebert_model_configuration_failed") from exc

    tensors = list(model.parameters())
    if hasattr(model, "buffers"):
        tensors.extend(model.buffers())
    for tensor in tensors:
        if tensor.device.type != "cpu":
            raise ScoringError("codebert_model_not_cpu")
        if tensor.is_floating_point() and tensor.dtype != torch.float32:
            raise ScoringError("codebert_model_not_float32")
    if getattr(model, "training", True):
        raise ScoringError("codebert_model_not_eval")
    return torch


def _content_subtoken_ids(tokenizer: Any, normalized: str) -> list[int]:
    try:
        encoded = tokenizer(
            normalized,
            add_special_tokens=False,
            padding=False,
            truncation=False,
            return_attention_mask=False,
            return_token_type_ids=False,
            verbose=False,
        )
    except Exception as exc:
        raise ScoringError("codebert_tokenization_failed") from exc
    if not isinstance(encoded, Mapping):
        raise ScoringError("codebert_tokenizer_result_not_mapping")
    raw_ids = encoded.get("input_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ScoringError("codebert_content_subtokens_empty")
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in raw_ids):
        raise ScoringError("codebert_content_subtoken_invalid")
    return raw_ids


def _codebert_embedding_prepared(
    tokens: Sequence[str], *, tokenizer: Any, model: Any, torch: Any
) -> CodeBertEmbedding:
    normalized = normalized_scoring_view(tokens)
    normalized_bytes = normalized.encode("utf-8", errors="strict")
    content_ids = _content_subtoken_ids(tokenizer, normalized)
    bos_id = getattr(tokenizer, "bos_token_id", None)
    eos_id = getattr(tokenizer, "eos_token_id", None)
    if (
        not isinstance(bos_id, int)
        or isinstance(bos_id, bool)
        or not isinstance(eos_id, int)
        or isinstance(eos_id, bool)
    ):
        raise ScoringError("codebert_special_token_ids_invalid")

    weighted_sum = None
    chunk_count = 0
    with torch.inference_mode():
        for start in range(0, len(content_ids), CODEBERT_MAX_CONTENT_TOKENS):
            chunk = content_ids[start : start + CODEBERT_MAX_CONTENT_TOKENS]
            wrapped = [bos_id, *chunk, eos_id]
            input_ids = torch.tensor([wrapped], dtype=torch.long, device="cpu")
            attention_mask = torch.ones_like(input_ids, dtype=torch.long)
            try:
                output = model(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    return_dict=True,
                )
            except Exception as exc:
                raise ScoringError("codebert_model_inference_failed") from exc
            hidden = getattr(output, "last_hidden_state", None)
            if hidden is None or hidden.ndim != 3:
                raise ScoringError("codebert_last_hidden_state_invalid")
            if hidden.shape[0] != 1 or hidden.shape[1] != len(wrapped) or hidden.shape[2] < 1:
                raise ScoringError("codebert_last_hidden_state_shape_mismatch")
            if hidden.device.type != "cpu" or hidden.dtype != torch.float32:
                raise ScoringError("codebert_last_hidden_state_not_cpu_float32")
            content_hidden = hidden[0, 1:-1, :]
            if content_hidden.shape[0] != len(chunk):
                raise ScoringError("codebert_content_pool_shape_mismatch")
            if not bool(torch.isfinite(content_hidden).all().item()):
                raise ScoringError("codebert_last_hidden_state_nonfinite")

            # Float64 pooling makes the exact reduction regime explicit while
            # retaining the frozen float32 transformer outputs.  Weighting each
            # chunk mean by its content-token count is algebraically equivalent
            # to one mean over all non-special content states.
            chunk_mean = content_hidden.to(dtype=torch.float64).mean(dim=0)
            contribution = chunk_mean * len(chunk)
            weighted_sum = contribution if weighted_sum is None else weighted_sum + contribution
            chunk_count += 1

    if weighted_sum is None:  # pragma: no cover - nonempty tokenization invariant
        raise ScoringError("codebert_no_chunks")
    method_vector = weighted_sum / len(content_ids)
    vector = tuple(float(value) for value in method_vector.tolist())
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ScoringError("codebert_method_embedding_invalid")
    return CodeBertEmbedding(
        vector=vector,
        content_subtoken_count=len(content_ids),
        chunk_count=chunk_count,
        normalized_utf8_bytes=len(normalized_bytes),
        normalized_sha256=hashlib.sha256(normalized_bytes).hexdigest(),
    )


def codebert_embedding(
    tokens: Sequence[str],
    *,
    tokenizer: Any,
    model: Any,
    torch_module: Any | None = None,
) -> CodeBertEmbedding:
    """Embed one complete normalized method using final-layer mean pooling."""

    torch = configure_codebert_determinism(model, torch_module)
    return _codebert_embedding_prepared(
        tokens, tokenizer=tokenizer, model=model, torch=torch
    )


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Compute a finite float64 cosine, rejecting zero or mismatched vectors."""

    if not left or len(left) != len(right):
        raise ScoringError("cosine_vector_shape_mismatch")
    if any(not math.isfinite(float(value)) for value in (*left, *right)):
        raise ScoringError("cosine_vector_nonfinite")
    numerator = math.fsum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm_sq = math.fsum(float(value) * float(value) for value in left)
    right_norm_sq = math.fsum(float(value) * float(value) for value in right)
    if left_norm_sq <= 0.0 or right_norm_sq <= 0.0:
        raise ScoringError("cosine_zero_vector")
    similarity = numerator / math.sqrt(left_norm_sq * right_norm_sq)
    if not math.isfinite(similarity):
        raise ScoringError("cosine_nonfinite")
    # Floating roundoff may exceed the mathematical interval by a few ulps.
    return min(1.0, max(-1.0, similarity))


def codebert_similarity(
    reference_tokens: Sequence[str],
    candidate_tokens: Sequence[str],
    *,
    tokenizer: Any,
    model: Any,
    torch_module: Any | None = None,
) -> CodeBertScore:
    """Compare complete Code-1/Code-2 methods by pinned CodeBERT cosine."""

    torch = configure_codebert_determinism(model, torch_module)
    reference = _codebert_embedding_prepared(
        reference_tokens, tokenizer=tokenizer, model=model, torch=torch
    )
    candidate = _codebert_embedding_prepared(
        candidate_tokens, tokenizer=tokenizer, model=model, torch=torch
    )
    similarity = cosine_similarity(reference.vector, candidate.vector)
    return CodeBertScore(
        cosine_similarity=similarity,
        reference_content_subtokens=reference.content_subtoken_count,
        candidate_content_subtokens=candidate.content_subtoken_count,
        reference_chunks=reference.chunk_count,
        candidate_chunks=candidate.chunk_count,
        reference_normalized_sha256=reference.normalized_sha256,
        candidate_normalized_sha256=candidate.normalized_sha256,
    )


def verify_codebert_snapshot(
    snapshot_directory: Path, manifest_path: Path
) -> dict[str, dict[str, int | str]]:
    """Verify every required local checkpoint file before deserialization."""

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoringError("codebert_manifest_unreadable") from exc
    if not isinstance(manifest, Mapping):
        raise ScoringError("codebert_manifest_not_object")
    if manifest.get("schema_version") != "backtranslation.codebert-artifacts.v1":
        raise ScoringError("codebert_manifest_schema_mismatch")
    if manifest.get("model_id") != CODEBERT_MODEL_ID:
        raise ScoringError("codebert_manifest_model_mismatch")
    if manifest.get("revision") != CODEBERT_REVISION:
        raise ScoringError("codebert_manifest_revision_mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ScoringError("codebert_manifest_files_invalid")

    verified: dict[str, dict[str, int | str]] = {}
    for entry in files:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "bytes", "sha256"}:
            raise ScoringError("codebert_manifest_file_entry_invalid")
        relative = entry["path"]
        byte_count = entry["bytes"]
        digest = entry["sha256"]
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 1
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ScoringError("codebert_manifest_file_entry_invalid")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ScoringError("codebert_manifest_file_entry_invalid") from exc
        if relative in verified:
            raise ScoringError("codebert_manifest_duplicate_file")
        path = snapshot_directory / relative
        try:
            if not path.is_file():
                raise ScoringError("codebert_snapshot_file_missing")
            actual_bytes = path.stat().st_size
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
        except ScoringError:
            raise
        except OSError as exc:
            raise ScoringError("codebert_snapshot_file_unreadable") from exc
        actual_digest = hasher.hexdigest()
        if actual_bytes != byte_count:
            raise ScoringError("codebert_snapshot_file_size_mismatch")
        if actual_digest != digest:
            raise ScoringError("codebert_snapshot_file_hash_mismatch")
        verified[relative] = {"bytes": actual_bytes, "sha256": actual_digest}
    if set(verified) != CODEBERT_REQUIRED_FILES:
        raise ScoringError("codebert_manifest_required_files_mismatch")
    return verified


def load_pinned_codebert(
    snapshot_directory: Path, manifest_path: Path
) -> tuple[Any, Any]:
    """Verify and load the frozen tokenizer/model without network access."""

    verify_codebert_snapshot(snapshot_directory, manifest_path)
    _require_distribution_version("torch", TORCH_RUNTIME_VERSION)
    _require_distribution_version("transformers", "4.57.6")
    _require_distribution_version("tokenizers", "0.22.2")
    try:
        import torch
        from transformers import RobertaModel, RobertaTokenizerFast
    except ImportError as exc:  # pragma: no cover - guarded by metadata checks
        raise ScoringError("codebert_runtime_import_failed") from exc

    try:
        # Construct the fast tokenizer from the two verified vocabulary files
        # explicitly.  This prevents an unmanifested tokenizer.json placed in
        # the directory from silently changing tokenization.
        tokenizer = RobertaTokenizerFast(
            vocab_file=str(snapshot_directory / "vocab.json"),
            merges_file=str(snapshot_directory / "merges.txt"),
            tokenizer_file=None,
            model_max_length=512,
        )
        model = RobertaModel.from_pretrained(
            snapshot_directory,
            local_files_only=True,
            use_safetensors=False,
            dtype=torch.float32,
        )
    except Exception as exc:
        raise ScoringError("codebert_local_load_failed") from exc

    config = model.config
    if (
        type(tokenizer).__name__ != "RobertaTokenizerFast"
        or not getattr(tokenizer, "is_fast", False)
        or len(tokenizer) != CODEBERT_EXPECTED_VOCAB_SIZE
        or getattr(tokenizer, "vocab_size", None) != CODEBERT_EXPECTED_VOCAB_SIZE
        or getattr(tokenizer, "model_max_length", None) != 512
        or getattr(tokenizer, "bos_token_id", None) != 0
        or getattr(tokenizer, "eos_token_id", None) != 2
        or tokenizer.num_special_tokens_to_add(pair=False) != 2
        or getattr(config, "model_type", None) != "roberta"
        or getattr(config, "vocab_size", None) != CODEBERT_EXPECTED_VOCAB_SIZE
        or getattr(config, "hidden_size", None) != CODEBERT_EXPECTED_HIDDEN_SIZE
        or getattr(config, "num_hidden_layers", None) != CODEBERT_EXPECTED_LAYERS
        or getattr(config, "max_position_embeddings", None) != 514
    ):
        raise ScoringError("codebert_checkpoint_contract_mismatch")
    configure_codebert_determinism(model, torch)
    return tokenizer, model
