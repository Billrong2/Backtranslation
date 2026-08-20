# Frozen Protocol: 120-Valid Complete-Case Correlation

Status: **FROZEN CONTENT. ASSOCIATION ANALYSIS REQUIRES A CANONICAL MANIFEST
AND INDEPENDENT APPROVAL OF ITS EXACT DIGEST.**

## 1. Timing and scope

This outcome-blind amendment was adopted on 2026-08-12 after generation
validity and failure counts were known, but before any generated fidelity score
was joined to AU or PBU. It supersedes only the v0.6 requirement to generate
150 successes and the base protocol's run-0/45-score confirmatory gate. No new
provider call is authorized. All metric definitions, Java tokenization,
CodeBERT checkpoint, TSE outcome definitions, and source identities remain as
previously frozen.

## 2. Fixed cohort

The sole cohort selector is the canonical legacy inventory
`artifacts/provenance/legacy-attempt-inventory-v0.5.json`, SHA-256
`4172483486daabe839e7d74b1efa7def98d037099e6a398936ff5c287729ad4a`.
Include exactly records satisfying all of the following:

1. `source_kind = legacy-v0.5` and `attempt_index = 1`;
2. `origin.protocol_sha256` equals the v0.5 canonical digest
   `b0cf3c04fdf53ef0df2d233637b98ee17086f0c8ce6314b3d9b10a7cb1d16996`;
3. every frozen predicate check is true; and
4. `eligible = true` and `failure = null`.

Reverify each retained raw-evidence snapshot immediately before scoring. The
fixed result is 120 cells: run 0 = 38, run 1 = 42, run 2 = 40. The method
coverage distribution is 28 methods with three valid runs, 15 with two, 6 with
one, and one (`tse-020`) with zero. Any mismatch fails closed before outcomes.

The 30 invalid cells remain failure diagnostics. They receive neither a zero
score nor an imputed score. Attempt count, provider usage, failure class,
latency, and score magnitude cannot alter cohort membership.

## 3. Frozen fidelity computation

For each of the 120 cells compare exact Code 1 with the exact retained Code 2:

- RUBY-Java paper-specification adaptation v1 is the primary score. Its
  selected reproducible tier is retained per cell. GRS remains unavailable;
- CodeBERT uses `microsoft/codebert-base` revision
  `3b0952feddeffad0063f274080e3c23d75e7eb39`, CPU float32 final-layer mean
  pooling and cosine similarity under the authenticated local checkpoint;
- ROUGE-1, ROUGE-2, and ROUGE-L use exact normalized Java token arrays,
  case-sensitive and without stemming; designated values are F1;
- BLEU uses `segment-bleu-4-exp-v1` on the same tokens, Code 1 reference and
  Code 2 candidate, with effective order and frozen exponential smoothing.

Official RUSE remains unavailable and has no number. A deterministic scoring
failure blocks that metric rather than causing regeneration or a zero score.
All score records are recomputed and byte-compared before outcome loading.

## 4. Method-level predictor aggregation

For metric `q` and method `m`, define

`Q_m = arithmetic_mean(q_mr for valid retained runs r)`.

The method, not the cell, is the inferential unit. No weighting by number of
valid runs, participant count, token count, project, or score uncertainty is
used. The primary analysis denominator is exactly the 49 methods with at least
one valid round trip. The valid-run count is retained as a descriptive field
and is not an outcome predictor.

## 5. Outcomes and associations

Load the exact hash-pinned expanded TSE file only after authorization and score
recomputation. Validate 444 evaluations, 50 methods, 10 projects, and 63
participants. Aggregate AU and PBU exactly as in the base protocol. Join the 49
method predictors to their method outcomes by frozen method ID.

For each metric, report against AU and separately PBU:

1. raw Spearman rho with average ranks;
2. dataset-LOC-controlled partial Spearman on independently average-ranked
   predictor, outcome, and LOC;
3. method-within-project percentile bootstrap 95% intervals (10,000
   replicates), plus the frozen whole-project sensitivity interval;
4. deterministic within-project Freedman-Lane tests (100,000 replicates),
   reporting two-sided p-values; and
5. sample size, valid-run-count distribution, and missing method IDs.

RUBY--AU is primary only in ordering and interpretation; this amended analysis
has no GO/NO-GO threshold. The five supporting AU p-values are Holm-adjusted as
one family. The six PBU p-values, including RUBY, form a separate Holm family.
Raw and adjusted values are both retained. No favorable supporting metric may
replace an unfavorable RUBY result.

## 6. Required interpretation

Report this as a complete-case association conditional on successful Java
round-trip generation. Explicitly state that one method is absent and that
validity may be informative. Do not generalize the 49-method estimate to
unconditional model generation fidelity without this limitation. Do not claim
that correlation establishes causation or that RUBY is the authors' exact
implementation.

