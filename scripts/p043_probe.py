"""JSON stdin operator for guarded policy trials; never accepts a DSN or SQL."""
from pathlib import Path
import sys,json,re
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from psycopg import sql
from evals.policy_world import PolicyWorld
from evals.banking_world import database_dsn
from store.banking_policies import stage,activate,retrieve


def operate(req):
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    if req['op']=='create':
        world=PolicyWorld.create(config)
        return dict(world=world.name,identity=world.identity(),policy_data='synthetic',embedding='caller-supplied fixture; no live calls')
    name=req['world']
    if type(name) is not str or not re.fullmatch(r'p03_slice_[a-f0-9]{32}',name):
        raise ValueError('Guarded trial name required')
    manifest=json.loads((ROOT/'.genesis/local/p03-worlds'/name/'manifest.json').read_text())
    world=PolicyWorld(config,name,manifest['marker']);world.guard()
    op=req['op']
    if op=='identity':return world.identity()
    if op=='oracle':return world.oracle()
    if op=='lane':
        if req['enabled'] not in (True,False):raise ValueError('Boolean required')
        with psycopg.connect(world.admin_dsn) as c:
            c.execute(sql.SQL(('GRANT EXECUTE ON FUNCTION banking.policy_lexical(text,text) TO {}' if req['enabled'] else 'REVOKE EXECUTE ON FUNCTION banking.policy_lexical(text,text) FROM {}')).format(sql.Identifier(config['roles']['runtime'])))
        return dict(lexical_enabled=req['enabled'])
    role='runtime' if op in ('query','runtime_stage','runtime_activate','runtime_update') else 'migrator'
    with psycopg.connect(database_dsn(config['dsns'][role],name),autocommit=True) as c:
        if op in ('stage','runtime_stage'):return dict(set_id=stage(c,req['manifest']))
        if op in ('activate','runtime_activate'):
            activate(c,req['set_id'],expected=req.get('expected'));return dict(activated=req['set_id'])
        if op=='runtime_update':
            c.execute('UPDATE banking.policy_sets SET manifest=manifest');return dict(updated=True)
        if op=='immutable_update':
            c.execute('UPDATE banking.policy_sets SET manifest=manifest');return dict(updated=True)
        if op=='query':return retrieve(c,**req['query'])
    raise ValueError('Unknown operation')


if __name__=='__main__':
    try:
        result=operate(json.load(sys.stdin));print(json.dumps(dict(ok=True,result=result),default=str))
    except (ValueError,KeyError,psycopg.Error) as e:
        # No DSNs, SQL, params, traces or config values in the public operator.
        print(json.dumps(dict(ok=False,error=type(e).__name__,sqlstate=getattr(e,'sqlstate',None))))
        sys.exit(1)
