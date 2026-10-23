"""T02.1 real-service checks; fresh, sanitized and source-versioned evidence."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.p01_check import safe_container, connection_identity, hashes as p01_hashes
from store.banking_migrate import apply

def hashes():
    values=p01_hashes()
    for pattern in ['scripts/p02*.py','tests/p02/*.py','store/banking_legacy_plan.py','store/legacy_cutover_guard.sql','store/memories.py','jobs/banking_consumer*.py','api/preferences.py','api/context_memory.py','store/banking_context.py','store/preference_grammar.py','frontend/components/PreferencePanel.tsx']:
        for p in ROOT.glob(pattern):
            values[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    return dict(sorted(values.items()))

def row_counts(config):
    import psycopg
    from psycopg import sql
    with psycopg.connect(config['admin_dsn']) as c:
        tables=c.execute("SELECT tablename FROM pg_tables WHERE schemaname='banking' AND tablename<>'schema_migrations' ORDER BY tablename").fetchall()
        return {name:c.execute(sql.SQL('SELECT count(*) FROM banking.{}').format(sql.Identifier(name))).fetchone()[0] for (name,) in tables}

def role_connections(config,containers):
    """Prove each tested role reaches the same server as the container-matched admin."""
    import psycopg
    proof={}
    port=containers['postgres']['ports']['5432/tcp'][0]['HostPort']
    with psycopg.connect(config['admin_dsn'],autocommit=True) as admin:
        expected=admin.execute('SELECT current_database(),inet_server_addr()::text,inet_server_port(),pg_postmaster_start_time()::text').fetchone()
        for kind,dsn in config['dsns'].items():
            parsed=urlsplit(dsn)
            assert parsed.hostname in ('127.0.0.1','localhost') and str(parsed.port)==port
            with psycopg.connect(dsn) as c:
                row=c.execute('SELECT current_database(),inet_server_addr()::text,inet_server_port(),pg_postmaster_start_time()::text,current_user,pg_backend_pid()').fetchone()
                assert row[:4]==expected and row[4]==config['roles'][kind]
                assert admin.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=%s AND usename=%s AND datname=%s',(row[5],row[4],row[0])).fetchone()[0]==1
                proof[kind]=dict(database=row[0],server_address=row[1],server_port=row[2],postmaster_started=row[3],
                    role=row[4],backend_pid=row[5],backend_seen_in_container_matched_cluster=True,host_binding_matches=True)
    return proof

def check(destination,task):
    if task not in ('T02.1','T02.2','T02.3','T02.4','T02.5'): raise ValueError('Unsupported P02 check task')
    destination.mkdir(parents=True,exist_ok=False)
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    secrets=[config['admin_dsn'],*config['dsns'].values(),config['env']['OIDC_CLIENT_SECRET']]
    secrets += [u['password'] for u in config['users']]
    secrets += [urlsplit(v).password for v in [config['admin_dsn'],*config['dsns'].values(),config['env']['BANK_REDIS_URL']]]
    def scrub(value):
        for secret in sorted(filter(None,secrets),key=len,reverse=True): value=value.replace(secret,'[REDACTED]')
        return value
    import psycopg
    before=hashes()
    migration_before=row_counts(config)
    # Explicit constrained additive migration before test identity snapshot.
    with psycopg.connect(config['dsns']['migrator']) as c: apply(c,config['roles'],through={'T02.1':'0003','T02.2':'0006','T02.3':'0011','T02.4':'0015','T02.5':'0016'}[task])
    migration_after=row_counts(config)
    preserved=all(migration_after.get(name)==count for name,count in migration_before.items())
    (destination/'migration-counts.json').write_text(json.dumps(dict(before=migration_before,after=migration_after,
        existing_rows_preserved=preserved,cutover=False,backfill=False),indent=2)+'\n',encoding='utf-8')
    if not preserved: raise RuntimeError('Additive migration changed existing row counts')
    containers={k:safe_container(v) for k,v in config['containers'].items()}
    identity_before=connection_identity(config,containers)
    roles_before=role_connections(config,containers)
    selection={'T02.1':['tests/p02/test_schema.py'],'T02.2':['tests/p02/test_consumer.py'],
               'T02.3':['tests/p02/test_preferences.py','tests/p02/test_preferences_independent.py'],
               'T02.4':['tests/p02/test_context_erasure.py'],'T02.5':['tests/p02/test_cutover.py']}[task]
    command=[sys.executable,'-m','pytest',*selection,'-q','--tb=short','--timeout=30',
             '--junitxml='+str(destination/'checks.xml')]
    result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',
        env={**os.environ,'PYTHONUTF8':'1','LITELLM_LOCAL_MODEL_COST_MAP':'True','P02_EVIDENCE_DIR':str((destination/'observations').resolve())})
    output=scrub(result.stdout+'\n'+result.stderr)
    (destination/'checks.txt').write_text(output,encoding='utf-8')
    xml=destination/'checks.xml'
    if xml.exists(): xml.write_text(scrub(xml.read_text(encoding='utf-8')),encoding='utf-8')
    suites=ET.parse(xml).getroot() if xml.exists() else []
    counts={key:sum(int(s.get(key,0)) for s in suites) for key in ['tests','failures','errors','skipped']}
    identity_after=connection_identity(config,containers)
    roles_after=role_connections(config,containers)
    unchanged=hashes()==before
    report=dict(task_id=task,recorded_at=datetime.now(timezone.utc).isoformat(),
        status='passed' if result.returncode==0 and counts['tests']>0 and not counts['skipped'] and unchanged and identity_before==identity_after else 'failed-or-incomplete',
        exit_code=result.returncode,counts=counts,command=command,source_hashes=before,source_unchanged=unchanged,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        containers=containers,identities_before=identity_before,identities_after=identity_after,
        role_connections_before=roles_before,role_connections_after=roles_after,
        policy={'T02.1':['SPEC04','SPEC11','SPEC16','INV11','INV12','INV15'],'T02.2':['SPEC11','INV21','INV22'],
                  'T02.3':['SPEC04','SPEC05','INV11','INV12','INV13','INV14','INV15'],
                  'T02.4':['SPEC05','SPEC06','INV13','INV23','INV24'],
                  'T02.5':['SPEC04','SPEC16','INV11','INV15','INV23']}[task],
        data='Synthetic per-test customer roots retained in isolated P01 Docker database; no existing rows reset.',
        model='not-called',reviewer=None,
        limitations=(['T02.1 schema and SQL primitives only; not wired to chat/panel routes.',
          'No durable consumer, fencing behavior, extraction, UI status or memory cutover implemented.',
          'Redis identity verified; Redis is not the authority for PostgreSQL preference ordering.',
          'Legacy classification uses explicit synthetic fixtures; no production legacy dataset available.',
          'Executable success does not imply independent or human review.'] if task=='T02.1' else [
          'Local acceptance.record v1 effect only; no preference extraction/application or UI wiring.',
          'No transport exactly-once or external provider effect guarantees.',
          'Redis identity verified; PostgreSQL is the durable worker authority.',
          'Dedicated synthetic rows retained; no existing dataset reset.',
          'Executable success does not imply independent or human review.']))
    if task=='T02.3':
        report['limitations']=[
            'Bounded explicit preference grammar only; no general natural-language extraction or model calls.',
            'Preference-only capture endpoint and response preview; no banking chat, model response or legacy writer cutover.',
            'Current-response preview requires explicit response_event_id and session binding; conversation scope never promotes to global.',
            'Per-store deletion/erasure and full working context remain T02.4; legacy cutover remains T02.5.',
            'Redis identity verified; PostgreSQL is preference and worker authority.',
            'Executable success does not imply independent or human review.']
    if task=='T02.4':
        report['limitations']=[
            'Governed lexical history only; semantic embeddings and external providers disabled.',
            'Context selections currently canonical owned conversation references, not future bank products.',
            'Deletion blocks active source/derivative serving transactionally; minimal source IDs and sequence tombstones retained.',
            'Local pre-delete snapshots retained in restricted quarantine for 24 hours; expiry cleanup occurs on authenticated retention requests.',
            'Restore proof imports those snapshots into a private transaction-local PostgreSQL quarantine and scrubs using the outside ledger; no production backup deletion claim.',
            'Synthetic test evidence is retained; no provider copy or external backup cleanup claimed.',
            'T02.5 legacy cutover remains unchanged.',
            'Executable success does not imply independent or human review.']
    if task=='T02.5':
        report['limitations']=[
            'Synthetic cutover rehearsal exercises real PostgreSQL and the actual legacy persist_candidates entry point.',
            'Legacy preference content is never used as fallback settings; accepted source projections remain authoritative.',
            'Backfill requires explicit privileged owner attestation and an existing matching accepted typed source; no historical sequence is inferred.',
            'Source-backed synthetic mappings do not establish production legacy mappings or production migration completion.',
            'Rollback disables new typed preference acceptance only; reads, erasure and already accepted durable work continue.',
            'Legacy SQL guard installation on an independently configured database has separate inventory/installation evidence.',
            'Executable success does not imply independent or human review.']
    report['artifacts']={p.relative_to(destination).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in destination.rglob('*') if p.is_file()}
    (destination/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(output)
    print('Evidence:',destination/'report.json')
    return 0 if report['status']=='passed' else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--task',default='T02.1')
    args=parser.parse_args()
    existed_before=args.directory.exists()
    try:
        raise SystemExit(check(args.directory,args.task))
    except Exception as error:
        # Connection errors can include credentials: retain type only.
        print('P02 setup/evidence failure:',type(error).__name__)
        if not existed_before and args.directory.exists() and not (args.directory/'report.json').exists():
            (args.directory/'report.json').write_text(json.dumps(dict(task_id=args.task,status='setup-failed',
                recorded_at=datetime.now(timezone.utc).isoformat(),error_type=type(error).__name__,reviewer=None),indent=2)+'\n',encoding='utf-8')
        raise SystemExit(1)
