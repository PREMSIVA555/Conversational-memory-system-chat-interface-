"""Install only UX migration0029, preserving pending P05 and prior checksums.

Windows worktrees can check out LF/CRLF differently. Existing migrations are
verified against both exact newline representations, never rewritten/reapplied.
Unknown content or an existing different0029 checksum aborts the transaction.
"""
import argparse,hashlib,json,re,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from psycopg import sql

def upgrade(config,recorded_migrations=None):
    folder=ROOT/'store/banking_migrations';path=folder/'0029_workspace.sql'
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    with psycopg.connect(config['dsns']['migrator']) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('banking.migration'))")
        recorded=dict(conn.execute('SELECT name,sha256 FROM banking.schema_migrations').fetchall())
        if '0027_policy_catalog.sql' not in recorded:raise RuntimeError('Approved P04/H01 baseline required')
        for name,expected in recorded.items():
            source=folder/name
            if name=='0028_support_proposals.sql' and not source.exists():continue # preserved separate pending phase
            if not source.exists():raise RuntimeError('Unknown installed migration: '+name)
            raw=source.read_bytes();lf=raw.replace(b'\r\n',b'\n')
            if expected not in {hashlib.sha256(x).hexdigest() for x in (raw,lf,lf.replace(b'\n',b'\r\n'))}:
                # Some old files contain mixed endings. An explicitly supplied
                # original checkout must match the recorded bytes AND this
                # checkout's exact SQL after newline normalization.
                original=Path(recorded_migrations)/name if recorded_migrations else None
                old=original.read_bytes() if original and original.is_file() else b''
                if hashlib.sha256(old).hexdigest()!=expected or old.replace(b'\r\n',b'\n')!=lf:
                    raise RuntimeError('Recorded migration content changed: '+name)
        if path.name in recorded:
            if recorded[path.name]!=digest:raise RuntimeError('UX migration checksum changed')
            return {'migration':path.name,'already_applied':True,'prior_checksums_preserved':True}
        script=path.read_text(encoding='utf-8')
        for role in ('RUNTIME','AUTH','WORKER','DISPATCHER','MIGRATOR'):
            value=config['roles'][role.lower()]
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,60}',value):raise ValueError('Unsafe role')
            script=script.replace('@'+role+'@',sql.Identifier(value).as_string(conn))
        conn.execute(script)
        conn.execute('INSERT INTO banking.schema_migrations(name,sha256) VALUES(%s,%s)',(path.name,digest))
    return {'migration':path.name,'already_applied':False,'prior_checksums_preserved':True}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recorded-migrations',type=Path)
    options=parser.parse_args()
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    print(json.dumps(upgrade(config,options.recorded_migrations)))
