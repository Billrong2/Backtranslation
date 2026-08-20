# CODE-UP human-versus-agent study

This pipeline builds a paired dataset from the 50% CODE-UP cohort. A case is
included only when the raw pull-request artifact contains a nonempty same-file
revision after the target inline review. The fixed cohort contains 915 cases;
285 sampled reviews are retained as exclusions.

For each case, the human arm is the recorded revision. The agent arm receives
only the review request, file path, pre-review diff context, and pre-review code
fragment; it never receives the human revision. DeepSeek V4 Flash is called
through a fresh `codex exec --ephemeral` process for the agent revision and for
each direction/reconstruction stage. Malformed responses retry from a new
attempt. Account/authentication failures stop the run and do not consume the
format-retry budget.

After all generation succeeds, DeepSeek V4 Pro extracts review-request intents
and independently evaluates human original, human reconstruction, agent
original, and agent reconstruction code. Every Pro call also uses a separate
ephemeral Codex process. The same prompts and validation apply to both arms.

The scoring stage computes paired CodeBERT, BLEU, and ROUGE similarities;
review-intent fidelity; intent counts; a transparent fragment CCN proxy; and
eleven identical code-smell/antipattern rules. It also retains review timing,
comment, reviewer, commit, reply, merge-record, and sparse CODE-UP RQ2--RQ6
metadata. ROC--AUC always defines human as the positive class and reports both
directional AUC and direction-free separation.

The runner is portable and resolves the repository from its own location:

```sh
sh tools/run_codeup_human_agent_goal.sh
```

It requires the Flash adapter at `127.0.0.1:8770` and the Pro adapter at
`127.0.0.1:8771`. Start each with
`tools/deepseek_responses_proxy.py`, passing a mode-0600 credential file
outside the repository and a private audit-log path. The checked-in Codex
profiles under `config/` are copied into ignored run directories
automatically. Set `CODEX_PATH` if the Codex CLI is not on `PATH`.
The fixed output paths are
`artifacts/codeup-human-agent/results.json`,
`reports/2026-08-20-codeup-human-vs-agent.md`, and
`reports/2026-08-20-codeup-human-vs-agent.tex`.

The August 20, 2026 production run is complete. It contains 915 selected valid
Flash generations (with 829 rejected attempts retained), 4,575 selected Pro
intent stages (with 479 rejected attempts retained), and 915 paired scored case
rows. The exact model-event ledgers contain 6,462 Flash events and 5,062 Pro
events. The final machine-readable result is
`artifacts/codeup-human-agent/results.json`; the dated Markdown and LaTeX
reports contain the aggregate tables, interpretation, retained metadata
coverage, and limitations.

The upstream CODE-UP checkout and raw pull-request cache are not distributed
from this repository. Set `CODEUP_DATASET_DIR` to an independently obtained
CODE-UP checkout when rebuilding a cohort and pass the predecessor stage cache
explicitly with `--stage-root`. Aggregate results and summaries are committed;
raw generated code, model responses, provider ledgers, and credentials are not.
