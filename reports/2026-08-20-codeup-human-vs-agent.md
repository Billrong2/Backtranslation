# CODE-UP Before/After Revision and Round-Trip Study

**Report date:** August 20, 2026<br>
**Aligned paired cases:** 503<br>
**Backtranslation model:** `deepseek-v4-flash` through separate Codex instances<br>
**Code-derived intent extraction/judging:** `deepseek-v4-pro` through separate Codex instances

## Study design

For each understandability-related CODE-UP revision, the old and new fragments come from the two sides of the same human-authored revision diff. The old human code is the **Before revision** arm; the new human code is the **Human revision** arm. The **Agent revision** arm is the retained independent model revision for the same review event, generated from CODE-UP's review-target context; that context is not generally byte-identical to the aligned old diff fragment. Each arm is separately translated from code to natural-language directions and back to code. The review comment is used to identify the revision but is not used as the intent reference. Instead, Pro extracts intent independently from each source fragment and judges only that source against its own reconstruction.

## Overall statistics

The largest numeric arm mean in each row is bolded; bold does not imply that a larger value is desirable for complexity, smell, size, or change measures.

- **Measure:** the code similarity, code-derived intent, size, complexity, smell, or round-trip-change statistic.
- **Before revision:** mean for the human-authored old side of the aligned revision diff.
- **Human revision:** mean for the human-authored new side of that same diff.
- **Agent revision:** mean for the retained model revision from the same CODE-UP review event; its review-target input scope usually differs from the exact old diff fragment.
- **Wilcoxon p:** paired two-sided Wilcoxon signed-rank p-value for Before revision versus Human revision; the Agent revision is not part of this column.
- **AUC:** descriptive one-variable AUC for separating Before revision (positive class) from Human revision. Values near 0.5 mean little separation; direction is visible from the means.
- **Separation:** `max(AUC, 1-AUC)`, an unsigned effect-separation summary from 0.5 to 1.0.
- **Code-smell pair:** in the `(A, B)` row, `A` is the mean smell count in the source fragment and `B` is the mean smell count after round-trip reconstruction. Its Wilcoxon, AUC, and separation values compare the per-case changes `B - A` between arms.
- **Cyclomatic-complexity pair:** in the `(A, B)` row, `A` is the mean source CCN proxy and `B` is the mean reconstructed CCN proxy. Its Wilcoxon, AUC, and separation values compare the per-case changes `B - A` between arms.

| Measure | Before revision | Human revision | Agent revision | Wilcoxon p | AUC | Separation |
|---|---:|---:|---:|---:|---:|---:|
| Round-trip CodeBERT similarity | 0.9905 | 0.9953 | **0.9955** | 3.41e-15 | 0.3503 | 0.6497 |
| Round-trip BLEU | 0.7505 | **0.8834** | 0.8783 | 1.14e-30 | 0.3020 | 0.6980 |
| Round-trip ROUGE-L F1 | 0.8361 | **0.9229** | 0.9147 | 7.36e-26 | 0.3143 | 0.6857 |
| Code-derived intent fidelity | 0.9156 | **0.9609** | 0.9561 | 3.01e-12 | 0.3752 | 0.6248 |
| Strict intent-preservation rate | 0.8735 | **0.9479** | 0.9380 | 2.16e-12 | 0.3875 | 0.6125 |
| Number of source-code intents | 11.1948 | 10.7356 | **13.7296** | 0.4090 | 0.5151 | 0.5151 |
| Intent-count change after round trip | -0.4453 | -0.3082 | **-0.2903** | 0.2812 | 0.5063 | 0.5063 |
| Cyclomatic-complexity proxy (source, round-trip) | (2.8111, 2.6640) | (2.4334, 2.3917) | (3.0179, 2.9185) | 0.2945 | 0.4828 | 0.5172 |
| Code-smell count (source, round-trip) | (0.7256, 0.7873) | (0.6899, 0.7734) | (1.0994, 1.2167) | 0.3953 | 0.4912 | 0.5088 |

## Human-versus-agent paired contrasts

This secondary table keeps the Human revision versus Agent revision inferential comparison separate from the main before-versus-after question. Here AUC treats Human revision as the positive class. Because the retained agent input uses CODE-UP's review-target scope rather than the exact old diff side, this is a same-event comparison with a known granularity limitation.
Bold marks a major difference under the report's display rule: `p < 0.05` and separation at least `0.55`.

| Measure | Wilcoxon p | AUC | Separation |
|---|---:|---:|---:|
| Round-trip CodeBERT similarity | 0.0275 | 0.4742 | 0.5258 |
| Round-trip BLEU | 0.8244 | 0.4962 | 0.5038 |
| Round-trip ROUGE-L F1 | 0.6279 | 0.5026 | 0.5026 |
| Code-derived intent fidelity | 0.2200 | 0.5347 | 0.5347 |
| Strict intent-preservation rate | 0.3603 | 0.5217 | 0.5217 |
| **Number of source-code intents** | **3.70e-10** | **0.4130** | **0.5870** |
| Intent-count change after round trip | 0.3426 | 0.4862 | 0.5138 |
| Cyclomatic-complexity proxy (source, round-trip) | 0.6132 | 0.5012 | 0.5012 |
| Code-smell count (source, round-trip) | 0.1623 | 0.4809 | 0.5191 |
