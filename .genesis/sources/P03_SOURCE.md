# P03 authoritative source extract

{
  "source_path": "C:/Users/mprem/Documents/Codex/2026-10-17/https-antern-co-evidence-portfolio-https/outputs/banking-implementation-plan/PLAN_READER.html",
  "source_sha256": "9f59fb49a54ea305387465fb606deca567d8118d7e296f7edee8605559ba70fd",
  "recorded_at": "2026-10-19T16:17:25.808175+00:00",
  "scope": "P03 and T03.1-T03.4 only; original SPEC/INV terminology retained",
  "authorization": "approved proceed",
  "limitations": [
    "Planned contracts only, not passing tests or future approvals.",
    "P04 and later excluded."
  ]
}

P03 — First end-to-end account enquiry
Entry:
 P02 closed with evidence.
Outcome:
 Login -> accepted turn -> bounded planning -> authorized account/transaction tools -> checked answer -> UI recovery.
Primary components:
 UX01 UX02 AO01 AO02 AO03 AO04 BD01 BD02 SP03 RE04
Work items:
 T03.1, T03.2, T03.3, T03.4. See 
backlog
.
First invariant gates:
 INV04, INV06, INV07, INV10, INV16, INV25
Exit criteria:
 Exact current values and selected product match DB; ambiguity clarifies; foreign/unknown tool rejected; input retry/reconnect works; model/tool failures stop safely.
Evidence produced:
 One API-path scenario transcript; typed DTO/API changes; no-unchecked-answer proof; queue/run/answer latency and usage; baseline enquiry eval slice.
Recovery/rollback:
 Disable new tool/route by flag. Preserve auth/memory events; expose unavailable rather than fallback remembered bank fields.

{
  "task_id": "T03.1",
  "phase": "P03",
  "title": "Define banking DTO and provider port",
  "depends_on": [
    "T02.5"
  ],
  "component_ids": [
    "BD01",
    "BD02",
    "BD04"
  ],
  "spec_ids": [
    "SPEC07"
  ],
  "invariant_ids": [
    "INV02",
    "INV06",
    "INV07"
  ],
  "implementation": "Add typed account/transaction contracts, canonical UUIDs, exact Money/currency metadata, source as_of/version, signed cursor pagination and scoped provider.",
  "acceptance": "DB-oracle amount/status/range/cursor results; foreign direct-adapter access denied.",
  "evidence_target": "evidence/banking/<run-id>/account-provider/",
  "status": "planned-not-started"
}

{
  "task_id": "T03.2",
  "phase": "P03",
  "title": "Build coordinator, registry and checked answers",
  "depends_on": [
    "T03.1"
  ],
  "component_ids": [
    "AO01",
    "AO02",
    "AO03",
    "AO04",
    "SP03",
    "RE04"
  ],
  "spec_ids": [
    "SPEC09"
  ],
  "invariant_ids": [
    "INV04",
    "INV06",
    "INV10",
    "INV25"
  ],
  "implementation": "Extend existing model seam with capability checks/usage; one bounded graph; schema-validated registered reads; evidence registry and deterministic financial rendering.",
  "acceptance": "Malformed/unknown/injected plan, bad numeric draft, current-memory conflict and deadline tests terminate safely.",
  "evidence_target": "evidence/agent/<run-id>/account-run/",
  "status": "planned-not-started"
}

{
  "task_id": "T03.3",
  "phase": "P03",
  "title": "Expose durable API, UI and recovery",
  "depends_on": [
    "T03.2"
  ],
  "component_ids": [
    "UX01",
    "UX02",
    "RE01"
  ],
  "spec_ids": [
    "SPEC03",
    "SPEC05"
  ],
  "invariant_ids": [
    "INV15",
    "INV16",
    "INV24"
  ],
  "implementation": "Wire turn acceptance, payload fingerprint/conflict, GET status and persisted SSE; reset view and discard previous-session delayed responses.",
  "acceptance": "Same-key concurrent turn/reconnect/disconnect/changed-payload test; owner-only stream with expiry.",
  "evidence_target": "evidence/ui/<run-id>/turn-recovery/",
  "status": "planned-not-started"
}

{
  "task_id": "T03.4",
  "phase": "P03",
  "title": "Close first account vertical slice",
  "depends_on": [
    "T03.3"
  ],
  "component_ids": [
    "UX01",
    "AO03",
    "BD02",
    "EV01"
  ],
  "spec_ids": [
    "SPEC07",
    "SPEC09",
    "SPEC13"
  ],
  "invariant_ids": [
    "INV06",
    "INV07",
    "INV10",
    "INV29"
  ],
  "implementation": "Run login -> product clarification -> balance/history -> checked reply through same user-facing API, including outages.",
  "acceptance": "Exact DB state/evidence/answer and safe unsupported behavior; distinguish queue vs run vs full latency.",
  "evidence_target": "evidence/scenarios/<run-id>/account-slice/",
  "status": "planned-not-started"
}

