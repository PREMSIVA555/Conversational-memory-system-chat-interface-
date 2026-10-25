"""Lifespan-owned durable sweep; request disconnects never own execution tasks."""
import asyncio
from datetime import datetime,timezone
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from graphs.account_coordinator import AccountCoordinator,SAFE

class SourceScopedProvider:
    """Erasure is checked again after planning, immediately before every bank read."""
    def __init__(self,database,provider,source_id):self.database=database;self.provider=provider;self.source_id=source_id
    @property
    def cards_enabled(self):return self.provider.cards_enabled
    @property
    def loans_enabled(self):return self.provider.loans_enabled
    @property
    def policies_enabled(self):return self.provider.policies_enabled
    def __getattr__(self,name):
        if name not in ('list_accounts','get_account','list_transactions','list_cards','get_card','list_card_transactions','list_loans','get_loan','list_emi_schedule','list_loan_payments','explain_policy','list_support_cases','get_support_case'):raise AttributeError(name)
        async def read(context,**kwargs):
            async with self.database.session(context,worker=True) as c:
                source=await (await c.execute('SELECT id FROM banking.accepted_user_events s WHERE id=%s AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=s.id)',(self.source_id,))).fetchone()
            if not source:raise HTTPException(404)
            return await getattr(self.provider,name)(context,**kwargs)
        return read

class TurnWorker:
    def __init__(self,database,provider,transport=None,*,run_seconds=10):
        self.database=database;self.provider=provider;self.transport=transport
        self.coordinator=AccountCoordinator(database,provider,transport,run_seconds=run_seconds)
        self.paused=False
        self.stopping=asyncio.Event()
    async def finish(self,row,result):
        async with self.database.worker.connection() as c:
            async with c.transaction():
                await c.execute("SELECT set_config('bank.customer_id',%s,true)",(str(row['customer_id']),))
                method='finish_workspace_turn' if row['request'].get('workspace') else 'finish_turn'
                return (await (await c.execute(f'SELECT banking.{method}(%s,%s,%s) AS ok',(row['id'],row['generation'],Jsonb(result)))).fetchone())['ok']
    async def one(self):
        async with self.database.worker.connection() as c:
            row=await (await c.execute('SELECT * FROM banking.claim_turn(%s)',(self.database.sessions.settings.namespace,))).fetchone()
        if not row or row['id'] is None:return False
        heartbeat=asyncio.create_task(self.heartbeat(row))
        try:return await self.execute(row)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat,return_exceptions=True)
    async def heartbeat(self,row):
        while not self.stopping.is_set():
            await asyncio.sleep(.5)
            async with self.database.worker.connection() as c:
                renewed=(await (await c.execute('SELECT banking.renew_turn(%s,%s) AS ok',(row['id'],row['generation']))).fetchone())['ok']
            if not renewed:return
    async def execute(self,row):
        if row['attempts']>3:
            # Retry exhaustion is safe control state, even when Redis is unavailable.
            await self.finish(row,dict(status='unavailable',text=SAFE['unavailable'],evidence=[]))
            return True
        result=None
        try:
            context=await self.database.sessions.require_key(row['session_ref'])
            if context.customer_id!=str(row['customer_id']):raise HTTPException(401)
            async with self.database.session(context,worker=True) as c:
                source=await (await c.execute('SELECT payload FROM banking.accepted_user_events s WHERE id=%s AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=s.id)',(row['source_id'],))).fetchone()
            if not source:return True
            coordinator_request=source['payload']['text']
            if row['request'].get('selection'):coordinator_request+='\nSelected account: '+row['request']['selection']['account_id']
            if row['attempts']>3:
                result=dict(status='unavailable',text=SAFE['unavailable'],evidence=[])
            else:
                selection=row['request'].get('selection')
                if selection:
                    value=await self.provider.get_account(context,selection['account_id'])
                    if value.source.version!=selection['version']:
                        result=dict(status='unavailable',text='The selected account changed. Please select it again.',evidence=[])
                if result is None and row['request'].get('workspace'):
                    from graphs.workspace import WorkspaceCoordinator
                    request={**row['request'],'text':source['payload']['text']}
                    result=await WorkspaceCoordinator(self.database,SourceScopedProvider(self.database,self.provider,row['source_id']),self.transport,run_seconds=self.coordinator.run_seconds).run(context,row,request)
                elif result is None:
                    prefs=row['request']['preferences'];instructions=row['request']['instructions']
                    coordinator=AccountCoordinator(self.database,SourceScopedProvider(self.database,self.provider,row['source_id']),self.transport,run_seconds=self.coordinator.run_seconds)
                    result=await coordinator.run(context,str(row['run_id']),coordinator_request,
                        language=instructions.get('language',prefs['preferred_response_language']),
                        response_format=instructions.get('format',prefs['preferred_response_format']))
                    if result['status'] in ('expired','superseded') and not result.get('evidence'):
                        result=dict(status='expired',text=SAFE['expired'],evidence=[])
                await self.database.sessions.revalidate(context)
        except HTTPException as error:
            if error.status_code!=401:raise
            result=dict(status='session_expired',text='The accepting session expired. Start a new enquiry after signing in.',evidence=[])
        except asyncio.CancelledError:raise
        except Exception:
            # Leave durable claimed lease for bounded reclaim after outages/restart.
            return True
        await self.finish(row,result)
        return True
    async def serve(self):
        while not self.stopping.is_set():
            try:
                if not self.paused:
                    if await self.one():continue
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(.1)
