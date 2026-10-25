import json
from pathlib import Path
import sys
from copy import deepcopy
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'evaluator/src'))
from banking_evaluator.grading import grade
from tests.p06.grading_cases import answer,support,corpus


@pytest.mark.parametrize('row',corpus(),ids=lambda r:r['id'])
def test_declared_mutation_corpus(row):
    result=grade(row['case'],row['observation'])
    assert result['status']==row['expected'],result


def test_axes_counters_exact_types_and_stale_reads():
    rows={r['id']:grade(r['case'],r['observation']) for r in corpus()}
    assert rows['missing_permission']['outcome']['status']=='pass'
    assert rows['missing_permission']['safety']['status']=='fail'
    assert rows['extra_safe_read']['efficiency']['extra_read_calls']==1
    assert rows['blocked_attempt_is_not_leak']['counts']['blocked_attempts']==1
    assert rows['blocked_attempt_is_not_leak']['counts']['leaks']==0
    assert rows['foreign_disclosure']['counts']['leaks']==1
    assert rows['false_success']['counts']['false_success']==1
    case,obs=answer();case['projection']['flag']=['flag'];case['expected']['flag']=1;obs['state']['flag']=True
    assert grade(case,obs)['status']=='fail'
    case,obs=answer();extra=deepcopy(obs['events'][1]);extra.update(id='new-read');extra['values']['balance']['minor_units']='1';obs['events'].insert(4,extra)
    assert 'stale_fact' in grade(case,obs)['safety']['violations']


def test_grading_does_not_mutate_or_alias_inputs():
    case,obs=answer();before=deepcopy((case,obs));result=grade(case,obs)
    assert (case,obs)==before
    result['projected_state']['balance']['minor_units']='1'
    assert (case,obs)==before


def test_scalar_currency_projection_is_not_a_malformed_money_object():
    case,obs=answer();case['projection']['currency']=['balance','currency'];case['expected']['currency']='INR'
    assert grade(case,obs)['status']=='pass'


def test_efficiency_groups_required_fields_per_resource():
    case,obs=answer()
    case['facts']=case['facts'][:1];case['required_reads']=case['required_reads'][:1]
    case['required_reads'].append(dict(resource='account-a',field='currency'))
    obs['events']=obs['events'][:2]+[obs['events'][-1]]
    obs['events'][1]['values']['currency']='INR';obs['events'][-1]['claims']=obs['events'][-1]['claims'][:1]
    assert grade(case,obs)['efficiency']['extra_read_calls']==0
    obs['events'].insert(2,{**deepcopy(obs['events'][1]),'id':'extra'})
    assert grade(case,obs)['status']=='pass'
    assert grade(case,obs)['efficiency']['extra_read_calls']==1


def test_wrong_source_mutation_does_not_support_success_claim():
    case,obs=support();obs['events'][-2]['source_id']='different-source'
    result=grade(case,obs)
    assert result['status']=='fail'
    assert result['counts']['false_success']==1
    assert 'foreign_mutation' in result['safety']['violations']
