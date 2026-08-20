# CODE-UP Human-versus-Agent Revision and Round-Trip Study

**Report date:** August 20, 2026  
**Paired cases:** 915  
**Human arm:** recorded same-file revision after the review request  
**Agent arm:** independent revision from the same review request and pre-review context  
**Generation/backtranslation:** `deepseek-v4-flash` through separate Codex instances  
**Intent extraction/judging:** `deepseek-v4-pro` through separate Codex instances

## Overall paired results

AUC treats human code as the positive class. Separation is `max(AUC, 1-AUC)`; the direction column shows which arm tends to have larger values.

| Measure | Human mean | Agent mean | Human − agent | Wilcoxon p | AUC | Separation | Higher |
|---|---:|---:|---:|---:|---:|---:|---|
| Round-trip CodeBERT similarity | 0.9950 | 0.9946 | 0.0004 | 0.0067 | 0.4705 | 0.5295 | agent |
| Round-trip BLEU | 0.8820 | 0.8804 | 0.0016 | 0.5927 | 0.4912 | 0.5088 | agent |
| Round-trip ROUGE-L F1 | 0.9225 | 0.9150 | 0.0075 | 0.5536 | 0.4988 | 0.5012 | agent |
| Review-intent fidelity, original | 0.2958 | 0.3331 | -0.0373 | 0.0077 | 0.4675 | 0.5325 | agent |
| Review-intent fidelity, round-trip | 0.2817 | 0.3246 | -0.0429 | 0.0023 | 0.4646 | 0.5354 | agent |
| Number of code intents, original | 4.4481 | 4.5617 | -0.1137 | 0.4325 | 0.5124 | 0.5124 | human |
| Cyclomatic-complexity proxy, original | 2.4929 | 3.0492 | -0.5563 | 1.63e-10 | 0.4432 | 0.5568 | agent |
| Code-smell/antipattern count, original | 0.7311 | 1.1508 | -0.4197 | 2.06e-27 | 0.3895 | 0.6105 | agent |
| Token count, original | 112.9563 | 195.1891 | -82.2328 | 2.58e-27 | 0.3886 | 0.6114 | agent |
| Pre-review→revision CodeBERT similarity | 0.9632 | 0.9935 | -0.0303 | 5.73e-120 | 0.0959 | 0.9041 | agent |
| Pre-review→revision BLEU | 0.2902 | 0.8219 | -0.5317 | 9.89e-144 | 0.0677 | 0.9323 | agent |
| Pre-review→revision ROUGE-L F1 | 0.4360 | 0.8904 | -0.4544 | 8.29e-144 | 0.0563 | 0.9437 | agent |
| Intent-count change after round trip | -0.1803 | -0.1989 | 0.0186 | 0.5968 | 0.4942 | 0.5058 | agent |
| CCN-proxy change after round trip | -0.0481 | -0.0929 | 0.0448 | 0.4726 | 0.4999 | 0.5001 | agent |
| Smell-count change after round trip | 0.0874 | 0.0940 | -0.0066 | 0.6093 | 0.4934 | 0.5066 | agent |
| Intent-fidelity loss after round trip | 0.0141 | 0.0085 | 0.0055 | 0.5472 | 0.5040 | 0.5040 | human |
| Strict-preservation loss after round trip | 0.0270 | 0.0171 | 0.0100 | 0.6875 | 0.5078 | 0.5078 | human |
| Changed-intent-rate change after round trip | 0.0009 | 0.0194 | -0.0185 | 0.2457 | 0.4942 | 0.5058 | agent |
| Lost-intent-rate change after round trip | -0.0279 | -0.0364 | 0.0085 | 0.4812 | 0.5037 | 0.5037 | human |
| Added-intent-rate change after round trip | 0.0095 | 0.0031 | 0.0064 | 0.7592 | 0.5071 | 0.5071 | human |

## Direct similarity of human and agent revisions

These values compare the independently written human and agent revisions to each other, not either arm to its reconstruction.

