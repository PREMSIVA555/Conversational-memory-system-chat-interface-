import json
from pathlib import Path
import pytest
import pytest_asyncio
from auth.sessions import Sessions
from store.banking import Database
from store.banking_provider import AccountProvider
from evals.card_world import CardWorld

@pytest.fixture(scope='module')
def card_world():
    config=json.loads(Path('.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    return CardWorld.create(config)

@pytest_asyncio.fixture
async def service(card_world):
    card_world.reset()
    sessions=Sessions(card_world.settings);db=Database(card_world.settings,sessions);await db.open()
    tokens=[await sessions.issue(owner) for owner in card_world.fixture['customers']]
    contexts=[await sessions.require(token) for token in tokens]
    from datetime import datetime,timezone
    provider=AccountProvider(db,b'p04-synthetic-cursor-key-32-bytes!',clock=lambda:datetime(2026,1,15,tzinfo=timezone.utc),cards_enabled=True)
    try:yield provider,contexts,db,card_world
    finally:
        for context in contexts:await sessions.revoke(context)
        await db.close();await sessions.close()
