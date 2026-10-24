# Phase commits

- After each phase passes its required checks and the user approves it, create a Git commit and push the phase branch to the configured GitHub remote before starting the next phase. The user authorized this workflow on 2026-10-23.
- Use a clear subject naming the phase and outcome, with a body describing the main changes, actual validation, and material limitations.
- Do not invent approvals or passing checks. Preserve the Genesis SPEC/INV terminology and evidence history.
- Review the staged diff and scan for credentials before committing. Keep private runtime configuration, secrets, generated indexes/dashboards, and raw run artifacts out of Git.
- Commit the implementation, tests, safe documentation, and canonical Genesis state together. Keep referenced local evidence available; do not imply ignored artifacts are included in the commit.
- Do not mix unrelated user changes into a phase commit. Use normal pushes; do not force-push, merge into the default branch, or change remotes without separate authorization.

# Model selection

- User instruction on 2026-10-24: consider Astra only for complex tasks, and obtain the user's explicit confirmation before each Astra use. Complexity alone is not permission. This supersedes earlier unrestricted requirement-based Astra selection; existing reviews remain historical.
- Use a suitable non-Astra model for routine implementation and verification. Preserve maker/checker separation and the existing fresh-context behavioral verification requirements. Never silently substitute Astra after a capacity error.

# Manual test scenarios

- Maintain [docs/MANUAL_TEST_SCENARIOS.md](docs/MANUAL_TEST_SCENARIOS.md) as the user's cumulative application testing guide.
- After every completed phase, add or update its scenarios before the phase commit/push. Include setup, user/account, exact input or UI steps, expected facts/status, failure conditions, cleanup and current limitations. Also add reviewable task scenarios when a phase is still in progress, clearly labeled pending approval.
- Keep stable scenario IDs and preserve older scenarios. Check current synthetic demo data before documenting exact values, dates or IDs; distinguish the normal demo from isolated automated fixtures. Keep passwords, tokens and private DSNs out of the guide.
- Expected results are not execution evidence. Leave new manual run results as Not run until actually observed; record failures and blocked runs honestly. Reference automated evidence separately. Do not label a partial phase complete.
