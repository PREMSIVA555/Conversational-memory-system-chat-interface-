# Authoritative P01 extract

Source: `C:/Users/mprem/Documents/Codex/2026-10-17/https-antern-co-evidence-portfolio-https/outputs/banking-implementation-plan/PLAN_READER.html`

SHA-256: `9f59fb49a54ea305387465fb606deca567d8118d7e296f7edee8605559ba70fd`

Requirements only; no implementation or passing checks claimed.

P01 — Authenticated and observable foundation
Entry: P00 closed with evidence.
Outcome: Authenticated principal, constrained DB access, safe telemetry and validated environment before banking data is exposed.
Primary components: SP01 SP02 SP05 OB01 OB02 OB03 PL03
Work items: T01.1, T01.2, T01.3. See backlog.
First invariant gates: INV01, INV02, INV03, INV24, INV26, INV27, INV33
Exit criteria: Positive owner access and all relevant negative identity/ownership/CSRF/pool tests pass; actual roles verified; secret canaries absent from tested sinks; unsafe legacy routes disabled.
Evidence produced: Threat model; role/grant inspection; API isolation results; first correlated trace; redaction evidence; environment runbook.
Recovery/rollback: Disable banking routes on failed auth/role/readiness checks. Never fall back to client-selected identity.

T01.1 · Wire OIDC and opaque server sessions
P01 · depends on T00.3 · planned
SP01, UX02, PL03
SPEC01, INV01, INV24, INV33
Implementation
Configure issuer/client secrets, callback lifecycle, session rotation/expiry, CSRF/origin; remove client identity authority and disable legacy deployed route.
Acceptance gate
Valid login plus callback/session/CSRF/queued-after-logout negatives.
Evidence target
evidence/security/<run-id>/auth/

T01.2 · Provision roles, owner schema and RLS
P01 · depends on T01.1 · planned
SP02, BD01, PL01
SPEC02, INV02, INV03
Implementation
Create internal identity links/customer sequence root, owner-consistent keys/FKs, runtime/worker/dispatcher/migrator grants and trusted transaction context.
Acceptance gate
Role inspection, missing-context and reused-pool two-customer tests; owner access still works.
Evidence target
evidence/security/<run-id>/roles-and-rls/

T01.3 · Add privacy and operational seams
P01 · depends on T01.2 · planned
SP05, OB01, OB02, OB03, PL03
SPEC12, INV26, INV27
Implementation
Introduce metadata allowlists/redaction before model/export, linked spans, bounded metric dimensions and health/readiness configuration.
Acceptance gate
Synthetic secrets in exceptions/payloads are absent from every enabled sink; auth readiness fails closed.
Evidence target
evidence/operations/<run-id>/redaction-and-tracing/

SPEC01 — Authentication and browser session
Components: SP01, UX02, PL03
Behavioral contract: Use configured OIDC code flow with PKCE S256 and vetted callback/token validation. Map issuer/subject to internal customer. Redis stores opaque rotated sessions; __Host-bank_sid is Secure, HttpOnly, SameSite=Lax, Path=/, without Domain. All mutation routes check CSRF token and allowed Origin. Deployed banking profile disables legacy unauthenticated chat. Session expiry is checked at API entry and before queued banking access; logout clears browser working state.
Failure/recovery contract: Invalid or expired identity -> 401; invalid CSRF/origin -> 403; required auth store unavailable -> access closed, 503. Never select customer from a request field.
Acceptance criteria: Good login accesses only owned resources. Invalid issuer/audience/signature/state/nonce, callback replay, old session after rotation/logout, and cross-site mutation are rejected. Test long SSE expiry and queued access after logout.
Excluded: Building an identity provider, storing passwords, or unsigned development authentication in deployed banking profiles.

