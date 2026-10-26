"""Read-only P07.1 protocol integrity check. Does not run banking trials/providers."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONTROLS = {'identity', 'ownership', 'rls', 'csrf_origin', 'audit', 'erasure',
            'evidence_validation', 'durable_acceptance', 'replay', 'deadline',
            'required_disambiguation'}
COMPARISONS = {'preference_update', 'memory', 'optional_clarification'}
BASELINE = 'ac45cb68c72007b372a93ccea7954e8a6633eb49'


def pin_inventory(baseline):
    paths = git('ls-tree', '-r', '--name-only', baseline).splitlines()
    return {
        'source': {p for p in paths if p.startswith(('api/', 'auth/', 'capture/', 'graphs/', 'jobs/', 'llm/', 'store/', 'evaluator/src/')) and p.endswith(('.py', '.sql'))},
        'tools': {'pyproject.toml', 'frontend/package-lock.json', 'infra/docker-compose.yml', 'evaluator/pyproject.toml'},
        'policy': {'store/banking_policies.py', 'store/banking_policy_answers.py', 'scripts/p054_policy.py', 'scripts/p043_policy.py'},
        'fixtures': {'scripts/p03_slice_fixture.py', 'scripts/p04_fixture.py', 'scripts/p042_fixture.py', 'evaluator/review_sets/pilot_v1.json'},
        'grader': {'evaluator/src/banking_evaluator/grading.py', 'evaluator/src/banking_evaluator/contracts.py'}}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def git(*args, input=None):
    return subprocess.check_output(['git', *args], input=input, cwd=ROOT, stderr=subprocess.PIPE).decode().strip()


def fields(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected.split()), 'unexpected or missing fields')


def check(folder):
    folder = Path(folder).resolve()
    seal = load(folder / 'freeze.json')
    require(set(seal) == {'schema_version', 'frozen_at', 'files'}, 'invalid freeze fields')
    require(seal['schema_version'] == '1', 'freeze version')
    instant = datetime.fromisoformat(seal['frozen_at'])
    require(instant.tzinfo is not None and instant <= datetime.now(timezone.utc), 'invalid freeze time')
    require(set(seal['files']) == {'protocol.json', 'suites.json', 'locks.json'}, 'freeze inventory')
    for name, expected in seal['files'].items():
        path = folder / name
        require(not path.is_symlink() and digest(path.read_bytes()) == expected, 'frozen bytes changed: ' + name)
    protocol, suites, locks = [load(folder / n) for n in ('protocol.json', 'suites.json', 'locks.json')]
    fields(protocol, 'schema_version id status prior_exposure budget model mandatory_controls comparisons matched_fields optional_gate_definition order pilot_split execution_requires_new_freeze world metrics')
    fields(suites, 'schema_version cases')
    fields(locks, 'schema_version baseline_commit model grader groups environment')
    require(protocol['schema_version'] == suites['schema_version'] == locks['schema_version'] == '1', 'schema version')
    require(protocol['id'] == 'p07-exploratory-v1', 'protocol identity')
    require(protocol['status'] == 'frozen-design-no-trials', 'trial results not part of protocol')
    require(protocol['prior_exposure'] == 'P06 R01-R08 and all authored P07 cases are exposed; no unseen holdout claimed.', 'exposure disclosure')
    budget = protocol['budget']
    fields(budget, 'pilot_attempts repeats_per_case_condition confirmatory_attempts live_model_requests retries_count_toward_budget automatic_replacements')
    require(all(type(budget[k]) is int for k in ('pilot_attempts', 'repeats_per_case_condition', 'confirmatory_attempts', 'live_model_requests'))
            and all(type(budget[k]) is bool for k in ('retries_count_toward_budget', 'automatic_replacements')), 'budget scalar types')
    require(budget == {'pilot_attempts': 24, 'repeats_per_case_condition': 2,
                       'confirmatory_attempts': 0, 'live_model_requests': 0,
                       'retries_count_toward_budget': True, 'automatic_replacements': False}, 'budget closed or pilot size changed')
    require(all(protocol['model'][k] is False for k in ('enabled', 'judge_enabled', 'astra_authorized')) and
            protocol['model'] == {'enabled': False, 'provider': None, 'version': None,
                                  'judge_enabled': False, 'astra_authorized': False}, 'model allowance closed')
    require(set(protocol['mandatory_controls']) == CONTROLS and
            all(v is True for v in protocol['mandatory_controls'].values()), 'backend controls must remain enabled')
    comparisons = protocol['comparisons']
    require({c['id'] for c in comparisons} == COMPARISONS and len(comparisons) == 3, 'comparison inventory')
    require(protocol['matched_fields'] == ['input', 'actor_authorization', 'world_hash', 'domain_clock',
            'security_clock_policy', 'tools', 'policy', 'model', 'grader', 'deadline', 'budgets'], 'matched context')
    for comparison in comparisons:
        fields(comparison, 'id conditions only_variable operator_status operator_digest hypothesis')
        require(len(comparison['conditions']) == 2 and len(set(comparison['conditions'])) == 2, 'paired conditions')
        require(comparison['operator_status'] == 'not-implemented' and comparison['operator_digest'] is None,
                'T07.2 execution bundle not yet pinned')
        require(comparison['only_variable'] in {'preference_mechanism', 'preference_context_read',
                                               'optional_proactive_clarification'}, 'uncontrolled variable')
    require(protocol['optional_gate_definition'] == 'Proactive nonessential conversational clarification only; required ownership/product/policy disambiguation remains on in both conditions.', 'unsafe optional gate definition')
    require(protocol['order'] == ['A,B', 'B,A'], 'counterbalanced paired order')
    require(protocol['world'] == {'db': 'unique-guarded-per-attempt', 'redis': 'unique-guarded-per-attempt',
            'files': 'unique-guarded-per-attempt', 'domain_clock': '2026-10-17T12:00:00Z',
            'security_clock': 'real-monotonic-expiry', 'reset': 'verify-marker-and-canonical-hash',
            'parallel_probe_required': True}, 'world/reset contract')
    require(protocol['metrics'] == {
        'primary': 'paired outcome counts by family; process violations reported separately',
        'attempt_denominator': 'every started attempt including task_failure, infra_failure, invalid and incomplete',
        'valid_outcome_denominator': 'valid passed + task_failure only; report exclusions and coverage separately',
        'all_k': 'family/condition passes all 2 repeats; any missing/invalid/infra repeat is not all-k success',
        'uncertainty': 'descriptive paired deltas and exact counts only; two families do not establish reliability',
        'latency': 'monotonic product-load, checked-answer, optional-wording separately; no synthetic speed claim',
        'cost': 'actual provider requests/tokens/currency only; unavailable is null; no provider calls planned',
        'regressions': 'retain wins, ties, losses, blocked attempts, leaks and invalid-oracle cases',
        'corrections': 'append corrected version and old/new labels; retain every raw attempt'}, 'metric/denominator contract')
    cases = suites['cases']
    require(len(cases) == 12 and len({c['id'] for c in cases}) == len(cases), 'case inventory')
    split_families = {s: set() for s in ('development', 'calibration', 'holdout')}
    split_sources = {s: set() for s in split_families}
    for case in cases:
        fields(case, 'id split comparison family source_family exposure confirmatory_eligible input expected_outcome expected_process')
        require(case['split'] in split_families and case['comparison'] in COMPARISONS, 'invalid split/comparison')
        require(case['exposure'] == 'author-visible-synthetic' and case['confirmatory_eligible'] is False, 'exposed holdout cannot be confirmatory')
        for key in ('id', 'family', 'source_family', 'input', 'expected_outcome', 'expected_process'):
            require(isinstance(case[key], str) and bool(case[key].strip()), 'empty case ' + key)
        split_families[case['split']].add(case['family'])
        split_sources[case['split']].add(case['source_family'])
    for left, right in [('development', 'calibration'), ('development', 'holdout'), ('calibration', 'holdout')]:
        require(not split_families[left] & split_families[right] and
                not split_sources[left] & split_sources[right], 'family/source split leakage')
    require(len({c['family'] for c in cases}) == 12, 'case family alias')
    for comparison in COMPARISONS:
        require([sum(c['comparison'] == comparison and c['split'] == split for c in cases)
                 for split in split_families] == [2, 1, 1], 'comparison split balance')
    require(protocol['pilot_split'] == 'development' and protocol['execution_requires_new_freeze'] is True,
            'execution requires versioned operator pins and new freeze')
    require(locks['model'] == protocol['model'] and locks['grader'] == 'banking-outcome-process/1.1', 'model/grader version')
    baseline = locks['baseline_commit']
    require(baseline == BASELINE, 'approved P06 baseline required')
    require(git('rev-parse', baseline + '^{commit}') == baseline, 'unknown baseline')
    require(set(locks['groups']) == {'source', 'tools', 'policy', 'fixtures', 'grader'}, 'version categories')
    inventory = pin_inventory(baseline)
    # Batch Git reads: per-file Git subprocesses made this small check need minutes on Windows.
    committed = {line.split('\t', 1)[1]: line.split('\t', 1)[0].split()[2]
                 for line in git('ls-tree', '-r', baseline).splitlines()}
    all_paths = sorted(set().union(*inventory.values()))
    hashes = git('hash-object', '--stdin-paths', input=('\n'.join(all_paths) + '\n').encode()).splitlines()
    require(len(hashes) == len(all_paths), 'current source inventory')
    current = dict(zip(all_paths, hashes))
    protocol_paths = ['research/protocols/v1/' + name for name in seal['files']] + ['research/protocols/v1/freeze.json']
    attributes = git('check-attr', 'text', '--', *protocol_paths).splitlines()
    require(len(attributes) == 4 and all(line.endswith(': text: unset') for line in attributes), 'frozen files require exact-byte Git attributes')
    for category, files in locks['groups'].items():
        require(set(files) == inventory[category], 'incomplete version category ' + category)
        for path, blob in files.items():
            require(not Path(path).is_absolute() and '..' not in Path(path).parts and
                    not path.startswith('-') and re.fullmatch('[0-9a-f]{40}', blob), 'unsafe version path/blob')
            require(committed[path] == blob, 'baseline pin mismatch: ' + path)
            require(current[path] == blob, 'current source drift: ' + path)
    return {'status': 'passed-design-integrity', 'protocol': protocol['id'], 'freeze_sha256': digest((folder / 'freeze.json').read_bytes()),
            'case_count': len(cases), 'pilot_attempts_planned': 24, 'actual_trial_attempts': 0,
            'model_requests_made_by_tool': 0, 'confirmatory_eligible': False, 'execution_ready': False,
            'execution_blockers': ['Comparison operators not implemented/pinned', 'New execution freeze required before any trial',
                                   'Confirmatory/live budgets are zero; reserve cases exposed'], 'baseline_commit': baseline}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'execution-ready'])
    parser.add_argument('--protocol', type=Path, default=ROOT / 'research/protocols/v1')
    parser.add_argument('--output', type=Path, required=True, help='New receipt, never overwritten')
    args = parser.parse_args()
    # Reserve receipt before checking; no overwrite even for rejected protocols.
    with args.output.open('x', encoding='utf-8') as receipt:
        try:
            report = check(args.protocol)
            code = 0
            if args.action == 'execution-ready':
                report['status'] = 'blocked-no-trial-execution'
                code = 2
        except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError) as error:
            report = {'status': 'rejected', 'reason': str(error), 'actual_trial_attempts': 0,
                      'model_requests_made_by_tool': 0, 'execution_ready': False}
            code = 1
        receipt.write(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'status': report['status'], 'report': str(args.output)}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
