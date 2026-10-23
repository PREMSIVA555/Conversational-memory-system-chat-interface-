# Authoritative P00 extract

Source: `C:/Users/mprem/Documents/Codex/2026-10-17/https-antern-co-evidence-portfolio-https/outputs/banking-implementation-plan/PLAN_READER.html`

SHA-256: `9f59fb49a54ea305387465fb606deca567d8118d7e296f7edee8605559ba70fd`

Text extracted from PLAN_READER.html with HTML formatting removed. Proposed requirements, not implementation or execution evidence. Companion task records checked against HTML. Later phases are not imported.

## P00 — Baseline and evidence skeleton
Entry:
None closed with evidence.
Outcome:
Known source/runtime, isolated synthetic world skeleton and smallest runnable scenario before altering behavior.
Primary components:
PL01 PL04 EV01 BD04
Work items:
T00.1, T00.2, T00.3. See
backlog
.
First invariant gates:
INV29, INV31
Exit criteria:
Baseline setup/import/service checks recorded; test reset refuses non-test target; two resets reproduce semantic baseline; incident is reproduced with evidence or explicitly not reproduced.
Evidence produced:
Baseline manifest; commands/results; source snapshot; first scenario fixture; initial artifact ledger.
Recovery/rollback:
No deployed behavior changes. Preserve original checkout/results; debug only in isolated branch/environment.

## T00.1 · Freeze source and record environment
P00 · depends on none · planned
PL01, PL04
SPEC16, INV31, INV32
Implementation
Record inspected and actual implementation-start commits, dependency/runtime/service versions; inventory offline vs live tests and package entries. Preserve baseline.
Acceptance gate
Fresh environment transcript identifies failures without pretending all tests are offline or already passing.
Evidence target
evidence/baseline/<run-id>/manifest.json

## T00.2 · Build safe world/reset skeleton
P00 · depends on T00.1 · planned
BD04, EV01, PL01
SPEC13, INV29, INV31
Implementation
Seed minimal two-customer data; isolate DB/schema/Redis/files; expose DomainClock separately; refuse non-test reset.
Acceptance gate
Run two resets and contamination/target-guard checks through actual persistence.
Evidence target
evidence/world/<run-id>/reset-report.json

## T00.3 · Reproduce and characterize memory incident
P00 · depends on T00.2 · planned
MM02, MM04, EV01, PL04
SPEC04, SPEC06, INV11, INV14, INV32
Implementation
Capture actual old/new stored rows, capture completion order, retrieval context and answer. Convert limited legacy attribute-null probe into integration reproduction where possible.
Acceptance gate
Record reproduced or not-reproduced; no model blame or live-incident claim from a mocked unit probe.
Evidence target
evidence/baseline/<run-id>/memory-repro/

## SPEC04 — Typed ordered preferences
Components:
MM02, MM04, RE01, RE02, UX03
Behavioral contract:
Closed keys are preferred_response_language={en,hi} and preferred_response_format={concise,detailed}. All user changes, including panel writes, receive customer sequence from accepted_user_events. Per key/scope atomic conditional UPSERT accepts only a larger source sequence. Panel expected_version is checked before successful acceptance. Include first-write serialization, unique event keys and DELETE tombstones. Pending overlay follows source order and applicable scope; persisted status requires committed projection.
Failure/recovery contract:
Invalid provenance/key/value -> reject; stale source -> ignored_stale; stale panel version ->409; extraction failure -> failed/retry status and remove only that pending overlay.
Acceptance criteria:
Force older English after newer Hindi, concurrent first inserts, duplicate jobs, DELETE before old SET, competing panel/chat events and failed extraction. Verify projection, output language, version/order and truthful UI state.
Excluded:
Arbitrary inferred profile fields in typed preferences; worker-completion order as user order; temporary instructions as automatic global updates.

## SPEC06 — Correction, forgetting and retention
Components:
MM04, SP05, AU03
Behavioral contract:
Block deleted source material and dependent serving paths before acknowledging active erasure. Track direct memory, preference payloads/source spans, vectors, summaries, cache, checkpoints, accepted jobs and restore plans. Preserve minimal ordering tokens without forgotten content. Physical cleanup has separate store-specific status. Assign retention purpose/owner and explicit limits for local and external stores.
Failure/recovery contract:
Any required serving invalidation fails -> deletion remains pending/incomplete. Do not claim provider/backups/public copies deleted based on a local tombstone.
Acceptance criteria:
Seed direct/derived canaries, delete during backlog and query exact/paraphrase/old value across stores. Stale work cannot resurrect value; separate restore-and-scrub drill proves declared backup handling.
Excluded:
Legal compliance claims or a fixed legal retention schedule; real customer content in published artifacts.

## SPEC13 — Resettable world and scenario harness
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

## SPEC16 — Delivery, migration and evidence release
Components:
PL01, PL02, PL03, PL04
Behavioral contract:
Additive schema/readers, dry-run legacy classification, explicit ambiguous-row review, stop legacy preference writes after cutover. Pin compatible deps/model config and prove clean install. Rollback flags disable features or revert compatible app, preserve committed events/cases and migrate forward. Each artifact declares own contract, license, audience, actual release links/DOIs and limitations. Publication uses explicitly authorized accounts later.
Failure/recovery contract:
Security/regression gate failure blocks release. Cannot reproduce baseline -> record not reproduced. Missing credentials/issuer is setup blocked, never silent insecure fallback. Unissued DOI remains absent.
Acceptance criteria:
Migration counts/checks and restart/recovery preserved; no duplicate replay on rollback; clean environment demo/evaluator proof; independent reviewer result recorded only when a real person runs it.
Excluded:
Deploying/publishing/scheduling in this planning task, destructive legacy table drop, or counting this plan as completed measured portfolio work.

## INV11 — Source-order correction
Must hold:
Older accepted update cannot replace newer value for the same key/scope.
Enforced by:
Customer acceptance sequence and conditional projection update
Verified by:
Invert job completion and race panel/chat updates
First gate:
P02.
Work:
T00.3, T02.1, T02.3, T02.5, T05.4.
Violation:
release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV14 — User provenance
Must hold:
Preference proposal references actual authorized user source, not assistant/retrieved instruction.
Enforced by:
accepted_user_events + source span/key/value validation
Verified by:
Historical/third-person/quoted/injected preference negatives
First gate:
P02.
Work:
T00.3, T02.3.
Violation:
release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV29 — Isolated repeatable worlds
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

## INV31 — Traceable evidence
Must hold:
Every reported run has source/data/model/tool/policy/grader/environment/reset identities and retained raw outcomes.
Enforced by:
Versioned run manifest validation
Verified by:
Missing field/hash mismatch invalidates corresponding claim
First gate:
P00.
Work:
T00.1, T00.2, T06.1, T06.4, T07.1, T07.2, T07.3, T08.1.
Violation:
release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV32 — Truthful portfolio claims
Must hold:
Planned is not measured; failure needs repro; negative/maintained/independent claims need actual evidence.
Enforced by:
Artifact ledger + release checklist + preserved corrections
Verified by:
Trace each published statement to source/run/repro and status
First gate:
P07.
Work:
T00.1, T00.3, T06.3, T07.2, T07.3, T07.4, T08.1.
Violation:
release-blocking; preserve failing evidence and block the affected slice/claim until resolved.
