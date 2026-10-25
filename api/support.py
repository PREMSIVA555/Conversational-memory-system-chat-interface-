"""Human preview routes, protected by existing server session and CSRF boundaries."""
from fastapi import Depends, HTTPException, Query
from store.banking_support import Support, Draft, Edit, PreviewBinding
from store.banking_confirmation import Confirmation, ConfirmationService, ExistingCase
from fastapi.responses import JSONResponse
from store.banking_continuity import Continuity


def install(app, database, principal, mutation, *, enabled=False, confirmation_enabled=False, continuity_enabled=False):
    service = Support(database)
    app.state.support = service
    base = '/api/v1/support/proposals'
    continuity=Continuity(database)

    def available():
        if not enabled: raise HTTPException(503, 'Support previews are unavailable')

    @app.get(base+'/capabilities')
    async def capabilities(context=Depends(principal)):
        available()
        return {'confirmation_enabled':confirmation_enabled,'continuity_enabled':continuity_enabled}

    def continuity_available():
        available()
        if not continuity_enabled:raise HTTPException(503,'Support continuity unavailable')

    @app.get('/api/v1/support/cases')
    async def cases(cursor:str|None=None,limit:int=Query(20,ge=1,le=20),context=Depends(principal)):
        continuity_available()
        return await continuity.cases(context,cursor,limit)

    @app.get('/api/v1/support/cases/{case_id}')
    async def case(case_id:str,context=Depends(principal)):
        continuity_available()
        return await continuity.case(context,case_id)

    @app.get(base+'/{proposal_id}/outcome')
    async def outcome(proposal_id:str,context=Depends(principal)):
        continuity_available()
        return await continuity.outcome(context,proposal_id)

    @app.post(base+'/{proposal_id}/reconcile')
    async def reconcile(proposal_id:str,body:Confirmation,context=Depends(mutation)):
        continuity_available()
        return await continuity.outcome(context,proposal_id,body)

    @app.post(base, status_code=201)
    async def create(body: Draft, context=Depends(mutation)):
        available()
        return await service.create(context, body)

    @app.get(base+'/by-turn/{turn_id}')
    async def latest(turn_id: str, context=Depends(principal)):
        available()
        return await service.latest(context, turn_id)

    @app.get(base+'/{proposal_id}')
    async def preview(proposal_id: str, context=Depends(principal)):
        available()
        return await service.get(context, proposal_id)

    @app.post(base+'/{proposal_id}/edit', status_code=201)
    async def edit(proposal_id: str, body: Edit, context=Depends(mutation)):
        available()
        return await service.create(context, body.draft, proposal_id=proposal_id, expected=body)

    @app.post(base+'/{proposal_id}/cancel')
    async def cancel(proposal_id: str, body: PreviewBinding, context=Depends(mutation)):
        available()
        return await service.validate(context, proposal_id, body, cancel=True)

    @app.post(base+'/{proposal_id}/confirm')
    async def confirm(proposal_id: str, body: Confirmation | PreviewBinding, context=Depends(mutation)):
        available()
        if confirmation_enabled:
            if not isinstance(body,Confirmation): raise HTTPException(422,'A stable operation key is required')
            try:return await (continuity.submit(context,proposal_id,body) if continuity_enabled else ConfirmationService(database).confirm(context,proposal_id,body))
            except ExistingCase as exc:return JSONResponse({'detail':exc.detail},status_code=409)
        await service.validate(context, proposal_id, body)
        # T05.2 will replace this fail-closed boundary with an atomic transaction.
        raise HTTPException(503, 'Case submission is not available yet')
