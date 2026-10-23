# P02 authoritative source extract

- source_path: C:/Users/mprem/Documents/Codex/2026-10-17/https-antern-co-evidence-portfolio-https/outputs/banking-implementation-plan/PLAN_READER.html
- source_sha256: 9f59fb49a54ea305387465fb606deca567d8118d7e296f7edee8605559ba70fd
- recorded_at: 2026-10-18T16:58:52.059727+00:00
- scope: P02 and T02.1-T02.5 only; original SPEC/INV terminology retained
- authorization: start P02
- limitations: ['Requirements and planned acceptance contracts, not implementation or passing test evidence.', 'Task records cross-checked against authoritative HTML. P03 and later phases excluded.']

P02 — Ordered memory and durable processing
Entry: P01 closed with evidence.

Outcome: Source-ordered typed preferences, session context and explicit pending/persisted/deleted states survive retry/restart.

Primary components: MM01 MM02 MM03 MM04 RE01 RE02 AU03

Work items: T02.1, T02.2, T02.3, T02.4, T02.5. See backlog.

First invariant gates: INV11, INV12, INV13, INV14, INV15, INV21, INV22, INV23

Exit criteria: Inverted completion, first-insert race, duplicate handler, worker fencing, manual/chat CAS, scope, deletion and migration dry-run checks pass.

Evidence produced: Regression traces and DB snapshots; migration counts/ambiguities; durable job/fencing evidence; first real failure/retest entry if reproduced.

Recovery/rollback: Stop old capture from mutating cutover keys; disable optional semantic memory/feature flag safely while retaining source events. Do not revert by replaying old updates.

## T02.1 — Add acceptance events, projections and outbox schema

task_id: T02.1
phase: P02
title: Add acceptance events, projections and outbox schema
depends_on: ['T01.3']
component_ids: ['MM02', 'RE01', 'AU03']
spec_ids: ['SPEC04', 'SPEC11', 'SPEC16']
invariant_ids: ['INV11', 'INV12', 'INV15']
implementation: Add accepted_user_events shared by chat/panel, conditional projection/tombstones, lineage, outbox/receipt and fencing fields. Author additive migration and dry-run legacy classification.
acceptance: Empty-key first-write race, ambiguous legacy rows and migration count checks; no destructive cutover.
evidence_target: evidence/migrations/<run-id>/preference-plan/
status: planned-not-started

## T02.2 — Implement durable consumer and fencing

task_id: T02.2
phase: P02
title: Implement durable consumer and fencing
depends_on: ['T02.1']
component_ids: ['RE01', 'RE02']
spec_ids: ['SPEC11']
invariant_ids: ['INV21', 'INV22']
implementation: Implement restricted claim, receipt-before-effect transaction gate, lease generation, bounded retries/dead letters and generation-checked publications.
acceptance: Crash/duplicate/concurrent effect gate and A-paused/B-reclaimed/A-resumed tests.
evidence_target: evidence/reliability/<run-id>/consumer/
status: planned-not-started

## T02.3 — Implement typed preference resolver and status

task_id: T02.3
phase: P02
title: Implement typed preference resolver and status
depends_on: ['T02.2']
component_ids: ['MM02', 'UX03']
spec_ids: ['SPEC04', 'SPEC05']
invariant_ids: ['INV11', 'INV12', 'INV13', 'INV14', 'INV15']
implementation: Extract allowlisted user-origin preferences, enforce accepted order/CAS/scopes, exact reads and source-ordered overlays with truthful UI states.
acceptance: Hindi vs late English, historical/third-party statement, panel/chat race, temporary scope and extraction failure.
evidence_target: evidence/memory/<run-id>/preference-cases/
status: planned-not-started

## T02.4 — Implement working context, lineage and forgetting

task_id: T02.4
phase: P02
title: Implement working context, lineage and forgetting
depends_on: ['T02.3']
component_ids: ['MM01', 'MM03', 'MM04', 'AU03']
spec_ids: ['SPEC05', 'SPEC06']
invariant_ids: ['INV13', 'INV23', 'INV24']
implementation: Scope selections and optional semantic retrieval, retain derivative provenance, block deleted sources before serving, define per-store cleanup/restore status.
acceptance: Cross-session selections, deletion during backlog, derived/cached/checkpoint retrieval and restore-and-scrub.
evidence_target: evidence/memory/<run-id>/context-and-erasure/
status: planned-not-started

