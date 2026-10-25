"""Protocol verification and honest actual-input pilot report; no model calls."""
from pathlib import Path
import hashlib,json,os,subprocess,sys,uuid
ROOT=Path(__file__).resolve().parents[1]


def registered(name,origin):
    from banking_evaluator.contracts import parse_json
    registry=ROOT/'.genesis/local'/name
    if not registry.exists():return [],[]
    value=parse_json(registry.read_text(encoding='utf-8'))
    if set(value)!={'schema_version','files'} or value['schema_version']!='1' or not isinstance(value['files'],list):raise ValueError('Invalid input registry')
    records=[];identities=[];seen=set()
    for item in value['files']:
        if set(item)!={'path','sha256','origin'} or item['origin']!=origin:raise ValueError('Input origin mismatch')
        path=(ROOT/item['path']).resolve()
        if not path.is_relative_to((ROOT/'.genesis/evidence').resolve()) or path in seen:raise ValueError('Only unique retained evidence inputs allowed')
        seen.add(path);raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Input bytes changed')
        data=parse_json(raw.decode('utf-8'))
        if data.get('origin')!=origin:raise ValueError('Artifact origin mismatch')
        records.append(data);identities.append(item)
    return records,identities


def main():
    interpreter=ROOT/'.genesis/local/ux-python.txt'
    if interpreter.exists() and Path(interpreter.read_text(encoding='utf-8').strip()).resolve()!=Path(sys.executable).resolve():
        return subprocess.call([interpreter.read_text(encoding='utf-8').strip(),str(Path(__file__).resolve()),*sys.argv[1:]])
    os.chdir(ROOT);os.environ.update(PYTHONUTF8='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'evaluator/src'))
    from banking_evaluator.rubric import analyze_reviews
    if '--require-human-only' in sys.argv:
        dataset=json.loads((ROOT/'evaluator/review_sets/pilot_v1.json').read_text(encoding='utf-8'))
        labels,identities=registered('T06.3-human-inputs.json','human')
        actual=analyze_reviews(dataset,labels)
        ready=actual['actual_human_annotators']>0 and actual['unrated_cases']==0
        result={'task':'T06.3','status':'human-data-ready' if ready else 'awaiting-human-ratings',
            'actual_human_annotators':actual['actual_human_annotators'],'rated_cases':actual['rated_cases'],
            'human_assistance':actual['human_assistance'],'unrated_cases':actual['unrated_cases'],'dataset_hash':actual['dataset_hash'],'input_identities':identities,
            'judge_calibrated':False,'meaning':'Collection gate only; unclear ratings remain unclear, judge still exploratory/not run.'}
        target=ROOT/('.genesis/evidence/T06.3-human-data-'+uuid.uuid4().hex+'.json')
        target.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({**result,'report':str(target)}));return 0 if ready else 2
    path=ROOT/('.genesis/evidence/T06.3-protocol-'+uuid.uuid4().hex+'.json')
    report={'task':'T06.3','status':'running','model_requests_made_by_tool':0,'Astra_used':False}
    try:
        subprocess.run([sys.executable,'scripts/p062_check.py'],check=True)
        dataset=ROOT/'evaluator/review_sets/pilot_v1.json';data=json.loads(dataset.read_text(encoding='utf-8'))
        ratings,rating_ids=registered('T06.3-human-inputs.json','human')
        predictions,prediction_ids=registered('T06.3-prediction-inputs.json','external_model')
        actual=analyze_reviews(data,ratings,predictions);report['actual_pilot']=actual
        report['input_identities']={'ratings':rating_ids,'predictions':prediction_ids}
        install=max((ROOT/'.genesis/evidence').glob('T06.1-clean-install-bank-evaluator-clean-*.json'),key=lambda p:p.stat().st_mtime)
        installed=json.loads(install.read_text(encoding='utf-8'));assert installed['status']=='passed';folder=Path(installed['outside_checkout'])
        py=folder/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        inputs=[]
        for i,value in enumerate(ratings):
            name=folder/('actual-human-'+str(i)+'.json');name.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');inputs+=['--ratings',str(name)]
        for i,value in enumerate(predictions):
            name=folder/('actual-prediction-'+str(i)+'.json');name.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');inputs+=['--predictions',str(name)]
        env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
        for order,verbosity in [('forward','base'),('reverse','base'),('forward','expanded')]:
            subprocess.run([str(py),'-m','banking_evaluator.rubric_cli','view','--dataset',str(dataset),'--order',order,'--verbosity',verbosity,
                '--output',str(folder/('review-'+order+'-'+verbosity+'.json'))],cwd=folder,env=env,check=True,capture_output=True,text=True,encoding='utf-8')
        output=folder/'actual-pilot.json'
        subprocess.run([str(py),'-m','banking_evaluator.rubric_cli','analyze','--dataset',str(dataset),*inputs,'--output',str(output)],cwd=folder,env=env,check=True,capture_output=True,text=True,encoding='utf-8')
        isolated=json.loads(output.read_text(encoding='utf-8'));assert isolated==actual
        assert isolated['judge']['model_requests_made_by_tool']==0 and isolated['judge']['calibrated'] is False
        if not ratings:assert isolated['actual_human_annotators']==0 and isolated['status']=='awaiting_human_labels'
        if not predictions:assert isolated['judge']['status']=='not_run' and isolated['comparisons']==[]
        report.update(status='passed-protocol',outside_checkout_report=str(output),wheel_report=install.as_posix(),
            task_completion_claim=False,human_label_requirement_met=bool(ratings) and isolated['unrated_cases']==0,
            interpretation='Protocol gate only. Actual human input and exploratory judge status are reported separately; tests never create real ratings.')
    except Exception as error:
        report.update(status='failed',error=type(error).__name__);raise
    finally:
        path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        print(json.dumps({'status':report['status'],'report':str(path)}))
    return 0


if __name__=='__main__':raise SystemExit(main())
