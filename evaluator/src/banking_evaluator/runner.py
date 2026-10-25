from dataclasses import asdict
from datetime import datetime, timezone
import platform
from uuid import uuid4
from .contracts import Task, Observation, FinalState, Grade, ObservableTrace, RunManifest, canonical, digest, parse_json


def run(task, adapter):
    task = Task.parse(parse_json(canonical(asdict(task))))
    run_id = uuid4().hex
    raw = None
    state = {}
    events = []
    identities = {}
    reset = {}
    error = None
    try:
        observation = Observation.parse(asdict(adapter.execute(task, run_id)),task)
        raw = asdict(observation)
        identities = observation.identities
        reset = observation.reset
        events = [dict(sequence=i+1, action=s, observation=r) for i,(s,r) in enumerate(zip(task.steps,observation.results))]
        for key,path in task.projection.items():
            value = observation.oracle
            for part in path:
                if not isinstance(value,dict) or part not in value: raise KeyError('Missing projected state')
                value = value[part]
            state[key] = value
        # JSON comparison avoids Python's True ==1 ambiguity. This is a smoke
        # oracle only; T06.2 validates banking outcomes/mandatory observables.
        status = 'pass' if canonical(state) == canonical(task.expected) else 'fail'
        grade = Grade(status, 'Declared projection matches' if status=='pass' else 'Declared projection differs')
    except (ValueError, KeyError, TypeError) as exc:
        error = type(exc).__name__
        grade = Grade('invalid', 'Contract or state evidence is invalid/missing')
    except Exception as exc:
        error = type(exc).__name__
        grade = Grade('unknown', 'Operator/infrastructure failed; no success claim')
    return RunManifest(run_id,datetime.now(timezone.utc).isoformat(),task.id,digest(asdict(task)),
        dict(name=adapter.name,kind=adapter.kind),{**identities,'grader':grade.grader_version},reset,
        dict(python=platform.python_version(),platform=platform.platform(),evaluator='0.3.0'),
        ObservableTrace(events),FinalState(state),grade,raw,digest(raw) if raw is not None else None,error)
