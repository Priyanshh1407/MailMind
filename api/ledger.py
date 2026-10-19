"""Routes for the Money tab."""
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from src.ledger import KINDS, MAX_MERCHANT_CHARS, MAX_MINOR
from src.ledger_store import (EntryConflict, correct_entry, ledger_summary, list_entries, local_month,
                              subscriptions)

MONTH = r'^\d{4}-(0[1-9]|1[0-2])$'


class EntryCorrection(BaseModel):
    expected_revision: int = Field(ge=0)
    amount_minor: int | None = Field(default=None, ge=1, le=MAX_MINOR)
    merchant: str | None = Field(default=None, min_length=1, max_length=MAX_MERCHANT_CHARS)
    kind: Literal[KINDS] | None = None

    @model_validator(mode='after')
    def something(self):
        if self.amount_minor is None and self.merchant is None and self.kind is None:
            raise ValueError('Change the amount, merchant or kind')
        if self.merchant is not None:
            self.merchant = ' '.join(self.merchant.split())
            if not self.merchant:
                raise ValueError('Enter a merchant name')
        return self


def register(application, session):
    def enabled():
        if not application.state.settings.ledger_enabled:
            raise HTTPException(404, 'The Money tab is switched off (MAILMIND_LEDGER_ENABLED).')

    def identity(context):
        return {'account_id': context.account_id, 'generation': context.generation}

    @application.get('/ledger/summary')
    def summary(month: str | None = Query(None, pattern=MONTH), context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read', readonly=True) as conn:
            return {**identity(context), **ledger_summary(conn, context.account_id, month or local_month())}

    @application.get('/ledger/entries')
    def entries(month: str | None = Query(None, pattern=MONTH), merchant: str | None = Query(None, max_length=80),
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read', readonly=True) as conn:
            rows, total = list_entries(conn, context.account_id, month=month, merchant=merchant,
                                       limit=limit, offset=offset)
        return {**identity(context), 'entries': rows, 'total': total, 'limit': limit, 'offset': offset}

    @application.patch('/ledger/entries/{entry_id}')
    def correct(entry_id: int, payload: EntryCorrection, context=Depends(session)):
        # Your correction wins: later rescans never overwrite this row.
        enabled()
        changes = payload.model_dump(exclude={'expected_revision'}, exclude_none=True)
        with application.state.accounts.guard(context) as conn:
            try:
                entry = correct_entry(conn, context.account_id, entry_id, payload.expected_revision, changes)
            except EntryConflict:
                raise HTTPException(409, 'This entry changed since you opened it. Refresh and try again.') from None
        if entry is None:
            raise HTTPException(404, 'This entry does not belong to the connected account.')
        return {**identity(context), 'entry': entry, 'message': 'Saved. Your correction is kept.'}

    @application.get('/ledger/subscriptions')
    def recurring(context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read', readonly=True) as conn:
            return {**identity(context), **subscriptions(conn, context.account_id)}
