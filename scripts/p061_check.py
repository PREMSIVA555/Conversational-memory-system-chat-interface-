"""Build/install outside checkout, two adapters and actual guarded banking bridge."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv

ROOT=Path(__file__).resolve().parents[1]


def main():
    interpreter=ROOT/'.genesis/local/ux-python.txt'
    if interpreter.exists() and Path(interpreter.read_text(encoding='utf-8').strip()).resolve()!=Path(sys.executable).resolve():
        return subprocess.call([interpreter.read_text(encoding='utf-8').strip(),str(Path(__file__).resolve())])
    folder=Path(tempfile.mkdtemp(prefix='bank-evaluator-clean-'))
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
    env.update(PYTHONUTF8='1',LITELLM_LOCAL_MODEL_COST_MAP='True',PIP_DISABLE_PIP_VERSION_CHECK='1')
    evidence=[]
    def command(args,*,cwd=folder,timeout=180):
        result=subprocess.run([str(a) for a in args],cwd=cwd,env=env,capture_output=True,text=True,encoding='utf-8',timeout=timeout)
        evidence.append({'command':[str(a) for a in args],'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
        if result.returncode:raise RuntimeError('Clean-install command failed: '+str(args[1:3]))
        return result.stdout.strip()
    report={'task':'T06.1','status':'running','outside_checkout':str(folder),'commands':evidence,'paid_calls':0,'Astra_used':False}
    path=ROOT/'.genesis/evidence'/('T06.1-clean-install-'+folder.name+'.json')
    try:
        command([sys.executable,'-m','pip','wheel','--no-deps','--wheel-dir',folder/'wheels',ROOT/'evaluator'])
        wheel=next((folder/'wheels').glob('banking_evaluator-*.whl'))
        venv.EnvBuilder(with_pip=True).create(folder/'venv')
        py=folder/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        command([py,'-m','pip','install','--no-index','--no-deps',wheel])
        origin=Path(command([py,'-c','import banking_evaluator;print(banking_evaluator.__file__)']))
        assert origin.is_relative_to(folder/'venv') and not origin.is_relative_to(ROOT)
        cli=[py,'-m','banking_evaluator.cli']
        command([*cli,'--demo','--output',folder/'demo.json'])
        command([*cli,'--demo','--replay',folder/'demo.json','--output',folder/'replay.json'])
        first=json.loads((folder/'demo.json').read_text(encoding='utf-8'));second=json.loads((folder/'replay.json').read_text(encoding='utf-8'))
        assert first['grade']['status']==second['grade']['status']=='pass'
        assert first['final_state']==second['final_state'] and first['adapter']['kind']!=second['adapter']['kind']
        # Actual production HTTP/provider reads in separate guarded worlds.
        task={'schema_version':'1','id':'owned-product-discovery-smoke','steps':[{'op':'prepare'},{'op':'workspace_products','kind':'card'}],
            'projection':{'bank':['financial_data_unchanged'],'matched':['identity','matched'],'model':['model_calls']},
            'expected':{'bank':True,'matched':True,'model':0}}
        (folder/'bank-task.json').write_text(json.dumps(task),encoding='utf-8')
        argv=json.dumps([sys.executable,str(ROOT/'evaluator/banking_operator.py')])
        bank=[]
        for name in ('bank-a','bank-b'):
            command([*cli,'--task',folder/'bank-task.json','--command-json',argv,'--timeout','120','--output',folder/(name+'.json')])
            bank.append(json.loads((folder/(name+'.json')).read_text(encoding='utf-8')))
        assert bank[0]['reset']['namespace']!=bank[1]['reset']['namespace']
        assert bank[0]['reset']['semantic_hash']==bank[1]['reset']['semantic_hash']
        assert all(r['grade']['status']=='pass' for r in bank)
        assert all(r['raw_observation']['results'][0]['status']==200 and r['raw_observation']['results'][1]['status']==200 for r in bank)
        command([sys.executable,'-m','pytest','tests/p06','-q','--junitxml=.genesis/evidence/T06.1-contracts.xml'],cwd=ROOT)
        report.update(status='passed',wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),installed_module=str(origin),
            adapters=[first['adapter'],second['adapter']],guarded_namespaces=[r['reset']['namespace'] for r in bank],
            semantic_hash=bank[0]['reset']['semantic_hash'],grading_claim='projection smoke only; T06.2 validation pending')
    except Exception as exc:
        report.update(status='failed',error=type(exc).__name__)
        raise
    finally:
        path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        print(json.dumps({'status':report['status'],'report':str(path),'artifacts':str(folder)}))
    return 0


if __name__=='__main__':raise SystemExit(main())
