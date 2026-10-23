# P01 implementation and independent verification

P01 is approved and closed. The user explicitly stated "P01 approved". Native
Genesis recorded human approval and completed T01.1, T01.2 and T01.3 in dependency
order. The separate verifier reran the successor native gates: seven roles/RLS
checks and fifteen privacy/readiness checks passed on the unchanged source.
T01.1's eighteen authentication checks already had a current native proof.
See [the closure record](P01_CLOSURE.json). P02 has not started.

## What changed

- SPEC01 / INV01, INV24, INV33: Authlib OIDC code flow with PKCE S256, one-use
  browser-bound state, nonce/signature/issuer/audience/time checks, provisioned
  issuer/subject mapping, opaque Redis sessions, atomic rotation, logout,
  real-clock expiry, CSRF and allowed Origin. Banking disables legacy routes
  and client-selected identity. Browser working state and delayed responses
  are invalidated across logout/customer switching.
- SPEC02 / INV02, INV03: separate runtime/auth/worker/dispatcher/migrator roles;
  owned conversation roots and composite owner foreign keys; forced RLS;
  transaction-local server context; session revalidation after pool waits;
  separate read-only worker pool and an actual worker-process probe. Dispatcher
  has no customer-data access. Runtime cannot rewrite audit.
- SPEC12 / INV26, INV27: safe error envelopes, metadata-only application/library
  logs, bounded metrics and OTel export, cross-process trace links without
  authorization authority, secret-canary scans, atomic write/audit rollback,
  bounded access-queue failure counters, Prometheus rule evaluation, and
  dependency readiness distinct from liveness.

Entry points: `auth/`, `api/foundation.py`, `api/privacy.py`, `store/banking.py`,
`store/banking_migrations/`, `jobs/banking_access.py` and `jobs/banking_probe.py`.
The UI is `frontend/components/BankingView.tsx`. See the
[launch/threat-model runbook](../docs/P01_RUNBOOK.md).

## Actual verification

The maker implemented code; a separate `p01_security_verifier` agent reviewed it,
returned findings, and executed final checks without implementation edits.

| Check | Observed result |
|---|---|
| Native Genesis T01.1 executable proof | 18 auth checks passed; current source/config receipt retained |
| Full independent P01 acceptance | 40 passed, 0 failures/errors/skips; 52 dependency deprecation warnings |
| Container connection identity | PostgreSQL 16.15 and Redis 7.4.11 host identities matched values read inside exact containers; ports and identities stable before/after |
| Signed issuer | Actual Keycloak 26.8.0 login/code exchange over backend certificate-verified HTTPS |
| Real browser | A login/owner create/logout, B login/customer switch, delayed A response rejected from B's UI |
| Frontend type checking | Passed independently |
| Earlier offline regression subset | 98 passed before final focused P01 refinements; no full-suite claim |
| Evidence integrity/privacy | Current selected source and artifact hashes verified; actual test credentials absent from scanned evidence |

Review [the independent assessment](../evidence/security/p01-independent-01/independent-review.md),
[versioned run report](../evidence/security/p01-independent-01/report.json),
[test output](../evidence/security/p01-independent-01/checks.txt),
[actual worker trace](../evidence/security/p01-independent-01/observations/worker-trace.json),
[canary sink scan](../evidence/security/p01-independent-01/observations/sink-scan.json),
and [browser result](../output/playwright/p01-independent/browser-result-02.txt).
The native receipt is referenced in `project.json` and `evidence-ledger.json`.

An earlier native attempt was rejected as source-mutated during final maker
refinements and is preserved. The first browser switch probe omitted Keycloak's
remembered-user Restart-login UI branch; it is retained as a harness failure.
Neither is treated as passing evidence. Initial local issuer setup was corrected
from HTTP to certificate-verified HTTPS after real secure-cookie tests failed.

## Runtime and claim boundaries

The banking UI runs at `https://localhost:3443`; readiness returned 200 and the
legacy `/chat` endpoint returned 404 after restarting the owned API on final code.
Local TLS is self-signed. Synthetic sign-in details are in ignored
`.genesis/local/p01/config.json`; the provided `infra/.env` was preserved.

This verifies isolated fictional customers and the enabled application sinks.
It does not certify a deployed issuer, external reverse-proxy/exporter logs,
delivery of live operator alerts, all legacy/provider-dependent tests or live
model output. The bounded queue is not a durable P02 worker. Conversation roots
do not implement bank products, durable turns, preferences or support actions.
P00's original live incident remains not-reproduced. P02 was not started.

The concrete code and retained results were explicitly approved by the user.
Native dependencies and review gates were enforced during closure; no approval
was inferred from executable checks. Await user authorization before P02.
