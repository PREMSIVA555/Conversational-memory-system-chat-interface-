import json
from pathlib import Path
from uuid import uuid4
import psycopg
import pytest
from tests.p01.conftest import protected
from store.banking_migrate import apply

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(scope='session')
def config():
    value=protected(json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8')))
    with psycopg.connect(value['dsns']['migrator']) as c:
        apply(c,value['roles'],through='0003')
    return value

@pytest.fixture
def customer(config):
    customer=uuid4()
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(customer,))
    return customer
