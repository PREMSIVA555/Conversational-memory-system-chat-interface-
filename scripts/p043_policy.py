"""Offline policy catalog operator. No web ingestion or customer mutations.

Uses BANK_POLICY_MIGRATOR_DSN / BANK_POLICY_RUNTIME_DSN when configured; local
development otherwise uses the existing private P01 configuration. Never logs
connection strings. Staging and activation are deliberately separate commands.
"""
from pathlib import Path
import argparse,json,os,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from store.banking_policies import Manifest,stage,activate,retrieve


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='operation',required=True)
    for operation in ('validate','stage'):
        commands.add_parser(operation).add_argument('manifest',type=Path)
    a=commands.add_parser('activate');a.add_argument('set_id');a.add_argument('--expected',required=True,help='previous set hash or none for initial activation')
    q=commands.add_parser('query')
    for name in ('product','scope','locale','question'):q.add_argument('--'+name,required=True)
    q.add_argument('--at');q.add_argument('--embedding',type=json.loads);q.add_argument('--embedding-model');q.add_argument('--limit',type=int,default=5)
    args=parser.parse_args()
    if args.operation in ('validate','stage'):
        value=json.loads(args.manifest.read_text(encoding='utf-8'));Manifest.model_validate(value)
        if args.operation=='validate':return dict(valid=True,synthetic=True)
    role='runtime' if args.operation=='query' else 'migrator'
    dsn=os.getenv('BANK_POLICY_'+role.upper()+'_DSN')
    if not dsn:
        config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'));dsn=config['dsns'][role]
    with psycopg.connect(dsn,autocommit=True) as conn:
        if args.operation=='stage':return dict(staged=stage(conn,value),activated=False)
        if args.operation=='activate':
            activate(conn,args.set_id,expected=None if args.expected=='none' else args.expected)
            return dict(activated=args.set_id)
        return retrieve(conn,**{k:v for k,v in vars(args).items() if k!='operation'})


if __name__=='__main__':
    try:print(json.dumps(main(),default=str))
    except (ValueError,OSError,KeyError,psycopg.Error) as e:
        print(json.dumps(dict(error=type(e).__name__,sqlstate=getattr(e,'sqlstate',None))))
        sys.exit(1)
