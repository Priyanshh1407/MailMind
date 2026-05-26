import sqlite3
import uuid
from datetime import datetime

def insert_new_email(sender, subject, body, human_label):
    # 1. Connect to your database
    conn = sqlite3.connect('data/email_logs.db')
    cursor = conn.cursor()
    
    # 2. Generate required metadata
    email_id = str(uuid.uuid4().hex)[:16] # Creates a fake 16-char ID
    created_at = datetime.now().strftime("%d-%m-%Y %H:%M")
    
    # 3. Insert the new row safely
    try:
        cursor.execute('''
            INSERT INTO email_logs (email_id, sender, subject, body, prediction, human_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (email_id, sender, subject, body, "MANUAL_ENTRY", human_label, created_at))
        
        conn.commit()
        print(f"Success! Added 1 new '{human_label}' email to the database.")
    except Exception as e:
        print(f"Database error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    # TODO: Read the screenshot you uploaded and fill these in!
    new_sender = "Sender Name <sender@example.com>"
    new_subject = "Type the subject here"
    new_body = """Type the exact body of the email here. 
    You can use multiple lines because of the triple quotes."""
    
    # Set this to "IGNORE" or "IMPORTANT"
    new_label = "IMPORTANT" 
    
    # Execute the injection
    insert_new_email(new_sender, new_subject, new_body, new_label)