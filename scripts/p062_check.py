"""Mutation corpus + backwards-compatible clean wheel + real read normalization."""
from pathlib import Path
from copy import deepcopy
import json,os,subprocess,sys,uuid
ROOT=Path(__file__).resolve().parents[1]


def main():
    interpreter=ROOT/'.genesis/local/ux-python.txt'
    if interpreter.exists() and Path(interpreter.read_text(encoding='utf-8').strip()).resolve()!=Path(sys.executable).resolve():
        return subprocess.call([interpreter.read_text(encoding='utf-8').strip(),str(Path(__file__).resolve())])
    os.chdir(ROOT);os.environ.update(PYTHONUTF8='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'evaluator/src'))
    from banking_evaluator.grading import grade
    from tests.p06.grading_cases import corpus
    path=ROOT/('.genesis/evidence/T06.2-validation-'+uuid.uuid4().hex+'.json')
    report={'task':'T06.2','status':'running','paid_calls':0,'Astra_used':False,'declared_corpus':[]}
    try:
        for row in corpus():
            result=grade(row['case'],row['observation'])
            report['declared_corpus'].append({**row,'actual':result})
            assert result['status']==row['expected'],row['id']
        subprocess.run([sys.executable,'-m','pytest','tests/p06','-q','--junitxml=.genesis/evidence/T06.2-regression.xml'],check=True)
        subprocess.run([sys.executable,'scripts/p061_check.py'],check=True)
        install=max((ROOT/'.genesis/evidence').glob('T06.1-clean-install-bank-evaluator-clean-*.json'),key=lambda p:p.stat().st_mtime)
        installed=json.loads(install.read_text(encoding='utf-8'));assert installed['status']=='passed'
        folder=Path(installed['outside_checkout']);py=folder/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        raw=subprocess.run([sys.executable,str(ROOT/'evaluator/grading_bank_check.py')],cwd=folder,capture_output=True,text=True,encoding='utf-8',check=True,timeout=120)
        observed=json.loads(raw.stdout);report['real_banking']=observed
        (folder/'grading-case.json').write_text(json.dumps(observed['case']),encoding='utf-8')
        variants={'actual':(observed['observation'],'pass')}
        bad=deepcopy(observed['observation']);bad['events'][-1]['claims'][0]['value']['minor_units']='1'
        variants['seeded-wrong-answer']=(bad,'fail')
        bad=deepcopy(observed['observation']);bad['events'][-1]['claims'][0]['resource']='foreign-not-owned'
        variants['seeded-foreign-disclosure']=(bad,'fail')
        report['installed_grades']={}
        env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
        for name,(obs,expected) in variants.items():
            source=folder/(name+'-observation.json');source.write_text(json.dumps(obs),encoding='utf-8');output=folder/(name+'-grade.json')
            done=subprocess.run([str(py),'-m','banking_evaluator.grade_cli','--case',str(folder/'grading-case.json'),'--observation',str(source),'--output',str(output)],cwd=folder,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
            result=json.loads(output.read_text(encoding='utf-8'))['grade'];assert result['status']==expected and done.returncode==(0 if expected=='pass' else 1),name
            report['installed_grades'][name]=result
        report.update(status='passed',wheel_report=install.as_posix(),corpus_count=len(report['declared_corpus']),
            validation_claim='bounded normalized synthetic mutation corpus plus actual owned/denied read observations; no universal/subjective calibration')
    except Exception as error:
        report.update(status='failed',error=type(error).__name__);raise
    finally:
        path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        print(json.dumps({'status':report['status'],'report':str(path)}))
    return 0


if __name__=='__main__':raise SystemExit(main())
