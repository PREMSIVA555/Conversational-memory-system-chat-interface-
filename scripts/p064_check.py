"""T06.4 delivery gate: seeded failure, full real checks; no deployment."""
from pathlib import Path
import json,os,subprocess,sys,uuid
ROOT=Path(__file__).resolve().parents[1]


def main():
    interpreter=ROOT/'.genesis/local/ux-python.txt'
    if interpreter.exists() and Path(interpreter.read_text(encoding='utf-8').strip()).resolve()!=Path(sys.executable).resolve():
        return subprocess.call([interpreter.read_text(encoding='utf-8').strip(),str(Path(__file__).resolve())])
    os.chdir(ROOT);os.environ.update(PYTHONUTF8='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    folder=ROOT/'.genesis/evidence';folder.mkdir(parents=True,exist_ok=True)
    failed=folder/('T06.4-seeded-block-'+uuid.uuid4().hex+'.json')
    code=subprocess.call([sys.executable,'scripts/p064_release.py','--seed-failure','security','--output',str(failed)])
    result=json.loads(failed.read_text(encoding='utf-8'))
    assert code==1 and result['status']=='failed' and not result['release_allowed'] and not result['deployed']
    assert [x['stage'] for x in result['stages']]==['security'] and result['stages'][0]['status']=='fail'
    positive=folder/('T06.4-full-'+uuid.uuid4().hex+'.json')
    code=subprocess.call([sys.executable,'scripts/p064_release.py','--output',str(positive)])
    if code:return code
    report=json.loads(positive.read_text(encoding='utf-8'))
    assert report['release_allowed'] and not report['deployed'] and all(s['status']=='pass' for s in report['stages'])
    print(json.dumps({'task':'T06.4','status':'passed-local-release-checks','report':str(positive),'seeded_block':str(failed),'production_deployment':False,'github_run_observed':False}))
    return 0


if __name__=='__main__':raise SystemExit(main())
