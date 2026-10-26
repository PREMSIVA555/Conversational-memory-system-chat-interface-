"""Observe a real production queue counter -> Prometheus -> local test receiver."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread,Lock
import hashlib,json,os,subprocess,sys,time,uuid,urllib.request
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from prometheus_client import generate_latest
from jobs.banking_access import AccessQueue,REJECTED


def run(folder):
    received=[];lock=Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            body=generate_latest() if self.path=='/metrics' else b''
            self.send_response(200 if body else 404);self.end_headers();self.wfile.write(body)
        def do_POST(self):
            size=int(self.headers.get('Content-Length','0'))
            if self.path!='/api/v2/alerts' or not 0<size<1000000:self.send_error(400);return
            try:value=json.loads(self.rfile.read(size))
            except ValueError:self.send_error(400);return
            with lock:received.extend(value)
            self.send_response(200);self.end_headers()
    server=ThreadingHTTPServer(('0.0.0.0',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    name='bank-p064-prom-'+uuid.uuid4().hex;created=False
    def docker(*args):
        p=subprocess.run(['docker',*map(str,args)],capture_output=True,text=True,encoding='utf-8',timeout=45)
        if p.returncode:raise RuntimeError('Owned monitoring container operation failed')
        return p.stdout.strip()
    try:
        # Unmodified production rules; only scrape/evaluation cadence is shortened.
        import shutil
        shutil.copy2(ROOT/'infra/banking-alerts.yml',folder/'rules.yml')
        port=server.server_address[1]
        config=f'''global:
  scrape_interval: 1s
  evaluation_interval: 1s
rule_files: [/drill/rules.yml]
scrape_configs:
  - job_name: banking-release-drill
    static_configs:
      - targets: ['host.docker.internal:{port}']
alerting:
  alertmanagers:
    - api_version: v2
      static_configs:
        - targets: ['host.docker.internal:{port}']
'''
        (folder/'prometheus.yml').write_text(config,encoding='utf-8')
        docker('run','--rm','--network','none','--entrypoint','/bin/promtool','--mount',f'type=bind,source={folder},target=/drill,readonly','prom/prometheus:v2.54.1','check','rules','/drill/rules.yml')
        args=['run','-d','--name',name,'-p','127.0.0.1::9090']
        if os.name!='nt':args+=['--add-host','host.docker.internal:host-gateway']
        args+=['--mount',f'type=bind,source={folder},target=/drill,readonly','prom/prometheus:v2.54.1','--config.file=/drill/prometheus.yml','--storage.tsdb.path=/prometheus','--rules.alert.resend-delay=1s','--web.listen-address=:9090']
        docker(*args);created=True
        info=json.loads(docker('inspect',name))[0];host_port=info['NetworkSettings']['Ports']['9090/tcp'][0]['HostPort'];url='http://127.0.0.1:'+host_port
        deadline=time.monotonic()+75;ready=False
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(url+'/api/v1/query?query=up',timeout=2) as r:value=json.load(r)
                if value['data']['result'] and value['data']['result'][0]['value'][1]=='1':ready=True;break
            except Exception:pass
            time.sleep(.25)
        assert ready,'Prometheus never scraped the isolated metrics server'
        time.sleep(3)
        before=REJECTED._value.get();queue=AccessQueue(None,None,capacity=1)
        assert queue.submit(None,None,None) and not queue.submit(None,None,None)
        assert REJECTED._value.get()==before+1
        matched=[]
        while time.monotonic()<deadline:
            with lock:matched=[a for a in received if a.get('labels',{}).get('alertname')=='BankingAccessQueueRejected' and a.get('labels',{}).get('job')=='banking-release-drill']
            if matched:break
            time.sleep(.25)
        assert matched,'No actual firing notification received'
        (folder/'notifications.json').write_text(json.dumps(matched,indent=2)+'\n',encoding='utf-8')
        return {'task':'T06.4','status':'passed','production_counter_delta':1,'production_rules_sha256':hashlib.sha256((folder/'rules.yml').read_bytes()).hexdigest(),
            'prometheus_image_id':info['Image'],'receiver_observed_notifications':len(matched),'alertname':'BankingAccessQueueRejected','notification_path':'/api/v2/alerts','counter_to_prometheus_to_receiver':True,
            'limits':['Local synthetic receiver implementing the notification endpoint, not a production Alertmanager/downstream paging service.','Unmodified production alert expressions; isolated1s scrape/evaluation and1s resend cadence.','No machine-wide firewall/trust configuration changes; no external recipient messaged.']}
    finally:
        if created:
            logs=subprocess.run(['docker','logs',name],capture_output=True,timeout=20)
            (folder/'prometheus-private.log').write_bytes(logs.stdout+logs.stderr)
            docker('rm','-f',name)
        server.shutdown();server.server_close();thread.join(timeout=5)


def main():
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    folder=ROOT/'.genesis/local'/('p064-alerts-'+uuid.uuid4().hex);folder.mkdir(parents=True)
    path=ROOT/('.genesis/evidence/T06.4-alerts-'+uuid.uuid4().hex+'.json');report={'task':'T06.4','status':'failed'}
    try:report=run(folder);return 0
    except Exception as error:report['error']=type(error).__name__;return 1
    finally:
        report['private_run_folder']=str(folder)
        path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(json.dumps({'status':report['status'],'report':str(path)}))


if __name__=='__main__':raise SystemExit(main())
