# Goal Amendment: Analyze the 120 Valid Round Trips

Status: **FROZEN CONTENT — NO FURTHER PROVIDER GENERATION IS AUTHORIZED.**

Amendment date: 2026-08-12  
Base goal: `GOAL.md`  
Source generation authorization: canonical v0.5 digest
`b0cf3c04fdf53ef0df2d233637b98ee17086f0c8ce6314b3d9b10a7cb1d16996`

Before any generated fidelity score was associated with AU or PBU, the user
directed the pilot to stop pursuing the 150-success retry quota and analyze the
120 already valid v0.5 round trips. The v0.6 generation authorization remains
historical provenance but no v0.6 provider request was issued.

The analysis cohort is fixed by the 150-cell legacy inventory at
`artifacts/provenance/legacy-attempt-inventory-v0.5.json`, file SHA-256
`4172483486daabe839e7d74b1efa7def98d037099e6a398936ff5c287729ad4a`.
Exactly the 120 records whose frozen raw-evidence eligibility has
`eligible=true` are included. Failed or structurally invalid cells are not
assigned zero similarity and are not analysis rows.

The primary unit remains the method. Similarity is computed for every valid
cell, then averaged arithmetically within method across its available valid
runs. Thus a method with three valid runs receives the same weight as a method
with one valid run. The fixed cohort contains 49 methods: 28 with three valid
runs, 15 with two, and 6 with one. `tse-020` has no valid round trip and is
absent from the correlation denominator.

The requested result is the association between round-trip fidelity and human
understandability:

- primary predictor: method-mean RUBY-Java adaptation similarity;
- supporting predictors: method-mean CodeBERT cosine, ROUGE-1/2/L F1, and
  BLEU-4;
- primary outcome: method-mean AU;
- supporting outcome: method-mean PBU;
- report raw Spearman rho and LOC-controlled partial Spearman rho, with the
  frozen project-aware uncertainty and permutation procedures.

This is a validity-conditioned complete-case analysis, not the original run-0
confirmatory test and not the v0.6 150-success estimand. It has no pass/fail
gate. Selection bias from generation validity, unequal valid-run counts, and
the missing `tse-020` method must be reported prominently.

