"""Independent-oracle rejection and destructive-target guard regression checks."""
from copy import deepcopy
import pytest
from evals.banking_world import BankingWorld,UnsafeTarget,database_dsn
from scripts.p03_slice import grade_evidence,money

def test_shared_or_remote_reset_target_refused_before_connection():
    for name in ['bank_p01','memory_system','p03_slice_../bank_p01','p03_slice_unknown']:
        with pytest.raises(UnsafeTarget):BankingWorld({},name,'test').guard()
    for value in ['postgresql://owner@remote.invalid:5432/bank_p01','postgresql://owner@127.0.0.1/bank_p01','postgresql://owner@127.0.0.1:5432/bank_p01?options=unsafe']:
        with pytest.raises(UnsafeTarget):database_dsn(value,'p03_slice_'+'a'*32)

def test_oracle_grader_rejects_wrong_amount_version_source_and_missing_balance():
    ref='10000000-0000-4000-8000-000000000001';at='2026-01-15T12:00:00+00:00'
    oracle={'accounts':[(ref,'owner','Synthetic','INR','9007199254740993','7','3',at)],'transactions':[]}
    result=dict(status='completed',text='INR 90071992547409.93; INR 0.07',evidence=[dict(ref=ref,field=field,value=dict(minor_units=minor,currency='INR',exponent=2),version='3',as_of=at,authority='fresh_authorized_bank') for field,minor in [('current_balance','9007199254740993'),('available_balance','7')]])
    grade_evidence(result,oracle,'balance',ref)
    for change in ['amount','version','source','missing','text']:
        invalid=deepcopy(result)
        if change=='amount':invalid['evidence'][0]['value']['minor_units']='9007199254740992'
        elif change=='version':invalid['evidence'][0]['version']='2'
        elif change=='source':invalid['evidence'][0]['as_of']='2026-01-14T12:00:00+00:00'
        elif change=='missing':invalid['evidence'].pop()
        else:invalid['text']='INR 999.00'
        with pytest.raises(AssertionError):grade_evidence(invalid,oracle,'balance',ref)

def test_oracle_integer_format_preserves_large_negative_and_zero_values():
    assert money('9007199254740993','INR')=='INR 90071992547409.93'
    assert money('-1','INR')=='INR -0.01'
    assert money('0','USD')=='USD 0.00'
