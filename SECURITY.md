# Security

Do not commit API credentials, authentication files, raw provider responses,
participant-level outcome data, downloaded model weights, or local Codex state.

Pass the DeepSeek adapter a mode-0600 credential file using
--credential-path. Keep it outside the repository. If a credential is ever
committed, revoke it immediately and rewrite the Git history before publishing.

Security-sensitive output should be reported privately to the repository owner
rather than placed in a public issue.
