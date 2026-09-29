"""SQLite migrations for the integrated intelligence features.

This module has no provider, model, vector, or network dependencies so database
initialization remains safe and deterministic.
"""


def migration_11(conn):
    conn.execute("""CREATE TABLE email_analysis (
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        analysis_version TEXT NOT NULL CHECK(length(analysis_version) BETWEEN 1 AND 80),
        predicted_category TEXT CHECK(predicted_category IS NULL OR predicted_category IN ('IMPORTANT','UPDATES','SPAM')),
        explanation_summary TEXT NOT NULL CHECK(length(explanation_summary) BETWEEN 1 AND 240),
        signals_json TEXT NOT NULL DEFAULT '[]' CHECK(length(signals_json)<=4096 AND json_valid(signals_json) AND json_type(signals_json)='array'),
        source TEXT NOT NULL CHECK(source IN ('gemini','groq','local_heuristic','system')),
        model_version TEXT CHECK(model_version IS NULL OR length(model_version) BETWEEN 1 AND 160),
        retrieval_used INTEGER NOT NULL DEFAULT 0 CHECK(retrieval_used IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(predicted_category IS NOT NULL OR source='system'),
        PRIMARY KEY(account_id,email_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )""")
    conn.execute("""CREATE TABLE token_usage_events (
        usage_id INTEGER PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
        email_id TEXT,
        job_id INTEGER REFERENCES worker_jobs(job_id) ON DELETE SET NULL,
        request_id TEXT NOT NULL UNIQUE CHECK(length(request_id) BETWEEN 1 AND 160),
        provider TEXT NOT NULL CHECK(provider IN ('gemini','groq','local','embedding')),
        model_version TEXT NOT NULL CHECK(length(model_version) BETWEEN 1 AND 160),
        operation TEXT NOT NULL CHECK(operation IN ('classification_analysis','local_shadow','document_embedding','query_embedding','manual_prediction','action_reanalysis')),
        input_tokens INTEGER CHECK(input_tokens IS NULL OR (typeof(input_tokens)='integer' AND input_tokens>=0)),
        output_tokens INTEGER CHECK(output_tokens IS NULL OR (typeof(output_tokens)='integer' AND output_tokens>=0)),
        total_tokens INTEGER CHECK(total_tokens IS NULL OR (typeof(total_tokens)='integer' AND total_tokens>=0)),
        count_method TEXT NOT NULL CHECK(count_method IN ('provider_reported','tokenizer_counted','estimated','unavailable')),
        outcome TEXT NOT NULL CHECK(outcome IN ('success','failed','timeout','cancelled')),
        metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(length(metadata_json)<=2048 AND json_valid(metadata_json) AND json_type(metadata_json)='object'),
        created_at TEXT NOT NULL,
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX token_usage_account_time ON token_usage_events(account_id,created_at,usage_id)')
    conn.execute('CREATE INDEX token_usage_provider_time ON token_usage_events(account_id,provider,created_at)')
    conn.execute('CREATE INDEX token_usage_operation_time ON token_usage_events(account_id,operation,created_at)')
    conn.execute('CREATE INDEX token_usage_email ON token_usage_events(account_id,email_id,created_at)')


def migration_12(conn):
    conn.execute("""CREATE TABLE email_actions (
        action_id INTEGER PRIMARY KEY,
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
        action_type TEXT NOT NULL CHECK(action_type IN ('reply_required','approval_required','payment_required','document_required','meeting','review_required','follow_up_required','general_task')),
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
        description TEXT NOT NULL CHECK(length(description)<=500),
        evidence TEXT NOT NULL CHECK(length(evidence) BETWEEN 1 AND 320),
        due_at TEXT,
        due_precision TEXT NOT NULL CHECK(due_precision IN ('exact_time','date_only','relative','unknown')),
        confidence TEXT NOT NULL CHECK(confidence IN ('low','medium','high')),
        extraction_source TEXT NOT NULL CHECK(extraction_source IN ('gemini','groq','local_heuristic','system')),
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','completed','dismissed','snoozed')),
        snoozed_until TEXT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(typeof(revision)='integer' AND revision>=0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        CHECK((due_precision='unknown' AND due_at IS NULL) OR (due_precision!='unknown' AND due_at IS NOT NULL)),
        CHECK((status='snoozed' AND snoozed_until IS NOT NULL) OR (status!='snoozed' AND snoozed_until IS NULL)),
        CHECK((status='completed' AND completed_at IS NOT NULL) OR (status!='completed' AND completed_at IS NULL)),
        UNIQUE(account_id,email_id,fingerprint),
        UNIQUE(account_id,action_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )""")
    conn.execute("""CREATE TABLE action_reminders (
        reminder_id INTEGER PRIMARY KEY,
        action_id INTEGER NOT NULL,
        account_id TEXT NOT NULL,
        remind_at TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'dashboard' CHECK(channel IN ('dashboard','telegram')),
        status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','claimed','delivered','dismissed','retry','dead')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(attempt_count)='integer' AND attempt_count>=0),
        owner_token TEXT,
        next_retry_at REAL NOT NULL DEFAULT 0 CHECK(next_retry_at>=0),
        error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 80),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(account_id,action_id,channel,remind_at),
        FOREIGN KEY(account_id,action_id) REFERENCES email_actions(account_id,action_id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX email_actions_status_due ON email_actions(account_id,status,due_at,action_id)')
    conn.execute('CREATE INDEX email_actions_email ON email_actions(account_id,email_id,action_id)')
    conn.execute('CREATE INDEX action_reminders_due ON action_reminders(account_id,status,next_retry_at,remind_at,reminder_id)')
    conn.execute('CREATE INDEX action_reminders_action ON action_reminders(account_id,action_id,status)')


def migration_13(conn):
    conn.execute('''CREATE TABLE intelligence_mutation_limits (
        account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
        scope TEXT NOT NULL CHECK(scope IN ('reminder','reanalysis')),
        resource_hash TEXT NOT NULL CHECK(
            length(resource_hash)=64
            AND resource_hash NOT GLOB '*[^0-9a-f]*'),
        last_at REAL NOT NULL CHECK(last_at>=0),
        PRIMARY KEY(account_id,scope,resource_hash)
    )''')


def migration_14(conn):
    conn.execute('''CREATE TABLE intelligence_backfill_items (
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK(status IN ('queued','running','retry','complete','dead')),
        error_code TEXT CHECK(
            error_code IS NULL OR length(error_code) BETWEEN 1 AND 80),
        requested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id),
        FOREIGN KEY(account_id,email_id)
            REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )''')
    conn.execute('''CREATE INDEX intelligence_backfill_status
        ON intelligence_backfill_items(account_id,status,updated_at,email_id)''')
