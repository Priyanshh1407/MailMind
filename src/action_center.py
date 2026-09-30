"""Account-scoped repositories for extracted actions and durable reminders."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from .database import connection, utc_timestamp
from .email_text import normalize_text, quote_in_source
from .intelligence_contract import (
    ACTION_STATUS_TRANSITIONS,
    MAX_ACTION_DESCRIPTION_CHARS,
    MAX_ACTION_EVIDENCE_CHARS,
    MAX_ACTION_TITLE_CHARS,
    MAX_ACTIONS_PER_EMAIL,
    MAX_AUTOMATIC_DEADLINE_DAYS,
    EXACT_TIME_REMINDER_LEAD_MINUTES,
    DATE_ONLY_REMINDER_LOCAL_HOUR,
    DEFAULT_TIMEZONE,
    ActionStatus,
    ActionType,
    AnalysisSource,
    ConfidenceBand,
    DuePrecision,
    ReminderChannel,
    ReminderStatus,
)
from .prediction import ActionCandidate, EmailAnalysis


MAX_ACTION_PAGE = 200
DUE_SOON_DAYS = 7


class ActionRevisionConflict(Exception):
    pass


class ActionTransitionError(Exception):
    pass


def _database(db_conn, db_path):
    return nullcontext(db_conn) if db_conn is not None else connection(db_path)


def _text(value, name, limit, *, required=True):
    text = normalize_text(value, limit=limit + 1)
    if len(text) > limit or (required and not text):
        raise ValueError(f"Invalid {name}")
    return text


def _timestamp(value, name, *, required=False):
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"Invalid {name}") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"Invalid {name}")
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return utc_timestamp(parsed)


def action_fingerprint(*, action_type, title, description, evidence, due_at,
                       due_precision):
    canonical = {
        "action_type": ActionType(action_type).value,
        "title": _text(title, "title", MAX_ACTION_TITLE_CHARS).casefold(),
        "description": _text(
            description, "description", MAX_ACTION_DESCRIPTION_CHARS, required=False
        ).casefold(),
        "evidence": _text(evidence, "evidence", MAX_ACTION_EVIDENCE_CHARS).casefold(),
        "due_at": _timestamp(due_at, "due_at"),
        "due_precision": DuePrecision(due_precision).value,
    }
    if (canonical["due_precision"] == DuePrecision.UNKNOWN.value) != (canonical["due_at"] is None):
        raise ValueError("Unknown precision requires no due date and resolved precision requires one")
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_action(account_id, email_id, *, action_type, title, description,
                  evidence, due_at=None, due_precision="unknown",
                  confidence="medium", extraction_source="system",
                  fingerprint=None, db_path=None, db_conn=None):
    account_id = _text(account_id, "account_id", 320)
    email_id = _text(email_id, "email_id", 256)
    action_type = ActionType(action_type).value
    title = _text(title, "title", MAX_ACTION_TITLE_CHARS)
    description = _text(
        description, "description", MAX_ACTION_DESCRIPTION_CHARS, required=False
    )
    evidence = _text(evidence, "evidence", MAX_ACTION_EVIDENCE_CHARS)
    due_precision = DuePrecision(due_precision).value
    due_at = _timestamp(due_at, "due_at")
    confidence = ConfidenceBand(confidence).value
    extraction_source = AnalysisSource(extraction_source).value
    generated = action_fingerprint(
        action_type=action_type, title=title, description=description,
        evidence=evidence, due_at=due_at, due_precision=due_precision,
    )
    if fingerprint is not None and fingerprint != generated:
        raise ValueError("Action fingerprint does not match its content")
    stamp = utc_timestamp()
    with _database(db_conn, db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM email_logs WHERE account_id=? AND email_id=?",
            (account_id, email_id),
        ).fetchone():
            raise LookupError("Email does not belong to this account")
        inserted = conn.execute("""INSERT INTO email_actions(
            account_id,email_id,fingerprint,action_type,title,description,evidence,
            due_at,due_precision,confidence,extraction_source,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id,email_id,fingerprint) DO NOTHING""", (
                account_id, email_id, generated, action_type, title, description,
                evidence, due_at, due_precision, confidence, extraction_source,
                stamp, stamp,
            )).rowcount
        row = conn.execute("""SELECT * FROM email_actions
            WHERE account_id=? AND email_id=? AND fingerprint=?""",
            (account_id, email_id, generated)).fetchone()
        result = dict(row)
        result["inserted"] = bool(inserted)
        return result


def get_action(account_id, action_id, *, db_path=None, db_conn=None):
    if type(action_id) is not int or action_id < 1:
        raise ValueError("Invalid action_id")
    with _database(db_conn, db_path) as conn:
        row = conn.execute(
            "SELECT * FROM email_actions WHERE account_id=? AND action_id=?",
            (account_id, action_id),
        ).fetchone()
        return dict(row) if row else None


def list_actions(account_id, *, status=None, due_from=None, due_to=None,
                 email_id=None, action_type=None, limit=50, offset=0, db_path=None,
                 db_conn=None):
    if type(limit) is not int or not 1 <= limit <= MAX_ACTION_PAGE:
        raise ValueError("Invalid action limit")
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("Invalid action offset")
    clauses, parameters = ["a.account_id=?"], [account_id]
    if status is not None:
        clauses.append("a.status=?")
        parameters.append(ActionStatus(status).value)
    if email_id is not None:
        clauses.append("a.email_id=?")
        parameters.append(_text(email_id, "email_id", 256))
    if action_type is not None:
        clauses.append("a.action_type=?")
        parameters.append(ActionType(action_type).value)
    start = _timestamp(due_from, "due_from")
    end = _timestamp(due_to, "due_to")
    if start is not None:
        clauses.append("a.due_at>=?")
        parameters.append(start)
    if end is not None:
        clauses.append("a.due_at<?")
        parameters.append(end)
    if start is not None and end is not None and start >= end:
        raise ValueError("due_from must precede due_to")
    if (start is not None and end is not None
            and (_datetime(end, 'due_to') - _datetime(start, 'due_from')
                 > timedelta(days=MAX_AUTOMATIC_DEADLINE_DAYS))):
        raise ValueError('Action date range cannot exceed 366 days')
    parameters.extend((limit, offset))
    with _database(db_conn, db_path) as conn:
        rows = conn.execute(f"""SELECT a.*,
                analysis.updated_at AS analysis_revision,
                (SELECT MIN(r.remind_at) FROM action_reminders r
                 WHERE r.account_id=a.account_id AND r.action_id=a.action_id
                   AND r.status IN ('scheduled','claimed','retry'))
                    AS next_reminder_at,
                (SELECT COUNT(*) FROM action_reminders r
                 WHERE r.account_id=a.account_id AND r.action_id=a.action_id
                   AND r.status='delivered') AS delivered_reminder_count
            FROM email_actions a
            LEFT JOIN email_analysis analysis
              ON analysis.account_id=a.account_id
             AND analysis.email_id=a.email_id
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE WHEN a.due_at IS NULL THEN 1 ELSE 0 END,
                     a.due_at,a.action_id
            LIMIT ? OFFSET ?""", parameters).fetchall()
        return [dict(row) for row in rows]


def update_action_status(account_id, action_id, *, status, expected_revision,
                         snoozed_until=None, now=None, db_path=None,
                         db_conn=None):
    if type(action_id) is not int or action_id < 1:
        raise ValueError("Invalid action_id")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("Invalid expected revision")
    target = ActionStatus(status).value
    current_time = now or datetime.now(timezone.utc)
    if not isinstance(current_time, datetime) or current_time.tzinfo is None:
        raise ValueError("Current time must include a timezone")
    snooze = _timestamp(snoozed_until, "snoozed_until")
    if target == ActionStatus.SNOOZED.value:
        if snooze is None or snooze <= utc_timestamp(current_time):
            raise ValueError("Snooze time must be in the future")
    elif snooze is not None:
        raise ValueError("Only snoozed actions may have snoozed_until")
    stamp = utc_timestamp(current_time)
    with _database(db_conn, db_path) as conn:
        row = conn.execute(
            "SELECT * FROM email_actions WHERE account_id=? AND action_id=?",
            (account_id, action_id),
        ).fetchone()
        if row is None:
            raise LookupError("Action does not belong to this account")
        if row["revision"] != expected_revision:
            raise ActionRevisionConflict("Action changed; refresh and retry")
        if target not in ACTION_STATUS_TRANSITIONS[row["status"]]:
            raise ActionTransitionError("Invalid action status transition")
        completed_at = stamp if target == ActionStatus.COMPLETED.value else None
        updated = conn.execute("""UPDATE email_actions SET status=?,snoozed_until=?,
            completed_at=?,revision=revision+1,updated_at=?
            WHERE account_id=? AND action_id=? AND revision=?""", (
                target, snooze, completed_at, stamp, account_id, action_id,
                expected_revision,
            ))
        if updated.rowcount != 1:
            raise ActionRevisionConflict("Action changed; refresh and retry")
        if target in (ActionStatus.COMPLETED.value, ActionStatus.DISMISSED.value,
                      ActionStatus.SNOOZED.value):
            conn.execute("""UPDATE action_reminders SET status='dismissed',
                owner_token=NULL,updated_at=? WHERE account_id=? AND action_id=?
                AND status IN ('scheduled','claimed','retry')""",
                (stamp, account_id, action_id))
        return get_action(account_id, action_id, db_conn=conn)


def schedule_reminder(account_id, action_id, *, remind_at, channel="dashboard",
                      now=None, db_path=None, db_conn=None):
    if type(action_id) is not int or action_id < 1:
        raise ValueError("Invalid action_id")
    channel = ReminderChannel(channel).value
    reminder_time = _timestamp(remind_at, "remind_at", required=True)
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("Current time must include a timezone")
    if reminder_time <= utc_timestamp(current):
        raise ValueError("Reminder time must be in the future")
    stamp = utc_timestamp(current)
    with _database(db_conn, db_path) as conn:
        action = conn.execute(
            "SELECT status FROM email_actions WHERE account_id=? AND action_id=?",
            (account_id, action_id),
        ).fetchone()
        if action is None:
            raise LookupError("Action does not belong to this account")
        if action["status"] != ActionStatus.OPEN.value:
            raise ActionTransitionError("Only open actions can receive reminders")
        inserted = conn.execute("""INSERT INTO action_reminders(
            action_id,account_id,remind_at,channel,created_at,updated_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,action_id,channel,remind_at)
            DO NOTHING""", (
                action_id, account_id, reminder_time, channel, stamp, stamp,
            )).rowcount
        row = conn.execute("""SELECT * FROM action_reminders WHERE account_id=?
            AND action_id=? AND channel=? AND remind_at=?""",
            (account_id, action_id, channel, reminder_time)).fetchone()
        result = dict(row)
        result["inserted"] = bool(inserted)
        return result


def list_reminders(account_id, *, action_id=None, status=None, limit=100,
                   db_path=None, db_conn=None):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid reminder limit")
    clauses, parameters = ["account_id=?"], [account_id]
    if action_id is not None:
        if type(action_id) is not int or action_id < 1:
            raise ValueError("Invalid action_id")
        clauses.append("action_id=?")
        parameters.append(action_id)
    if status is not None:
        from .intelligence_contract import ReminderStatus
        clauses.append("status=?")
        parameters.append(ReminderStatus(status).value)
    parameters.append(limit)
    with _database(db_conn, db_path) as conn:
        rows = conn.execute(f"""SELECT * FROM action_reminders
            WHERE {' AND '.join(clauses)} ORDER BY remind_at,reminder_id LIMIT ?""",
            parameters).fetchall()
        return [dict(row) for row in rows]



def _datetime(value, name):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"Invalid {name}") from error
    else:
        raise ValueError(f"Invalid {name}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _utc(value):
    return utc_timestamp(value.astimezone(timezone.utc))


def resolve_action_candidate(candidate, *, source_created_at,
                             timezone_name=DEFAULT_TIMEZONE):
    """Revalidate and canonicalize one provider candidate against its email."""
    if not isinstance(candidate, ActionCandidate):
        raise ValueError("Invalid action candidate")
    zone = ZoneInfo(timezone_name)
    source = _datetime(source_created_at, "source email timestamp")
    precision = DuePrecision(candidate.due_precision).value
    due = None
    if candidate.due_at is not None:
        due = _datetime(candidate.due_at, "action deadline")
        local_due = due.astimezone(zone)
        if precision == DuePrecision.DATE_ONLY.value or (
                precision == DuePrecision.RELATIVE.value
                and local_due.hour == 0 and local_due.minute == 0
                and local_due.second == 0):
            due = local_due.replace(
                hour=DATE_ONLY_REMINDER_LOCAL_HOUR,
                minute=0, second=0, microsecond=0,
            )
        if due > source + timedelta(days=MAX_AUTOMATIC_DEADLINE_DAYS):
            due = None
            precision = DuePrecision.UNKNOWN.value
    elif precision != DuePrecision.UNKNOWN.value:
        raise ValueError("Known deadline precision requires due_at")
    return {
        "action_type": ActionType(candidate.action_type).value,
        "title": _text(candidate.title, "title", MAX_ACTION_TITLE_CHARS),
        "description": _text(
            candidate.description, "description",
            MAX_ACTION_DESCRIPTION_CHARS, required=False,
        ),
        "evidence": _text(
            candidate.evidence, "evidence", MAX_ACTION_EVIDENCE_CHARS),
        "due_at": _utc(due) if due is not None else None,
        "due_precision": precision,
        "confidence": ConfidenceBand(candidate.confidence).value,
    }


def automatic_reminder_time(action, *, source_created_at, now=None,
                            timezone_name=DEFAULT_TIMEZONE):
    """Return a safe default reminder time or None when automation is forbidden."""
    if action.get("confidence") != ConfidenceBand.HIGH.value:
        return None
    precision = DuePrecision(action.get("due_precision")).value
    due_value = action.get("due_at")
    if precision == DuePrecision.UNKNOWN.value or due_value is None:
        return None
    source = _datetime(source_created_at, "source email timestamp")
    due = _datetime(due_value, "action deadline")
    current = now or datetime.now(timezone.utc)
    current = _datetime(current, "current time")
    if due <= source or due > source + timedelta(days=MAX_AUTOMATIC_DEADLINE_DAYS):
        return None
    if precision == DuePrecision.DATE_ONLY.value:
        reminder = due
    elif precision == DuePrecision.RELATIVE.value:
        local_due = due.astimezone(ZoneInfo(timezone_name))
        reminder = (due if (
            local_due.hour == DATE_ONLY_REMINDER_LOCAL_HOUR
            and local_due.minute == 0 and local_due.second == 0
        ) else due - timedelta(minutes=EXACT_TIME_REMINDER_LEAD_MINUTES))
    else:
        reminder = due - timedelta(minutes=EXACT_TIME_REMINDER_LEAD_MINUTES)
    if reminder <= current:
        if precision in (DuePrecision.EXACT_TIME.value,
                         DuePrecision.RELATIVE.value) and due > current:
            reminder = due
        else:
            return None
    return _utc(reminder)


def persist_analysis_actions(account_id, email_id, analysis, *,
                             source_created_at, source_text,
                             timezone_name=DEFAULT_TIMEZONE,
                             reminders_enabled=False, now=None,
                             db_path=None, db_conn=None):
    """Persist validated medium/high candidates and safe dashboard reminders."""
    if not isinstance(analysis, EmailAnalysis):
        raise ValueError("Invalid email analysis")
    if len(analysis.actions) > MAX_ACTIONS_PER_EMAIL:
        raise ValueError("Too many action candidates")
    result = {
        "candidate_count": len(analysis.actions),
        "accepted_count": 0,
        "created_count": 0,
        "reminder_count": 0,
        "rejected_low_confidence": 0,
        "rejected_invalid": 0,
    }
    with _database(db_conn, db_path) as conn:
        for candidate in analysis.actions:
            if candidate.confidence == ConfidenceBand.LOW.value:
                result["rejected_low_confidence"] += 1
                continue
            try:
                values = resolve_action_candidate(
                    candidate, source_created_at=source_created_at,
                    timezone_name=timezone_name,
                )
                if not quote_in_source(values["evidence"], source_text):
                    raise ValueError("Action evidence is not present in the source email")
                action = create_action(
                    account_id, email_id, db_conn=conn,
                    extraction_source=analysis.source, **values,
                )
            except (TypeError, ValueError):
                result["rejected_invalid"] += 1
                continue
            result["accepted_count"] += 1
            result["created_count"] += int(action["inserted"])
            if reminders_enabled and action["status"] == ActionStatus.OPEN.value:
                reminder_at = automatic_reminder_time(
                    action, source_created_at=source_created_at,
                    now=now, timezone_name=timezone_name,
                )
                if reminder_at is not None:
                    reminder = schedule_reminder(
                        account_id, action["action_id"],
                        remind_at=reminder_at,
                        channel=ReminderChannel.DASHBOARD.value,
                        now=now, db_conn=conn,
                    )
                    result["reminder_count"] += int(reminder["inserted"])
    return result


def action_summary(account_id, *, now=None, db_path=None, db_conn=None):
    current_time = now or datetime.now(timezone.utc)
    current = _utc(current_time)
    due_soon_end = _utc(
        _datetime(current_time, 'current time') + timedelta(days=DUE_SOON_DAYS)
    )
    with _database(db_conn, db_path) as conn:
        statuses = {
            row["status"]: row["count"]
            for row in conn.execute(
                """SELECT status,COUNT(*) AS count FROM email_actions
                   WHERE account_id=? GROUP BY status""", (account_id,))
        }
        reminders = {
            row["status"]: row["count"]
            for row in conn.execute(
                """SELECT status,COUNT(*) AS count FROM action_reminders
                   WHERE account_id=? GROUP BY status""", (account_id,))
        }
        # Only types the account actually has, so filters never offer empty options.
        type_counts = {
            row["action_type"]: row["count"]
            for row in conn.execute(
                """SELECT action_type,COUNT(*) AS count FROM email_actions
                   WHERE account_id=? GROUP BY action_type
                   ORDER BY count DESC, action_type""", (account_id,))
        }
        total = sum(statuses.values())
        overdue = conn.execute(
            """SELECT COUNT(*) FROM email_actions
               WHERE account_id=? AND status='open' AND due_at IS NOT NULL
               AND due_at<?""", (account_id, current)
        ).fetchone()[0]
        due_soon = conn.execute(
            '''SELECT COUNT(*) FROM email_actions
               WHERE account_id=? AND status='open' AND due_at>=?
               AND due_at<?''', (account_id, current, due_soon_end)
        ).fetchone()[0]
    return {
        'due_soon': due_soon,
        "total": total,
        "status_counts": {
            status.value: statuses.get(status.value, 0)
            for status in ActionStatus
        },
        "overdue": overdue,
        "type_counts": type_counts,
        "reminder_counts": {
            status.value: reminders.get(status.value, 0)
            for status in ReminderStatus
        },
    }


def expire_snoozed_actions(account_id, *, now=None, db_path=None, db_conn=None):
    stamp = _utc(now or datetime.now(timezone.utc))
    with _database(db_conn, db_path) as conn:
        return conn.execute(
            """UPDATE email_actions SET status='open',snoozed_until=NULL,
               revision=revision+1,updated_at=?
               WHERE account_id=? AND status='snoozed' AND snoozed_until<=?""",
            (stamp, account_id, stamp),
        ).rowcount
