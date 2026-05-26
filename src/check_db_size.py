import sqlite3
import os

# Safely point to our database using absolute pathing
current_dir = os.path.dirname(__file__)
DB_PATH = os.path.abspath(os.path.join(current_dir, '..', 'data', 'email_logs.db'))

def check_dataset_size():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Query 1: Total emails fetched ever
    cursor.execute("SELECT COUNT(*) FROM email_logs")
    total_emails = cursor.fetchone()[0]

    # Query 2: Emails with a human label (Ready for Phase 3)
    cursor.execute("SELECT COUNT(*) FROM email_logs WHERE human_label IS NOT NULL")
    reviewed_emails = cursor.fetchone()[0]

    # Query 3: Breakdown of Important vs Ignore
    cursor.execute("SELECT human_label, COUNT(*) FROM email_logs WHERE human_label IS NOT NULL GROUP BY human_label")
    breakdown = cursor.fetchall()

    print("\n" + "=" * 40)
    print("📊 DATASET HEALTH CHECK")
    print("=" * 40)
    print(f"Total Raw Emails Fetched: {total_emails}")
    print(f"Human-Reviewed Emails (Ground Truth): {reviewed_emails}")
    print("-" * 40)
    
    if breakdown:
        for label, count in breakdown:
            print(f" - {label}: {count}")
    print("=" * 40 + "\n")

    conn.close()

if __name__ == "__main__":
    check_dataset_size()