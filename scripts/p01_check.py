"""Run P01 acceptance against dedicated services and retain sanitized evidence."""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def hashes():
    sources=[]
    for pattern in ['auth/*.py','api/foundation.py','api/privacy.py','api/main.py',
        'store/banking*.py','store/banking_migrations/*.sql','jobs/banking*.py',
        'scripts/p01*.py','tests/p01/*.py','tests/p01/*.yml','frontend/components/BankingView.tsx',
        'frontend/app/page.tsx','frontend/app/chat/page.tsx','frontend/next.config.ts',
        'infra/banking-alerts.yml','infra/prometheus.yml','infra/docker-compose.yml','pyproject.toml','.gitignore']:
        sources+=list(ROOT.glob(pattern))
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(sources))}


def safe_container(name):
    data=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
    image=json.loads(subprocess.check_output(['docker','image','inspect',data['Image']],text=True))[0]
    return dict(name=name,id=data['Id'],image_id=data['Image'],image=data['Config']['Image'],
                digests=image.get('RepoDigests',[]),ports=data['NetworkSettings']['Ports'],running=data['State']['Running'])


def connection_identity(config,containers):
    import psycopg,redis,httpx
    from urllib.parse import urlsplit
    with psycopg.connect(config['admin_dsn'],connect_timeout=3) as db:
        pgid,version=db.execute('SELECT system_identifier::text,version() FROM pg_control_system()').fetchone()
        roles=db.execute('''SELECT rolname,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles
                           WHERE rolname=ANY(%s) ORDER BY rolname''',(list(config['roles'].values()),)).fetchall()
        tables=db.execute('''SELECT c.relname,pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity
             FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname='banking'
             AND c.relkind='r' ORDER BY c.relname''').fetchall()
        constraints=db.execute("""SELECT conrelid::regclass::text,pg_get_constraintdef(oid)
             FROM pg_constraint WHERE connamespace=(SELECT oid FROM pg_namespace WHERE nspname='banking')
             AND contype='f' ORDER BY conrelid,conname""").fetchall()
    inside=subprocess.check_output(['docker','exec',containers['postgres']['name'],'psql','-U','p01_owner',
               '-d','bank_p01','-Atc','SELECT system_identifier FROM pg_control_system()'],text=True).strip()
    with redis.Redis.from_url(config['env']['BANK_REDIS_URL'],decode_responses=True) as cache:
        cacheid=cache.info('server')
    auth=urlsplit(config['env']['BANK_REDIS_URL']).password
    r=subprocess.run(['docker','exec','-e','REDISCLI_AUTH',containers['redis']['name'],'redis-cli','--no-auth-warning',
         'INFO','server'],env={**os.environ,'REDISCLI_AUTH':auth},capture_output=True,text=True)
    if r.returncode: raise RuntimeError('Container Redis identity query failed')
    inside_cache=dict(line.split(':',1) for line in r.stdout.splitlines() if ':' in line and not line.startswith('#'))
    pgport=containers['postgres']['ports']['5432/tcp'][0]['HostPort']
    redisport=containers['redis']['ports']['6379/tcp'][0]['HostPort']
    issuerport=containers['oidc']['ports']['8443/tcp'][0]['HostPort']
    assert str(urlsplit(config['admin_dsn']).port)==pgport
    assert str(urlsplit(config['env']['BANK_REDIS_URL']).port)==redisport
    assert str(urlsplit(config['env']['OIDC_ISSUER']).port)==issuerport
    assert pgid==inside==config['system_identifier'] and version.startswith('PostgreSQL 16.')
    assert cacheid['run_id']==inside_cache['run_id'] and cacheid['redis_version'].startswith('7.')
    assert all(not any(row[1:]) for row in roles) and len(roles)==5
    response=httpx.get(config['env']['OIDC_ISSUER']+'/.well-known/openid-configuration',
                       verify=config['env']['OIDC_CA_FILE'],timeout=5)
    response.raise_for_status()
    assert response.json()['issuer']==config['env']['OIDC_ISSUER']
    return dict(postgres_system_identifier=pgid,postgres_inside_container=inside,postgres_version=version,
                redis_run_id=cacheid['run_id'],redis_inside_container=inside_cache['run_id'],
                redis_version=cacheid['redis_version'],host_ports_match_container_bindings=True,
                certificate_verified_issuer=response.json()['issuer'],
                tables=[dict(zip(['table','owner','rls','force_rls'],row)) for row in tables],
                foreign_keys=constraints,
                roles=[dict(zip(['role','superuser','bypassrls','createdb','createrole'],row)) for row in roles])


