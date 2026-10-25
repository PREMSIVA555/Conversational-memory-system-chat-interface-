"""Version1 JSON contracts. Projection smoke checks are not validated graders."""
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Protocol

VERSION = "1"
GRADER = "projection-smoke/1-unvalidated"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parse_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('Duplicate JSON key')
            value[key] = item
        return value
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite number')))


def versioned(value, required, optional=()):
    if not isinstance(value, dict) or value.get('schema_version') != VERSION:
        raise ValueError('Unsupported schema version')
    if not set(required) <= value.keys() or value.keys() - set(required) - set(optional) - {'schema_version'}:
        raise ValueError('Invalid contract fields')
    canonical(value)


@dataclass(frozen=True)
class Task:
    id: str
    steps: list[dict]
    projection: dict[str, list[str]]
    expected: dict
    schema_version: str = VERSION

    @classmethod
    def parse(cls, value):
        versioned(value, ('id', 'steps', 'projection', 'expected'))
        if not isinstance(value['id'], str) or not value['id'].strip() or not 1 <= len(value['id']) <= 100:
            raise ValueError('Task id required')
        if not isinstance(value['steps'], list) or not 1 <= len(value['steps']) <= 100:
            raise ValueError('One to100 observable steps required')
        if any(not isinstance(s, dict) or not isinstance(s.get('op'), str) or not s['op'].strip() for s in value['steps']):
            raise ValueError('Each step requires op')
        projection = value['projection']
        if not isinstance(projection, dict) or not projection or not isinstance(value['expected'], dict) or projection.keys() != value['expected'].keys():
            raise ValueError('Projection/expected keys must agree')
        if any(not isinstance(path, list) or not path or any(not isinstance(k, str) or not k for k in path) for path in projection.values()):
            raise ValueError('Projection requires explicit object paths')
        return cls(**value)


@dataclass(frozen=True)
class ObservableTrace:
    events: list[dict]
    schema_version: str = VERSION


@dataclass(frozen=True)
class FinalState:
    values: dict
    schema_version: str = VERSION


@dataclass(frozen=True)
class Grade:
    status: str
    reason: str
    grader_version: str = GRADER
    schema_version: str = VERSION


@dataclass(frozen=True)
class Observation:
    results: list
    oracle: dict
    identities: dict
    reset: dict
    schema_version: str = VERSION

    @classmethod
    def parse(cls, value, task):
        versioned(value, ('results', 'oracle', 'identities', 'reset'))
        if not isinstance(value['results'], list) or len(value['results']) != len(task.steps) or not isinstance(value['oracle'], dict):
            raise ValueError('Missing observable results/state')
        identities = value['identities']
        if not isinstance(identities, dict) or set(identities) != {'source','data','model','tools','policy','environment'} or any(not isinstance(s,str) or not s.strip() for s in identities.values()):
            raise ValueError('Complete declared identities required')
        reset = value['reset']
        if not isinstance(reset, dict) or set(reset) != {'namespace','semantic_hash','kind'} or any(not isinstance(s,str) or not s.strip() for s in reset.values()):
            raise ValueError('Reset/namespace identity required')
        return cls(**value)


class Adapter(Protocol):
    name: str
    kind: str
    def execute(self, task: Task, run_id: str) -> Observation: ...


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    observed_at: str
    task_id: str
    task_hash: str
    adapter: dict
    identities: dict
    reset: dict
    environment: dict
    trace: ObservableTrace
    final_state: FinalState
    grade: Grade
    raw_observation: dict | None
    raw_hash: str | None
    error: str | None
    schema_version: str = VERSION

    def to_dict(self):
        value = asdict(self)
        canonical(value)
        return value
