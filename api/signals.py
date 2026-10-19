"""Routes for the email security signals backfill."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from src.email_signals import MAX_SIGNALS_BACKFILL, queue_signals_backfill, signals_summary
from src.work_queue import enqueue_cycle


class SignalsBackfillRequest(BaseModel):
    limit: int = Field(default=MAX_SIGNALS_BACKFILL, ge=1, le=MAX_SIGNALS_BACKFILL)


def register(application, session):
    @application.post('/signals/backfill', status_code=202)
    def signals_backfill(payload: SignalsBackfillRequest | None = None, context=Depends(session)):
        # Re-reads saved messages from Gmail for their headers and links only.
        # Nothing is reclassified, alerted, labelled or marked read.
        if application.state.settings.local_only:
            raise HTTPException(403, 'Reading Gmail is disabled in local-only mode.')
        limit = (payload or SignalsBackfillRequest()).limit
        with application.state.accounts.guard(context) as conn:
            admitted = queue_signals_backfill(conn, context.account_id, limit)
            summary = signals_summary(conn, context.account_id)
        if not admitted:
            raise HTTPException(409, 'Every saved email already has its security signals.')
        job_id = enqueue_cycle(application.state.accounts, context)
        return {'job_id': job_id, 'status': 'queued', 'admitted': admitted, 'eligible': summary['eligible'],
                'message': f"Capturing security signals for {admitted} saved email{'s' if admitted != 1 else ''}. "
                           'This only re-reads headers and links; nothing else changes.'}
