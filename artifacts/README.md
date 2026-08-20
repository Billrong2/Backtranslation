# Published aggregate artifacts

Only source-free aggregate outputs are committed:

- complete-case-120/: Denny/TSE per-run similarity scores and aggregate AU/PBU
  association results.
- codeup-human-agent/results.json: 915 paired case-level metrics and review
  metadata, without generated code or model responses.
- codeup-human-agent/before-after-results.json: 503 exact aligned human
  before/after revision cases plus the retained same-review agent arm, with
  source-free round-trip, CodeBERT/BLEU/ROUGE, code-derived intent, complexity,
  and smell metrics.
- codeup-human-agent/generation-summary.json and
  codeup-human-agent/intent-analysis/summary.json: terminal run counts.
- codeup-human-agent/before-after-generation-summary.json and
  codeup-human-agent/before-after-intent-summary.json: terminal counts for the
  aligned before/after extension.

Raw generated code, natural-language directions, provider event ledgers,
Codex homes, credentials, participant rows, and upstream dataset clones are
excluded from Git. They are not required to read or audit the final aggregate
reports, but are required for a byte-for-byte replay.
