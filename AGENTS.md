# Phase commits

- After each phase passes its required checks and the user approves it, create a Git commit and push the phase branch to the configured GitHub remote before starting the next phase. The user authorized this workflow on 2026-10-23.
- Use a clear subject naming the phase and outcome, with a body describing the main changes, actual validation, and material limitations.
- Do not invent approvals or passing checks. Preserve the Genesis SPEC/INV terminology and evidence history.
- Review the staged diff and scan for credentials before committing. Keep private runtime configuration, secrets, generated indexes/dashboards, and raw run artifacts out of Git.
- Commit the implementation, tests, safe documentation, and canonical Genesis state together. Keep referenced local evidence available; do not imply ignored artifacts are included in the commit.
- Do not mix unrelated user changes into a phase commit. Use normal pushes; do not force-push, merge into the default branch, or change remotes without separate authorization.
