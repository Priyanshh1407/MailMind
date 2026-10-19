"""Saved scam/phishing reports: score on arrival, catch up in bounded sweeps.

The rules are pure (src/risk_analysis.py); this module only reads the email,
its security signals and the classifier's explanation signals, and stores the
report. Scoring is local and cheap, so the worker also re-scores reports made
with older rules or before the email's signals were captured.
"""
import json

from .database import utc_timestamp
from .email_signals import get_email_signals
from .risk_analysis import assess, trusted_domains, weights

SWEEP_LIMIT = 50


def _analysis_signals(conn, account_id, email_id):
    row = conn.execute('SELECT signals_json FROM email_analysis WHERE account_id=? AND email_id=?',
                       (account_id, email_id)).fetchone()
    try:
        return [item.get('signal') for item in json.loads(row['signals_json']) if isinstance(item, dict)] if row else []
    except (TypeError, ValueError):
        return []


def score_email(conn, account_id, email_id, *, trusted=None, analysis_signals=None):
    email = conn.execute('SELECT sender,subject,body FROM email_logs WHERE account_id=? AND email_id=?',
                         (account_id, email_id)).fetchone()
    if email is None:
        return None
    report = assess(
        sender=email['sender'], subject=email['subject'], body=email['body'],
        signals=get_email_signals(conn, account_id, email_id),
        trusted=trusted_domains(conn, account_id) if trusted is None else trusted,
        analysis_signals=_analysis_signals(conn, account_id, email_id) if analysis_signals is None else analysis_signals)
    conn.execute(
        """INSERT INTO email_risk(account_id,email_id,risk_version,score,band,evidence_level,findings_json,computed_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(account_id,email_id) DO UPDATE SET
               risk_version=excluded.risk_version,score=excluded.score,band=excluded.band,
               evidence_level=excluded.evidence_level,findings_json=excluded.findings_json,
               computed_at=excluded.computed_at""",
        (account_id, email_id, report['risk_version'], report['score'], report['band'], report['evidence_level'],
         json.dumps(report['findings'], ensure_ascii=False), utc_timestamp()))
    return report


def score_pending(conn, account_id, limit=SWEEP_LIMIT):
    """Score unscored mail, mail scored by older rules, and mail whose signals
    arrived after it was scored. Newest first; returns how many were scored."""
    rows = conn.execute(
        """SELECT e.email_id FROM email_logs e
           LEFT JOIN email_risk r ON r.account_id=e.account_id AND r.email_id=e.email_id
           WHERE e.account_id=? AND (
               r.email_id IS NULL OR r.risk_version!=?
               OR (r.evidence_level='content_only' AND EXISTS (
                   SELECT 1 FROM email_signals s WHERE s.account_id=e.account_id AND s.email_id=e.email_id)))
           ORDER BY e.created_at DESC, e.email_id LIMIT ?""",
        (account_id, weights().get('version', 'risk-v1'), limit)).fetchall()
    if not rows:
        return 0
    trusted = trusted_domains(conn, account_id)
    for row in rows:
        score_email(conn, account_id, row['email_id'], trusted=trusted)
    return len(rows)


def risk_rows(conn, account_id, email_ids):
    """Reports for a page of emails, keyed by email ID."""
    if not email_ids:
        return {}
    marks = ','.join('?' * len(email_ids))
    return {row['email_id']: public_report(row) for row in conn.execute(
        f'SELECT * FROM email_risk WHERE account_id=? AND email_id IN ({marks})', (account_id, *email_ids))}


def public_report(row):
    return {'score': row['score'], 'band': row['band'], 'evidence_level': row['evidence_level'],
            'risk_version': row['risk_version'], 'findings': json.loads(row['findings_json']),
            'reviewed_at': row['reviewed_at'], 'computed_at': row['computed_at']}


def get_risk(conn, account_id, email_id):
    row = conn.execute('SELECT * FROM email_risk WHERE account_id=? AND email_id=?',
                       (account_id, email_id)).fetchone()
    return public_report(row) if row else None


def risk_summary(conn, account_id):
    counts = {row['band']: row['count'] for row in conn.execute(
        'SELECT band,COUNT(*) AS count FROM email_risk WHERE account_id=? GROUP BY band', (account_id,))}
    unreviewed = conn.execute(
        "SELECT COUNT(*) FROM email_risk WHERE account_id=? AND band='high' AND reviewed_at IS NULL",
        (account_id,)).fetchone()[0]
    scored = sum(counts.values())
    total = conn.execute('SELECT COUNT(*) FROM email_logs WHERE account_id=?', (account_id,)).fetchone()[0]
    return {'high': counts.get('high', 0), 'medium': counts.get('medium', 0), 'low': counts.get('low', 0),
            'high_unreviewed': unreviewed, 'unscored': max(0, total - scored)}


def mark_reviewed(conn, account_id, email_id):
    stamp = utc_timestamp()
    changed = conn.execute(
        'UPDATE email_risk SET reviewed_at=? WHERE account_id=? AND email_id=? AND reviewed_at IS NULL',
        (stamp, account_id, email_id)).rowcount
    return stamp if changed else None
