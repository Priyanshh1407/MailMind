"""Routes for scam and phishing reports."""
from fastapi import Depends, HTTPException

from src.db_utils import get_email
from src.email_risk import get_risk, mark_reviewed, score_email


def register(application, session):
    def enabled():
        if not application.state.settings.risk_analysis_enabled:
            raise HTTPException(404, 'Scam checks are switched off (MAILMIND_RISK_ANALYSIS_ENABLED).')

    @application.get('/emails/{email_id}/risk')
    def email_risk(email_id: str, context=Depends(session)):
        enabled()
        with application.state.accounts.guard(context, connected='read') as conn:
            if not get_email(email_id, account_id=context.account_id, db_conn=conn):
                raise HTTPException(404, 'This email does not belong to the connected account.')
            report = get_risk(conn, context.account_id, email_id)
        return {'account_id': context.account_id, 'generation': context.generation,
                'email_id': email_id, 'risk': report}

    @application.post('/emails/{email_id}/risk/review')
    def review_risk(email_id: str, context=Depends(session)):
        # "I checked this one": the report stays, it just stops counting as
        # an unreviewed high-risk email.
        enabled()
        with application.state.accounts.guard(context) as conn:
            if not get_email(email_id, account_id=context.account_id, db_conn=conn):
                raise HTTPException(404, 'This email does not belong to the connected account.')
            if get_risk(conn, context.account_id, email_id) is None:
                score_email(conn, context.account_id, email_id)
            reviewed = mark_reviewed(conn, context.account_id, email_id)
        if reviewed is None:
            raise HTTPException(409, 'This scam check was already marked as reviewed.')
        return {'email_id': email_id, 'reviewed_at': reviewed, 'message': 'Marked as reviewed.'}
