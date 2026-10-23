"""Run real P00 persistence checks and retain an exclusive evidence directory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(destination: Path, config_path: Path) -> int:
    config_path = config_path.resolve(strict=True)
    os.environ["P00_WORLD_CONFIG"] = str(config_path)
    destination.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location("p00_checks", ROOT / "tests/p00/test_world.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    log = stream.getvalue()
    (destination / "checks.txt").write_text(log, encoding="utf-8")
    (destination / "snapshots.json").write_text(json.dumps(module.SNAPSHOTS, indent=2) + "\n", encoding="utf-8")
    config = json.loads(config_path.read_text())
    sources = ["evals/world.py", "evals/fixtures/p00_world.json", "tests/p00/test_world.py", "scripts/p00_check_world.py", "scripts/p00_services.py"]
    report = dict(task_id="T00.2", scenario_id="P00-WORLD-001", recorded_at=datetime.now(timezone.utc).isoformat(),
                  command=[sys.executable, *sys.argv], tests=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                  status="passed" if result.wasSuccessful() and not result.skipped else "failed-or-incomplete",
                  source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                  source_hashes={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources},
                  data="p00-two-customers-v1", model="not-called", policy="not-applicable-to-fixture-skeleton",
                  tool="p00-world-v1", grader="unittest-state-and-target-checks-v1", reset="per-trial-schema-cache-files-v1",
                  environment={key: config.get(key) for key in ("backend", "postgres_version", "redis_version", "instance", "system_identifier")},
                  artifacts={name: hashlib.sha256((destination/name).read_bytes()).hexdigest() for name in ("checks.txt", "snapshots.json")},
                  reviewer=None, limitations=["Persistence-layer fixture skeleton, not banking API, authentication, RLS or live model validation.",
                                             "Backend versions are recorded above; container identity requires separate runtime evidence.",
                                             "No session expiry claim; that behavior belongs to P01."])
    (destination / "reset-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(log)
    print(f"Evidence: {destination / 'reset-report.json'}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / ".genesis/local/p00-world.json")
    args = parser.parse_args()
    raise SystemExit(main(args.directory, args.config))