INV02 — Owned resource on every path
Must hold:
 Only authorized owned rows become tool evidence or responses.
Enforced by:
 Gateway + domain/provider + scoped repository
Verified by:
 Two-customer API/direct-adapter/worker probes; valid owner control
First gate:
 P01. 
Work:
 T01.2, T03.1, T04.1. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV04 — Bounded capabilities
Must hold:
 Models can use registered reads/proposals; cannot execute support or banking mutations.
Enforced by:
 ToolRegistry and distinct confirm endpoint
Verified by:
 Unknown tool, extra identity/SQL arg and injection rejected before provider
First gate:
 P03. 
Work:
 T03.2. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV06 — Current bank authority
Must hold:
 Current balance/payment/case status comes from fresh authorized evidence, not history.
Enforced by:
 Source-aware evidence registry + typed tools
Verified by:
 Conflicting memory and unavailable tool never yield remembered current fact
First gate:
 P03. 
Work:
 T03.1, T03.2, T03.4, T04.2, T05.3. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV07 — Exact financial representation
Must hold:
 Supported money remains exact through DB, JSON, UI and arithmetic; no implicit FX.
Enforced by:
 bigint/Decimal, integer strings, currency exponent allowlist
Verified by:
 Large/zero amounts, supported exponents, unsupported currency and range cases
First gate:
 P03. 
Work:
 T03.1, T03.4, T04.1, T04.2. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV10 — Supported displayed claims
Must hold:
 Exact fields match evidence; explanatory claims lacking support become limitation/fallback.
Enforced by:
 Typed rendering + source checks + curated rules; offline claim rubric
Verified by:
 Wrong amounts, relevant-but-invalid citations and contradiction fixtures
First gate:
 P03. 
Work:
 T03.2, T03.4, T04.4, T06.3. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV15 — Durable truthful acknowledgment
Must hold:
 Accepted work exists durably; Saved requires committed preference projection.
Enforced by:
 Atomic source+outbox acceptance and persisted status model
Verified by:
 Crash around acceptance/projection; UI pending/failed/persisted checks
First gate:
 P02. 
Work:
 T02.1, T02.3, T02.5, T03.3. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV16 — Idempotent turn acceptance
Must hold:
 Same scoped client turn identity/payload yields one logical turn; changed payload conflicts.
Enforced by:
 Unique client key + canonical request fingerprint + conflict handling
Verified by:
 Concurrent retries, changed payload and lost 202 response
First gate:
 P03. 
Work:
 T03.3. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV24 — Session context isolation
Must hold:
 Selections/proposals/cache/in-flight UI responses cannot cross customers or reuse invalid sessions.
Enforced by:
 Scoped keys, state reset and response-generation guard
Verified by:
 Logout A/login B with delayed A response and old pending proposal
First gate:
 P01. 
Work:
 T01.1, T02.4, T03.3, T05.1. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV25 — Bounded work and safe failure
Must hold:
 Runs stop within configured call/deadline limits; bank outage never becomes invented fact.
Enforced by:
 Monotonic budgets, bounded read retries, explicit unavailable state
Verified by:
 Slow providers, malformed plans, queue expiry and loop termination
First gate:
 P03. 
Work:
 T03.2. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

INV29 — Isolated repeatable worlds
Must hold:
 Trials reset to declared semantic world and share no mutable namespaces.
Enforced by:
 Test-only reset guard + per-trial DB/Redis/files + clock separation
Verified by:
 Parallel resets, contamination probe and canonical hash check
First gate:
 P00. 
Work:
 T00.2, T03.4, T06.1, T07.1. 
Violation:
 release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

SPEC03 — Durable turn API and response transport
Components:
 UX01, UX02, AO01, RE01
Behavioral contract:
 POST /api/v1/conversations/{id}/turns validates ownership and input then atomically persists accepted_user_event, turn/run and outbox before 202. customer/client_turn_id uniqueness provides replay. Same client key with a changed normalized request returns 409. GET status is recovery authority; persisted per-run event IDs support SSE reconnect. Emit answer.ready only after completed-answer checks. Disconnect does not cancel accepted work. Persist a canonical request fingerprint on turn acceptance so idempotency compares the same normalized operation fields, rather than only client_turn_id.
Failure/recovery contract:
 No durable commit -> no successful acceptance. Unknown commit -> reconcile by original client identity. 401/404/409/422/429/503 retain safe request IDs. Client merges repeated events by event ID.
