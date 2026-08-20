# Supporting Fidelity Scorers

These implementations are outcome-blind supporting metrics. They never read AU
or PBU. CodeBERT cosine, ROUGE, and BLEU do not substitute for the primary
RUBY-Java paper-specification adaptation. Official RUSE remains an unavailable,
nonnumeric, non-gating feasibility result and is not implemented here.

## Shared scoring input

`backtranslation.scoring` accepts a nonempty sequence of exact Java lexer token
spellings, not raw Java and not a whitespace-split string. Upstream code must
perform the protocol's strict UTF-8 decoding, NFC and LF normalization, parser-
span comment removal, and pinned Java lexing. The scorer rejects non-NFC tokens,
CR line endings, NULs, empty tokens, and lexer whitespace tokens. It retains
whitespace inside a Java string or text-block token.

For CodeBERT only, the exact normalized method view is formed by joining token
spellings with one ASCII space. For ROUGE and BLEU, the original token array is
retained so that an embedded space cannot create a false token boundary. Code 1
is always the reference and Code 2 is always the candidate.

## CodeBERT cosine

- Checkpoint: `microsoft/codebert-base`
- Immutable revision: `3b0952feddeffad0063f274080e3c23d75e7eb39`
- Artifact sizes and SHA-256 values: `../config/codebert-base-revision.json`
- Runtime: Python 3.11, PyTorch dependency pin 2.9.1 (installed CPU build
  `2.9.1+cpu`), Transformers 4.57.6, Tokenizers 0.22.2
- Device/dtype: CPU transformer inference, float32 parameters and hidden states
- Determinism: evaluation mode, gradients disabled, deterministic algorithms
  required, one CPU thread, float32 matmul precision `highest`
- Tokenization: the matching fast RoBERTa byte-BPE tokenizer, vocabulary size
  50,265, no vocabulary additions, no padding, no truncation
- Chunking: sequential, nonoverlapping groups of at most 510 content subtokens;
  each group is wrapped as `<s> content </s>`
- Representation: final transformer layer only; exclude both special-token
  positions; compute each chunk's content-state mean in float64, then weight
  chunk means by their content-subtoken counts to obtain the full-method vector
- Comparison: float64 cosine of the two full-method vectors, with no centering,
  calibration, or score rescaling

`verify_codebert_snapshot` checks all six required files before Transformers can
deserialize the pinned PyTorch weights. `load_pinned_codebert` is offline-only
and also verifies the tokenizer and model architecture invariants. A failure or
empty stream is missing score data, never an imputed zero.

The manifest also contains a small Java-token reference fixture. On the pinned
CPU runtime its cosine is `0.998540899304726`; the frozen cross-run absolute
tolerance is `1e-6`. The integration test executes it whenever the local model
directory is present and otherwise reports an explicit skip rather than
downloading during tests.

Download the immutable artifacts without copying them into Git:

```sh
.venv/bin/hf download microsoft/codebert-base \
  config.json merges.txt pytorch_model.bin special_tokens_map.json \
  tokenizer_config.json vocab.json \
  --revision 3b0952feddeffad0063f274080e3c23d75e7eb39 \
  --local-dir models/codebert-base
```

Then load them with:

```python
from pathlib import Path
from backtranslation.scoring import load_pinned_codebert

tokenizer, model = load_pinned_codebert(
    Path("models/codebert-base"),
    Path("config/codebert-base-revision.json"),
)
```

## ROUGE

- Implementation: `rouge-score==0.1.2`
- Tokenizer: custom exact-token carrier; no lowercase conversion, stemming,
  punctuation filtering, whitespace splitting, or sentence splitting
- Metrics retained: ROUGE-1, ROUGE-2, and ROUGE-L precision, recall, and F1
- Designated supporting statistic: ROUGE-L F1

The exact token array is serialized only as a lossless internal JSON carrier to
the package's custom-tokenizer API. It is decoded back to the same array before
the package computes n-gram and longest-common-subsequence overlap. ROUGE
precision uses the Code-2/candidate denominator and recall uses the
Code-1/reference denominator.

## BLEU

Definition identifier: `segment-bleu-4-exp-v1`. The implementation is local
and dependency-free so every convention is visible and versioned with the
source rather than inherited from a package default.

- Unit of evaluation: one Code-1/Code-2 method pair. It is segment BLEU, never
  corpus BLEU and never a corpus-pooled statistic.
- Orientation: Code 1 is the sole reference and Code 2 is the candidate. BLEU
  is therefore not assumed to be symmetric.
- Tokenization: exact, case-sensitive normalized Java lexer tokens; no
  lowercasing, stemming, punctuation filtering, or further splitting.
- N-grams: orders 1 through 4 with candidate counts clipped to the corresponding
  reference counts (single-reference modified precision).
- Short candidates: use effective order `min(4, candidate_token_count)` and
  weight every included log precision equally. Orders that cannot exist are not
  assigned fabricated counts.
- Smoothing: if no unigram matches, return exact zero. Otherwise walk included
  orders from 1 upward. For the `k`th order having zero matches, use modified
  precision `1 / (2^k * candidate_ngram_count)`; leave positive-match
  precisions unchanged.
- Brevity penalty: `exp(min(0, 1 - reference_length / candidate_length))`.
- Score: brevity penalty times the geometric mean of included modified
  precisions, represented on `[0, 1]` rather than a 0-to-100 percentage scale.

The artifact-ready result also retains reference and candidate token lengths,
the effective order, brevity penalty, each modified precision, and the matched
and candidate n-gram counts. Empty or noncanonical streams are scoring failures,
not imputed zeros. Per-method BLEU values may later be associated with outcomes
only after protocol authorization; this scorer itself never reads outcomes.

The expanded per-run panel is serialized as
`backtranslation.supporting_fidelity.v2`; version 2 makes the BLEU record
mandatory. Its terminal failure schema and the scoring tool's status/invocation
schemas are likewise version 2. Version-1 supporting artifacts are rejected
rather than interpreted under the expanded contract.
