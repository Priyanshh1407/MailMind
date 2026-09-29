"""Versioned SQLite initialization, transactional migration, and closed connections."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3

from .config import CATEGORIES, LEGACY_ACCOUNT, Settings
from .intelligence_schema import (
    migration_11, migration_12, migration_13, migration_14,
)

SCHEMA_VERSION = 14


def utc_timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("Timestamp must specify a timezone")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_legacy_timestamp(value, manual_timezone=timezone(timedelta(hours=5, minutes=30))):
    if not value:
        return "1970-01-01T00:00:00.000000Z"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return utc_timestamp(parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed)
    except ValueError:
        return utc_timestamp(datetime.strptime(str(value), "%d-%m-%Y %H:%M").replace(tzinfo=manual_timezone))


@contextmanager
def connection(db_path=None, busy_timeout_ms=5000):
    path = Path(db_path or Settings.from_environment().db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        with conn:
            yield conn
    finally:
        conn.close()


def _migration_1(conn, manual_timezone=timezone(timedelta(hours=5, minutes=30))):
    old = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_logs'").fetchone()
    if old:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(email_logs)")}
        if "email_id" not in columns:
            raise ValueError("Unsupported legacy email schema")
        # Preserve every original column/value; never assign unknown ownership.
        conn.execute("ALTER TABLE email_logs RENAME TO email_logs_legacy_v0")
    conn.execute("CREATE TABLE accounts (account_id TEXT PRIMARY KEY NOT NULL CHECK(length(account_id)>0))")
    conn.execute("INSERT INTO accounts VALUES (?)", (LEGACY_ACCOUNT,))
    conn.execute("""CREATE TABLE email_logs (
        account_id TEXT NOT NULL DEFAULT 'legacy-unassigned' REFERENCES accounts(account_id),
        email_id TEXT NOT NULL CHECK(length(email_id)>0),
        sender TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
        prediction TEXT CHECK(prediction IS NULL OR prediction IN ('IMPORTANT','UPDATES','SPAM')),
        local_prediction TEXT CHECK(local_prediction IS NULL OR local_prediction IN ('IMPORTANT','UPDATES','SPAM')),
        human_label TEXT CHECK(human_label IS NULL OR human_label IN ('IMPORTANT','UPDATES','SPAM')),
        message_type TEXT NOT NULL DEFAULT 'gmail' CHECK(message_type IN ('gmail','manual','synthetic')),
        processing_state TEXT NOT NULL DEFAULT 'pending' CHECK(processing_state IN ('pending','processing','classified','error','abstained','complete')),
        created_at TEXT NOT NULL CHECK(length(created_at)=27 AND substr(created_at,27,1)='Z'),
        PRIMARY KEY(account_id,email_id)
    )""")
    if old:
        for row in conn.execute("SELECT * FROM email_logs_legacy_v0").fetchall():
            raw = dict(row)
            canonical = lambda value: value if value in CATEGORIES else None
            pred, local = raw.get("prediction"), raw.get("local_prediction")
            state = "error" if pred not in (*CATEGORIES, None, "MANUAL_ENTRY") else "classified"
            conn.execute("""INSERT INTO email_logs
                (account_id,email_id,sender,subject,body,prediction,local_prediction,human_label,message_type,processing_state,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                LEGACY_ACCOUNT, raw["email_id"], raw.get("sender") or "", raw.get("subject") or "", raw.get("body") or "",
                canonical(pred), canonical(local), canonical(raw.get("human_label")), "manual" if pred == "MANUAL_ENTRY" else "gmail",
                state, normalize_legacy_timestamp(raw.get("created_at"), manual_timezone),
            ))


