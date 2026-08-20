# Goal: Backtranslation as a Signal for Code Understandability

Status: **Approved and launched; metric panel amended outcome-blind on 2026-08-12.**

## Authoritative Inputs

- Primary plan: `Notes Perplexity, Code Complexity, and Program comprehension.pdf`
- Supplementary notes only: `suggestion.md`
- Authorized API credential: `feature_imp_key.txt`, subject to the security requirements below

If the PDF and supplementary notes conflict, follow the PDF and the confirmed decisions in this goal.

## Pre-outcome Metric Amendment (2026-08-12)

Before any generated fidelity score was joined to AU or PBU, the user directed
the study to try all of the discussed comparison metrics. The original use of
RUSE as the sole gate reflected a metric-name conflation: official RUSE is a
supervised English machine-translation evaluator, and its required public
end-to-end model assets cannot currently be authenticated and recovered. It
must be retained as a documented reproducibility attempt, not replaced with a
different score carrying the RUSE name.

The amended, prespecified metric panel is:

- **Primary code-aware fidelity test:** RUBY, using a completely frozen and
  transparently named implementation/reproduction of its lexical, AST, and PDG
  availability rules. The protocol must state exactly which representation
  levels are reproducibly available for this Java cohort and must not imply
  use of an unavailable authors' implementation.
- **Supporting fidelity tests:** CodeBERT cosine similarity, ROUGE-1/2/L, and
  segment-level BLEU-4, all computed from the same pinned Java tokenization.
- **Feasibility result only:** official RUSE. If its exact missing assets later
  become available before outcome analysis, it may be added as a prespecified
  sensitivity; otherwise report it as unavailable and never fabricate or
  substitute a score.

The existing run-0/LOC-controlled practical and statistical thresholds are
retained unchanged, with RUBY replacing RUSE in the fail-fast gate. Supporting
metrics cannot rescue or replace the RUBY result. This amendment itself must be
hashed with the frozen protocol before any predictor--outcome association is
computed.

## Generation-Success Quota Amendment (2026-08-12)

Before any generated score was computed or joined to AU or PBU, the user
required **150 successful round trips**, one for every fixed combination of 50
methods and three predeclared runs. A failed, malformed, schema-invalid, or
structurally unparsable generation is an attempted call, not a successful
study row, and therefore does not count toward 150.

Each fixed cell permanently selects its chronologically first full
Code 1 -> directions -> Code 2 attempt that passes the frozen raw-evidence,
schema, provider-provenance, and Java structural-validity predicate. A retry
restarts the whole pair with the unchanged prompt, model, context, and settings;
it cannot repair an output or choose among valid candidates using complexity,
RUBY, CodeBERT, ROUGE, BLEU, AU, PBU, or subjective quality. The 120 valid v0.5
round trips remain immutable attempt 1; the 30 invalid v0.5 cells are retained
as rejected attempt 1 and retried under a separately frozen v0.6 protocol.

Attempts are sequential within a cell and threaded only across distinct cells,
with at most five provider workers. Each cell is capped at 10 total attempts.
If any cell reaches that cap without a valid result, or if attempt provenance
cannot be proved, the entire study blocks and no partial-denominator score or
outcome analysis is authorized. This changes the estimand to fidelity
conditional on the frozen structural-validity predicate; all attempts and
failure classes must still be reported.

## Confirmed Study Decisions

1. Use the expanded **TSE version** of Scalabrino et al.'s *Automatically Assessing Code Understandability* dataset, not the earlier ASE version. The target version has 50 Java methods, 444 participant–method evaluations, and 63 participants.
2. Use **Actual Understandability (`AU`)** as the primary human outcome.
3. Include **Perceived Binary Understandability (`PBU`)** as a secondary measure of participants' perceived understanding or “confidence.” Do not substitute PBU for demonstrated comprehension.
4. Study both:
   - the complexity of the natural-language intent/implementation directions; and
   - the fidelity of the complete code round trip.
5. The central round trip is:

   ```text
   original code (Code 1) -> natural-language directions -> regenerated code (Code 2)
   ```

6. **RUBY similarity between Code 1 and Code 2 is the central code-aware fail-fast signal.** CodeBERT similarity, ROUGE, and BLEU are additional round-trip fidelity measures. Exact official RUSE remains a documented non-gating feasibility attempt because its required public assets are unavailable.
7. The backtranslation input may contain the complete selected method, its declaration/signature, and mechanically required type context. Apply the same context policy to every example and document it exactly.
8. Create and freeze an original backtranslation prompt informed by prior work; the unavailable Vijay extraction prompt is not required.
9. The terminology distinction between “intent” and “implementation directions” must not block the study. Define the chosen wording clearly and use it consistently.

