"""Package contract boundaries, not T06.2 banking-grader validation."""
from dataclasses import asdict,replace
import json
from pathlib import Path
import subprocess
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'evaluator/src'))
from banking_evaluator import Task, Observation, CommandAdapter, ReplayAdapter, run
from banking_evaluator.contracts import parse_json,digest


def task():
    return Task.parse(dict(schema_version='1',id='count',steps=[{'op':'read'}],projection={'count':['count']},expected={'count':1}))


class Script:
    name='script/1';kind='test-fixture'
    def __init__(self, value=None): self.value=value
    def execute(self,t,rid):
        return self.value or Observation([{'count':1}],{'count':1},
            dict(source='fixture',data='fixture',model='not-used',tools='fixture',policy='not-applicable',environment='fixture'),
            dict(namespace=rid,semantic_hash='fixture',kind='isolated-fixture'))


def test_versions_missing_identity_and_type_mismatch():
    value=asdict(task())
    with pytest.raises(ValueError):Task.parse({**value,'schema_version':'2'})
    with pytest.raises(ValueError):Task.parse({**value,'projection':{}})
    with pytest.raises(ValueError):Task.parse({**value,'id':' '})
    with pytest.raises(ValueError):Task.parse({**value,'steps':[{'op':''}]})
    with pytest.raises(ValueError):parse_json('{"id":1,"id":2}')
    with pytest.raises(ValueError):parse_json('{"x":NaN}')
    broken=replace(Script().execute(None,'trial'),identities={})
    assert run(task(),Script(broken)).grade.status=='invalid'
    broken=replace(Script().execute(None,'trial'),schema_version='2')
    assert run(task(),Script(broken)).grade.status=='invalid'
    assert run(replace(task(),expected={'count':True}),Script()).grade.status=='fail'


def test_repeatability_namespace_replay_recompute_and_tamper(tmp_path):
    first=run(task(),Script()).to_dict();second=run(task(),Script()).to_dict()
    assert first['reset']['namespace']!=second['reset']['namespace']
    assert first['final_state']==second['final_state']
    path=tmp_path/'run.json';first['grade']['status']='fail';path.write_text(json.dumps(first),encoding='utf-8')
    assert run(task(),ReplayAdapter(path)).grade.status=='pass'
    first['raw_observation']['oracle']['count']=2;path.write_text(json.dumps(first),encoding='utf-8')
    assert run(task(),ReplayAdapter(path)).grade.status=='invalid'
    first['raw_hash']=digest(first['raw_observation']);path.write_text(json.dumps(first),encoding='utf-8')
    assert run(task(),ReplayAdapter(path)).grade.status=='fail'
    assert run(replace(task(),id='different'),ReplayAdapter(path)).grade.status=='invalid'


def test_missing_state_and_failed_operator_never_pass():
    broken=replace(Script().execute(None,'trial'),oracle={})
    assert run(task(),Script(broken)).grade.status=='invalid'
    assert run(task(),CommandAdapter([sys.executable,'-c','raise SystemExit(7)'])).grade.status=='unknown'
    assert run(task(),CommandAdapter([sys.executable,'-c','print("not-json")'])).grade.status=='invalid'
    assert run(task(),CommandAdapter([sys.executable,'-c','import time;time.sleep(2)'],timeout=.05)).grade.status=='unknown'