def _migration_2(conn):
    for statement in (
        """CREATE TABLE prediction_attempts (
            attempt_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, email_id TEXT NOT NULL,
            category TEXT CHECK(category IS NULL OR category IN ('IMPORTANT','UPDATES','SPAM')),
            local_category TEXT CHECK(local_category IS NULL OR local_category IN ('IMPORTANT','UPDATES','SPAM')),
            outcome TEXT NOT NULL CHECK(outcome IN ('CLASSIFIED','ABSTAIN','UNAVAILABLE','ERROR')),
            source TEXT NOT NULL, model_version TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE)""",
        """CREATE TABLE feedback_history (
            revision_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, email_id TEXT NOT NULL,
            label TEXT NOT NULL CHECK(label IN ('IMPORTANT','UPDATES','SPAM')),
            indexing_state TEXT NOT NULL DEFAULT 'pending' CHECK(indexing_state IN ('pending','indexed','failed')),
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE)""",
        """CREATE TABLE notification_attempts (
            notification_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, email_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','sent','failed','unknown')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE)""",
        """CREATE TABLE worker_jobs (
            job_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
            status TEXT NOT NULL CHECK(status IN ('queued','running','complete','failed','cancelled')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE INDEX email_logs_recent ON email_logs(created_at DESC,account_id,email_id)",
        "CREATE INDEX email_logs_account_recent ON email_logs(account_id,created_at DESC,email_id)",
        "CREATE INDEX feedback_pending ON feedback_history(indexing_state,revision_id)",
        "CREATE INDEX notification_status ON notification_attempts(status,notification_id)",
    ):
        conn.execute(statement)
    conn.execute("""INSERT INTO prediction_attempts(account_id,email_id,category,local_category,outcome,source,created_at)
        SELECT account_id,email_id,prediction,local_prediction,CASE WHEN processing_state='error' THEN 'ERROR' WHEN prediction IS NULL THEN 'ABSTAIN' ELSE 'CLASSIFIED' END,'legacy-import',created_at FROM email_logs""")
    conn.execute("""INSERT INTO feedback_history(account_id,email_id,label,created_at)
        SELECT account_id,email_id,human_label,created_at FROM email_logs WHERE human_label IS NOT NULL""")


def _migration_3(conn):
    conn.execute("""CREATE TABLE runtime_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_id TEXT,
        generation INTEGER NOT NULL DEFAULT 0, connected INTEGER NOT NULL DEFAULT 0,
        auth_in_progress INTEGER NOT NULL DEFAULT 0, purge_pending INTEGER NOT NULL DEFAULT 0,
        is_polling INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("INSERT INTO runtime_state(singleton) VALUES (1)")
    conn.execute("""CREATE TABLE local_sessions (
        token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at REAL NOT NULL,
        generation INTEGER NOT NULL)""")


def _migration_4(conn):
    conn.execute('ALTER TABLE email_logs ADD COLUMN body_truncated INTEGER NOT NULL DEFAULT 0 CHECK(body_truncated IN (0,1))')
    conn.execute("ALTER TABLE email_logs ADD COLUMN body_kind TEXT CHECK(body_kind IS NULL OR body_kind IN ('plain','html'))")
    conn.execute("ALTER TABLE email_logs ADD COLUMN parse_warnings TEXT NOT NULL DEFAULT '[]'")
    conn.execute("""CREATE TABLE ingestion_state (
        account_id TEXT PRIMARY KEY NOT NULL REFERENCES accounts(account_id), page_token TEXT,
        status TEXT NOT NULL CHECK(status IN ('empty','success','partial','error','deferred')),
        fetched_count INTEGER NOT NULL, failed_count INTEGER NOT NULL, pages_count INTEGER NOT NULL,
        scanned_count INTEGER NOT NULL, warning_count INTEGER NOT NULL, truncated_count INTEGER NOT NULL,
        has_more INTEGER NOT NULL, listing_error INTEGER NOT NULL, last_checked_at TEXT NOT NULL)""")


def _migration_5(conn):
    conn.execute("ALTER TABLE prediction_attempts ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
    conn.execute('ALTER TABLE feedback_history ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0)')
    conn.execute('CREATE INDEX feedback_latest ON feedback_history(account_id,email_id,revision_id DESC)')
    # Old index entries have no revision binding. Rebuild only current revisions.
    conn.execute("""UPDATE feedback_history SET indexing_state='pending'
        WHERE revision_id IN (SELECT MAX(revision_id) FROM feedback_history GROUP BY account_id,email_id)""")


def _migration_6(conn):
    for column in ('worker_token TEXT', 'lease_until REAL NOT NULL DEFAULT 0',
                   'heartbeat_at TEXT', 'last_success_at TEXT', 'last_error_at TEXT', 'last_error_code TEXT'):
        conn.execute('ALTER TABLE runtime_state ADD COLUMN ' + column)
    for column in ("kind TEXT NOT NULL DEFAULT 'processing'", 'generation INTEGER NOT NULL DEFAULT 0',
                   'owner_token TEXT', 'error_code TEXT'):
        conn.execute('ALTER TABLE worker_jobs ADD COLUMN ' + column)
    conn.execute("""CREATE TABLE processing_tasks (
        account_id TEXT NOT NULL, email_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','retry','complete','dead')),
        stage TEXT NOT NULL DEFAULT 'classify' CHECK(stage IN ('classify','notify','mark_read','complete')),
        category TEXT CHECK(category IS NULL OR category IN ('IMPORTANT','UPDATES','SPAM')),
        attempt_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
        owner_token TEXT, error_code TEXT, read_required INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE)""")
    conn.execute("""CREATE TABLE processing_attempts (
        attempt_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, email_id TEXT NOT NULL,
        stage TEXT NOT NULL, outcome TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL,
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE)""")
    conn.execute("""CREATE TABLE notification_outbox (
        account_id TEXT NOT NULL, email_id TEXT NOT NULL, action TEXT NOT NULL DEFAULT 'priority_alert',
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','sending','sent','retry','blocked','unknown','dead')),
        attempt_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
        provider_message_id TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id,action),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE)""")
    conn.execute("""CREATE TABLE ingestion_failures (
        account_id TEXT NOT NULL REFERENCES accounts(account_id), email_id TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL CHECK(status IN ('retry','dead')),
        next_retry_at REAL NOT NULL DEFAULT 0, error_code TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id))""")
    conn.execute("""CREATE TABLE auth_jobs (
        job_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, session_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','complete','failed','cancelled')),
        processing_job_id INTEGER REFERENCES worker_jobs(job_id) ON DELETE SET NULL,
        error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE worker_health (
        account_id TEXT PRIMARY KEY REFERENCES accounts(account_id), heartbeat_at TEXT,
        last_success_at TEXT, last_error_at TEXT, last_error_code TEXT)""")
    conn.execute('CREATE INDEX processing_due ON processing_tasks(account_id,status,next_retry_at,created_at)')
    conn.execute('CREATE INDEX outbox_due ON notification_outbox(account_id,status,next_retry_at)')
    # Preserve old records; old delivery/read completion cannot be inferred.


def _migration_7(conn):
    # A null label is an audited withdrawal, not deletion of previous feedback.
    conn.execute("""CREATE TABLE feedback_history_v7 (
        revision_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, email_id TEXT NOT NULL,
        label TEXT CHECK(label IS NULL OR label IN ('IMPORTANT','UPDATES','SPAM')),
        indexing_state TEXT NOT NULL DEFAULT 'pending' CHECK(indexing_state IN ('pending','indexed','failed')),
        created_at TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE)""")
    conn.execute('INSERT INTO feedback_history_v7 SELECT revision_id,account_id,email_id,label,indexing_state,created_at,attempt_count FROM feedback_history')
    conn.execute('DROP TABLE feedback_history')
    conn.execute('ALTER TABLE feedback_history_v7 RENAME TO feedback_history')
    conn.execute('CREATE INDEX feedback_pending ON feedback_history(indexing_state,revision_id)')
    conn.execute('CREATE INDEX feedback_latest ON feedback_history(account_id,email_id,revision_id DESC)')


def _migration_8(conn):
    # Existing installations keep their backlog paused. New accounts explicitly
    # initialize a first 100-message batch when their ingestion row is created.
    for column in (
        'history_id TEXT',
        'backlog_authorized INTEGER NOT NULL DEFAULT 0 CHECK(backlog_authorized IN (0,1))',
        'backlog_remaining INTEGER NOT NULL DEFAULT 0 CHECK(backlog_remaining>=0)',
        'initial_batch_complete INTEGER NOT NULL DEFAULT 1 CHECK(initial_batch_complete IN (0,1))',
        "live_status TEXT NOT NULL DEFAULT 'never' CHECK(live_status IN ('never','empty','success','partial','error','deferred'))",
        'live_fetched_count INTEGER NOT NULL DEFAULT 0 CHECK(live_fetched_count>=0)',
        'last_live_sync_at TEXT',
        'last_live_error TEXT',
        'last_manual_sync_at REAL NOT NULL DEFAULT 0',
    ):
        conn.execute('ALTER TABLE ingestion_state ADD COLUMN ' + column)
    conn.execute("ALTER TABLE processing_tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'backlog' CHECK(source IN ('live','backlog'))")
    conn.execute('CREATE INDEX processing_priority ON processing_tasks(account_id,status,source,next_retry_at,created_at)')


def _migration_9(conn):
    conn.execute('''CREATE TABLE email_search_index (
        account_id TEXT NOT NULL, email_id TEXT NOT NULL,
        indexing_state TEXT NOT NULL DEFAULT 'pending' CHECK(indexing_state IN ('pending','indexed','failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0), updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE)''')
    conn.execute('''INSERT INTO email_search_index(account_id,email_id,updated_at)
        SELECT account_id,email_id,created_at FROM email_logs''')
    conn.execute('''CREATE TRIGGER email_search_queue_after_insert
        AFTER INSERT ON email_logs BEGIN
            INSERT INTO email_search_index(account_id,email_id,updated_at)
            VALUES (NEW.account_id,NEW.email_id,NEW.created_at)
            ON CONFLICT(account_id,email_id) DO UPDATE SET
                indexing_state='pending',attempt_count=0,updated_at=excluded.updated_at;
        END''')
    conn.execute('CREATE INDEX email_search_pending ON email_search_index(account_id,indexing_state,updated_at)')


def _migration_10(conn):
    # Version 8 originally seeded a current Gmail history cursor before doing a
    # newest-page catch-up. Existing installations need one safe catch-up cycle
    # so mail already waiting at that boundary is not permanently skipped.
    conn.execute('ALTER TABLE ingestion_state ADD COLUMN history_bootstrap_complete '
                 'INTEGER NOT NULL DEFAULT 0 CHECK(history_bootstrap_complete IN (0,1))')


def initialize_database(db_path=None, *, manual_timezone=timezone(timedelta(hours=5, minutes=30))):
    with connection(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("Database schema is newer than this application")
        expected = set()
        for introduced, names in ((1, {'accounts','email_logs'}),
                                  (2, {'prediction_attempts','feedback_history','notification_attempts','worker_jobs'}),
                                  (3, {'runtime_state','local_sessions'}), (4, {'ingestion_state'}),
                                  (6, {'processing_tasks','processing_attempts','notification_outbox','ingestion_failures','auth_jobs','worker_health'}),
                                  (9, {'email_search_index'}),
                                  (11, {'email_analysis','token_usage_events'}),
                                  (12, {'email_actions','action_reminders'}),
                                  (13, {'intelligence_mutation_limits'}),
                                  (14, {'intelligence_backfill_items'})):
            if version >= introduced:
                expected.update(names)
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not expected.issubset(existing):
            raise ValueError('Database schema is incomplete')
        for number, migration in ((1, _migration_1), (2, _migration_2), (3, _migration_3), (4, _migration_4), (5, _migration_5), (6, _migration_6), (7, _migration_7), (8, _migration_8), (9, _migration_9), (10, _migration_10), (11, migration_11), (12, migration_12), (13, migration_13), (14, migration_14)):
            if version < number:
                if number == 1:
                    migration(conn, manual_timezone)
                else:
                    migration(conn)
                conn.execute(f"PRAGMA user_version={number}")
        required = {'accounts','email_logs','prediction_attempts','feedback_history','notification_attempts','worker_jobs','runtime_state','local_sessions','ingestion_state','email_search_index','email_analysis','token_usage_events','email_actions','action_reminders','intelligence_mutation_limits','intelligence_backfill_items'}
        actual = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(actual):
            raise ValueError("Database schema is incomplete")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Database foreign-key validation failed")
