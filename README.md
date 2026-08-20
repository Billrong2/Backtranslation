# Code backtranslation and understandability studies

This repository contains the code, aggregate results, and final reports for two
completed empirical studies of Java code backtranslation:

| Study | Cohort | Question | Final report |
|---|---:|---|---|
| Denny/TSE understandability | 50 source snippets; 49 with at least one valid round trip | Do Code1→NL→Code2 similarity scores correlate with Actual Understandability (AU) or Perceived Binary Understandability (PBU)? | [Markdown](reports/2026-08-12-denny-50-au-pbu-correlation.md) · [LaTeX](reports/2026-08-12-denny-50-au-pbu-correlation.tex) |
| CODE-UP human versus agent | 915 paired review requests | How do recorded human revisions and independently generated agent revisions differ before and after the same round trip? | [Markdown](reports/2026-08-20-codeup-human-vs-agent.md) · [LaTeX](reports/2026-08-20-codeup-human-vs-agent.tex) |

The earlier quota experiments and superseded CODE-UP stage reports have been
retired. GOAL.md remains only because its exact bytes are part of the frozen
Denny/TSE analysis manifest; the operative retained specifications are
GOAL.complete-case-120.frozen.md and the two complete-case protocols under
protocol/.

## Main results

- The Denny/TSE study found no meaningful AU or PBU correlation. The primary
  LOC-adjusted RUBY–AU estimate was ρ=0.025 with a 95% bootstrap interval of
  [-0.228, 0.276].
- In the CODE-UP study, round-trip similarity was high for human and agent code
  (mean CodeBERT 0.9950 and 0.9946), while mean intent-fidelity loss was small
  (0.0141 and 0.0085).
- Agent revisions were longer and had higher fragment-level complexity and
  smell counts. They also stayed much closer to the pre-review fragment, which
  can indicate conservative under-editing rather than better revisions.

Aggregate machine-readable outputs are under artifacts/. Raw model outputs,
provider ledgers, credentials, unlicensed participant outcomes, downloaded
models, and upstream dataset clones are deliberately excluded from Git.

## Repository layout

- src/backtranslation/: scoring, statistics, study execution, and validation
- tools/: command-line entry points for the two retained studies
- tests/: synthetic and contract tests
- data/tse/: the distributable 50-snippet source cohort and license evidence
- artifacts/: aggregate, source-free result JSON
- reports/: exactly the two final reports in Markdown and LaTeX
- protocol/: frozen complete-case analysis specifications and manifests
- config/: pinned parser, model, runtime, and complete-case freeze metadata

The retained `quota.py` and `quota_execution.py` modules are compatibility
code used by the complete-case verifier to authenticate the historical
generation inventory. The superseded quota workflow itself is not an active
study in this repository.

## Installation

Python 3.11 is required.

    python3.11 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e .

The optional DeepSeek Responses adapter additionally requires:

    .venv/bin/python -m pip install -r requirements-proxy.txt

CodeBERT is not committed because the required PyTorch checkpoint is about
499 MB. Download exactly revision
3b0952feddeffad0063f274080e3c23d75e7eb39 of microsoft/codebert-base into
models/codebert-base/. Required file hashes are recorded in
config/codebert-base-revision.json.

## Verification

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q

The archival runtime-lock check below is stricter than a normal dependency
check: it verifies the byte identity of the original interpreter and installed
distributions, so it is expected to pass only in that retained environment.

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tools/build_runtime_lock.py --check

To regenerate the CODE-UP aggregate report from the checked-in result JSON:

    PYTHONPATH=src .venv/bin/python tools/codeup_human_agent.py report

Generation requires an independently obtained CODE-UP artifact, a mode-0600
DeepSeek credential file, two loopback adapters, and the Codex CLI. Set
CODEUP_DATASET_DIR to the upstream CODE-UP checkout and CODEX_PATH when the CLI
is not on PATH. See
[the study documentation](docs/codeup-human-agent-study.md).

## Data and licensing

The TSE snippets retain their per-project licenses under data/tse/licenses/.
The raw TSE response data is not redistributed because its reuse license could
not be established. The CODE-UP checkout and pull-request source cache are also
not committed; obtain them from the upstream CODE-UP artifact and original
public repositories. No repository-wide license is asserted over third-party
data.

Never commit API keys, raw provider responses, participant-level outcomes, or
downloaded model weights. See [SECURITY.md](SECURITY.md).
