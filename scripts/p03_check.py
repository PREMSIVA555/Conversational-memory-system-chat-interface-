"""P03 task acceptance runner; actual services and preserved fresh evidence directories."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.p01_check import safe_container, connection_identity
from scripts.p02_check import hashes as prior_hashes, row_counts, role_connections
from store.banking_migrate import apply

def hashes():
    values=prior_hashes()
    for pattern in ['scripts/p03*.py','tests/p03/*.py','store/banking*.py','store/banking_migrations/*.sql','jobs/banking*.py','auth/*.py','api/turns.py','api/foundation.py','frontend/components/*.tsx','frontend/app/chat/page.tsx','graphs/account_coordinator.py','llm/config.py','api/privacy.py','tests/unit/test_llm_config.py']:
        for path in ROOT.glob(pattern): values[path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(values.items()))

def check(destination,task):
    if task not in ('T03.1','T03.2','T03.3'): raise ValueError('Unsupported P03 check task')
    destination.mkdir(parents=True,exist_ok=False)
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    provenance=json.loads((ROOT/'.genesis/sources/P03_SOURCE.json').read_text(encoding='utf-8'))['manifest']
    actual_source_digest=hashlib.sha256(Path(provenance['source_path']).read_bytes()).hexdigest()
    if actual_source_digest!=provenance['source_sha256']: raise ValueError('Authoritative P03 source digest changed')
    secrets=[config['admin_dsn'],*config['dsns'].values(),config['env']['OIDC_CLIENT_SECRET'],*[u['password'] for u in config['users']]]
    secrets += [urlsplit(v).password for v in [config['admin_dsn'],*config['dsns'].values(),config['env']['BANK_REDIS_URL']]]
    def scrub(value):
        for secret in sorted(filter(None,secrets),key=len,reverse=True): value=value.replace(secret,'[REDACTED]')
        return value
    import psycopg
    before=hashes(); counts_before=row_counts(config)
    immutable_through={'T03.1':'0016','T03.2':'0017','T03.3':'0018'}[task]
    target={'T03.1':'0017','T03.2':'0018','T03.3':'0024'}[task]
    immutable={k:v for k,v in before.items() if k.startswith('store/banking_migrations/') and Path(k).name[:4]<=immutable_through}
    with psycopg.connect(config['dsns']['migrator']) as c: apply(c,config['roles'],through=target)
    counts_after=row_counts(config)
    preserved=all(counts_after.get(name)==value for name,value in counts_before.items())
    (destination/'migration-counts.json').write_text(json.dumps(dict(before=counts_before,after=counts_after,existing_rows_preserved=preserved,backfill=False,reset=False),indent=2)+'\n',encoding='utf-8')
    if not preserved: raise RuntimeError('Migration changed existing rows')
    containers={k:safe_container(v) for k,v in config['containers'].items()}
    identity_before=connection_identity(config,containers); roles_before=role_connections(config,containers)
    test_file={'T03.1':'tests/p03/test_provider.py','T03.2':'tests/p03/test_coordinator.py','T03.3':'tests/p03/test_turns.py'}[task]
    command=[sys.executable,'-m','pytest',test_file,'-q','--tb=short','--timeout='+('60' if task=='T03.3' else '30'),'--junitxml='+str(destination/'checks.xml')]
    if task=='T03.2': command.insert(4,'tests/unit/test_llm_config.py')
    result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1','LITELLM_LOCAL_MODEL_COST_MAP':'True'})
    output=scrub(result.stdout+'\n'+result.stderr)
    (destination/'checks.txt').write_text(output,encoding='utf-8')
    xml=destination/'checks.xml'
    if xml.exists(): xml.write_text(scrub(xml.read_text(encoding='utf-8')),encoding='utf-8')
    suites=ET.parse(xml).getroot() if xml.exists() else []
    totals={key:sum(int(s.get(key,0)) for s in suites) for key in ['tests','failures','errors','skipped']}
    identity_after=connection_identity(config,containers); roles_after=role_connections(config,containers)
    unchanged=hashes()==before
    with psycopg.connect(config['admin_dsn']) as c:
        recorded={name:digest for name,digest in c.execute('SELECT name,sha256 FROM banking.schema_migrations').fetchall()}
    immutable_verified=len(immutable)==int(immutable_through) and all(recorded.get(Path(k).name)==v for k,v in immutable.items())
    report=dict(task_id=task,recorded_at=datetime.now(timezone.utc).isoformat(),
        status='passed' if result.returncode==0 and totals['tests']>0 and not totals['skipped'] and unchanged and immutable_verified and identity_before==identity_after else 'failed-or-incomplete',
        exit_code=result.returncode,counts=totals,command=command,source_hashes=before,source_unchanged=unchanged,
        immutable_migrations_0001_0016=immutable,immutable_migrations_match_database=immutable_verified,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        containers=containers,identities_before=identity_before,identities_after=identity_after,
        role_connections_before=roles_before,role_connections_after=roles_after,
        policy=['SPEC09','INV04','INV06','INV10','INV25','ADR02','ADR03','ADR07','ADR09','ADR11','ADR12','ADR17'] if task=='T03.2' else ['SPEC07','INV02','INV06','INV07','ADR03','ADR09','ADR16'],
        data='Fresh synthetic customer roots only, retained in existing isolated P01 Docker PostgreSQL16; no legacy reset.',model='deterministic-raw-transport-through-existing-llm-seam; no live model calls' if task=='T03.2' else 'not-called',reviewer=None,
        limitations=[('Internal coordinator only; public turn API/UI, durable SSE and P03 closure remain later tasks.' if task=='T03.2' else 'Account/transaction provider only; coordinator, checked rendering, chat API/UI and durable SSE remain later tasks.'),
            'No live-bank or production calls, cards, loans, FX or money movement.',
            ('Coordinator timeout/retry faults use explicitly labeled request-local wrappers; PostgreSQL16 and Redis7 persist real authorization and budgets.' if task=='T03.2' else 'Real SQL lock timeout exercised without restarting shared Docker services.'),
            'Session authorization uses actual Redis7 records; trusted application and credentials remain within the established threat model.',
            ('Optional semantic memory is excluded from this bank authority path; actual authorized P02 history is seeded to verify current-conflict/outage behavior.' if task=='T03.2' else 'Version-bound cursors reject relevant mutations rather than retaining cross-request snapshots.'),
            'Executable success does not establish independent or human review.'])
    report['authoritative_source']=dict(path=provenance['source_path'],sha256=actual_source_digest)
    report['control_flow']='fixed bounded plan -> validate/at-most-one-repair -> read/at-most-one-retry -> source-check -> deterministic-render -> fenced-publish' if task=='T03.2' else 'typed scoped provider reads'
    report['immutable_migrations_through']=immutable_through
    if task=='T03.2': report['immutable_migrations_0001_0017']=immutable
    if task=='T03.3':
        report.update(policy=['SPEC03','SPEC05','INV15','INV16','INV24'],
            immutable_migrations_0001_0018=immutable,
            model='scripted raw completion transport injected through trusted create_app kwargs; no live model calls',
            control_flow='actual socket HTTP -> atomic accepted source/turn/run/outbox -> durable leased worker -> bounded coordinator -> persisted fenced terminal -> authorized status/SSE replay',
            limitations=['T03.3 durable API/recovery tests only; T03.4 full journey closure and P04+ are excluded.',
                'Current-response instructions and existing P02 preferences read only; no implicit global preference extraction from chat.',
                'Actual process crash recovery uses a private subprocess and retains the same SQL turn/run, real Redis session and original deadline/counters.',
                'Concurrent first arrivals may both observe replayed=false; the durable turn ID, unique client key and status are authoritative. Sequential/reconciled retries report replayed=true.',
                'Retry-exhaustion authentication outage uses an explicitly labeled post-acceptance 503 wrapper; session expiry and revocation tests use actual Redis records and TTL.',
                'The runner does not substitute for independent UI/browser review or human approval.'])
    report['artifacts']={p.relative_to(destination).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.rglob('*') if p.is_file()}
    (destination/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(output); print('Evidence:',destination/'report.json')
    return 0 if report['status']=='passed' else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('directory',type=Path); parser.add_argument('--task',default='T03.1')
    args=parser.parse_args(); existed_before=args.directory.exists()
    try: raise SystemExit(check(args.directory,args.task))
    except Exception as error:
        print('P03 setup/evidence failure:',type(error).__name__)
        if not existed_before and args.directory.exists() and not (args.directory/'report.json').exists():
            (args.directory/'report.json').write_text(json.dumps(dict(task_id=args.task,status='setup-failed',recorded_at=datetime.now(timezone.utc).isoformat(),error_type=type(error).__name__,reviewer=None),indent=2)+'\n',encoding='utf-8')
        raise SystemExit(1)
