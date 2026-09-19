"""Insert deliberate manual examples: python -m scripts.add_manual_email."""
from uuid import uuid4
from src.config import LEGACY_ACCOUNT, Settings
from src.database import initialize_database, connection, utc_timestamp


def insert_new_email(sender, subject, body, human_label, *, account_id=LEGACY_ACCOUNT, db_path=None):
    path = db_path or Settings.from_environment().db_path
    initialize_database(path)
    email_id, timestamp = "manual-" + uuid4().hex, utc_timestamp()
    with connection(path) as conn:
        conn.execute("INSERT INTO accounts VALUES (?) ON CONFLICT DO NOTHING", (account_id,))
        conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,human_label,message_type,created_at)
            VALUES (?,?,?,?,?,?,'manual',?)""", (account_id,email_id,sender,subject,body,human_label,timestamp))
        conn.execute("INSERT INTO feedback_history(account_id,email_id,label,created_at) VALUES (?,?,?,?)",
                     (account_id,email_id,human_label,timestamp))
    return email_id


if __name__ == "__main__":
    raise SystemExit("Import insert_new_email with explicit content; placeholder records are not inserted automatically.")
