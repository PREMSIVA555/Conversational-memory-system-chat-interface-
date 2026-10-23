"""Private subprocess harness for actual process-death recovery tests, never an API option."""
import json
import sys
from pathlib import Path
from auth.config import Settings
from api.foundation import create_app
from tests.p03.test_coordinator import ScriptedTransport
import uvicorn

def main():
    control=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if control.get('prepare_metadata'):
        # Initial-worker setup precedes acceptance. A replacement deliberately
        # starts cold, inside the original persisted deadline.
        from llm.config import completion_capabilities
        assert completion_capabilities()['structured_json']
    app=create_app(Settings(**control['settings']),model_transport=ScriptedTransport(control['plan'],delay=control.get('delay',0)),turns_enabled=True,turn_run_seconds=30)
    uvicorn.run(app,host='127.0.0.1',port=control['port'],log_level='critical',access_log=False)
if __name__=='__main__':main()
