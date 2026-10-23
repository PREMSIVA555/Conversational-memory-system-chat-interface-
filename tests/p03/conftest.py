import json
from pathlib import Path
from uuid import uuid4
import psycopg
import pytest
import pytest_asyncio
from auth.sessions import Sessions
from store.banking import Database
from store.banking_migrate import apply
from store.banking_provider import AccountProvider
from tests.p01.conftest import protected, settings

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture
def config():
    value=protected(json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8')))
    with psycopg.connect(value['dsns']['migrator']) as c: apply(c,value['roles'],through='0017')
    return value

@pytest.fixture
def world(config):
    owners=[str(uuid4()),str(uuid4())]; accounts=[str(uuid4()) for _ in range(4)]
    with psycopg.connect(config['admin_dsn']) as c:
        for owner in owners: c.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(owner,))
        for i,(account,owner,currency,current,available) in enumerate(zip(accounts,[owners[0]]*3+[owners[1]],['INR','JPY','KWD','USD'],[9007199254740993,0,-9223372036854775808,1],[7,-1,9223372036854775807,2])):
            c.execute('INSERT INTO banking.accounts(id,customer_id,display_name,currency,current_minor,available_minor) VALUES(%s,%s,%s,%s,%s,%s)',(account,owner,'Synthetic '+str(i),currency,current,available))
        for i,status in enumerate(['declined','pending','posted','reversed']*2):
            c.execute('INSERT INTO banking.account_transactions(account_id,customer_id,amount_minor,status,booked_on,reason) VALUES(%s,%s,%s,%s,%s,NULL)',
                (accounts[0],owners[0],[-7,0,9007199254740993,-9223372036854775808,9223372036854775807,1,-1,3][i],status,'2026-10-'+('01' if i<4 else '19')))
    return dict(owners=owners,accounts=accounts)

@pytest_asyncio.fixture
async def service(settings,world):
    sessions=Sessions(settings); database=Database(settings,sessions)
    await database.open()
    tokens=[await sessions.issue(owner) for owner in world['owners']]
    contexts=[await sessions.require(token) for token in tokens]
    from datetime import datetime,timezone
    provider=AccountProvider(database,b'synthetic-provider-key-32bytes-long!',clock=lambda:datetime(2026,10,19,tzinfo=timezone.utc))
    yield provider,contexts,sessions,database
    for context in contexts: await sessions.revoke(context)
    await database.close(); await sessions.close()
