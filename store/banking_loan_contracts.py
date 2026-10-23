"""SPEC07: an EMI schedule is never evidence that a payment happened."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Literal
from pydantic import field_validator, model_validator
from store.banking_contracts import DTO, Money, Source, Status, canonical_uuid


class Loan(DTO):
    id: str
    display_name: str
    status: Literal['active', 'closed']
    principal: Money
    apr_percent: str
    monthly_payment: Money
    source: Source
    _uuid = field_validator('id')(canonical_uuid)

    @field_validator('apr_percent')
    @classmethod
    def decimal_apr(cls, value):
        try:
            if not re.fullmatch(r'(0|[1-9][0-9]*)(\.[0-9]{1,8})?', value): raise ValueError()
            if not Decimal('0') <= Decimal(value) <= Decimal('100'): raise ValueError()
        except (InvalidOperation, TypeError): raise ValueError('Invalid decimal APR') from None
        return value

    @model_validator(mode='after')
    def consistent(self):
        if self.principal.currency != self.monthly_payment.currency:
            raise ValueError('Mixed loan currencies')
        if int(self.principal.minor_units)<0 or int(self.monthly_payment.minor_units)<0:
            raise ValueError('Negative loan amount')
        return self


class LoanPage(DTO):
    items: list[Loan]
    next_cursor: str | None
    complete: bool
    source: Source


class EMIInstallment(DTO):
    id: str
    loan_id: str
    installment_number: int
    due_on: date
    scheduled_amount: Money
    source: Source
    _uuid = field_validator('id', 'loan_id')(canonical_uuid)

    @model_validator(mode='after')
    def valid(self):
        if self.installment_number<1 or int(self.scheduled_amount.minor_units)<0:
            raise ValueError('Invalid installment')
        return self


class LoanPayment(DTO):
    id: str
    loan_id: str
    installment_id: str | None
    amount: Money
    status: Status
    occurred_at: datetime
    reason: str | None
    source: Source
    _uuid = field_validator('id', 'loan_id')(canonical_uuid)

    @field_validator('installment_id')
    @classmethod
    def optional_id(cls, value): return None if value is None else canonical_uuid(value)

    @field_validator('occurred_at')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None: raise ValueError('Timezone required')
        return value

    @model_validator(mode='after')
    def nonnegative(self):
        if int(self.amount.minor_units)<0: raise ValueError('Negative payment')
        return self


class EMISchedulePage(DTO):
    loan_id: str
    items: list[EMIInstallment]
    next_cursor: str | None
    complete: bool
    source: Source
    start: date
    end: date


class LoanPaymentPage(DTO):
    loan_id: str
    installment_id: str | None
    items: list[LoanPayment]
    next_cursor: str | None
    complete: bool
    source: Source
    start: date
    end: date

