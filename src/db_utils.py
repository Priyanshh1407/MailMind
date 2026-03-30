import sqlite3
import os

# Ensure we are pointing to the correct DB inside the data/ folder
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'email_logs.db')

def log_email_to_db(email_id, sender, subject, body, prediction, human_label=None):
    """Logs an evaluated email to the SQLite database."""
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # ---------------------------------------------------------
    # YOUR TURN: 
    # 1. Write the SQL string using INSERT OR IGNORE and ? placeholders
    sql_query = "INSERT OR IGNORE INTO email_logs (email_id,sender,subject,body,prediction,human_label) VALUES (?,?,?,?,?,?)"
        
    
    # 2. Group the function arguments into a tuple
    data_tuple = (email_id, sender, subject, body, prediction, human_label) 
    
    # 3. Execute the query using the tuple
    cursor.execute(sql_query, data_tuple)
    
    # ---------------------------------------------------------
    
    conn.commit()
    conn.close()
    print(f"Logged email {email_id} to database.")