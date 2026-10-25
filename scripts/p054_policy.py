"""User-authorized synthetic demo policy; append catalog, never reset bank data."""
import copy,json,sys,hashlib
from urllib.parse import urlsplit
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from store.banking_policies import stage,activate
from scripts.p053_upgrade import snapshot
from evals.banking_world import BankingWorld

def with_card_support(manifest):
    value=copy.deepcopy(manifest)
    additions=json.loads((ROOT/'evals/fixtures/p05_card_support_policy.json').read_text(encoding='utf-8'))
    for doc in additions:
        existing=next((x for x in value['documents'] if x['version']==doc['version']),None)
        if existing:
            # Compare validated normalized content, including effective instants.
            from store.banking_policies import Document
            if Document.model_validate(existing)!=Document.model_validate(doc):raise ValueError('Existing synthetic policy version differs')
        else:value['documents'].append(doc)
    value['expected_documents']=len(value['documents'])
    return value

def install(config):
    if urlsplit(config['dsns']['migrator']).path!='/bank_p01':raise RuntimeError('Ordinary local demo target required')
    identity=BankingWorld(config,'bank_p01','').identity()
    before=snapshot(config)
    with psycopg.connect(config['dsns']['migrator'],autocommit=True) as c:
        expected=hashlib.sha256((ROOT/'store/banking_migrations/0032_support_continuity.sql').read_bytes()).hexdigest()
        if c.execute("SELECT sha256 FROM banking.schema_migrations WHERE name='0032_support_continuity.sql'").fetchone()!=(expected,):raise RuntimeError('Approved support baseline differs')
        old,manifest=c.execute('SELECT s.id,s.manifest FROM banking.policy_active a JOIN banking.policy_sets s ON s.id=a.set_id WHERE a.singleton').fetchone()
        new=stage(c,with_card_support(manifest))
        if new!=old:activate(c,new,expected=old)
    after=snapshot(config)
    if before!=after:raise RuntimeError('Financial resources changed during policy installation')
    report={'synthetic':True,'user_authorization':'A clearly labeled synthetic policy for this demo','previous_set':old,'active_set':new,'financial_resources_unchanged':True,'normal_demo_reset':False,'identity_matched':identity['matched']}
    (ROOT/'.genesis/evidence/T05.4-policy-install.json').write_text(json.dumps(report,indent=2,default=str)+'\n',encoding='utf-8')
    return report

if __name__=='__main__':
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    print(json.dumps(install(config),default=str))