## T02.5 — Rehearse and cut over memory writes

task_id: T02.5
phase: P02
title: Rehearse and cut over memory writes
depends_on: ['T02.4']
component_ids: ['MM02', 'MM04', 'PL01']
spec_ids: ['SPEC04', 'SPEC16']
invariant_ids: ['INV11', 'INV15', 'INV23']
implementation: Dry-run and review legacy mapping, deploy compatible reader, backfill, disable old preference writer, enable new path by flag.
acceptance: Counts and ambiguous rows recorded; rollback flag preserves current events and prevents old capture overwrite.
evidence_target: evidence/migrations/<run-id>/cutover/
status: planned-not-started

## SPEC04

SPEC04 — Typed ordered preferences
Components: MM02, MM04, RE01, RE02, UX03

Behavioral contract: Closed keys are preferred_response_language={en,hi} and preferred_response_format={concise,detailed}. All user changes, including panel writes, receive customer sequence from accepted_user_events. Per key/scope atomic conditional UPSERT accepts only a larger source sequence. Panel expected_version is checked before successful acceptance. Include first-write serialization, unique event keys and DELETE tombstones. Pending overlay follows source order and applicable scope; persisted status requires committed projection.

Failure/recovery contract: Invalid provenance/key/value -> reject; stale source -> ignored_stale; stale panel version ->409; extraction failure -> failed/retry status and remove only that pending overlay.

Acceptance criteria: Force older English after newer Hindi, concurrent first inserts, duplicate jobs, DELETE before old SET, competing panel/chat events and failed extraction. Verify projection, output language, version/order and truthful UI state.

Excluded: Arbitrary inferred profile fields in typed preferences; worker-completion order as user order; temporary instructions as automatic global updates.

## SPEC11

SPEC11 — Durable workers, deduplication and recovery
Components: RE01, RE02, RE03, RE04, AO01, AU02

Behavioral contract: Transactional outbox is acceptance source. Dispatcher claims bounded batches with short transaction and lease generation. Consumer claims unique handler receipt as same-tx gate before local effect. A fencing generation serializes reclaim and checkpoint/terminal-event publication so replaced workers cannot publish. Retry only classified transient errors within budgets; unsupported schemas/permanent failures dead-letter visibly. Durable local operation lookup resolves unknown outcomes.

Failure/recovery contract: Lost acknowledgment replays receipt; lease expiry alone is not success. Duplicate read/model computation may occur and is measured; duplicate logical effects/public terminal answers must not.

Acceptance criteria: Crash before/after effect commit, deliver twice, pause A until B reclaims/completes then resume A, and exhaust retry/dead-letter paths. Capture remains durable and stale ownership cannot publish.

Excluded: Transport exactly-once guarantee, unbounded retries or automatic archival replay beyond the supported retention/version window.

## SPEC16

SPEC16 — Delivery, migration and evidence release
Components: PL01, PL02, PL03, PL04

Behavioral contract: Additive schema/readers, dry-run legacy classification, explicit ambiguous-row review, stop legacy preference writes after cutover. Pin compatible deps/model config and prove clean install. Rollback flags disable features or revert compatible app, preserve committed events/cases and migrate forward. Each artifact declares own contract, license, audience, actual release links/DOIs and limitations. Publication uses explicitly authorized accounts later.

Failure/recovery contract: Security/regression gate failure blocks release. Cannot reproduce baseline -> record not reproduced. Missing credentials/issuer is setup blocked, never silent insecure fallback. Unissued DOI remains absent.

Acceptance criteria: Migration counts/checks and restart/recovery preserved; no duplicate replay on rollback; clean environment demo/evaluator proof; independent reviewer result recorded only when a real person runs it.

Excluded: Deploying/publishing/scheduling in this planning task, destructive legacy table drop, or counting this plan as completed measured portfolio work.

## INV11

INV11 — Source-order correction
Must hold: Older accepted update cannot replace newer value for the same key/scope.

Enforced by: Customer acceptance sequence and conditional projection update

Verified by: Invert job completion and race panel/chat updates

First gate: P02. Work: T00.3, T02.1, T02.3, T02.5, T05.4. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV12

INV12 — Safe first insert
Must hold: Concurrent first writes obey ordering even when projection row does not exist.