def check(destination,task):
    destination.mkdir(parents=True,exist_ok=False)
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    before=hashes()
    containers={k:safe_container(v) for k,v in config['containers'].items()}
    identity_before=connection_identity(config,containers)
    files={'T01.1':'test_auth.py','T01.2':'test_roles.py','T01.3':'test_privacy.py'}
    selected=['tests/p01'] if task=='P01' else ['tests/p01/'+files[task]]
    command=[sys.executable,'-m','pytest',*selected,'-q','--tb=short','--timeout=30',
             '--junitxml='+str(destination/'checks.xml')]
    env={**os.environ,'LITELLM_LOCAL_MODEL_COST_MAP':'True','PYTHONUTF8':'1',
         'P01_EVIDENCE_DIR':str((destination/'observations').resolve())}
    result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8')
    output=result.stdout+'\n'+result.stderr
    secrets=[config['admin_dsn'],*config['dsns'].values(),config['env']['OIDC_CLIENT_SECRET']]
    secrets+=[u['password'] for u in config['users']]
    from urllib.parse import urlsplit
    secrets+=[urlsplit(v).password for v in [config['admin_dsn'],*config['dsns'].values(),config['env']['BANK_REDIS_URL']]]
    def scrub(text):
        for value in sorted(filter(None,secrets),key=len,reverse=True): text=text.replace(value,'[REDACTED]')
        return re.sub(r'(?i)(code|code_verifier|client_secret)=([^&\s"<>]+)',r'\1=[REDACTED]',text)
    output=scrub(output)
    (destination/'checks.txt').write_text(output,encoding='utf-8')
    xml=destination/'checks.xml'
    if xml.exists(): xml.write_text(scrub(xml.read_text(encoding='utf-8')),encoding='utf-8')
    import xml.etree.ElementTree as ET
    suites=ET.parse(xml).getroot() if xml.exists() else []
    counts={key:sum(int(s.get(key,0)) for s in suites) for key in ['tests','failures','errors','skipped']}
    identity_after=connection_identity(config,containers)
    report=dict(task_id=task,recorded_at=datetime.now(timezone.utc).isoformat(),command=command,
        status='passed' if result.returncode==0 and counts['tests']>0 and not counts['skipped'] and hashes()==before and identity_after==identity_before else 'failed-or-incomplete',
        exit_code=result.returncode,counts=counts,source_hashes=before,source_unchanged=hashes()==before,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        environment={k:importlib.metadata.version(k) for k in ['Authlib','psycopg','redis','opentelemetry-sdk','fastapi']},
        containers=containers,
        identities_before=identity_before,identities_after=identity_after,
        data='two-fictional-OIDC-customers; per-test Redis namespaces',model='not-called',
        tool='P01 Authlib/real Keycloak/PostgreSQL/Redis',policy='SPEC01/SPEC02/SPEC12',
        grader='pytest-typed-security-and-canary-assertions',reset='no existing DB reset; isolated P01 cluster; new per-test session namespaces',
        limitations=['Local self-signed TLS CA, isolated development Keycloak realm; not deployed issuer validation.',
                     'P01 conversation ownership roots only; no bank products, durable turns, preferences or support behavior.',
                     'No human review inferred from executable tests.'],reviewer=None)
    report['artifacts']={p.relative_to(destination).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in destination.rglob('*') if p.is_file()}
    (destination/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(output)
    print('Evidence:',destination/'report.json')
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--task',choices=['P01','T01.1','T01.2','T01.3'],default='P01')
    args=parser.parse_args()
    raise SystemExit(check(args.directory,args.task))
