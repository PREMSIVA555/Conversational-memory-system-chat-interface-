# P07 experiment protocol, v1

T07.1 has passed 22 maker tooling tests and a fresh separate check (370 protocol/CLI assertions plus four Git byte-preservation checks). The user approved T07.1 on 2026-10-26 (“approved and proceed”). T07.2 execution work follows as a separate task. P06 was approved and pushed as `ac45cb6` before this work. This amendment adds protocol data and read-only validation; the banking application is unchanged.

The authoritative `PLAN_READER.html` mapping is in `.genesis/sources/P07_SOURCE.json`. This task preserves **SPEC15, INV29, INV30 and INV31**. T07.2–T07.4 are queued; P08 is unchanged.

## What is frozen

`research/protocols/v1/` contains the protocol, twelve authored synthetic case definitions, approved-baseline Git blob pins, observed environment versions and the exact-byte freeze receipt. Hashes identify these files; they are not an external timestamp, signature or proof that a scenario ran. The native source-bound gate adds an independently recorded check time. Freeze precedes experiments because no P07 trials have run.

Git attributes preserve frozen bytes across line-ending conversion. The separate checker first observed three permissive field/type cases and a missing Git attribute; the maker corrected these and the checker repeated its own attacks successfully. Original failures remain local evidence. The portability check compared actual Git-filtered/unfiltered blobs and simulated LF transport; no real Linux checkout is claimed. Receipt reuse fails without changing existing bytes, though its CLI error currently includes a traceback.

Three proposed comparisons each have two development families, one calibration family and one reserve family. The pilot design is **3 comparisons × 2 conditions × 2 development cases × 2 repeats = 24 attempts**. Pair order is A/B then B/A; each attempt needs its own guarded DB, Redis and file namespace, matching canonical world/input and explicit domain/security clocks. Both repeats and all failures stay in the denominator. A retry consumes another attempt; it never silently replaces a failed attempt. Only twelve pairs fit in this pilot, and two families per comparison cannot establish reliability.

1. Secured old preference mechanism versus ordered projection: inspect real accepted-order and scope outcomes. The historical incident remains not reproduced unless a new actual run reproduces it. An insecure legacy request path is never a valid baseline.
2. Preference memory context on versus off: only preference-context retrieval changes. Identity, resource selection, fresh banking evidence and durable preference writes stay matched. Report language/detail adherence separately from money/status correctness.
3. Optional proactive clarification on versus off: only nonessential conversational prompting changes. Required product/ownership/policy ambiguity handling remains enabled in both conditions. Report interaction burden alongside successful completion, including regressions. No negative-result finding is assumed.

All conditions preserve server identity, ownership, RLS, CSRF/origin, audit, erasure, checked evidence, durable acceptance/replay, deadlines and required disambiguation. An unsafe control-off comparison is invalid.

## Exposure and missing execution work

**There is no unseen holdout.** P06 R01–R08 were already seen by the maker and an AI-assisted human reviewer. Every P07 case was authored in this checkout and is author-visible. The `holdout` split is an exposed reserve for exploratory tooling checks, with confirmatory eligibility explicitly false. Family/source-family IDs prevent declared split collisions, but do not establish semantic independence or absence of exposure by themselves. An independent future author must supply new protected families before an unseen-heldout claim.

**Live model and confirmatory budgets are zero.** The historical UX 21/20 overrun remains recorded and the live budget stays closed. No Astra is authorized. Provider/model/judge are disabled and model versions are null. Agent verification does not count as an application model evaluation and cannot make the judge calibrated.

**Comparison operators do not exist yet.** Pinned P06 code, dependency files, observed Python/Node/Git/Docker/package versions, observed container image IDs, synthetic policy modules, fixtures and grader version identify the starting materials, not a runnable experiment bundle. T07.2 must implement and pin each secured operator, normalized task/oracle bindings, effective synthetic policy catalog, resolved dependencies and actual per-run service/environment/reset manifests. Implementing those adapters changes source, so it requires a **new versioned execution freeze before any trial**, retaining v1. A changed code/model/policy/task/grader population needs a new version and separated claims. No exact deployed policy catalog or remote bank integration is asserted here.

## Reporting rules

Every started attempt is retained with source/data/model/tool/policy/grader/environment/reset identities, condition, pair/family/repeat, order, seeds/clocks, raw outcomes, process trace, costs and timing. Count passed, task-failure, infrastructure-failure, invalid-oracle and incomplete attempts separately. Valid-outcome rates use only passed plus task-failure, alongside total coverage/exclusions; never hide infra/invalid failures in a headline success rate.

Report all-two-repeat success by family/condition. Missing, invalid or infrastructure-failed repeats cannot become all-k successes. At pilot size, publish descriptive paired deltas and raw counts with the explicit small-family limitation; no confidence/reliability or language fairness claim. Outcome, mandatory process violations, blocked attempts, leaks and efficiency are separate fields. Invalid gold/oracle is invalid, never an agent pass. Retain losses and corrections with old/new labels.

Record product-load, checked-answer and optional-wording latency separately with monotonic clocks. Provider cost is actual usage or unavailable/null, never invented zero-cost savings. No speed, reliability, negative-result, maintained-artifact or improvement claim exists before measurements. No T07.3 release, T07.4 reproduction, external publication or issued link is claimed.

## Check this design

From the checkout, using the configured Python test interpreter:

```powershell
python scripts/p071_check.py
python scripts/p071_protocol.py check --output .genesis/local/MY_NEW_DESIGN_RECEIPT.json
python scripts/p071_protocol.py execution-ready --output .genesis/local/MY_NEW_READINESS_RECEIPT.json
```

Use new output paths. Design integrity passes; readiness intentionally exits2 with blockers. Neither command connects to banking services, changes demo data, runs a trial or invokes a provider. Mutation checks test tooling, not banking behavior. Safe automated receipts remain local under `.genesis/evidence/`; expected manual scenarios remain Not run until actually observed.