SPEC02 — Authorization, resource identity and database roles
Components: SP02, SP03, BD01, PL01
Behavioral contract: Principal is server-created and carried in RequestContext. Gateway and domain/provider service authorize each resource. Display masks resolve within owned lists and ambiguity produces authorized choices. Runtime roles are nonowner, NOSUPERUSER, NOBYPASSRLS with no privileged role membership. RLS uses trusted transaction-local customer context and write checks; composite owner FKs prevent mismatched children. Restrict dispatcher, worker and migrator separately.
Failure/recovery contract: Missing or foreign resource -> non-enumerating 404; missing tenant context -> deny. Unknown operation/schema -> typed reject before provider access.
Acceptance criteria: Exercise API, direct provider, worker and real pooled DB paths with two customers. Valid owner succeeds; foreign UUID, missing context, stale pooled context, spoofed headers/body, arbitrary SQL/tool names do not expose evidence or execute a write.
Excluded: Guaranteeing isolation after complete application/DB-credential compromise; customer masks as canonical IDs; broad admin sessions in request handlers.

SPEC12 — Audit, telemetry, privacy and runtime configuration
Components: AU01, AU02, AU03, OB01, OB02, OB03, SP05, PL03
Behavioral contract: Action success audit shares local support transaction and runtime cannot edit/delete it. Redacted JSON logs and OTel linked spans use metadata allowlists; no raw bank facts/secrets/prompts by default. Metrics use bounded route/tool/status labels, no customer/request IDs. Propagate trace IDs across outbox as correlation only. Validate issuer/secrets/environment at startup; separate liveness from dependency readiness. Govern synthetic eval traces separately.
Failure/recovery contract: Mandatory audit failure rolls back support; optional exporter failure drops/buffers bounded telemetry with diagnostic counter. Unknown price -> cost unavailable. Missing auth config/store closes banking access.
Acceptance criteria: Synthetic secret canaries never export in tested sinks; forged trace cannot authorize; spans link across process hop; queue/dead-letter alerts exercised; bad environment fails safely; runtime audit update/delete denied.
Excluded: Tamper-proof audit against DBA compromise, uptime inferred from CI YAML, or raw sensitive labels/content everywhere for convenience.

INV01 — Server-established identity
Must hold: No HTTP/model-supplied customer/actor field confers authority.
Enforced by: SessionService + strict DTO rejection + issuer mapping
Verified by: Forged header/query/body, token/issuer/session negatives
First gate: P01. Work: T01.1. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV02 — Owned resource on every path
Must hold: Only authorized owned rows become tool evidence or responses.
Enforced by: Gateway + domain/provider + scoped repository
Verified by: Two-customer API/direct-adapter/worker probes; valid owner control
First gate: P01. Work: T01.2, T03.1, T04.1. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV03 — RLS role and context
Must hold: Runtime is nonowner, non-superuser, NOBYPASSRLS; missing tenant context denies.
Enforced by: Role provisioning + transaction-local trusted context + RLS USING/WITH CHECK
Verified by: Inspect actual roles and pooled-context reuse; no admin request path
First gate: P01. Work: T01.2. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV24 — Session context isolation
Must hold: Selections/proposals/cache/in-flight UI responses cannot cross customers or reuse invalid sessions.
Enforced by: Scoped keys, state reset and response-generation guard
Verified by: Logout A/login B with delayed A response and old pending proposal
First gate: P01. Work: T01.1, T02.4, T03.3, T05.1. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV26 — Safe public errors
Must hold: Errors do not expose foreign existence, secrets, SQL parameters or stack traces.
Enforced by: Safe error mapping and pre-export allowlist
Verified by: Compare foreign/missing error and synthetic-secret exception handling
First gate: P01. Work: T01.3. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV27 — Minimized telemetry
Must hold: No forbidden sensitive payloads or unbounded identities in operational telemetry.
Enforced by: Allowlist attributes/redaction; bounded metric dimensions
Verified by: Canary scans across logs/traces/exporters; cardinality assertions
First gate: P01. Work: T01.3, T06.4. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV33 — Mutation browser protection
Must hold: Cookie-authenticated mutation routes require valid CSRF and allowed Origin.
Enforced by: API middleware plus route contract checks
Verified by: Cross-site requests, absent/bad CSRF and direct API bypass negatives
First gate: P01. Work: T01.1. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.
