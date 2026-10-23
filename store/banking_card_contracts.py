"""SPEC07 card evidence. Suffixes are labels, never resource identities."""
from datetime import date, datetime
from typing import Literal, Protocol
from pydantic import field_validator, model_validator
from store.banking_contracts import DTO, Money, Source, Status, canonical_uuid


class Card(DTO):
    id: str
    display_name: str
    last_four: str
    kind: Literal['debit', 'credit']
    status: Literal['active', 'blocked', 'expired', 'closed']
    daily_limit: Money
    credit_limit: Money | None
    available_credit: Money | None
    outstanding: Money | None
    source: Source
    _uuid = field_validator('id')(canonical_uuid)

    @model_validator(mode='after')
    def consistent(self):
        import re
        if not re.fullmatch(r'[0-9]{4}', self.last_four):
            raise ValueError('Invalid card suffix')
        credit = [self.credit_limit, self.available_credit, self.outstanding]
        if self.kind == 'debit' and any(v is not None for v in credit):
            raise ValueError('Debit card has credit fields')
        if self.kind == 'credit' and any(v is None for v in credit):
            raise ValueError('Credit evidence is incomplete')
        if any(v.currency != self.daily_limit.currency for v in credit if v is not None):
            raise ValueError('Mixed card currency')
        return self


class CardPage(DTO):
    items: list[Card]
    next_cursor: str | None
    complete: bool
    source: Source


class CardTransaction(DTO):
    id: str
    card_id: str
    amount: Money
    status: Status
    occurred_at: datetime
    reason: str | None
    source: Source
    _uuid = field_validator('id', 'card_id')(canonical_uuid)

    @field_validator('occurred_at')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError('Timezone required')
        return value


class CardTransactionPage(DTO):
    items: list[CardTransaction]
    next_cursor: str | None
    complete: bool
    source: Source
    start: date
    end: date


class CardProviderPort(Protocol):
    async def list_cards(self, context, *, last_four=None, limit=20, cursor=None) -> CardPage: ...
    async def get_card(self, context, card_id: str) -> Card: ...
    async def list_card_transactions(self, context, card_id: str, *, start=None, end=None,
                                     status=None, limit=20, cursor=None) -> CardTransactionPage: ...
