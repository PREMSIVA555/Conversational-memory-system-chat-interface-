"""Bounded account enquiry DTOs. JSON money never passes through a float."""
from datetime import date, datetime
import re
from typing import Literal, Protocol
from uuid import UUID
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from auth.sessions import RequestContext

CURRENCY_EXPONENTS = {'INR': 2, 'USD': 2, 'EUR': 2, 'GBP': 2, 'JPY': 0, 'KWD': 3}
Status = Literal['declined', 'pending', 'posted', 'reversed']

def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Canonical UUID required')
    return value

class DTO(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

class Money(DTO):
    minor_units: str
    currency: str
    exponent: int

    @model_validator(mode='after')
    def exact(self):
        if not re.fullmatch(r'0|-?[1-9][0-9]*', self.minor_units) or not -(2**63) <= int(self.minor_units) < 2**63:
            raise ValueError('Invalid minor units')
        if self.currency not in CURRENCY_EXPONENTS or self.exponent != CURRENCY_EXPONENTS[self.currency]:
            raise ValueError('Unsupported currency metadata')
        return self

    def formatted(self):
        value=int(self.minor_units); digits=str(abs(value)).zfill(self.exponent+1)
        number=digits if self.exponent==0 else digits[:-self.exponent]+'.'+digits[-self.exponent:]
        return self.currency+' '+('-' if value<0 else '')+number

class Source(DTO):
    as_of: datetime
    version: str

    @field_validator('as_of')
    @classmethod
    def aware(cls,value):
        if value.tzinfo is None: raise ValueError('Timezone required')
        return value

class Account(DTO):
    id: str
    display_name: str
    current_balance: Money
    available_balance: Money
    source: Source
    _uuid=field_validator('id')(canonical_uuid)

class Transaction(DTO):
    id: str
    account_id: str
    amount: Money
    status: Status
    booked_on: date
    reason: str | None
    source: Source
    _uuid=field_validator('id','account_id')(canonical_uuid)

class AccountPage(DTO):
    items: list[Account]
    next_cursor: str | None
    complete: bool
    source: Source

class TransactionPage(DTO):
    items: list[Transaction]
    next_cursor: str | None
    complete: bool
    source: Source
    start: date
    end: date

class ProviderError(Exception):
    def __init__(self, code, status, retryable=False):
        self.code,self.status,self.retryable=code,status,retryable
        super().__init__(code)

class AccountProviderPort(Protocol):
    async def list_accounts(self, context: RequestContext, *, limit: int=20, cursor: str | None=None) -> AccountPage: ...
    async def get_account(self, context: RequestContext, account_id: str) -> Account: ...
    async def list_transactions(self, context: RequestContext, account_id: str, *, start: str | None=None,
                                end: str | None=None, status: Status | None=None, limit: int=20,
                                cursor: str | None=None) -> TransactionPage: ...