Enforced by: Existing sequence lock + unique projection key + conditional UPSERT

Verified by: Barrier-controlled concurrent empty-slot writes

First gate: P02. Work: T02.1, T02.3. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV15

INV15 — Durable truthful acknowledgment
Must hold: Accepted work exists durably; Saved requires committed preference projection.

Enforced by: Atomic source+outbox acceptance and persisted status model

Verified by: Crash around acceptance/projection; UI pending/failed/persisted checks

First gate: P02. Work: T02.1, T02.3, T02.5, T03.3. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV21

INV21 — Atomic consumer deduplication
Must hold: Duplicate delivery applies each local handler effect at most once.

Enforced by: Unique handler receipt gate in effect transaction

Verified by: Concurrent duplicates and crash after commit before acknowledgement

First gate: P02. Work: T02.2. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV22

INV22 — Fenced publication
Must hold: A replaced/expired worker cannot publish checkpoint or terminal run state.

Enforced by: Ownership generation and locked publication/reclaim + terminal uniqueness

Verified by: Pause A; B reclaims/completes; resume A -> rejected publication

First gate: P02. Work: T02.2. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## SPEC05

SPEC05 — Working context and long-term semantic context
Components: MM01, MM03, AO03, UX01

Behavioral contract: Working state holds bounded recent context and authorized canonical selections scoped by customer/session/conversation/version. Precedence: explicit current-response instruction, active conversation preference, global preference, default English/concise. Separate optional semantic history from exact settings and bank/policy truth. Retrieve only governed permitted source/derivative records; summaries retain lineage.

Failure/recovery contract: Ambiguous selection -> clarify; session changes clear transient selections/proposals. Optional memory failure degrades personalization explicitly when relevant; it never supplies a missing current bank fact.

Acceptance criteria: Follow-up resolves correct authorized product; multiple matches clarify; another session cannot consume a pending proposal; temporary Hindi does not overwrite global English; historical balance cannot override current tool evidence.

Excluded: Unbounded prompt history, cross-session pending proposal reuse, implicit global memory for every utterance.

## INV13

INV13 — Scope precedence
Must hold: Current-response > active conversation > global > declared default; no silent scope promotion.

Enforced by: PreferenceService effective resolver + overlay applicability

Verified by: Temporary language, competing scopes and failed overlay cases

First gate: P02. Work: T02.3, T02.4. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV14

INV14 — User provenance
Must hold: Preference proposal references actual authorized user source, not assistant/retrieved instruction.

Enforced by: accepted_user_events + source span/key/value validation

Verified by: Historical/third-person/quoted/injected preference negatives

First gate: P02. Work: T00.3, T02.3. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## SPEC06

SPEC06 — Correction, forgetting and retention
Components: MM04, SP05, AU03

Behavioral contract: Block deleted source material and dependent serving paths before acknowledging active erasure. Track direct memory, preference payloads/source spans, vectors, summaries, cache, checkpoints, accepted jobs and restore plans. Preserve minimal ordering tokens without forgotten content. Physical cleanup has separate store-specific status. Assign retention purpose/owner and explicit limits for local and external stores.

Failure/recovery contract: Any required serving invalidation fails -> deletion remains pending/incomplete. Do not claim provider/backups/public copies deleted based on a local tombstone.

Acceptance criteria: Seed direct/derived canaries, delete during backlog and query exact/paraphrase/old value across stores. Stale work cannot resurrect value; separate restore-and-scrub drill proves declared backup handling.

Excluded: Legal compliance claims or a fixed legal retention schedule; real customer content in published artifacts.

## INV23

INV23 — No serving after active erasure
Must hold: Deleted sources and derivatives cannot serve or resurrect; completion describes actual store scope.

Enforced by: Serving tombstones, lineage invalidation, cleanup/restore status

Verified by: Direct/derived/cache/checkpoint queries with delayed jobs and restore drill

First gate: P02. Work: T02.4, T02.5. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

## INV24

INV24 — Session context isolation
Must hold: Selections/proposals/cache/in-flight UI responses cannot cross customers or reuse invalid sessions.

Enforced by: Scoped keys, state reset and response-generation guard

Verified by: Logout A/login B with delayed A response and old pending proposal

First gate: P01. Work: T01.1, T02.4, T03.3, T05.1. Violation: release-blocking; preserve failing evidence and block the affected slice/claim until resolved.

