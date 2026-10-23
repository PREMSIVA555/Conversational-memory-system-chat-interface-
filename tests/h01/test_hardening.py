"""H01 regression boundaries, with real policy service and scripted SDK plans."""
import json
from uuid import uuid4
import pytest
from api.privacy import model_input
from graphs.account_coordinator import AccountCoordinator
from scripts.p03_slice import RawScript
from tests.p04.test_policy_answers import policy_world, policy_service
from graphs.policy_request import bind_policy_request


@pytest.mark.parametrize('sensitive',[
    'My name is Alice Example.', 'My phone is 9876543210.',
    'SSN 123-45-6789', 'Call +91 98765 43210.',
    'Card 4111\t1111\t1111\t1111', 'Card 4111\u00a01111\u00a01111\u00a01111',
    'SSN 123\u201145\u20116789',
    'Email alice\u200b@example.test',
])
def test_sensitive_identifiers(sensitive):
    scrubbed=model_input(sensitive)
    assert not any(value in scrubbed for value in ('Alice Example','9876543210','123-45-6789','98765 43210','4111','123\u201145\u20116789'))
    if 'Email' in sensitive:assert 'alice' not in scrubbed


def test_planning_tokens_retained():
    text='Balance 12345678-1234-4234-8234-123456789abc; INR 9876543210.00; policy en-IN at 2026-06-01T00:00:00Z; card ending 4321'
    assert model_input(text)==text
    assert model_input('Search transactions for amount 123456789')=='Search transactions for amount 123456789'


@pytest.mark.asyncio
@pytest.mark.parametrize('constraint',[
    'de-DE at 2026-06-01T00:00:00Z',
    'scope business at 2026-06-01T00:00:00Z',
    'at 2025-06-01T00:00:00Z',
])
async def test_model_omitted_policy_filter(policy_service,monkeypatch,constraint):
    _,provider,context,db=policy_service
    monkeypatch.setenv('LLM_STRUCTURED_JSON_ENABLED','true')
    script=RawScript();script.set(dict(tool='explain_policy',args=dict(product='credit-card',topic='annual_fee')))
    result=await AccountCoordinator(db,provider,script,run_seconds=15).run(context,str(uuid4()),'Explain credit card annual fee '+constraint)
    assert not result['evidence'] and result['status'] in ('no_evidence','clarify')


@pytest.mark.asyncio
@pytest.mark.parametrize('text',[
    'Explain credit card annual fee en-IN de-DE at 2026-06-01T00:00:00Z',
    'Explain credit card annual fee not de-DE at 2026-06-01T00:00:00Z',
    'He said "de-DE"; explain credit card annual fee at 2026-06-01T00:00:00Z',
    'Explain credit card annual fee at 2026-06-01',
    'Explain credit card annual fee scope business and retail at 2026-06-01T00:00:00Z',
])
async def test_ambiguous_constraints_clarify_without_tool(policy_service,monkeypatch,text):
    _,provider,context,db=policy_service
    monkeypatch.setenv('LLM_STRUCTURED_JSON_ENABLED','true')
    script=RawScript();script.set(dict(tool='explain_policy',args=dict(product='credit-card',topic='annual_fee')))
    result=await AccountCoordinator(db,provider,script,run_seconds=15).run(context,str(uuid4()),text)
    assert result['status']=='clarify' and not result['evidence']
    assert [u['kind'] for u in result['usage']]==['model']


@pytest.mark.parametrize('field,value,text',[
    ('locale','en-IN','Explain credit card annual fee de-DE'),
    ('scope','retail','Explain credit card annual fee scope business'),
    ('at','2026-06-01T00:00:00Z','Explain credit card annual fee at 2025-06-01T00:00:00Z'),
])
def test_contradicting_plan_cannot_override_request(field,value,text):
    assert bind_policy_request(text,dict(product='credit-card',topic='annual_fee',**{field:value})) is None


def test_positive_binding_and_language_separation():
    args=dict(product='credit-card',topic='annual_fee')
    bound=bind_policy_request('Explain credit card annual fee de-DE scope business at 2025-06-01T00:00:00Z',args,language='hi')
    assert bound==args|dict(locale='de-DE',scope='business',at='2025-06-01T00:00:00Z')
    assert bind_policy_request('Explain credit card annual fee',args)['locale']=='en-IN'
    assert bind_policy_request('Explain credit card annual fee',args,language='hi')['locale']=='hi-IN'


@pytest.mark.parametrize('text',[
    'Explain credit card annual fee locale German',
    'Explain credit card annual fee as of 01/06/2025',
    'Explain credit card annual fee last year',
])
def test_unsupported_explicit_filters_do_not_default(text):
    assert bind_policy_request(text,dict(product='credit-card',topic='annual_fee')) is None


def test_scanner_unavailable_fails_closed(monkeypatch):
    import capture.pii
    def fail():raise RuntimeError('Synthetic scanner unavailable')
    monkeypatch.setattr(capture.pii,'get_engines',fail)
    with pytest.raises(ValueError,match='Local privacy scan unavailable'):
        model_input('My name is Alice Example. Phone 9876543210')


def test_missing_local_model_does_not_build_or_download(monkeypatch):
    import capture.pii
    monkeypatch.setenv('PII_SPACY_MODEL','h01_synthetic_model_not_installed')
    called=[]
    monkeypatch.setattr(capture.pii,'get_engines',lambda:called.append(True))
    with pytest.raises(ValueError,match='Local privacy scan unavailable'):model_input('Explain credit card annual fee')
    assert not called