Acceptance criteria:
 Duplicate and concurrent same-key requests return one logical turn; changed input conflicts. Restart/disconnect after acceptance and reconnect from last event recovers authorized ordered state without duplicate rendering.
Excluded:
 Financial token streaming before validation; treating SSE itself as durable or exactly-once transport.

SPEC05 — Working context and long-term semantic context
Components:
 MM01, MM03, AO03, UX01
Behavioral contract:
 Working state holds bounded recent context and authorized canonical selections scoped by customer/session/conversation/version. Precedence: explicit current-response instruction, active conversation preference, global preference, default English/concise. Separate optional semantic history from exact settings and bank/policy truth. Retrieve only governed permitted source/derivative records; summaries retain lineage.
Failure/recovery contract:
 Ambiguous selection -> clarify; session changes clear transient selections/proposals. Optional memory failure degrades personalization explicitly when relevant; it never supplies a missing current bank fact.
Acceptance criteria:
 Follow-up resolves correct authorized product; multiple matches clarify; another session cannot consume a pending proposal; temporary Hindi does not overwrite global English; historical balance cannot override current tool evidence.
Excluded:
 Unbounded prompt history, cross-session pending proposal reuse, implicit global memory for every utterance.

SPEC07 — Typed banking enquiry services
Components:
 BD01, BD02, BD04, UX01, SP03
Behavioral contract:
 Synthetic accounts expose current/available balance distinctly; cards distinguish daily limit, credit limit, available credit and outstanding; transactions have explicit status/date and deterministic cursor order; existing loans expose principal/APR/payment; EMI schedule and actual payments are separate evidence. UUID refs, source version/as_of, currency and integer-string minor units are mandatory. Pin supported currency exponent metadata; enforce ranges, Decimal APR and no mixed-currency sum.
Failure/recovery contract:
 Unknown decline/payment reason remains unknown. Unsupported currency/range -> typed failure. Bank timeout -> unavailable; no stale memory fallback. Resource changes require fresh reads.
Acceptance criteria:
 Compare service DTO and rendered answer to DB oracle for debit/credit, declined/pending/posted/reversed, timestamp ties, all pages, multiple cards, null reason, scheduled-unpaid EMI and large/zero amounts. Supported exponent formatting remains exact.
Excluded:
 Money movement, live bank connections, new loan decisions, limit increases, card-setting changes, travel notices and formal refund/dispute execution.

SPEC09 — Bounded planning and answer construction
Components:
 AO01, AO02, AO03, AO04, SP03, RE04
Behavioral contract:
 One coordinator uses registered enquiry tools and support drafting. Strict tool/plan schema rejects extras and model-created identity. Structured provider errors distinguish unavailable, invalid and no evidence. Typed bank facts are server-rendered; optional explanatory prose is checked and falls back to supported templates/limitations. Trace included sources and usage. All repairs/retries count against common call and monotonic deadline budgets. Persist logical run start/deadline and atomically reserve model/tool attempts against durable counters before dispatch. Reclaim/restart does not reset call/deadline allowance. Derive process-local monotonic remaining budgets from the real execution deadline, never from fixture DomainClock; duplicate in-flight calls may still consume real cost and must be reported.
Failure/recovery contract:
 Unknown capability -> explain unsupported; invalid plan -> at most one bounded repair then clarify/error; insufficient mandatory context -> narrow rather than drop decisive evidence. No loop continues beyond budget.
Acceptance criteria:
 Inject malformed JSON, unknown tools, SQL-like args, merchant/policy injection, wrong numeric draft, unsupported citations, provider emptiness/timeouts and evidence-position perturbations. No invented bank field or consequential execution is shown.
Excluded:
 Model-triggered support execution, multi-agent supervisor by default, private chain-of-thought as an eval requirement.

SPEC13 — Resettable world and scenario harness
Components:
 BD04, EV01, PL01
Behavioral contract:
 Dedicated synthetic schema/DB and Redis/file namespace per trial. Pin fixture/tool/policy/source versions; script state changes. DomainClock is separate from security expiry and monotonic budgets. Reset canonical semantic state and verify no contamination. Initial runner calls the same API/service wiring as the demo; deterministic stubs are labeled.
Failure/recovery contract:
 Non-test reset target -> refuse. Mismatched initial state -> invalid trial; insufficient setup -> blocked, not a passing benchmark.
Acceptance criteria:
 Two clean resets match canonical hashes; parallel trials share no mutable state; state changes reproduce by script; regular application role cannot reset fixtures; frozen banking date cannot freeze session expiry.
Excluded:
 Real customer records, assuming fixed fixtures make model output deterministic, or shortcuts that bypass production authorization.
