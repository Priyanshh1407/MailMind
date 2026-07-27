import sqlite3
import os

# Ensure we are pointing to the correct DB inside the data/ folder
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'email_logs.db')

def log_email_to_db(email_id, sender, subject, body, prediction, local_prediction, human_label=None):
    """Logs an evaluated email to the SQLite database."""
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # ---------------------------------------------------------
    # YOUR TURN: 
    # 1. Write the SQL string using INSERT OR IGNORE and ? placeholders
    sql_query = "INSERT OR IGNORE INTO email_logs (email_id,sender,subject,body,prediction,local_prediction,human_label) VALUES (?,?,?,?,?,?,?)"
        
    
    # 2. Group the function arguments into a tuple
    data_tuple = (email_id, sender, subject, body, prediction, local_prediction, human_label) 
    
    # 3. Execute the query using the tuple
    cursor.execute(sql_query, data_tuple)
    
    # ---------------------------------------------------------
    
    conn.commit()
    conn.close()
    print(f"Logged email {email_id} to database.")

def get_recent_emails(limit=50):
    """Fetches recent emails from SQLite to display in the UI."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT email_id, sender, subject, body, prediction, local_prediction, human_label FROM email_logs ORDER BY created_at DESC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    
    emails = []
    for row in rows:
        emails.append({
            "id": row["email_id"],
            "sender": row["sender"],
            "subject": row["subject"],
            "body": row["body"],
            "prediction": row["prediction"],
            "local_prediction": row["local_prediction"],
            "human_label": row["human_label"]
        })
        
    conn.close()
    return emails

def clear_all_emails():
    """Wipes all emails from the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM email_logs")
    conn.commit()
    conn.close()
    print("Cleared all emails from database.")

def update_human_label(email_id, human_label):
    """Updates the human feedback label for a given email."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE email_logs SET human_label = ? WHERE email_id = ?", (human_label, email_id))
    conn.commit()
    conn.close()