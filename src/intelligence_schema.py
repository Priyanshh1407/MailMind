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


def migration_15(conn):
    # Security evidence per email (scam forensics, red-team). Derived from the
    # Gmail message and recaptured on demand, so it cascades with the email.
    conn.execute("""CREATE TABLE email_signals (
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        signals_version INTEGER NOT NULL CHECK(typeof(signals_version)='integer' AND signals_version>=1),
        thread_id TEXT CHECK(thread_id IS NULL OR length(thread_id) BETWEEN 1 AND 64),
        from_address TEXT NOT NULL DEFAULT '' CHECK(length(from_address)<=320),
        from_domain TEXT NOT NULL DEFAULT '' CHECK(length(from_domain)<=255),
        reply_to_domain TEXT NOT NULL DEFAULT '' CHECK(length(reply_to_domain)<=255),
        return_path_domain TEXT NOT NULL DEFAULT '' CHECK(length(return_path_domain)<=255),
        auth_spf TEXT NOT NULL CHECK(auth_spf IN ('pass','fail','softfail','neutral','none','temperror','permerror','policy','unknown')),
        auth_dkim TEXT NOT NULL CHECK(auth_dkim IN ('pass','fail','softfail','neutral','none','temperror','permerror','policy','unknown')),
        auth_dmarc TEXT NOT NULL CHECK(auth_dmarc IN ('pass','fail','softfail','neutral','none','temperror','permerror','policy','unknown')),
        list_unsubscribe TEXT NOT NULL DEFAULT '' CHECK(length(list_unsubscribe)<=2048),
        list_unsubscribe_post INTEGER NOT NULL DEFAULT 0 CHECK(list_unsubscribe_post IN (0,1)),
        links_json TEXT NOT NULL DEFAULT '[]' CHECK(length(links_json)<=131072 AND json_valid(links_json) AND json_type(links_json)='array'),
        hidden_text TEXT NOT NULL DEFAULT '' CHECK(length(hidden_text)<=1000),
        captured_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX email_signals_thread ON email_signals(account_id,thread_id)')
    conn.execute('CREATE INDEX email_signals_from_domain ON email_signals(account_id,from_domain)')
    conn.execute("""CREATE TABLE signals_backfill_items (
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','retry','complete','dead')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(attempt_count)='integer' AND attempt_count>=0),
        next_retry_at REAL NOT NULL DEFAULT 0 CHECK(next_retry_at>=0),
        error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 80),
        requested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX signals_backfill_due ON signals_backfill_items(account_id,status,next_retry_at,requested_at)')


def migration_16(conn):
    # Scam/phishing report per email. Deterministic and recomputable: a new
    # risk version or newly captured signals simply replace the row.
    conn.execute("""CREATE TABLE email_risk (
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        risk_version TEXT NOT NULL CHECK(length(risk_version) BETWEEN 1 AND 40),
        score INTEGER NOT NULL CHECK(typeof(score)='integer' AND score BETWEEN 0 AND 100),
        band TEXT NOT NULL CHECK(band IN ('low','medium','high')),
        evidence_level TEXT NOT NULL CHECK(evidence_level IN ('full','content_only')),
        findings_json TEXT NOT NULL DEFAULT '[]' CHECK(length(findings_json)<=16384 AND json_valid(findings_json) AND json_type(findings_json)='array'),
        reviewed_at TEXT,
        computed_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX email_risk_band ON email_risk(account_id,band,reviewed_at)')


def rebuild_check(conn, table, old, new):
    """Change one CHECK clause of a table, keeping its rows and indexes.

    SQLite cannot alter a constraint, so the table is recreated from its own
    stored definition with `old` replaced by `new`, filled, and swapped in.
    """
    import re
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    if new in sql:
        return  # already rebuilt (for example a rewound test database)
    if sql.count(old) != 1:
        raise ValueError(f'Unexpected {table} definition')
    indexes = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
    created = re.sub(rf'^CREATE TABLE\s+"?{table}"?', f'CREATE TABLE {table}_rebuild', sql, count=1)
    conn.execute(created.replace(old, new))
    conn.execute(f'INSERT INTO {table}_rebuild SELECT * FROM {table}')
    conn.execute(f'DROP TABLE {table}')
    conn.execute(f'ALTER TABLE {table}_rebuild RENAME TO {table}')
    for index in indexes:
        conn.execute(index)


def migration_17(conn):
    # The assistant features (ledger, Ask, follow-ups, daily brief) meter their
    # provider calls and rate-limit their user actions like earlier features.
    rebuild_check(
        conn, 'token_usage_events',
        "operation IN ('classification_analysis','local_shadow','document_embedding','query_embedding','manual_prediction','action_reanalysis')",
        "operation IN ('classification_analysis','local_shadow','document_embedding','query_embedding','manual_prediction','action_reanalysis','ledger_extraction','inbox_question','follow_up_draft','daily_brief')")
    rebuild_check(
        conn, 'intelligence_mutation_limits',
        "scope IN ('reminder','reanalysis')",
        "scope IN ('reminder','reanalysis','ledger_correction','question','follow_up','brief')")
    conn.execute("""CREATE TABLE ledger_entries (
        entry_id INTEGER PRIMARY KEY,
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('purchase','subscription','refund','bill','debit')),
        amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor BETWEEN 1 AND 10000000000),
        currency TEXT NOT NULL CHECK(currency IN ('INR','USD','EUR','GBP')),
        merchant TEXT NOT NULL CHECK(length(merchant) BETWEEN 1 AND 80),
        occurred_at TEXT NOT NULL,
        renews_on TEXT CHECK(renews_on IS NULL OR (length(renews_on)=10 AND date(renews_on) IS NOT NULL)),
        period TEXT CHECK(period IS NULL OR period IN ('weekly','monthly','quarterly','yearly')),
        evidence TEXT NOT NULL DEFAULT '' CHECK(length(evidence)<=200),
        source TEXT NOT NULL CHECK(source IN ('local','gemini','groq')),
        extraction_version TEXT NOT NULL CHECK(length(extraction_version) BETWEEN 1 AND 40),
        user_corrected INTEGER NOT NULL DEFAULT 0 CHECK(user_corrected IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(typeof(revision)='integer' AND revision>=0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(account_id,email_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX ledger_entries_time ON ledger_entries(account_id,occurred_at,entry_id)')
    conn.execute('CREATE INDEX ledger_entries_merchant ON ledger_entries(account_id,merchant)')
    conn.execute("""CREATE TABLE ledger_scans (
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('done','not_transaction','retry','failed')),
        extraction_version TEXT NOT NULL CHECK(length(extraction_version) BETWEEN 1 AND 40),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(attempt_count)='integer' AND attempt_count>=0),
        next_retry_at REAL NOT NULL DEFAULT 0 CHECK(next_retry_at>=0),
        error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 80),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id,email_id),
        FOREIGN KEY(account_id,email_id) REFERENCES email_logs(account_id,email_id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX ledger_scans_due ON ledger_scans(account_id,status,next_retry_at)')