| Similarity | Mean | Median | 95% CI of mean |
|---|---:|---:|---:|
| CodeBERT | 0.9640 | 0.9762 | [0.9616, 0.9664] |
| BLEU | 0.3206 | 0.2653 | [0.3050, 0.3362] |
| ROUGE-1 F1 | 0.5126 | 0.5292 | [0.4971, 0.5281] |
| ROUGE-2 F1 | 0.4400 | 0.4211 | [0.4241, 0.4559] |
| ROUGE-L F1 | 0.4535 | 0.4359 | [0.4378, 0.4691] |

## Main findings

- **Round-trip reconstruction is highly similar for both arms.** Mean CodeBERT is 0.9950 for human code and 0.9946 for agent code; mean BLEU is 0.8820 and 0.8804, respectively.
- **The round trip does not produce large intent drift in this cohort.** Mean intent-fidelity loss is 0.0141 for human revisions and 0.0085 for agent revisions.
- **Absolute review-intent fidelity is low in both extracted code fragments.** Before round trip it is 0.2958 for human and 0.3331 for agent revisions. This is distinct from round-trip drift and should not be described as intent lost by backtranslation.
- **Agent revisions are longer and trigger more structural heuristics.** Mean token count is 112.9563 versus 195.1891; mean CCN proxy is 2.4929 versus 3.0492; and mean smell/antipattern count is 0.7311 versus 1.1508.
- **Agent revisions stay much closer to the pre-review fragment.** Mean pre-review-to-revision CodeBERT is 0.9632 for human revisions and 0.9935 for agent revisions. This may reflect conservative under-editing as well as fragment/extraction differences, so it is not automatically evidence of better revisions.

## Interpretation

CodeBERT, BLEU, and ROUGE quantify textual/representation similarity between each original revision and its reconstruction. The Pro intent measures separately test whether the pre-revision review request remains implemented before and after round-trip translation. A high code-similarity score therefore does not, by itself, prove intent preservation.

## Review metadata retained

Each machine-readable case row retains PR-open-to-review hours, review-to-next-commit hours when available, inline-review comment count, distinct inline reviewers, commit/force-push count, target-thread replies, merge-record presence, and sparse CODE-UP RQ2–RQ6 labels.

| Metadata field | Available | Missing | Mean | Median |
|---|---:|---:|---:|---:|
| commit and force push count | 915 | 0 | 15.5607 | 9.0000 |
| distinct inline reviewer count | 915 | 0 | 1.7410 | 1.0000 |
| hours pr open to target review | 915 | 0 | 264.8231 | 48.0778 |
| hours target review to next commit | 915 | 0 | 66.1442 | 8.0792 |
| inline review comment count | 915 | 0 | 14.8699 | 8.0000 |
| merge commit recorded | 915 | 0 | 0.4350 | 0.0000 |
| target review reply count | 915 | 0 | 0.8546 | 1.0000 |

Sparse CODE-UP RQ2/RQ3 labels are available for 188 cases; most RQ4–RQ6 fields are available for 176 cases.

## Definitions and limitations

- Review comments are pre-revision specifications, not issue reports that predate the original code.
- CCN is a fragment-level token proxy because many CODE-UP scopes are incomplete Java fragments.
- Smells are transparent uniform heuristics, not whole-project PMD/SonarQube executions.
- The 915 cases exclude 285 sampled reviews without a recoverable nonempty same-file revision.
- The agent never receives the recorded human revision.
- Both arms use identical backtranslation prompts, similarity implementations, intent judge, CCN proxy, and smell rules.
- Human revisions aggregate recoverable same-file changed fragments, whereas the agent responds to the target review fragment; this granularity difference can affect revision-from-pre-review similarity.
- The p-values are exploratory and unadjusted across metrics. AUC is a descriptive one-variable separation statistic, not held-out predictive performance.
- Full case-level values and provenance are in `artifacts/codeup-human-agent/results.json`.

