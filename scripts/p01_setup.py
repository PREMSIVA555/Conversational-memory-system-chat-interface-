"""Provision isolated local P01 services and two fictional OIDC customers.

No existing app/test database is reset. Secrets and self-signed local TLS key
stay under ignored .genesis/local/p01. Re-running reuses the exact config.
"""
from pathlib import Path
import ipaddress
import json
import secrets
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime,timedelta,timezone
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
LOCAL=ROOT/'.genesis/local/p01'


def docker(*args):
    p=subprocess.run(['docker',*map(str,args)],capture_output=True,text=True,encoding='utf-8')
    if p.returncode:
        raise RuntimeError('Docker operation failed; inspect dedicated P01 container status')
    return p.stdout.strip()


def inspect(name):
    return json.loads(docker('inspect',name))[0]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def tls():
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Bank P01 local test')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
          .not_valid_after(now+timedelta(days=30)).add_extension(x509.SubjectAlternativeName([
              x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
          .sign(key,hashes.SHA256()))
    (LOCAL/'tls.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
             serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    (LOCAL/'tls.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def setup():
    import httpx
    import psycopg
    from psycopg import sql
    from store.banking_migrate import apply
    config=LOCAL/'config.json'
    if config.exists():
        print('P01 already provisioned. Credentials/config retained locally; no reset performed.')
        return
    if LOCAL.exists():
        raise RuntimeError('Partial P01 setup exists; inspect it rather than overwriting')
    LOCAL.mkdir(parents=True)
    run=uuid.uuid4().hex[:8]
    admin_password=secrets.token_hex(24)
    redis_password=secrets.token_hex(24)
    client_secret=secrets.token_hex(24)
    kc_port=free_port()
    origin='https://localhost:3443'
    issuer=f'https://127.0.0.1:{kc_port}/realms/bank-p01'
    roles={kind:'b01_'+run+'_'+kind for kind in ['runtime','auth','worker','dispatcher','migrator']}
    passwords={kind:secrets.token_hex(24) for kind in roles}
    users=[dict(username='customer-'+letter,password=secrets.token_hex(16),
                subject=str(uuid.uuid4()),customer=str(uuid.uuid4())) for letter in ['a','b']]
    names={kind:'bank-p01-'+kind+'-'+run for kind in ['postgres','redis','oidc']}
    # Checkpoint before any process starts; no credentials on command lines.
    bootstrap=dict(run=run,containers=names,roles=roles,passwords=passwords,users=users,
                   admin_password=admin_password,redis_password=redis_password,client_secret=client_secret)
    (LOCAL/'bootstrap.json').write_text(json.dumps(bootstrap,indent=2),encoding='utf-8')
    (LOCAL/'postgres.env').write_text(f'POSTGRES_USER=p01_owner\nPOSTGRES_PASSWORD={admin_password}\nPOSTGRES_DB=bank_p01\n',encoding='utf-8')
    (LOCAL/'redis.conf').write_text(f'bind 0.0.0.0\nprotected-mode yes\nrequirepass {redis_password}\nappendonly yes\n',encoding='utf-8')
    realm=dict(realm='bank-p01',enabled=True,sslRequired='none',registrationAllowed=False,
        clients=[dict(clientId='bank-browser',secret=client_secret,publicClient=False,
            standardFlowEnabled=True,directAccessGrantsEnabled=False,implicitFlowEnabled=False,
            redirectUris=[origin+'/bank-api/auth/callback'],webOrigins=[origin],
            attributes={'pkce.code.challenge.method':'S256'})],
        users=[dict(id=u['subject'],username=u['username'],enabled=True,
                    emailVerified=True,firstName='Synthetic',lastName='Customer',
                    email=u['username']+'@example.invalid',
                    credentials=[dict(type='password',value=u['password'],temporary=False)]) for u in users])
    (LOCAL/'realm.json').write_text(json.dumps(realm),encoding='utf-8')
    tls()
    docker('run','-d','--name',names['postgres'],'--env-file',LOCAL/'postgres.env',
           '-p','127.0.0.1::5432','pgvector/pgvector:pg16')
    docker('run','-d','--name',names['redis'],'-p','127.0.0.1::6379',
           '--mount',f'type=bind,source={LOCAL / "redis.conf"},target=/usr/local/etc/redis/redis.conf,readonly',
           'redis:7-alpine','redis-server','/usr/local/etc/redis/redis.conf')
    docker('run','-d','--name',names['oidc'],'-p',f'127.0.0.1:{kc_port}:8443',
           '--mount',f'type=bind,source={LOCAL / "realm.json"},target=/opt/keycloak/data/import/bank-p01-realm.json,readonly',
           '--mount',f'type=bind,source={LOCAL / "tls.crt"},target=/opt/keycloak/conf/tls.crt,readonly',
           '--mount',f'type=bind,source={LOCAL / "tls.key"},target=/opt/keycloak/conf/tls.key,readonly',
           'quay.io/keycloak/keycloak:26.8.0','start-dev','--import-realm',
           '--http-enabled=false','--https-certificate-file=/opt/keycloak/conf/tls.crt',
           '--https-certificate-key-file=/opt/keycloak/conf/tls.key',
           '--hostname',f'https://127.0.0.1:{kc_port}')
    pgport=inspect(names['postgres'])['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
    redisport=inspect(names['redis'])['NetworkSettings']['Ports']['6379/tcp'][0]['HostPort']
    admin=f'postgresql://p01_owner:{admin_password}@127.0.0.1:{pgport}/bank_p01'
    for _ in range(90):
        try:
            conn=psycopg.connect(admin,connect_timeout=1,autocommit=True)
            break
        except psycopg.OperationalError:
            time.sleep(1)
    else: raise RuntimeError('P01 PostgreSQL readiness timed out')
    with conn:
        for kind,name in roles.items():
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE').format(
                         sql.Identifier(name),sql.Literal(passwords[kind])))
            conn.execute(sql.SQL('GRANT CONNECT ON DATABASE bank_p01 TO {}').format(sql.Identifier(name)))
        conn.execute(sql.SQL('CREATE SCHEMA banking AUTHORIZATION {}').format(sql.Identifier(roles['migrator'])))
    dsns={kind:f'postgresql://{name}:{passwords[kind]}@127.0.0.1:{pgport}/bank_p01' for kind,name in roles.items()}
    with psycopg.connect(dsns['migrator'],autocommit=True) as conn:
        apply(conn,roles)
    with psycopg.connect(admin) as conn:
        for user in users:
            conn.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(user['customer'],))
            conn.execute('INSERT INTO banking.identity_links(issuer,subject,customer_id) VALUES(%s,%s,%s)',
                         (issuer,user['subject'],user['customer']))
        pgid=str(conn.execute('SELECT system_identifier FROM pg_control_system()').fetchone()[0])
    for _ in range(120):
        try:
            r=httpx.get(issuer+'/.well-known/openid-configuration',timeout=2,verify=str(LOCAL/'tls.crt'))
            if r.status_code==200 and r.json().get('issuer')==issuer: break
        except httpx.HTTPError: pass
        time.sleep(1)
    else: raise RuntimeError('P01 test OIDC issuer readiness timed out')
    env=dict(APP_PROFILE='banking',API_HOST='127.0.0.1',API_PORT='8000',
             OIDC_ISSUER=issuer,OIDC_CLIENT_ID='bank-browser',OIDC_CLIENT_SECRET=client_secret,
             OIDC_REDIRECT_URI=origin+'/bank-api/auth/callback',BANK_ORIGIN=origin,
             OIDC_ALLOW_LOOPBACK_HTTP='false',OIDC_CA_FILE=str(LOCAL/'tls.crt'),BANK_REDIS_URL=f'redis://:{redis_password}@127.0.0.1:{redisport}/0',
             BANK_NAMESPACE='p01_'+run,BANK_DATABASE_URL=dsns['runtime'],BANK_AUTH_DATABASE_URL=dsns['auth'],
             BANK_WORKER_DATABASE_URL=dsns['worker'])
    (LOCAL/'runtime.env').write_text('\n'.join(k+'='+v for k,v in env.items())+'\n',encoding='utf-8')
    result=dict(run=run,containers=names,roles=roles,dsns=dsns,admin_dsn=admin,
                env=env,users=users,system_identifier=pgid)
    config.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('Isolated P01 PostgreSQL16, Redis7 and signed Keycloak OIDC realm ready.')
    print('Local configuration: .genesis/local/p01/config.json (contains synthetic credentials).')


if __name__=='__main__':
    setup()
