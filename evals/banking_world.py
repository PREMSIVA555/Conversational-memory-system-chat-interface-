"""T03.4 test-only per-database worlds. Never imported by the application.

Reset requires both a workspace manifest and an independently stored DB marker.
Only a newly created, allowlisted database on the verified local P01 cluster is
eligible. No shared database, container, role, or Redis configuration is reset.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import hashlib
import json
import re
import subprocess
import uuid
import psycopg
from psycopg import sql
import redis
from auth.config import Settings
from store.banking_migrate import apply

ROOT=Path(__file__).resolve().parents[1]
WORLD_ROOT=ROOT/'.genesis/local/p03-worlds'
FIXTURE=ROOT/'evals/fixtures/p03_account_slice.json'

class UnsafeTarget(ValueError): pass

def database_dsn(value,name):
    p=urlsplit(value)
    if p.hostname!='127.0.0.1' or not p.port or p.query or p.fragment:
        raise UnsafeTarget('Only explicit loopback PostgreSQL targets are allowed')
    return urlunsplit((p.scheme,p.netloc,'/'+name,'',''))

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()

@dataclass
class BankingWorld:
    config:dict=field(repr=False)
    name:str
    marker:str

    @property
    def folder(self):return WORLD_ROOT/self.name
    @property
    def admin_dsn(self):return database_dsn(self.config['admin_dsn'],self.name)
    @property
    def namespace(self):return self.name
    @property
    def fixture(self):return json.loads(FIXTURE.read_text(encoding='utf-8'))
    @property
    def settings(self):
        e=self.config['env']
        return Settings(issuer=e['OIDC_ISSUER'],client_id=e['OIDC_CLIENT_ID'],client_secret=e['OIDC_CLIENT_SECRET'],
            redirect_uri=e['OIDC_REDIRECT_URI'],origin=e['BANK_ORIGIN'],redis_url=e['BANK_REDIS_URL'],
            runtime_dsn=database_dsn(self.config['dsns']['runtime'],self.name),
            identity_dsn=database_dsn(self.config['dsns']['auth'],self.name),
            worker_dsn=database_dsn(self.config['dsns']['worker'],self.name),
            namespace=self.namespace,oidc_ca_file=e['OIDC_CA_FILE'])

    @classmethod
    def create(cls,config):
        # Verify the live server and matched Docker connection before CREATE DATABASE.
        obj=cls(config,'p03_slice_'+uuid.uuid4().hex,uuid.uuid4().hex)
        obj.identity()
        obj.folder.mkdir(parents=True,exist_ok=False)
        with psycopg.connect(config['admin_dsn'],autocommit=True) as c:
            c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(obj.name)))
        with psycopg.connect(obj.admin_dsn) as c:
            c.execute('CREATE TABLE public.p03_trial_marker(name text PRIMARY KEY,marker text NOT NULL)')
            c.execute('INSERT INTO public.p03_trial_marker VALUES(%s,%s)',(obj.name,obj.marker))
        (obj.folder/'manifest.json').write_text(json.dumps(dict(name=obj.name,marker=obj.marker,system_identifier=config['system_identifier'])),encoding='utf-8')
        obj.reset()
        return obj

    def identity(self):
        def dock(*args):
            p=subprocess.run(['docker',*args],capture_output=True,text=True,encoding='utf-8')
            if p.returncode:raise UnsafeTarget('Dedicated Docker identity unavailable')
            return p.stdout.strip()
        names=self.config['containers']
        pg=json.loads(dock('inspect',names['postgres']))[0]
        rd=json.loads(dock('inspect',names['redis']))[0]
        pgport=int(pg['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
        rdport=int(rd['NetworkSettings']['Ports']['6379/tcp'][0]['HostPort'])
        if (urlsplit(self.config['admin_dsn']).port!=pgport or urlsplit(self.config['env']['BANK_REDIS_URL']).port!=rdport
            or not pg['State']['Running'] or not rd['State']['Running']):raise UnsafeTarget('Container port/state mismatch')
        docker_id=dock('exec',names['postgres'],'psql','-U','p01_owner','-d','bank_p01','-Atc','SELECT system_identifier FROM pg_control_system()')
        with psycopg.connect(self.config['admin_dsn']) as c:
            version,system=c.execute('SELECT current_setting(\'server_version\'),system_identifier::text FROM pg_control_system()').fetchone()
        with redis.Redis.from_url(self.config['env']['BANK_REDIS_URL'],decode_responses=True) as r:
            ri=r.info('server');runid=ri['run_id'];rv=ri['redis_version']
        # Redis Docker identity is matched by its loopback mapped port and process run ID
        # obtained inside the container using credentials passed via stdin, not argv.
        password=urlsplit(self.config['env']['BANK_REDIS_URL']).password
        command='AUTH '+password+'\nINFO server\n'
        p=subprocess.run(['docker','exec','-i',names['redis'],'redis-cli','--raw'],input=command,capture_output=True,text=True,encoding='utf-8')
        if p.returncode or ('run_id:'+runid) not in p.stdout:raise UnsafeTarget('Redis process identity mismatch')
        if system!=str(self.config['system_identifier']) or docker_id!=system or not version.startswith('16.') or not rv.startswith('7.'):
            raise UnsafeTarget('Expected PostgreSQL16/Redis7 identity mismatch')
        return dict(postgresql_version=version,postgresql_system_identifier=system,postgres_container=names['postgres'],
                    redis_version=rv,redis_run_id=runid,redis_container=names['redis'],matched=True)

    def guard(self,dsn=None):
        if not re.fullmatch('p03_slice_[a-f0-9]{32}',self.name):raise UnsafeTarget('Non-test reset target refused')
        base=WORLD_ROOT.resolve()
        if self.folder.is_symlink() or not self.folder.resolve().is_relative_to(base):raise UnsafeTarget('Unsafe trial file path')
        # Reject Windows junction/reparse points in every ancestor; never recursively delete files.
        for p in [ROOT/'.genesis',ROOT/'.genesis/local',WORLD_ROOT,self.folder]:
            if p.exists() and (p.is_symlink() or getattr(p.stat(),'st_file_attributes',0)&0x400):raise UnsafeTarget('Linked trial path refused')
        manifest=self.folder/'manifest.json'
        if manifest.is_symlink():raise UnsafeTarget('Linked trial manifest refused')
        m=json.loads(manifest.read_text(encoding='utf-8'))
        if m!=dict(name=self.name,marker=self.marker,system_identifier=self.config['system_identifier']):raise UnsafeTarget('Unowned trial manifest')
        target=dsn or self.admin_dsn
        if urlsplit(target).path!='/'+self.name:raise UnsafeTarget('Database is not owned trial')
        with psycopg.connect(target) as c:
            row=c.execute('SELECT current_database(),current_user,system_identifier::text FROM pg_control_system()').fetchone()
            if row!=(self.name,urlsplit(self.config['admin_dsn']).username,str(self.config['system_identifier'])):raise UnsafeTarget('Reset role/cluster mismatch')
            if c.execute('SELECT name,marker FROM public.p03_trial_marker').fetchall()!=[(self.name,self.marker)]:raise UnsafeTarget('Missing independent trial marker')

    def reset(self,dsn=None):
        self.guard(dsn)
        f=self.fixture
        with psycopg.connect(self.admin_dsn,autocommit=True) as c:
            c.execute('DROP SCHEMA IF EXISTS banking CASCADE')
            c.execute(sql.SQL('CREATE SCHEMA banking AUTHORIZATION {}').format(sql.Identifier(self.config['roles']['migrator'])))
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'],self.name)) as c:apply(c,self.config['roles'],through='0024')
        with psycopg.connect(self.admin_dsn) as c:
            for i,owner in enumerate(f['customers']):
                c.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(owner,))
                c.execute('INSERT INTO banking.identity_links VALUES(%s,%s,%s)',(self.settings.issuer,self.config['users'][i]['subject'],owner))
            for a in f['accounts']:
                c.execute('INSERT INTO banking.accounts(id,customer_id,display_name,currency,current_minor,available_minor,as_of) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                    (a['id'],f['customers'][a['owner']],a['name'],a['currency'],a['current'],a['available'],f['domain_time']))
            for t in f['transactions']:
                a=f['accounts'][t['account']]
                c.execute('INSERT INTO banking.account_transactions(id,account_id,customer_id,amount_minor,status,booked_on,reason,as_of) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                    (t['id'],a['id'],f['customers'][a['owner']],t['amount'],t['status'],t['date'],t['reason'],f['domain_time']))
            # Test reset pins semantic source metadata after deterministic insert triggers.
            c.execute('ALTER TABLE banking.accounts DISABLE TRIGGER account_revision')
            c.execute('UPDATE banking.accounts SET version=1,as_of=%s',(f['domain_time'],))
            c.execute('ALTER TABLE banking.accounts ENABLE TRIGGER account_revision')
        with redis.Redis.from_url(self.settings.redis_url) as r:
            keys=list(r.scan_iter(self.namespace+':*'))
            if keys:r.delete(*keys)
            r.set(self.namespace+':world',self.marker)
        (self.folder/'domain-clock.json').write_text(json.dumps(dict(domain_time=f['domain_time'])),encoding='utf-8')
        canary=self.folder/'canary.txt'
        if canary.is_symlink():raise UnsafeTarget('Linked trial canary refused')
        if canary.exists():canary.unlink()
        return self.semantic_hash()

    def oracle(self):
        with psycopg.connect(self.admin_dsn) as c:
            return dict(accounts=c.execute('SELECT id::text,customer_id::text,display_name,currency,current_minor::text,available_minor::text,version::text,as_of::text FROM banking.accounts ORDER BY id').fetchall(),
                transactions=c.execute('SELECT id::text,account_id::text,customer_id::text,amount_minor::text,status,booked_on::text,reason,version::text,as_of::text FROM banking.account_transactions ORDER BY booked_on,id').fetchall())
    def semantic_hash(self):return digest(self.oracle())
