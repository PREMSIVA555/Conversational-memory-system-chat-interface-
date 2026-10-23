"""Start the configured local banking API or HTTPS browser app in the background.

Uses only ignored runtime configuration. Refuses occupied ports; never kills
an unrelated server. Does not install a local CA in the operating system.
"""
from pathlib import Path
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
LOCAL=ROOT/'.genesis/local/p01'


def start(kind):
    env={**os.environ,**dict(dotenv_values(LOCAL/'runtime.env')),'PYTHONUTF8':'1',
         'PYTHONIOENCODING':'utf-8','API_BASE_URL':'http://127.0.0.1:8000'}
    port=8000 if kind=='api' else 3443
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1',port))==0:
            raise SystemExit(f'Port {port} is occupied. Inspect the existing process before restarting.')
    command=([sys.executable,'-m','api.main'] if kind=='api' else
             [shutil.which('node'),str(ROOT/'frontend/node_modules/next/dist/bin/next'),'dev',
              '--hostname','127.0.0.1','--port','3443','--experimental-https',
              '--experimental-https-key',str(LOCAL/'tls.key'),'--experimental-https-cert',str(LOCAL/'tls.crt')])
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (LOCAL/(kind+'.stdout.log')).open('ab') as out,(LOCAL/(kind+'.stderr.log')).open('ab') as err:
        process=subprocess.Popen(command,cwd=ROOT if kind=='api' else ROOT/'frontend',env=env,
            stdin=subprocess.DEVNULL,stdout=out,stderr=err,creationflags=flags)
    (LOCAL/(kind+'.pid')).write_text(str(process.pid),encoding='utf-8')
    print(kind,'started; PID',process.pid)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=['api','frontend'])
    start(parser.parse_args().kind)
