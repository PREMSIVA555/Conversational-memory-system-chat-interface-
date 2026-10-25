import json
from pathlib import Path
import pytest
from scripts.p054_journey import run
initial_hashes=[]

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['lost_ack','audit_rollback'])
async def test_complete_journey(mode):
    report=await run(mode)
    Path(f'.genesis/evidence/T05.4-{mode}.json').write_text(json.dumps(report,indent=2,default=str)+'\n',encoding='utf-8')
    assert [s['step'] for s in report['checkpoints']]==list(range(1,31))
    initial_hashes.append(report['initial_semantic_hash'])
    assert len(set(initial_hashes))==1
