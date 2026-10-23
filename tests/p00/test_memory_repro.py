"""Seeded evidence defects must fail the T00.3 report validator; no services used."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("p00_memory_repro", ROOT / "scripts/p00_reproduce_memory.py")
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)


class EvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        cases = []
        for name in ("legacy_null_attribute", "typed_control", "inverted_completion"):
            observations = dict(sole_value_is_newest_source=name == "typed_control")
            cases.append(dict(case_id=name, observations=observations))
            repro.write_json(self.path / (name + ".json"), dict(observations=observations,
                             answer=dict(kind="deterministic-stub", live_model_answer=None)))
        repro.write_json(self.path / "migrations.json", dict(applied_files=10))
        self.report = dict(task_id="T00.3", status="observed", source_hashes=repro.source_hashes(),
                           environment={}, cases=cases, limitations=["Synthetic fixture only"],
                           original_live_incident=dict(status="not-reproduced"), model="not-called",
                           reset="new-test-database", grader="integrity-check", command=["test"],
                           artifacts={p.name: repro.hash_file(p) for p in self.path.glob("*.json")})

    def check_report(self, report):
        repro.write_json(self.path / "report.json", report)
        with redirect_stdout(io.StringIO()):
            repro.verify(self.path)

    def test_intact_synthetic_report_is_valid(self):
        self.check_report(self.report)

    def test_changed_artifact_is_rejected(self):
        (self.path / "migrations.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.check_report(self.report)

    def test_original_live_incident_claim_is_rejected(self):
        self.report["original_live_incident"]["status"] = "reproduced"
        with self.assertRaisesRegex(ValueError, "original live incident"):
            self.check_report(self.report)

    def test_missing_case_is_rejected(self):
        self.report["cases"][2] = copy.deepcopy(self.report["cases"][1])
        with self.assertRaisesRegex(ValueError, "Missing or duplicate"):
            self.check_report(self.report)

    def test_stale_source_identity_is_rejected(self):
        self.report["source_hashes"]["store/memories.py"] = "wrong-source-hash"
        with self.assertRaisesRegex(ValueError, "sources changed"):
            self.check_report(self.report)

    def test_summary_disagreement_is_rejected_even_with_matching_file_hash(self):
        name = "typed_control.json"
        detail = repro.read_json(self.path / name)
        detail["observations"]["sole_value_is_newest_source"] = False
        repro.write_json(self.path / name, detail)
        self.report["artifacts"][name] = repro.hash_file(self.path / name)
        with self.assertRaisesRegex(ValueError, "Summary/detail"):
            self.check_report(self.report)


if __name__ == "__main__":
    unittest.main()
