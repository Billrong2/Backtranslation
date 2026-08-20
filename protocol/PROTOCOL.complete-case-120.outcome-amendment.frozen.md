# Outcome-Loading Correction for the 120-Valid Analysis

Status: **FROZEN CONTENT. ASSOCIATION ANALYSIS REQUIRES A CANONICAL MANIFEST
AND APPROVAL OF ITS EXACT DIGEST.**

Date: **August 12, 2026**

After all 120 fidelity-score records were computed and frozen, the first load
of the exact TSE outcome file stopped because the loader assumed that
`(participant_id, method_id)` was unique. Inspection of the already pinned
444-row source file showed seven repeated participant–method pairs. Five pairs
repeat the same AU/PBU values and two pairs contain different recorded
responses. Adding participant group or project does not distinguish them.

The target TSE cohort is explicitly the authoritative 444 evaluations, 50
methods, 63 participants, and 10 projects. Dropping rows, choosing one response,
or averaging a pair before the normal method aggregation would invent an
unprespecified data-cleaning rule and would contradict the 444-evaluation
denominator. Therefore this correction retains all 444 source rows as distinct
recorded evaluations, assigns their source-order row index as an internal
evaluation identity, and performs the already frozen method-level arithmetic
aggregation over every row.

This correction does not change the 120-cell predictor cohort, any similarity
score, the method-level analysis unit, AU/PBU definitions, LOC adjustment, or
statistical procedures. The seven repeated pairs are disclosed as a dataset
limitation in both report formats.

