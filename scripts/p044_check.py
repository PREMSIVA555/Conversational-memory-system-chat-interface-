"""T04.4: independent relevance/support measures and real domain regressions."""
from pathlib import Path
import sys,os,json,hashlib,asyncio,subprocess
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from evals.policy_world import PolicyWorld
from evals.banking_world import BankingWorld,database_dsn
from evals.card_world import CardWorld
from evals.loan_world import LoanWorld
from store.banking_policies import stage,activate
from store.banking_policy_answers import explain
from scripts.p044_probe import api_turn


def hashes():
    patterns=('store/banking*.py','store/banking_migrations/*.sql','graphs/account_coordinator.py','jobs/banking_turns.py','api/foundation.py','frontend/components/TurnPanel.tsx','evals/*world.py','evals/fixtures/p04_policy*.json','scripts/p04*.py','tests/p04/*.py','.genesis/sources/P04_SOURCE.json')
    files=set(p for pattern in patterns for p in ROOT.glob(pattern))
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


async def execute(out):
    before=hashes();cfg=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    groups=[('policy',['tests/p04/test_policies.py','tests/p04/test_policy_answers.py']),
        ('accounts',['tests/p03/test_provider.py']),('cards',['tests/p04/test_cards.py']),
        ('loans',['tests/p04/test_loans.py']),('coordinator',['tests/p03/test_coordinator.py']),('turns',['tests/p03/test_turns.py'])]
    counts={}
    for name,files in groups:
        xml=out/(name+'.xml')
        command=[sys.executable,'-m','pytest',*files,'-q','--tb=short','--timeout=60','--junitxml='+str(xml)]
        p=await asyncio.to_thread(subprocess.run,command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1','LITELLM_LOCAL_MODEL_COST_MAP':'True'})
        (out/(name+'.txt')).write_text(p.stdout+p.stderr,encoding='utf-8')
        assert p.returncode==0,(name,p.stdout[-5000:])
        counts[name]=len(ET.parse(xml).getroot().findall('.//testcase'))
    fixture=json.loads((ROOT/'evals/fixtures/p04_policy_claims.json').read_text(encoding='utf-8'))
    rubric=[]
    for case in fixture['cases']:
        answer=explain(case['retrieval'],case['topic']);actual=bool(answer.evidence)
        assert actual==case['expected_supported'],case['id']
        rubric.append(dict(id=case['id'],expected=case['expected_supported'],actual=actual,metadata=answer.metadata,text=answer.text,evidence=answer.evidence))
    w=PolicyWorld.create(cfg);identity=w.identity()
    catalog=json.loads((ROOT/'evals/fixtures/p04_policy_queries.json').read_text(encoding='utf-8'))
    with psycopg.connect(database_dsn(cfg['dsns']['migrator'],w.name),autocommit=True) as c:
        sid=stage(c,catalog['manifest']);activate(c,sid,expected=None)
    api=[]
    args=dict(product='credit-card',topic='annual_fee',at='2026-06-01T00:00:00Z')
    cases=[dict(name='supported-fee',text='Explain credit card annual fee at 2026-06-01T00:00:00Z',plan=dict(tool='explain_policy',args=args),status='completed'),
        dict(name='supported-waiver',text='Explain credit card fee waiver at 2026-06-01T00:00:00Z',plan=dict(tool='explain_policy',args=args|dict(topic='fee_waiver')),status='completed'),
        dict(name='wrong-prose',text='Explain credit card annual fee at 2026-06-01T00:00:00Z',plan=dict(tool='explain_policy',args=args,draft=dict(text='Your fee is INR 0. You qualify for a waiver.',claims=[])),status='completed'),
        dict(name='wrong-locale',text='Explain credit card fee de-DE at 2026-06-01T00:00:00Z',plan=dict(tool='explain_policy',args=args|dict(locale='de-DE')),status='no_evidence'),
        dict(name='implicit-product',text='Explain my annual fee at 2026-06-01T00:00:00Z',plan=dict(tool='explain_policy',args=args),status='clarify'),
        dict(name='flag-disabled',text='Explain credit card annual fee at 2026-06-01T00:00:00Z',plan=dict(tool='explain_policy',args=args),enabled=False,status='unsupported'),
        dict(name='hindi-template',text='Explain credit card annual fee en-IN at 2026-06-01T00:00:00Z',plan=dict(tool='explain_policy',args=args|dict(locale='en-IN')),instructions=dict(language='hi'),status='completed')]
    for case in cases:
        value=await api_turn(w,case);r=value['result'];assert r['status']==case['status'],(case['name'],r)
        if case['status']=='completed':
            assert r['policy']['support']=='supported_template' and r['evidence']
            assert r['policy']['lanes']==dict(vector='unavailable',lexical='ok')
            assert r['evidence'][0]['set_id']==sid and r['evidence'][0]['citation']['product']=='credit-card'
            assert r['draft_accepted']==(case['name']!='wrong-prose')
        else:assert not r['evidence']
        if case['name']=='hindi-template':assert 'वार्षिक शुल्क' in r['text'] and 'INR 100.00' in r['text']
        api.append(dict(name=case['name'],**value))
    # Existing scenario functions retain their own real PKCE, API, SQL-oracle,
    # mutation/authorization, paging and deadline checks. New isolated worlds.
    from scripts.p03_slice import scenarios as accounts
    from scripts.p04_check import scenarios as cards
    from scripts.p042_check import scenarios as loans
    domains={}
    for name,world_type,runner in [('accounts',BankingWorld,accounts),('cards',CardWorld,cards),('loans',LoanWorld,loans)]:
        folder=out/name;folder.mkdir()
        domains[name]=await runner(world_type.create(cfg),folder)
    assert hashes()==before,'Source changed during gate'
    report=dict(task='T04.4',passed=True,identity=identity,pytest=counts,unique_pytest=sum(counts.values()),
        claim_support=dict(labeled_cases=len(rubric),correct=len(rubric),scope='Curated template/claim rubric, separate from relevance; not model quality'),
        policy_api_cases=len(api),domain_api=domains,source_sha256=before,source_unchanged=True,
        limits=['Raw SDK plans scripted; actual auth/API/worker/SQL. No paid calls or browser policy-flow claim.',
        'Policy service explicitly uses degraded lexical lane; deterministic vector relevance separately checked in T04.3. No live embedding-quality claim.',
        'Only exact curated annual-fee and waiver clauses supported. Other explanatory text narrows or yields limitation. No personal eligibility or charge-cause inference.'])
    (out/'claim-support.json').write_text(json.dumps(rubric,indent=2),encoding='utf-8')
    (out/'policy-api.json').write_text(json.dumps(api,indent=2,default=str),encoding='utf-8')
    (out/'report.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}))


if __name__=='__main__':
    out=(ROOT/sys.argv[1]).resolve();base=(ROOT/'evidence/policy').resolve()
    if out==base or not out.is_relative_to(base):raise ValueError('Fresh evidence/policy/<run>/claim-support path required')
    out.mkdir(parents=True,exist_ok=False);asyncio.run(execute(out))
