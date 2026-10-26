"""Fail-closed local/CI release checks. No deployment or model dispatch."""
from pathlib import Path
import argparse,hashlib,json,os,re,shutil,subprocess,sys,tempfile,uuid
ROOT=Path(__file__).resolve().parents[1]
STAGES=('security','backend','evaluator','frontend','wheel','recovery','alerts')


def source_identity():
    names=subprocess.check_output(['git','ls-files','-co','--exclude-standard'],cwd=ROOT,text=True).splitlines()
    manifest={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in sorted(set(names)) if (ROOT/n).is_file() and not n.startswith('.genesis/')}
    return {'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'manifest':manifest,
            'sha256':hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()}


def security():
    config=ROOT/'.genesis/local/p01/config.json';private=[]
    def visit(v,key=''):
        if isinstance(v,dict):
            for k,x in v.items():visit(x,k)
        elif isinstance(v,list):
            for x in v:visit(x,key)
        elif isinstance(v,str) and len(v)>10 and (re.search('password|secret|token|dsn',key,re.I) or v.startswith('postgresql://')):private.append(v)
    if config.exists():visit(json.loads(config.read_text(encoding='utf-8')))
    names=subprocess.check_output(['git','ls-files','-co','--exclude-standard'],cwd=ROOT,text=True).splitlines();hits=[]
    for n in set(names):
        p=ROOT/n
        if not p.is_file():continue
        # Only the safe ignore sentinel is tracked inside private runtime storage.
        if n.startswith(('.genesis/evidence/','.genesis/local/')) and n!='.genesis/local/.gitignore':hits.append(n);continue
        text=p.read_text(encoding='utf-8',errors='replace')
        if any(v in text for v in private) or re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',text) or re.search(r'(?:sk-proj-|sk-ant-|ghp_)[A-Za-z0-9_-]{25,}',text):hits.append(n)
    if hits:raise ValueError('Credential/private-artifact scan failed in '+','.join(sorted(hits)))
    return {'scanned_files':len(set(names)),'private_value_hits':0,'scope':'versionable files, known local fixture credentials, high-signal key patterns; not universal secret detection'}


