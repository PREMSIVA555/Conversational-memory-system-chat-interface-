"""Human preview routes, protected by existing server session and CSRF boundaries."""
from fastapi import Depends, HTTPException
from store.banking_support import Support, Draft, Edit, PreviewBinding


def install(app, database, principal, mutation, *, enabled=False):
    service = Support(database)
    app.state.support = service
    base = '/api/v1/support/proposals'

    def available():
        if not enabled: raise HTTPException(503, 'Support previews are unavailable')

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
    async def confirm(proposal_id: str, body: PreviewBinding, context=Depends(mutation)):
        available()
        await service.validate(context, proposal_id, body)
        # T05.2 will replace this fail-closed boundary with an atomic transaction.
        raise HTTPException(503, 'Case submission is not available yet')
