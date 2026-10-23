"""Explicit local operator: inventory, guard, reviewed backfill and rollout flag.

DSNs come only from named environment variables and are never printed. Mapping
or flag mutation is not exposed to authenticated customer HTTP callers.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import psycopg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from store.db import load_env
from store.banking_cutover import inventory,install_guard,record_guard,build_plan,apply_plan,digest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['inventory','install-guard','plan','backfill','flag'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--legacy-dsn-env',default='DATABASE_URL')
    parser.add_argument('--bank-dsn-env',default='BANK_MIGRATOR_DATABASE_URL')
    parser.add_argument('--mappings',type=Path)
    parser.add_argument('--reviewed-plan-sha256')
    parser.add_argument('--expected-snapshot-sha256')
    parser.add_argument('--private-backup',type=Path)
    parser.add_argument('--enabled',choices=['true','false'])
    parser.add_argument('--expected-generation',type=int)
    args=parser.parse_args(); load_env()
    if args.output.exists(): raise ValueError('Evidence output already exists')
    if args.operation=='flag':
        if args.enabled is None or args.expected_generation is None: raise ValueError('Flag CAS arguments required')
        with psycopg.connect(os.environ[args.bank_dsn_env]) as bank:
            report=bank.execute('SELECT banking.set_memory_rollout(%s,%s)',(args.enabled=='true',args.expected_generation)).fetchone()[0]
    else:
        with psycopg.connect(os.environ[args.legacy_dsn_env]) as legacy:
            if args.operation=='inventory':
                legacy.execute('SET TRANSACTION READ ONLY'); report=inventory(legacy)
            elif args.operation=='install-guard':
                if not args.expected_snapshot_sha256 or not args.private_backup: raise ValueError('Reviewed inventory and private backup required')
                report=install_guard(legacy,args.expected_snapshot_sha256,args.private_backup)
            else:
                if args.mappings is None: raise ValueError('Explicit mapping manifest required, including an empty []')
                mappings=json.loads(args.mappings.read_text(encoding='utf-8'))
                with psycopg.connect(os.environ[args.bank_dsn_env]) as bank:
                    if args.operation=='plan':
                        legacy.execute('SET TRANSACTION READ ONLY'); bank.execute('SET TRANSACTION READ ONLY')
                        report=build_plan(legacy,bank,mappings); report['plan_sha256']=digest(report)
                    else:
                        if not args.reviewed_plan_sha256: raise ValueError('Reviewed plan hash required')
                        report=apply_plan(legacy,bank,mappings,args.reviewed_plan_sha256)
    if args.operation=='install-guard':
        # The legacy DDL has committed before this separate-database attestation.
        # A crash before recording it leaves banking acceptance disabled.
        with psycopg.connect(os.environ[args.bank_dsn_env]) as bank:
            report['banking_guard_attestation']=record_guard(bank,report)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,default=str)+'\n',encoding='utf-8')
    print('Cutover operation completed; sanitized evidence:',args.output)

if __name__=='__main__':
    try: main()
    except Exception as error:
        print('Cutover operation failed:',type(error).__name__)
        raise SystemExit(1)
