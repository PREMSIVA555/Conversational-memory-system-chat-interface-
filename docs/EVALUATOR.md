# Evaluator0.1.0 — T06.1 approved2026-10-25

This standalone package generalizes the early JSON behavioral runner. It requires Python3.11+, uses the standard library and imports no banking runtime. It is separate from the application's existing wheel. Its projection grader is an **unvalidated smoke oracle**. Banking outcome/trajectory attack validation is T06.2; human labels/optional judge calibration are T06.3. There are no paid calls or subjective quality claims.

Building uses pinned setuptools75.8.0 and wheel0.45.1 in pip's isolated build environment (network/cache required). Installation of the resulting wheel needs no runtime dependencies or editable checkout. Version1 describes the Python contracts and wire format below; it is not a claim that an external JSON Schema validator was run.

## Public version1 contract

All JSON contracts carry `schema_version: "1"`. Unsupported versions, extra task/observation fields, duplicate JSON keys, nonfinite numbers, missing identities or missing projected state are invalid. Public exports: `Task`, `Adapter`, `Observation`, `ObservableTrace`, `FinalState`, `Grade`, `RunManifest`, `CommandAdapter`, `ReplayAdapter`, `run`.

| Contract | Fields / meaning |
|---|---|
| Task | id, steps (1–100 JSON objects with op), projection (named object-key paths), expected (same keys). Example below. |
| Adapter | Protocol execute(task, run_id) returns Observation; name/version and kind identify the implementation. |
| Observation | results (one per requested step), oracle, identities, reset. Identifies observed facts, not trusted authorization. |
| ObservableTrace | events with sequence, requested action and actual observation. Chronology is recorded; no golden action order grader. |
| FinalState | values projected from the observed oracle. Missing paths invalidate the run. |
| Grade | pass/fail/invalid/unknown, reason, grader_version. JSON comparison distinguishes true from1. |
| RunManifest | unique run ID, UTC time, task hash, adapter, source/data/model/tools/policy/environment/grader identities, reset, actual evaluator environment, trace, projected state, grade, raw observation/hash and safe error class. |

`pass` means the declared projection matched, not that banking correctness/security or naturalness is validated. A mismatched projection fails; bad contracts/state are invalid; process/timeout/infra failures are unknown. No exception becomes a success. Input failures retain an explicitly invalid input record. Evidence files cannot be overwritten.

CommandAdapter runs an explicitly configured trusted argv list without a shell, bounded to120seconds. No command can come from a model/browser. Stderr is not copied into a user result. This is a local evaluation tool, not a sandbox for hostile executables. The operator receives `{schema_version,run_id,steps}` and returns Observation. It declares complete source/data/model/tools/policy/environment identities and reset namespace/semantic hash/kind. Those declarations are evidence pointers, not attestations of honesty.

ReplayAdapter checks the task/raw hashes and recomputes the projection; it ignores a saved grade. Its manifest explicitly says **replay-not-fresh-execution**, retaining the original reset identity. It never claims a fresh world/reset. Hashes detect accidental/tampered content without a recomputed hash; they are not signatures and cannot authenticate a malicious self-consistent record.

## Clean installation and two adapters

Build from the repository root, then change to a new folder outside the checkout:

```powershell
python -m pip wheel --no-deps --wheel-dir C:\Temp\bank-wheels .\evaluator
python -m venv C:\Temp\bank-eval-env
C:\Temp\bank-eval-env\Scripts\python -m pip install --no-index --no-deps C:\Temp\bank-wheels\banking_evaluator-0.1.0-py3-none-any.whl
Set-Location C:\Temp
C:\Temp\bank-eval-env\Scripts\banking-eval --demo --output counter-run.json
C:\Temp\bank-eval-env\Scripts\banking-eval --demo --replay counter-run.json --output counter-replay.json
```

Use new paths if those outputs already exist. The bundled counter is a deliberately simple synthetic second-system fixture, not bank evidence. Command execution and replay both produce count2; replay is labeled separately. Imports resolve from the new environment's site-packages, with no editable/source import. The installed CLI also supports `--task task.json --command-json '["python","operator.py"]' --timeout60 --output new-run.json`.

```json
{"schema_version":"1","id":"counter-check","steps":[{"op":"increment"},{"op":"read"}],"projection":{"count":["count"]},"expected":{"count":1}}
```

## Actual banking bridge and limits

`evaluator/banking_operator.py` is an **external application adapter**, deliberately not bundled in the standalone wheel. It wraps the preserved JourneyProbe, using real authenticated HTTP/provider reads and the existing guarded per-trial PostgreSQL16/Redis7 reset. It requires the application's private test configuration/interpreter. It records source/fixture hashes, installed dependencies, active policy set, blocked model attempts and actual world namespace/semantic hash. Normal demo data is never reset. Safe raw observations/receipts stay in ignored local evidence.

`python scripts/p061_check.py` builds a wheel, creates a noneditable environment outside the checkout, runs both adapters, then invokes that external bridge twice from the installed CLI. Actual product discovery must return200, identity matching must hold, financial resources remain unchanged, model attempts are0, namespaces differ and initial semantic hashes match. Contract tests exercise invalid evidence, version/type errors, replay mutation, process failure and timeout. This is package/adapter verification, not P06 completion or T06.2 oracle calibration. Raw gate logs and the outside-checkout environments are retained for inspection; no release/publication or production portability claim is made.
