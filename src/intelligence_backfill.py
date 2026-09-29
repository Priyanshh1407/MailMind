"""Bounded, explicit admission of saved mail to the existing worker queue."""
from contextlib import nullcontext

from .database import connection, utc_timestamp


MAX_BACKFILL_BATCH = 100
BACKFILL_STATUSES = ('queued', 'running', 'retry', 'complete', 'dead')

_ELIGIBLE_FROM = """
    FROM email_logs e
    LEFT JOIN email_analysis analysis
      ON analysis.account_id=e.account_id AND analysis.email_id=e.email_id
    LEFT JOIN processing_tasks task
      ON task.account_id=e.account_id AND task.email_id=e.email_id
    LEFT JOIN intelligence_backfill_items backfill
      ON backfill.account_id=e.account_id AND backfill.email_id=e.email_id
    WHERE e.account_id=?
      AND backfill.email_id IS NULL
      AND (task.email_id IS NULL OR task.status='complete')
      AND (
        analysis.email_id IS NULL
        OR (
          NOT EXISTS (
            SELECT 1 FROM processing_attempts attempt
            WHERE attempt.account_id=e.account_id
              AND attempt.email_id=e.email_id
              AND attempt.stage='actions'
              AND attempt.outcome='complete'
          )
          AND NOT EXISTS (
            SELECT 1 FROM email_actions action
            WHERE action.account_id=e.account_id
              AND action.email_id=e.email_id
          )
        )
      )
"""


def _database(db_conn, db_path):
    return nullcontext(db_conn) if db_conn is not None else connection(db_path)


def _account(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 320:
        raise ValueError('Invalid account_id')
    return value


def backfill_summary(account_id, *, db_path=None, db_conn=None):
    account_id = _account(account_id)
    with _database(db_conn, db_path) as conn:
        eligible = conn.execute(
            'SELECT COUNT(*) ' + _ELIGIBLE_FROM, (account_id,)
        ).fetchone()[0]
        counts = {
            row['status']: row['count'] for row in conn.execute(
                """SELECT status,COUNT(*) AS count
                   FROM intelligence_backfill_items
                   WHERE account_id=? GROUP BY status""",
                (account_id,),
            )
        }
    return {
        'eligible': eligible,
        **{key: counts.get(key, 0) for key in BACKFILL_STATUSES},
    }


def queue_intelligence_backfill(account_id, *, limit=20,
                                max_pending_tasks=100,
                                db_path=None, db_conn=None):
    account_id = _account(account_id)
    if type(limit) is not int or not 1 <= limit <= MAX_BACKFILL_BATCH:
        raise ValueError('Invalid backfill limit')
    if (type(max_pending_tasks) is not int
            or not 1 <= max_pending_tasks <= 1000):
        raise ValueError('Invalid pending-task limit')
    with _database(db_conn, db_path) as conn:
        active = conn.execute(
            """SELECT COUNT(*) FROM processing_tasks
               WHERE account_id=?
                 AND status IN ('queued','retry','running')""",
            (account_id,),
        ).fetchone()[0]
        capacity = max(0, max_pending_tasks - active)
        requested = min(limit, capacity)
        eligible = conn.execute(
            'SELECT COUNT(*) ' + _ELIGIBLE_FROM, (account_id,)
        ).fetchone()[0]
        if requested == 0 or eligible == 0:
            return {
                'requested': limit,
                'admitted': 0,
                'eligible': eligible,
                'capacity': capacity,
            }
        rows = conn.execute(
            """SELECT e.email_id """ + _ELIGIBLE_FROM + """
               ORDER BY e.created_at DESC,e.email_id
               LIMIT ?""",
            (account_id, requested),
        ).fetchall()
        stamp = utc_timestamp()
        for row in rows:
            email_id = row['email_id']
            conn.execute(
                """INSERT INTO intelligence_backfill_items(
                       account_id,email_id,status,requested_at,updated_at)
                   VALUES (?,?,'queued',?,?)""",
                (account_id, email_id, stamp, stamp),
            )
            conn.execute(
                """INSERT INTO processing_tasks(
                       account_id,email_id,status,stage,attempt_count,
                       next_retry_at,read_required,created_at,updated_at,source)
                   VALUES (?,?,'queued','classify',0,0,0,?,?,'backlog')
                   ON CONFLICT(account_id,email_id) DO UPDATE SET
                       status='queued',stage='classify',category=NULL,
                       attempt_count=0,next_retry_at=0,owner_token=NULL,
                       error_code=NULL,read_required=0,updated_at=excluded.updated_at,
                       source='backlog'""",
                (account_id, email_id, stamp, stamp),
            )
            conn.execute(
                """UPDATE email_logs SET processing_state='pending'
                   WHERE account_id=? AND email_id=?""",
                (account_id, email_id),
            )
        admitted = len(rows)
        return {
            'requested': limit,
            'admitted': admitted,
            'eligible': eligible,
            'capacity': capacity,
        }
