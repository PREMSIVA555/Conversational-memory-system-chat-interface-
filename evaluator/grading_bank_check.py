"""Export independently readable normalized facts from actual guarded reads.

Permission events derive from authenticated owned-product discovery. The
balance oracle is a separate migrator SQL read, not the returned DTO/reply.
"""
import asyncio,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.p054_probe import JourneyProbe
from store.banking_contracts import ProviderError,CURRENCY_EXPONENTS
import psycopg


async def main():
    p=await JourneyProbe().start()
    try:
        discovery=await p.step({'op':'workspace_products','kind':'account'})
        assert discovery['status']==200
        actor=p.contexts['A'].customer_id;other=p.contexts['B'].customer_id
        with psycopg.connect(p.world.admin_dsn) as c:
            rid,minor,currency=c.execute('SELECT id,available_minor,currency FROM banking.accounts WHERE customer_id=%s ORDER BY id LIMIT 1',(actor,)).fetchone()
            foreign=c.execute('SELECT id FROM banking.accounts WHERE customer_id=%s ORDER BY id LIMIT 1',(other,)).fetchone()[0]
        rid=str(rid);foreign=str(foreign)
        ids=[x['id'] for x in discovery['body']['items']];assert rid in ids and foreign not in ids
        provider=p.app.state.turn_worker.provider
        actual=(await provider.get_account(p.contexts['A'],rid)).model_dump(mode='json')
        try:
            await provider.get_account(p.contexts['A'],foreign)
            raise AssertionError('Foreign read unexpectedly allowed')
        except ProviderError as error: denied=error.status
        assert denied==404
        oracle_money=dict(minor_units=str(minor),currency=currency,exponent=CURRENCY_EXPONENTS[currency])
        case=dict(schema_version='1',id='actual-owned-balance-with-denial',actor=actor,goal='answer',owned_resources=ids,
            projection={'balance':['balance']},expected={'balance':oracle_money},
            facts=[dict(resource=rid,field='available_balance',value=oracle_money)],
            required_reads=[dict(resource=rid,field='available_balance')],source_id=None)
        observation=dict(schema_version='1',state=dict(cases=[],audit=[],balance=oracle_money),events=[
            dict(id='owned-discovery',kind='permission',resource=rid,owner=actor,allowed=True),
            dict(id='account-provider',kind='read',resource=rid,owner=actor,allowed=True,values={'available_balance':actual['available_balance']}),
            dict(id='foreign-denial',kind='permission',resource=foreign,owner=other,allowed=False),
            dict(id='foreign-provider',kind='read',resource=foreign,owner=other,allowed=False,values={}),
            dict(id='checked-display',kind='response',claims=[dict(kind='fact',resource=rid,field='available_balance',value=actual['available_balance'],read_id='account-provider')])])
        oracle=await p.step({'op':'oracle'})
        assert oracle['financial_data_unchanged'] and oracle['model_calls']==0
        print(json.dumps(dict(case=case,observation=observation,provenance=dict(
            namespace=p.world.name,semantic_hash=p.before,identity=oracle['identity'],model_calls=oracle['model_calls'],
            financial_data_unchanged=oracle['financial_data_unchanged'],discovery_http_status=discovery['status'],foreign_provider_status=denied,
            raw_discovery=discovery,raw_account=actual,response_kind='checked normalized display of actual provider facts; no LLM',
            limits='SQL oracle/fixture-issued session; no OIDC/browser or support-write coverage in this read probe')),default=str))
    finally:await p.close()


if __name__=='__main__':asyncio.run(main())
