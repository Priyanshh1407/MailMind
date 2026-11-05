"""Is the AI out of capacity right now? Worked out from what the worker records.

The provider pauses live in the worker's memory; the dashboard talks to the
API. Instead of sharing that state, this reads the worker's own records: the
emails waiting to be classified again because of a provider failure, and the
latest classification attempts. It reports a limit only when the newest
attempts all failed that way, so one brief failure never shows an alert.
"""
from datetime import datetime, timezone

# Failure codes that mean "the provider cannot take more work now", by cause.
QUOTA_CODES = ('provider_quota',)
BUSY_CODES = ('provider_transient', 'provider_timeout', 'provider_connect_timeout', 'provider_connection',
              'provider_budget_exhausted')
CONSECUTIVE_FAILURES = 3
RECENT_SECONDS = 15 * 60


def _epoch(stamp):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()


def ai_status(conn, account_id, *, now=None):
    """{'state': 'ok' | 'limited', 'reason': 'quota' | 'busy' | None,
    'waiting': emails waiting, 'retry_in_seconds': until the next try}."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    ok = {'state': 'ok', 'reason': None, 'waiting': 0, 'retry_in_seconds': None}
    if not account_id:
        return ok
    codes = QUOTA_CODES + BUSY_CODES
    marks = ','.join('?' * len(codes))
    waiting = {row[0]: (row[1], row[2]) for row in conn.execute(
        f"""SELECT error_code, COUNT(*), MIN(next_retry_at) FROM processing_tasks
            WHERE account_id=? AND status='retry' AND stage='classify' AND error_code IN ({marks})
            GROUP BY error_code""", (account_id, *codes))}
    if not waiting:
        return ok
    recent = conn.execute(
        """SELECT outcome, error_code, created_at FROM processing_attempts
           WHERE account_id=? AND stage='classify' AND outcome IN ('CLASSIFIED','ERROR')
           ORDER BY attempt_id DESC LIMIT ?""", (account_id, CONSECUTIVE_FAILURES)).fetchall()
    still_failing = (len(recent) == CONSECUTIVE_FAILURES
                     and all(row[0] == 'ERROR' and row[1] in codes for row in recent)
                     and now - _epoch(recent[0][2]) <= RECENT_SECONDS)
    if not still_failing:
        return {**ok, 'waiting': sum(count for count, _next in waiting.values())}
    quota = sum(waiting.get(code, (0, None))[0] for code in QUOTA_CODES)
    busy = sum(waiting.get(code, (0, None))[0] for code in BUSY_CODES)
    next_try = min(next_at for _count, next_at in waiting.values() if next_at is not None)
    return {'state': 'limited', 'reason': 'quota' if quota >= busy else 'busy', 'waiting': quota + busy,
            'retry_in_seconds': max(0, round(next_try - now))}
