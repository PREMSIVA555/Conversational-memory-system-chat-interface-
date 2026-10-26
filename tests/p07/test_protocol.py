"""Adverse protocol contracts; these are tooling tests, never experiment results."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('p071_protocol', ROOT / 'scripts/p071_protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


class ProtocolContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p071-contract-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name in ('protocol.json', 'suites.json', 'locks.json', 'freeze.json'):
            (self.folder / name).write_bytes((ROOT / 'research/protocols/v1' / name).read_bytes())

    def change(self, name, edit, reseal=True):
        path = self.folder / name
        data = json.loads(path.read_text(encoding='utf-8'))
        edit(data)
        path.write_text(json.dumps(data, ensure_ascii=False) + '\n', encoding='utf-8')
        if reseal:
            seal = json.loads((self.folder / 'freeze.json').read_text(encoding='utf-8'))
            seal['files'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
            (self.folder / 'freeze.json').write_text(json.dumps(seal), encoding='utf-8')

    def reject(self, name, edit, reason):
        self.change(name, edit)
        with self.assertRaisesRegex(ValueError, reason):
            protocol.check(self.folder)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/p071_protocol.py'), *args], cwd=ROOT,
                              text=True, encoding='utf-8', capture_output=True)

    def test_design_passes_without_execution(self):
        result = protocol.check(self.folder)
        self.assertEqual(result['actual_trial_attempts'], 0)
        self.assertEqual(result['model_requests_made_by_tool'], 0)
        self.assertFalse(result['execution_ready'])
        self.assertFalse(result['confirmatory_eligible'])

    def test_frozen_bytes(self):
        self.change('suites.json', lambda x: x['cases'][0].update(input='changed'), reseal=False)
        with self.assertRaisesRegex(ValueError, 'frozen bytes changed'):
            protocol.check(self.folder)

    def test_family_source_and_id_leakage(self):
        for field, reason in [('family', 'leakage'), ('source_family', 'leakage'), ('id', 'inventory')]:
            with self.subTest(field=field):
                original = (self.folder / 'suites.json').read_bytes()
                self.reject('suites.json', lambda x: x['cases'][6].update({field: x['cases'][0][field]}), reason)
                (self.folder / 'suites.json').write_bytes(original)
                self.change('suites.json', lambda x: None)

    def test_exposed_reserve_cannot_be_confirmatory(self):
        self.reject('suites.json', lambda x: x['cases'][-1].update(confirmatory_eligible=True), 'exposed holdout')

    def test_live_and_confirmatory_budget_closed(self):
        for field in ('live_model_requests', 'confirmatory_attempts', 'pilot_attempts', 'repeats_per_case_condition'):
            with self.subTest(field=field):
                self.reject('protocol.json', lambda x: x['budget'].update({field: 99}), 'budget')
                self.change('protocol.json', lambda x: x['budget'].update({field: json.loads((ROOT / 'research/protocols/v1/protocol.json').read_text(encoding='utf-8'))['budget'][field]}))

    def test_every_backend_control_required(self):
        for control in protocol.CONTROLS:
            with self.subTest(control=control):
                self.reject('protocol.json', lambda x: x['mandatory_controls'].update({control: False}), 'backend controls')
                self.change('protocol.json', lambda x: x['mandatory_controls'].update({control: True}))

    def test_models_cannot_enable(self):
        self.reject('protocol.json', lambda x: x['model'].update(enabled=True), 'model allowance')

    def test_execution_status_cannot_be_fabricated(self):
        self.reject('protocol.json', lambda x: x['comparisons'][0].update(operator_status='ready', operator_digest='a'*64), 'not yet pinned')

    def test_no_failures_removed_from_denominator(self):
        self.reject('protocol.json', lambda x: x['metrics'].update(attempt_denominator='passed only'), 'denominator')

    def test_no_automatic_replacement_attempts(self):
        self.reject('protocol.json', lambda x: x['budget'].update(automatic_replacements=True), 'budget')

    def test_optional_gate_cannot_disable_required_ambiguity(self):
        self.reject('protocol.json', lambda x: x.update(optional_gate_definition='Disable ownership ambiguity'), 'unsafe optional gate')

    def test_world_reset_cannot_share_state(self):
        self.reject('protocol.json', lambda x: x['world'].update(redis='shared'), 'world/reset')

    def test_pins_cannot_drop_a_versioned_source(self):
        self.reject('locks.json', lambda x: x['groups']['source'].pop('api/foundation.py'), 'incomplete version')

    def test_wrong_baseline_rejected(self):
        self.reject('locks.json', lambda x: x.update(baseline_commit='0'*40), 'approved P06')

    def test_wrong_blob_rejected(self):
        self.reject('locks.json', lambda x: x['groups']['grader'].update({'evaluator/src/banking_evaluator/grading.py': '0'*40}), 'pin mismatch')

    def test_unknown_trial_claim_rejected(self):
        self.reject('suites.json', lambda x: x['cases'][0].update(trial_result='passed'), 'unexpected or missing')

    def test_unknown_protocol_flag_rejected(self):
        self.reject('protocol.json', lambda x: x.update(surprise_execution_enabled=True), 'unexpected or missing')

    def test_boolean_budget_is_not_integer(self):
        self.reject('protocol.json', lambda x: x['budget'].update(live_model_requests=False), 'budget scalar')

    def test_duplicate_json_key(self):
        (self.folder / 'freeze.json').write_text('{"schema_version":"1","schema_version":"1"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON'):
            protocol.check(self.folder)

    def test_future_freeze_rejected(self):
        self.change('freeze.json', lambda x: x.update(frozen_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()), reseal=False)
        with self.assertRaisesRegex(ValueError, 'freeze time'):
            protocol.check(self.folder)

    def test_cli_readiness_is_blocked_and_receipt_immutable(self):
        output = self.folder / 'receipt.json'
        result = self.cli('execution-ready', '--protocol', str(self.folder), '--output', str(output))
        self.assertEqual(result.returncode, 2, result.stderr)
        original = output.read_bytes()
        self.assertEqual(json.loads(original)['status'], 'blocked-no-trial-execution')
        second = self.cli('check', '--protocol', str(self.folder), '--output', str(output))
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(output.read_bytes(), original)

    def test_cli_unknown_does_not_create_receipt(self):
        output = self.folder / 'unknown.json'
        self.assertNotEqual(self.cli('run-trials', '--output', str(output)).returncode, 0)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