## Objective and Research Questions

Determine whether a code-to-natural-language-to-code round trip provides a useful cognitive signal for human code understandability.

### RQ1: Round-trip fidelity

Does similarity between Code 1 and Code 2 correlate with independently measured actual human understandability (`AU`), after controlling for code size?

- Primary fidelity measure and fail-fast test: RUBY
- Supporting fidelity measures: CodeBERT similarity, ROUGE, and BLEU
- Expected interpretation: if more understandable code is more faithfully recoverable through natural-language backtranslation, higher round-trip similarity should be associated with higher AU.

### RQ2: Natural-language complexity

Does the complexity of the recovered natural-language directions correlate with `AU`, after controlling for code size?

- Primary transparent complexity measure: atomic instruction count
- Supporting measures: direction length, condition count/density, dependency-edge count, dependency depth, and other outcome-independent structural features fixed in the protocol

### RQ3: Perceived understanding or confidence

How do the RQ1 and RQ2 relationships compare with `PBU`, the participant's self-reported perceived understanding?

- Aggregate and report PBU separately as a secondary confidence/perception measure.
- Examine agreement and disagreement between AU and PBU.
- Do not use PBU to redefine or replace the primary AU result after seeing outcomes.

## Required Work

### 1. Recover and validate the TSE dataset

- Obtain the exact TSE replication artifacts from their authoritative source.
- Document the 50 Java methods, 10 source projects, pinned revisions, 444 participant–method evaluations, and 63 participants.
- Record the unit of analysis, participant groups, AU and PBU definitions/scales, code-size fields, missing values, provenance, license, and exclusions.
- Reconstruct or obtain the exact method bodies at the pinned revisions.
- Produce a validated manifest mapping each dataset signature to repository, revision, path, source range, source hash, and license.
- Do not silently mix the older 324-evaluation ASE data with the TSE data.

### 2. Review prior work and define the representation

- Review the plan's cited work, including AfterVibe, ARCTIC/code-review intent, intent/cognitive-debt work, PatchGen-style backtranslation, and related round-trip-translation studies.
- Design an original representation appropriate for code-only backtranslation.
- Represent literal, developer-facing directions sufficient to recreate the observed behavior without copying source syntax unnecessarily.
- Define an atomic instruction consistently enough to support instruction counting.
- Keep unsupported rationale or business purpose out of generated directions when it cannot be inferred from Code 1.

### 3. Freeze the protocol before correlation analysis

Before generated scores are compared with AU or PBU, freeze and hash a protocol that specifies:

- the exact dataset version and exclusions;
- method-level aggregation of participant AU and PBU;
- any participant-level sensitivity model;
- the Code 1 context policy;
- the directions schema and atomicity rules;
- extraction and regeneration prompts;
- model identifiers, parameters, and repeated-run policy;
- the designated primary run and treatment of stochastic repeats;
- RUBY definition/reproduction, representation-availability rules, parser/graph dependencies, inputs, preprocessing, and similarity calculation;
- the official RUSE reproducibility disposition, without a substitute score;
- CodeBERT checkpoint/revision, tokenization, chunking, layer, pooling, comparison unit, and similarity function;
- ROUGE and BLEU variants, normalization, smoothing, and comparison procedures;
- code-size controls, including at minimum LOC;
- statistical tests, effect sizes, uncertainty intervals, missing-data policy, and multiple-comparison handling;
- the exact practical/statistical threshold that counts as “no correlation” for the RUBY–AU fail-fast test; and
- all planned secondary and sensitivity analyses.

The protocol may inspect dataset structure and outcome definitions, but it must not inspect associations between generated predictors/fidelity scores and AU or PBU before it is frozen.

### 4. Implement an isolated round-trip pipeline

For each method and each predeclared run:

1. Provide Code 1 plus the allowed standardized context to the backtranslation request.
2. Generate and validate structured natural-language directions.
3. Start a separate regeneration request.
4. Give regeneration only the generated directions and the standardized declaration/type context required by the protocol.
5. Do not expose Code 1, the extraction conversation, hidden source text, human AU/PBU values, or fidelity results to regeneration.
6. Produce Code 2 and record its validation status.

Record model identifiers, prompts, parameters, run indices or seeds when supported, timing, token usage, outputs, hashes, and sanitized failures. Separate dataset extraction, API, infrastructure, schema, backtranslation, regeneration, parsing/compilation, and scoring failures.

### 5. Measure natural-language complexity

