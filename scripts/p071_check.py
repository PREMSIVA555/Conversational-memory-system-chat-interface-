"""Native T07.1 gate: protocol integrity and adverse contract checks, no trials."""
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    # stdlib only; no application imports or service/provider connections.
    report = ROOT / ('.genesis/evidence/T07.1-protocol-' + uuid.uuid4().hex + '.json')
    tested = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests/p07', '-v'], cwd=ROOT)
    if tested.returncode:
        return tested.returncode
    command = [sys.executable, str(ROOT / 'scripts/p071_protocol.py'), 'check', '--output', str(report)]
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        return result.returncode
    print(json.dumps({'status': 'passed-protocol-only', 'report': str(report), 'actual_trials': 0,
                      'human_approval': False, 'P07_complete': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
