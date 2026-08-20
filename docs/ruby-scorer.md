# RUBY-Java paper-specification adaptation

`backtranslation.ruby_scoring` is an outcome-blind, independently implemented
Java adaptation of the metric design in Tran et al., *Does BLEU Score Work for
Code Migration?* (ICPC 2019; arXiv:1906.04903). It is not the authors' C#
implementation and is never described as official RUBY. The recovery and
identity evidence is in `ruby-audit.md`.

The immutable definition identifier is
`ruby-java-paper-specification-adaptation-v1`. Code 1 is the reference and Code
2 is the candidate. Every tier has range `[0,1]`; higher means more similar.
The scorer reads Java and generation artifacts only. It never reads AU or PBU.

## Paper rule and local availability

The paper selects the highest representation available for both code pieces:
GRS over PDGs, otherwise TRS over ASTs, otherwise STS over token sequences. The
adaptation preserves that selection structure while making every difference
explicit:

1. GRS is unavailable for every pair. Its exact reason is
   `no_authenticated_java_pdg_exas_pipeline`. Tree-sitter is not a PDG builder,
   and no AST/control-flow proxy is relabeled as Exas or GRS.
2. When both methods have an unambiguous, error-free Tree-sitter Java AST, the
   composite selects the independently specified `trs_adaptation` tier.
3. Otherwise the composite selects paper STS. STS is still computed and
   retained when the AST tier is available.

The composite is therefore a RUBY-style Java adaptation, not an exact
reproduction of the paper's empirical score.

## STS: exact published equation

Identifier: `ruby-paper-sts-token-levenshtein-v1`.

- Inputs are the common exact Java lexer-token arrays: strict UTF-8, NFC/LF,
  parser-span comment removal, case-sensitive spellings, and no identifier or
  literal normalization.
- Distance is unit-cost Levenshtein over tokens. Insertion, deletion, and
  substitution each cost 1; a token match costs 0.
- `STS = 1 - distance / max(reference_token_count, candidate_token_count)`.
- One empty stream against a nonempty stream scores 0. Two empty streams are
  rejected because the published normalization denominator would be zero.
- The artifact retains the distance, both token counts, and SHA-256 hashes of
  canonical JSON token arrays. It never retains source or tokens.

## AST representation

Identifier: `tree-sitter-java-named-ast-enriched-terminals-v1`.

The parser stack is the same pinned stack used by Java validation:

- `tree-sitter==0.25.2`;
- `tree-sitter-java==0.23.5`, grammar revision
  `94703d5a6bed02b98e438d7cad1136c01a60ba2c`;
- language ABI 14, 321 node kinds, and 1,385 parse states.

The represented root is the sole callable declaration inside the synthetic
validation wrapper; the wrapper itself never enters the tree. Child order is
source/grammar order. Every represented node label is canonical JSON containing:

- its Tree-sitter node type;
- the incoming Tree-sitter field name, or null at the root;
- exact UTF-8 spelling for a named leaf, otherwise null; and
- every direct anonymous non-comment terminal as
  `[syntax_position, node_type, exact_spelling]`.

Named child nodes are recursively represented in order. Comments are excluded.
An error/missing node, ambiguous callable, sibling member, enclosing type, or
missing body makes the candidate AST tier unavailable. Declaration mismatch by
itself does not suppress a syntactically complete candidate AST: the tree tier
measures the generated method that exists, while the artifact still binds it to
the expected case and exact Code-2 hash.

The complete nested representation is hashed as canonical JSON and is not
stored in per-run artifacts.

## Independent tree similarity

Identifier: `ruby-style-trs-java-ordered-ted-no-move-v1`; algorithm identifier:
`zhang-shasha-ordered-unit-cost-no-move-v1`.

The exact Zhang-Shasha ordered-tree dynamic program uses these costs:

- insert one node: 1;
- delete one node, promoting its children in ordered-tree semantics: 1;
- relabel one node: 0 for equal complete labels, otherwise 1.

There is no move operation. Reordering must be represented by the cheapest
allowed edit sequence. The original paper cites TREED and mentions move-aware
edits, but does not provide enough authenticated implementation detail to
reproduce that behavior. Consequently this value is always named a
`trs_adaptation`, never exact TRS or TREED.

The normalization follows the paper:

```text
score = 1 - ordered_tree_edit_distance /
            (reference_node_count + candidate_node_count)
```

Each tier record retains distance, both node counts, both representation
hashes, availability, and explicit unavailability reasons.

## Per-run artifact contract

`tools/score_ruby.py run` operates only after freeze authorization and writes
exactly one terminal RUBY artifact per generated run. It does not retry or
replace files.

Success file `ruby-fidelity.json`, schema
`backtranslation.ruby-fidelity.v1`, contains exactly:

```text
schema_version, definition, method_id, run_index,
freeze_manifest_sha256, code_1_sha256, code_2_sha256,
score, selected_tier, tiers, selection_reasons
```

`tiers` contains exactly `grs`, `trs_adaptation`, and `sts`. The composite
`score` must equal the selected tier's score.

Failure file `ruby-fidelity.failure.json`, schema
`backtranslation.ruby-fidelity-failure.v1`, contains exactly:

```text
schema_version, definition, method_id, run_index,
freeze_manifest_sha256, code_1_sha256, code_2_sha256, failure_code
```

Failures are missing measurements, never imputed zeros. Both terminal files
for one run are prohibited. The tool's `status` command reports generated,
complete, failed, and pending counts without opening human outcomes.

## Primary evidence

- Tran et al., ICPC 2019 paper: <https://arxiv.org/abs/1906.04903>
- DOI: <https://doi.org/10.1109/ICPC.2019.00034>
- Retrieved PDF SHA-256:
  `f9046a54278741014858b3334eafb7fee64b75ce34b40c0203d9679b88e3ce0f`
- Retrieved arXiv source SHA-256:
  `8d48df12d73f9448880ab250b16bcb6b236b5139ccd67b9b815884c14a3d06c2`

The paper evaluated Java-to-C# migration and does not validate this Java-to-NL-
to-Java transfer. The score is a structural fidelity proxy, not behavioral
equivalence, complexity, difficulty, or human understandability.