- Count atomic directions using the frozen schema.
- Compute the predeclared transparent textual and structural features.
- Test whether each feature merely reproduces LOC or another snippet-size measure.
- Keep any LLM-judge complexity assessment secondary.
- A knowledge graph may be explored later but must not block or redefine the pilot.

### 6. Measure round-trip fidelity

- Compute the pinned RUBY score between Code 1 and Code 2 for the central test, retaining the selected lexical/AST/PDG level for every pair.
- Compute the precisely specified CodeBERT similarity, ROUGE, and BLEU scores as supporting measures.
- Record the official RUSE attempt as unavailable unless the authenticated end-to-end assets are recovered; do not insert a placeholder or surrogate numerical value.
- Add parsing, compilation, or executable behavioral checks when the recovered projects make them feasible.
- Treat RUBY, CodeBERT, ROUGE, BLEU, and behavioral agreement as round-trip fidelity measures. They may be correlated with AU and PBU, but they are not natural-language complexity scores.

### 7. Analyze the pilot

- Use the 50 methods as the primary analysis units, with method-level mean AU as the primary outcome.
- Use method-level mean PBU as the secondary perceived-understanding/confidence outcome.
- Estimate the raw and LOC-controlled association between RUBY and AU first, exactly as frozen.
- Analyze CodeBERT–AU, ROUGE–AU, and BLEU–AU as supporting fidelity relationships.
- Analyze the predeclared natural-language complexity measures against AU.
- Repeat the corresponding secondary analyses with PBU and characterize AU–PBU disagreement.
- Report effect sizes, uncertainty intervals, predictor stability across runs, missingness, fidelity failures, and sensitivity analyses without replacing the designated primary result.

### 8. Apply the fail-fast decision

The pilot is a **no-go/fail-fast result** if the frozen primary analysis finds no credible RUBY–AU relationship between Code 1/Code 2 round-trip similarity and actual understandability, including after the predeclared LOC control.

“No credible relationship” must be operationalized in the frozen protocol before results are inspected; it cannot be chosen afterward based only on a p-value or changed to favor continuation.

- If the RUBY–AU criterion fails, stop expansion and deliver the null result, supporting CodeBERT/ROUGE/BLEU results, natural-language-complexity results, and error/threat analysis.
- If the RUBY–AU criterion passes, record the go decision before considering other datasets, models, code-review difficulty, patch difficulty, or broader experiments.
- A null result for one supporting metric must still be reported and must not be hidden by another favorable metric.

## Credential and Safety Requirements

- Use `feature_imp_key.txt` only for authorized API calls required by this approved study.
- Load the credential at runtime from the file. Never print, echo, quote, log, copy into source/configuration/reports, expose through command arguments or environment variables, or commit it.
- Validate safe ownership and permissions before use, redact diagnostics, retain no authorization headers, and use secret-safe temporary handling.
- Preserve all existing project files and unrelated user work.
- Do not contact authors or other people, publish artifacts, or create external resources without separate user authorization.

## Deliverables

Create a self-contained, reproducible artifact under `/home/xxr230000/backtranslation` containing:

- a secret-free setup and dependency lock;
- TSE dataset, provenance, and license documentation;
- a validated source-snippet manifest;
- the frozen protocol and protocol hash;
- original prompts and schemas;
- the isolated Code 1 -> directions -> Code 2 pipeline;
- tests for schema validation, isolation, scoring, statistics, and credential redaction;
- licensable generated directions and regenerated-code artifacts;
- machine-readable run, usage, timing, and failure records;
- RUBY, CodeBERT, ROUGE, BLEU, official-RUSE feasibility, natural-language-complexity, stability, and validation results;
- statistical tables and plots for AU and secondary PBU analyses;
- error analysis and threats to validity; and
- a final report with the evidence-based fail-fast/go decision.

## Completion Criteria

The goal is complete only when:

1. the exact 50-method TSE dataset and source snippets are documented and validated;
2. the full amended protocol, including the RUBY–AU fail-fast rule and all supporting metrics, was frozen before generated-score outcome correlations were computed;
3. the isolated Code 1 -> directions -> Code 2 pilot was executed reproducibly;
4. RUBY, CodeBERT, ROUGE, BLEU, and predeclared direction-complexity measures were produced as specified, and the official-RUSE feasibility result was reported without substitution;
5. the primary AU analysis, LOC-controlled analysis, and secondary PBU/confidence analysis were completed with uncertainty estimates;
6. the RUBY–AU fail-fast decision was applied without post-hoc substitution of another metric;
7. the artifact and tests were independently re-run successfully; and
8. a secret/prohibited-data scan confirmed that the credential and non-redistributable data were not leaked.
