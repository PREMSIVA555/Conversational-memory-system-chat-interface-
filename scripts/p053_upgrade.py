"""Checked additive T05.3 continuity installation; never resets ordinary demo data."""
import argparse,hashlib,json,re,sys
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from psycopg import sql
from evals.banking_world import BankingWorld
from scripts.ux_upgrade import upgrade as verify_existing

TABLES=('accounts','account_transactions','cards','card_transactions','loans','emi_schedule','loan_payments')
def snapshot(config):
    result={}
    with psycopg.connect(config['admin_dsn']) as conn:
        for table in TABLES:
            rows=conn.execute(sql.SQL('SELECT to_jsonb(t) FROM banking.{} t ORDER BY id').format(sql.Identifier(table))).fetchall()
            result[table]={'rows':len(rows),'sha256':hashlib.sha256(json.dumps(rows,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()}
    return result

def upgrade(config,recorded_migrations=None):
    if urlsplit(config['admin_dsn']).path!='/bank_p01' or urlsplit(config['dsns']['migrator']).path!='/bank_p01':
        raise RuntimeError('Explicit ordinary local demo target required')
    identity=BankingWorld(config,'bank_p01','').identity() # identity only; no create/reset
    source=json.loads((ROOT/'.genesis/sources/P05_SOURCE.json').read_text(encoding='utf-8'))
    if hashlib.sha256((ROOT/'store/banking_migrations/0028_support_proposals.sql').read_bytes()).hexdigest()!=source['manifest']['preserved_migration_sha256']:
        raise RuntimeError('Preserved migration0028 changed')
    before=snapshot(config)
    verify_existing(config,recorded_migrations) # exact old checksums/newline equivalence; no rewriting
    path=ROOT/'store/banking_migrations/0032_support_continuity.sql';digest=hashlib.sha256(path.read_bytes()).hexdigest()
    with psycopg.connect(config['dsns']['migrator']) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('banking.migration'))")
        prior=dict(conn.execute('SELECT name,sha256 FROM banking.schema_migrations').fetchall())
        if '0031_support_confirmation.sql' not in prior or '0029_workspace.sql' not in prior:raise RuntimeError('Existing support/workspace baseline required')
        existed=path.name in prior
        if existed and prior[path.name]!=digest:raise RuntimeError('Recorded0032 checksum differs')
        if not existed:
            script=path.read_text(encoding='utf-8')
            for role in ('RUNTIME','AUTH','WORKER','DISPATCHER','MIGRATOR'):
                name=config['roles'][role.lower()]
                if not re.fullmatch(r'[a-z][a-z0-9_]{0,60}',name):raise ValueError('Unsafe role')
                script=script.replace('@'+role+'@',sql.Identifier(name).as_string(conn))
            conn.execute(script)
            conn.execute('INSERT INTO banking.schema_migrations(name,sha256) VALUES(%s,%s)',(path.name,digest))
        after_records=dict(conn.execute('SELECT name,sha256 FROM banking.schema_migrations').fetchall())
        if any(after_records.get(k)!=v for k,v in prior.items()):raise RuntimeError('Prior migration record changed')
    after=snapshot(config)
    if before!=after:raise RuntimeError('Financial resource snapshot changed during upgrade')
    result={'identity':identity,'migration':path.name,'sha256':digest,'already_applied':existed,'bank_resources_unchanged':True,
            'before':before,'after':after,'prior_migration_records_preserved':True,'normal_demo_reset':False}
    (ROOT/'.genesis/evidence/T05.3-demo-upgrade.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return {'migration':path.name,'already_applied':existed,'bank_resources_unchanged':True,'identity_matched':identity['matched']}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--recorded-migrations',type=Path);args=parser.parse_args()
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    print(json.dumps(upgrade(config,args.recorded_migrations)))