def execute(argv,folder,cwd=ROOT,timeout=900):
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME') and not k.endswith('_API_KEY')}
    env.update(PYTHONUTF8='1',LITELLM_LOCAL_MODEL_COST_MAP='True',NEXT_TELEMETRY_DISABLED='1')
    number=len(list(folder.glob('command-*.json')));log=folder/f'command-{number}.log'
    with log.open('wb') as stream:
        result=subprocess.run([str(x) for x in argv],cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
    receipt={'argv':[str(x) for x in argv],'cwd':str(cwd),'exit_code':result.returncode,'private_log':str(log),'log_sha256':hashlib.sha256(log.read_bytes()).hexdigest()}
    (folder/f'command-{number}.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    if result.returncode:raise RuntimeError('Release subprocess failed; retained private log '+log.name)
    return receipt


def frontend(folder):
    ui=Path(tempfile.mkdtemp(prefix='bank-release-ui-'))
    for name in ('app','components','lib','public'):
        if (ROOT/'frontend'/name).exists():shutil.copytree(ROOT/'frontend'/name,ui/name)
    for name in ('package.json','package-lock.json','tsconfig.json','next.config.ts','next-env.d.ts'):
        shutil.copy2(ROOT/'frontend'/name,ui/name)
    npm='npm.cmd' if os.name=='nt' else 'npm'
    execute([npm,'ci','--no-audit','--no-fund'],folder,ui)
    execute([npm,'run','build'],folder,ui)
    execute([npm,'run','typecheck'],folder,ui)
    return {'outside_checkout':str(ui),'typecheck_and_production_build':True,'runtime_env_copied':False}


def wheel(folder):
    import zipfile
    wheels=folder/'application-wheel';wheels.mkdir()
    staging=Path(tempfile.mkdtemp(prefix='bank-release-app-'))
    shutil.copy2(ROOT/'pyproject.toml',staging/'pyproject.toml')
    for package in ('api','auth','capture','graphs','jobs','llm','store'):
        shutil.copytree(ROOT/package,staging/package,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    execute([sys.executable,'-m','pip','wheel','--no-deps','--wheel-dir',wheels,staging],folder,cwd=staging)
    path=next(wheels.glob('memory_system-*.whl'))
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        assert 'api/foundation.py' in names and any(n.startswith('store/banking_migrations/0032') for n in names)
        assert not any('/.env' in n or n.startswith('.genesis/') for n in names)
    return {'outside_checkout_source':str(staging),'wheel_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'migration_payload_present':True,'claim':'Application wheel build/content check; clean dependency-free evaluator installation is separate.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-failure',choices=STAGES);parser.add_argument('--only',choices=STAGES);parser.add_argument('--output',type=Path)
    a=parser.parse_args()
    interpreter=ROOT/'.genesis/local/ux-python.txt'
    if interpreter.exists() and Path(interpreter.read_text(encoding='utf-8').strip()).resolve()!=Path(sys.executable).resolve():
        return subprocess.call([interpreter.read_text(encoding='utf-8').strip(),str(Path(__file__).resolve()),*sys.argv[1:]])
    os.chdir(ROOT);os.environ.update(PYTHONUTF8='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    output=a.output or ROOT/('.genesis/evidence/T06.4-release-'+uuid.uuid4().hex+'.json')
    if output.exists():parser.error('Evidence exists; choose new output')
    folder=ROOT/'.genesis/local'/('p064-run-'+uuid.uuid4().hex);folder.mkdir(parents=True)
    report={'schema_version':'1','task':'T06.4','status':'running','release_allowed':False,'deployed':False,'model_calls':0,'seeded_failure':a.seed_failure,'selected_stage':a.only,'stages':[],'source':source_identity(),
        'data':'guarded synthetic world only','model':'disabled transport/no provider evaluation','tools':{'python':sys.version,'platform':sys.platform},'policy':'SPEC12 SPEC16 INV27 INV28 INV31; historical paid budget closed','grader':'release-checks/1','reset':'new unique scratch folders/worlds; no normal database reset'}
    try:
        for stage in ((a.only,) if a.only else STAGES):
            result={'stage':stage,'status':'running'};report['stages'].append(result)
            if stage==a.seed_failure:
                execute([sys.executable,'-c','raise SystemExit(19)'],folder)
            if stage=='security':result['result']=security()
            elif stage=='backend':result['result']=execute([sys.executable,'-m','pytest','tests/unit',
                'tests/p05/test_confirmation.py::test_exact_replay_keys_payload_and_durable_uniqueness',
                'tests/p05/test_confirmation.py::test_each_storage_fault_rolls_back_then_same_key_retries[support_action_audit]',
                'tests/p05/test_confirmation.py::test_optional_exporter_failure_preserves_committed_receipt',
                'tests/p05/test_confirmation.py::test_ownership_session_expiry_cancel_and_body_guards',
                'tests/p05/test_confirmation.py::test_erasure_scrubs_case_text_blocks_replay_and_keeps_uniqueness',
                'tests/h01/test_hardening.py::test_sensitive_identifiers',
                'tests/h01/test_hardening.py::test_scanner_unavailable_fails_closed',
                'tests/h01/test_hardening.py::test_missing_local_model_does_not_build_or_download',
                'tests/p03/test_account_slice.py','-q','--junitxml=.genesis/evidence/T06.4-backend.xml'],folder)
            elif stage=='evaluator':result['result']=execute([sys.executable,'scripts/p063_check.py'],folder)
            elif stage=='frontend':result['result']=frontend(folder)
            elif stage=='wheel':result['result']=wheel(folder)
            else:result['result']=execute([sys.executable,'scripts/p064_'+stage+'.py'],folder)
            result['status']='pass'
        assert report['source']['sha256']==source_identity()['sha256'],'Sources changed during verification'
        report.update(status='passed',release_allowed=a.only is None and a.seed_failure is None)
    except Exception as error:
        if report['stages'] and report['stages'][-1]['status']=='running':report['stages'][-1]['status']='fail'
        report.update(status='failed',error=type(error).__name__)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as f:f.write(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'release_allowed':report['release_allowed'],'report':str(output)}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
