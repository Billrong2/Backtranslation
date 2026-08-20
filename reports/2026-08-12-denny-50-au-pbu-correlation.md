# Denny 50-Snippet Round-Trip Similarity and Understandability

**Report date:** August 12, 2026  
**Source cohort:** 50 Denny/TSE code snippets; 49 methods have at least one complete valid round trip

## Executive result

The LOC-adjusted RUBY–AU association was positive (partial Spearman ρ=0.025, 95% bootstrap CI [-0.228, 0.276], two-sided within-project permutation p=0.857). The interval includes zero.

This analysis is descriptive and validity-conditioned; it has no GO/NO-GO gate.

## How similar Code1 and Code2 are

Scores closer to 1 mean Code2 is more like Code1. Each row summarizes 49 method-level values. If a method has one valid run, that run is used; if it has two or three, those valid runs are averaged first.

| Metric | Mean similarity | Median | SD | Minimum | Maximum |
|---|---:|---:|---:|---:|---:|
| RUBY-Java | 0.862 | 0.880 | 0.125 | 0.345 | 0.998 |
| CodeBERT | 0.998 | 0.999 | 0.005 | 0.976 | 1.000 |
| ROUGE-1 | 0.906 | 0.937 | 0.137 | 0.176 | 1.000 |
| ROUGE-2 | 0.852 | 0.896 | 0.171 | 0.088 | 1.000 |
| ROUGE-L | 0.866 | 0.898 | 0.144 | 0.166 | 0.998 |
| BLEU-4 | 0.800 | 0.842 | 0.185 | 0.050 | 0.998 |

## Actual Understandability (AU)

| Metric | Raw ρ | Raw p | LOC-adjusted ρ | Bootstrap 95% CI | Permutation p | Holm p |
|---|---:|---:|---:|---:|---:|---:|
| RUBY-Java | 0.028 | 0.847 | 0.025 | [-0.228, 0.276] | 0.857 | NA |
| CodeBERT | 0.123 | 0.398 | 0.134 | [-0.124, 0.375] | 0.357 | 1.000 |
| ROUGE-1 | 0.062 | 0.673 | 0.061 | [-0.215, 0.334] | 0.659 | 1.000 |
| ROUGE-2 | 0.032 | 0.827 | 0.032 | [-0.247, 0.310] | 0.827 | 1.000 |
| ROUGE-L | 0.048 | 0.746 | 0.043 | [-0.215, 0.302] | 0.756 | 1.000 |
| BLEU-4 | 0.037 | 0.802 | 0.036 | [-0.236, 0.304] | 0.800 | 1.000 |

RUBY-Java is the primary ordered result. Holm adjustment applies to the five supporting AU metrics, not RUBY.

## Perceived Understandability (PBU)

| Metric | Raw ρ | Raw p | LOC-adjusted ρ | Bootstrap 95% CI | Permutation p | Holm p |
|---|---:|---:|---:|---:|---:|---:|
| RUBY-Java | 0.042 | 0.773 | 0.037 | [-0.229, 0.280] | 0.783 | 1.000 |
| CodeBERT | 0.064 | 0.660 | 0.083 | [-0.185, 0.339] | 0.605 | 1.000 |
| ROUGE-1 | 0.022 | 0.882 | 0.019 | [-0.251, 0.280] | 0.885 | 1.000 |
| ROUGE-2 | -0.012 | 0.934 | -0.013 | [-0.284, 0.247] | 0.929 | 1.000 |
| ROUGE-L | 0.074 | 0.613 | 0.064 | [-0.205, 0.311] | 0.622 | 1.000 |
| BLEU-4 | -0.006 | 0.965 | -0.008 | [-0.273, 0.241] | 0.954 | 1.000 |

The six PBU metrics form a separate Holm-adjusted supporting family.

## Reproducibility

- Analysis manifest: `user-directed-results-2026-08-12`
- Score manifest: `63be0044a56484c6021e8d995d28a94994c3338ab4afa1e911535e38692ab5ac`
- Machine-readable analysis: `artifacts/complete-case-120/results-2026-08-12/analysis.json`
- Frozen protocols: `protocol/PROTOCOL.complete-case-120.frozen.md` and `protocol/PROTOCOL.complete-case-120.outcome-amendment.frozen.md`
