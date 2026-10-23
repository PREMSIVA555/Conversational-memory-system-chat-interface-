"""H01 native gate: unchanged-source regression + actual dispatch boundary."""
from pathlib import Path
import sys,os,json,asyncio,subprocess,uuid,hashlib
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
from scripts.h01_probe import operate
from scripts.p044_check import execute as banking_regression
from llm.config import bounded_completion_capabilities


def hashes():
    files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],cwd=ROOT,text=True).splitlines()
    return {f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files
        if not f.startswith('.genesis/') and (ROOT/f).is_file()}


def main():
    out=ROOT/'evidence/policy'/('h01-'+uuid.uuid4().hex);out.mkdir(parents=True)
    before=hashes()
    env={**os.environ,'PYTHONUTF8':'1','LITELLM_LOCAL_MODEL_COST_MAP':'True'}
    p=subprocess.run([sys.executable,'-m','pytest','tests/h01','tests/p01/test_privacy.py','-q','--tb=short',
        '--timeout=60','--junitxml='+str(out/'hardening.xml')],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env=env)
    (out/'hardening.txt').write_text(p.stdout+p.stderr,encoding='utf-8')
    assert p.returncode==0,p.stdout[-6000:]
    count=len(ET.parse(out/'hardening.xml').getroot().findall('.//testcase'))
    # Local SDK metadata import is test setup, before any accepted enquiry.
    # Warm it once; application budgets and assertions remain unchanged.
    asyncio.run(bounded_completion_capabilities(timeout=60))
    regression=out/'banking';regression.mkdir();asyncio.run(banking_regression(regression))
    trial=operate(dict(op='create',seed=True));world=trial['world']
    base=dict(op='turn',world=world,plan=dict(tool='explain_policy',args=dict(product='credit-card',topic='annual_fee')))
    cases=[
        ('omitted-locale',dict(text='Explain credit card annual fee de-DE at 2026-06-01T00:00:00Z'),'no_evidence'),
        ('omitted-scope',dict(text='Explain credit card annual fee scope business at 2026-06-01T00:00:00Z'),'no_evidence'),
        ('omitted-history',dict(text='Explain credit card annual fee at 2025-06-01T00:00:00Z'),'completed'),
        ('scanner-outage',dict(text='My name is Alice Example. Phone 9876543210. Explain credit card annual fee.',scanner_failure=True),'unavailable'),
        ('missing-local-model',dict(text='Explain credit card annual fee.',missing_model=True),'unavailable'),
        ('private-input',dict(text='My name is Alice Example. Phone 9876543210. SSN 123-45-6789. Email alice\u200b@example.test. Card 4111\t1111\t1111\t1111. Explain credit card annual fee at 2026-06-01T00:00:00Z'),'completed'),
    ]
    observations=[]
    for name,extra,status in cases:
        result=operate(base|extra);assert result['result']['status']==status,(name,result)
        if name=='omitted-history':assert result['result']['policy']['effective_at'].startswith('2025-06-01')
        if name in ('scanner-outage','missing-local-model'):assert result['raw_dispatches']==0 and not result['model_messages']
        if name=='private-input':
            assert result['raw_dispatches']==1
            user=' '.join(m['content'] for call in result['model_messages'] for m in call if m['role']=='user')
            assert all(v not in user for v in ('Alice Example','9876543210','123-45-6789','alice','4111'))
        observations.append(dict(name=name,**result))
    assert hashes()==before,'Source changed during H01 gate'
    (out/'api.json').write_text(json.dumps(observations,indent=2),encoding='utf-8')
    report=dict(task='TH01.1',passed=True,hardening_and_privacy_tests=count,banking_report=str(regression/'report.json'),
        api_cases=len(observations),identity=trial['identity'],source_sha256=before,source_unchanged=True,
        limits=['Raw SDK scripted, no live model-quality claim. English name detection is defense in depth.',
            'Indirect/ambiguous policy constraints clarify; no general multilingual parser certification.'])
    (out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(dict(passed=True,report=str(out/'report.json'),hardening_tests=count,api_cases=len(observations))))


if __name__=='__main__':main()
