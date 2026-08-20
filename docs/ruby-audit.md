# RUBY identity and Java-adaptation audit

Status: **the authors' exact RUBY implementation is not publicly reproducible;
this study uses an explicitly named, independently specified Java adaptation.**

This audit was completed outcome-blind on 2026-08-12. No generated fidelity
score had been joined to AU or PBU when the metric panel was amended.

## Primary sources and immutable evidence

- Ngoc Tran, Hieu Tran, Son Nguyen, Hoan Nguyen, and Tien N. Nguyen,
  *Does BLEU Score Work for Code Migration?*, ICPC 2019,
  arXiv:1906.04903. The retrieved PDF has SHA-256
  `f9046a54278741014858b3334eafb7fee64b75ce34b40c0203d9679b88e3ce0f`;
  the arXiv source archive has SHA-256
  `8d48df12d73f9448880ab250b16bcb6b236b5139ccd67b9b815884c14a3d06c2`.
- Authors' experiment site repository
  `https://github.com/doubleblindBleu/studyBLEU` at commit
  `bc2ecd64890327cdd6d85c5ad9914551d7c225b9`.
- Authors' writing/data repository
  `https://github.com/miketran238/BleuScoreStudy` at commit
  `e5dad6979bd0b5c7a2d8f1a3bea8ce75bbaffef1`.

The two repositories contain examples and score spreadsheets, but no scorer
implementation and no declared repository license. All reachable historical
trees were inspected. The arXiv distribution license is not treated as a
software license.

## Published definition

For reference code `R` and translated/candidate code `T`, the paper defines:

```text
STS = 1 - token_Levenshtein(S_R, S_T) / max(length(S_R), length(S_T))
TRS = 1 - tree_edit_distance(AST_R, AST_T) / (size(AST_R) + size(AST_T))
GRS = 1 - graph_edit_distance(PDG_R, PDG_T) / (size(PDG_R) + size(PDG_T))

RUBY = GRS, if both PDGs are applicable;
       TRS, otherwise if both ASTs are applicable;
       STS, otherwise.
```

Tree edits are described as add, delete, replace, and move. Graph edits are
vertex/edge insert, delete, and substitute, with Exas used as an approximation.
Higher means more similar. The paper evaluated C# generated code against C#
reference code in Java-to-C# migration, not Java against Java. It reported
average correlation with human semantic scores of 0.775 for RUBY versus 0.583
for BLEU across its three systems. Those values validate that experiment, not
this study's construct or implementation.

## Why exact author RUBY cannot be claimed

The publication does not fix the lexer, AST labels/trivia and edit costs,
matching/tie rules, PDG construction and labels, semantic context, Exas
settings, or executable revisions. The cited TREED/LIBSYNC and Exas papers do
not supply an authenticated, directly runnable RUBY scorer. The published
spreadsheets are historical outputs rather than executable oracle fixtures;
some recorded GRS values even exceed 1 slightly, so undocumented clamping or
numeric behavior cannot safely be inferred.

This artifact therefore must never say “official RUBY,” “authors'
implementation,” or “exact reproduction.”

## Frozen adaptation boundary

The study's scorer is named **RUBY-Java paper-specification adaptation** and
uses its own versioned schema. It follows the published best-available-tier
idea while fully exposing the transfer and substitutions:

1. STS uses the already pinned, exact Java lexer-token arrays and unit-cost
   Levenshtein insertion, deletion, and substitution, normalized by maximum
   token count.
2. The syntactic component uses the frozen independent tree-sitter-Java AST
   projection and a completely specified edit algorithm. If it lacks the
   paper's move-aware TREED semantics, its artifact name is an independent AST
   similarity and not `TRS` or `TREED` without qualification.
3. GRS is unavailable for every pair with reason
   `no_authenticated_java_pdg_exas_pipeline`. Tree-sitter is not a PDG builder,
   and this cohort deliberately has no symbol-resolved context or reproducible
   historical dependency closure. No AST/control-flow approximation is
   relabeled as a PDG.
4. The composite selects the frozen AST component only when both projections
   are valid; otherwise it selects STS. Every result retains the selected tier,
   all computable component scores, and higher-tier unavailability reasons.

The Java-to-Java and backtranslation use is a construct transfer from the
paper's code-migration setting. RUBY-Java is a fidelity proxy, not proof of
behavioral equivalence, understandability, difficulty, or complexity. The
primary gate is defensible only as the explicitly amended pilot test in
GOAL.md, with CodeBERT, ROUGE, and BLEU retained as supporting comparisons.
