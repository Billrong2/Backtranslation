# Expanded TSE understandability dataset recovery

This directory identifies and reconstructs the expanded dataset from Simone
Scalabrino, Gabriele Bavota, Christopher Vendome, Mario Linares-Vásquez, Denys
Poshyvanyk, and Rocco Oliveto, *Automatically Assessing Code
Understandability* (IEEE TSE, DOI `10.1109/TSE.2019.2901468`). It contains the
source-licensed 50 Java snippets and provenance metadata. It intentionally does
not redistribute the authors' human-response CSV or verification-question text
because the authoritative download page does not state a reuse license.

This recovery performs no association, correlation, or model-score analysis.

## Dataset identity and unit

The authoritative inputs are the [TSE replication page][dataset-page] and the
[TSE paper][paper]. The SHA-256-pinned raw CSV validates as follows:

- 444 participant–method evaluation rows (the unit in the authors' raw data);
- 63 distinct anonymous participants;
- 50 Java/Android methods, exactly five from each of ten projects;
- 44 methods with nine evaluations and six methods with eight evaluations;
- participant groups: 38 bachelor's students, nine master's students, three
  Ph.D. students, and 13 professional developers; and
- 128 raw columns, including method-constant `LOC` and the participant-level
  understandability variables.

The paper explains that each participant was assigned up to eight methods and
could browse related classes and the Web. `PBU` is the binary response to “I
understood” (`1`) versus “I cannot understand” (`0`). If PBU was positive, the
method was hidden and the participant answered three verification questions.
`AU` is the fraction correct, with possible values `0`, `1/3`, `2/3`, and `1`;
AU is set to zero when a participant selected “I cannot understand.” Thus AU is
demonstrated comprehension and PBU is perceived comprehension, not a substitute
for AU.

There are no missing AU, PBU, or LOC values. TNPU is missing in 135 rows, as
expected when a participant selected “I cannot understand.” The full nonzero
missingness inventory for the ratio metrics and TNPU is recorded in
[`provenance.json`](provenance.json).

The HTML replication page still contains stale prose describing 57
participants and 396 evaluations. The expanded paper, the downloaded CSV, and
the goal agree on 63 and 444; the hash-pinned CSV is therefore the data-version
authority. The earlier ASE dataset (324 evaluations from 46 participants) is
not used.

## Authoritative raw inputs

`tools/recover_tse_dataset.py` downloads these files only into the selected
cache and verifies their exact SHA-256 hashes:

| Input | SHA-256 |
| --- | --- |
| `RQ1/understandability.csv` | `77704e6a39ded74a4542d61aaf737432950905fe9b886a2dd822132f75395ca1` |
| `systems.csv` | `912a204ac80e72a0302aa52f6647a775df3ebe2d894d73ceb796109fdca7bb26` |
| `verification_questions.txt` | `43e8ce494dc171717a2e13e1526a0bc915d92020941a5a835fc43d873210dc56` |
| `RQ2/RQ2.zip` | `4dff1f0da9937a900255e9dc4f2e1cc57f615f6dc510c77c9d211840996804a2` |

All 50 dataset signatures occur exactly once as a header in the verification
question file. The question and answer text is not retained here.

## Exact source reconstruction

[`source_manifest.jsonl`](source_manifest.jsonl) maps every signature to:

- parent project URL and exact revision from `systems.csv`;
- source repository/revision (including the exact MyExpenses submodule
  gitlink for `StickyListHeadersListView`);
- repository-relative Java path and Git blob object ID;
- complete-file SHA-256;
- UTF-8 declaration/range and body-only offsets, body hash, one-based
  line/column range, and a revision-pinned Web URL;
- normalized parameter-type signature validation;
- extracted snippet and context SHA-256; and
- license expression, basis, and retained license/notice hashes.

The source range starts at an attached Javadoc when present, includes
annotations and the exact declaration/body, and ends at the matching closing
brace. A free-standing section comment before a Javadoc is not included. This
boundary rule is independently supported by a strong invariant: the physical
line count of every recovered range equals the dataset's constant `LOC` value
for that signature (50/50 exact matches). The method name, containing package
and class, and normalized parameter types also match every dataset signature.
No signature remains unresolved.

The stable, lexically ordered ID mapping is in
[`snippet_index.csv`](snippet_index.csv). The exact ranges are under
[`snippets/`](snippets/); complete third-party repositories are never retained
inside this artifact.

## Standardized context accepted for this protocol

Each file under [`contexts/`](contexts/) supplies the deterministic context
accepted by the study protocol. It contains:

1. the exact package declaration;
2. every exact source-file import declaration, in source order;
3. the exact enclosing type header through its opening brace; and
4. a path/hash reference to the separately stored selected method.

It uniformly excludes member stubs, including fields, initializers, sibling
methods, and invoked method/class source, rather than claiming unperformed
cross-project symbol resolution. It also excludes comments outside the selected
declaration, verification questions, and all human outcomes. The materialized
JSON predates protocol acceptance and retains the provenance label
`proposal_for_protocol_freeze`; that label is historical metadata, not an
unresolved design choice. The exact 50-file policy and hashes are accepted and
pinned by `config/freeze-spec.json` and `protocol/PROTOCOL.draft.md`. They still
cannot be used for outcome analysis until the complete protocol freeze passes.

## Licensing and retention

The authoritative TSE download page calls the raw data publicly available but
does not state a license. Public availability alone is not a redistribution
license. Its status is therefore machine-recorded as
`unresolved_no_license_statement_found_on_authoritative_download_page`. The raw
response CSV, question text, systems CSV, and RQ2 package remain reproducible,
hash-pinned cache inputs and are not copied into the artifact. Obtain permission
or a clear license before publishing those raw files or derived row-level
data.

The reconstructed source snippets are retained with their upstream licenses:

| Source | License recorded at the pinned revision |
| --- | --- |
| OpenCMS | LGPL-2.1-or-later |
| Jenkins | MIT, except the selected `AntClassLoader` method comes from an Apache-2.0 file |
| Spring Batch | Apache-2.0 |
| Hibernate ORM | LGPL-2.1-or-later |
| Weka | GPL-3.0-or-later |
| ANTLR 4 | BSD-3-Clause |
| Apache Phoenix | Apache-2.0 |
| MyExpenses | GPL-3.0-or-later, except the two `SyncAdapter` selections carry Apache-2.0 file headers |
| K-9 Mail | Apache-2.0 |
| Car Report | Apache-2.0 |
| StickyListHeaders submodule | Apache-2.0 |

Verbatim upstream license and notice files are retained under [`licenses/`](licenses/).
For file-level overrides, the relevant source-file license header is retained
verbatim under `licenses/file-headers/` and hashed in the manifest.
Copyleft and notice obligations continue to apply to any redistribution of the
snippets. This inventory is provenance documentation, not legal advice.

## Reproduction

Run from the project root with a cache outside the deliverable:

```sh
cache_dir=$(mktemp -d /tmp/tse-recovery-XXXXXX)
python3 tools/recover_tse_dataset.py download --cache-dir "$cache_dir"
python3 tools/recover_tse_dataset.py fetch-sources --cache-dir "$cache_dir"
python3 tools/recover_tse_dataset.py validate-raw --cache-dir "$cache_dir"
python3 tools/recover_tse_dataset.py materialize --cache-dir "$cache_dir" --output-dir data/tse
python3 tools/recover_tse_dataset.py validate-artifact --output-dir data/tse
```

The first two commands require network access and Git. `validate-artifact` is
offline and checks the retained manifest, snippet, context, and license hashes.

[dataset-page]: https://dibt-research.unimol.it/report/understandability-tse/
[paper]: https://sscalabrino.github.io/files/2019/TSE2019AutomaticallyAssessingCode.pdf
