"""T04.3 native gate: frozen source, real private PG16/Redis7, labeled retrieval."""
from pathlib import Path
import sys,json,hashlib,subprocess
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from evals.policy_world import PolicyWorld
from evals.banking_world import database_dsn
from store.banking_policies import stage,activate,retrieve


def hashes():
    files=[]
    for pattern in ('store/banking*.py','store/banking_migrations/*.sql','evals/*world.py','evals/fixtures/p04_policy*.json','scripts/p043*.py','tests/p04/test_policies.py','.genesis/sources/P04_SOURCE.json'):
        files.extend(ROOT.glob(pattern))
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(files))}


def main(output):
    out=(ROOT/output).resolve();allowed=(ROOT/'evidence/policy').resolve()
    if not out.is_relative_to(allowed) or out==allowed:raise ValueError('Expected new evidence/policy/<run>/retrieval directory')
    out.mkdir(parents=True,exist_ok=False)
    before=hashes()
    p=subprocess.run([sys.executable,'-m','pytest','tests/p04/test_policies.py','-q','--junitxml='+str(out/'pytest.xml')],cwd=ROOT,capture_output=True,text=True)
    (out/'pytest.txt').write_text(p.stdout+p.stderr,encoding='utf-8')
    suite=ET.parse(out/'pytest.xml').getroot();cases=suite.findall('.//testcase')
    assert p.returncode==0,p.stdout
    world=PolicyWorld.create(json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8')))
    identity=world.identity()
    fixture=json.loads((ROOT/'evals/fixtures/p04_policy_queries.json').read_text(encoding='utf-8'))
    with psycopg.connect(database_dsn(world.config['dsns']['migrator'],world.name),autocommit=True) as c:
        sid=stage(c,fixture['manifest']);activate(c,sid,expected=None)
    rows=[]
    with psycopg.connect(database_dsn(world.config['dsns']['runtime'],world.name),autocommit=True) as c:
        for case in fixture['queries']:
            result=retrieve(c,**case['query'])
            actual=[(e['version'],e['section']) for e in result['evidence']]
            expected=tuple(case['relevant']) if case['relevant'] else None
            rank=(actual.index(expected)+1) if expected in actual else None
            assert result['answer_support']=='not_evaluated'
            assert (result['status']=='missing' and not actual) if expected is None else rank is not None
            lane_ranks={}
            for lane,keys in result.get('rankings',{}).items():
                key=json.dumps(list(expected),separators=(',',':')) if expected else None
                lane_ranks[lane]=(keys.index(key)+1) if key in keys else None
            rows.append(dict(id=case['id'],relevant=case['relevant'],rank=rank,lane_ranks=lane_ranks,result=result))
    positive=[r for r in rows if r['relevant']]
    metrics={lane:dict(recall_at_5=sum(1 for r in positive if (r['rank'] if lane=='hybrid' else r['lane_ranks'].get(lane)) is not None and (r['rank'] if lane=='hybrid' else r['lane_ranks'].get(lane))<=5)/len(positive),
        mrr=sum(1/(r['rank'] if lane=='hybrid' else r['lane_ranks'].get(lane)) if (r['rank'] if lane=='hybrid' else r['lane_ranks'].get(lane)) else 0 for r in positive)/len(positive)) for lane in ('vector','lexical','hybrid')}
    assert hashes()==before,'Source changed during gate'
    report=dict(task='T04.3',passed=True,pytest_cases=len(cases),identity=identity,world=world.name,
        retrieval_queries=len(rows),relevance_metrics=metrics,answer_support='not_evaluated; T04.4 remains queued',
        embeddings='explicit deterministic 3D fixture, not a live model-quality evaluation',source_sha256=before,source_unchanged=True,cases=rows)
    (out/'report.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('source_sha256','cases')}))


if __name__=='__main__':main(sys.argv[1])
