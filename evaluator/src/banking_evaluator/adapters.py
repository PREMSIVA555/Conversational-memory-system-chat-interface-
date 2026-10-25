"""Adapters exchange the same explicit steps/results/oracle protocol."""
from dataclasses import asdict
from pathlib import Path
import subprocess
from .contracts import Observation, canonical, digest, parse_json


class CommandAdapter:
    name = 'json-command/1'
    kind = 'executed'

    def __init__(self, command, *, timeout=60, cwd=None):
        if not command or any(not isinstance(s,str) or not s for s in command) or not 0 < timeout <= 120:
            raise ValueError('Explicit command and bounded timeout required')
        self.command = list(command)
        self.timeout = timeout
        self.cwd = cwd

    def execute(self, task, run_id):
        # Local trusted CLI configuration, never shell syntax or model dispatch.
        result = subprocess.run(self.command, input=canonical({'schema_version':'1','run_id':run_id,'steps':task.steps}),
                                text=True, encoding='utf-8', capture_output=True, shell=False,
                                cwd=self.cwd, timeout=self.timeout, check=False)
        if result.returncode: raise RuntimeError('Operator process failed')
        if len(result.stdout.encode()) > 8_000_000: raise ValueError('Oversized observation')
        return Observation.parse(parse_json(result.stdout), task)


class ReplayAdapter:
    name = 'saved-observation/1'
    kind = 'replay-not-fresh-execution'

    def __init__(self, path): self.path = Path(path)

    def execute(self, task, run_id):
        report = parse_json(self.path.read_text(encoding='utf-8'))
        if report.get('schema_version') != '1' or report.get('task_hash') != digest(asdict(task)):
            raise ValueError('Replay task/version mismatch')
        raw = report.get('raw_observation')
        if raw is None or report.get('raw_hash') != digest(raw):
            raise ValueError('Missing or changed raw observation')
        # Recompute state/grade; never trust the saved verdict. Hash is an
        # integrity check, not an authenticity/signature claim.
        return Observation.parse(raw, task)
