import sqlite3
import os

# Safely point to our database using absolute pathing
current_dir = os.path.dirname(__file__)
DB_PATH = os.path.abspath(os.path.join(current_dir, '..', 'data', 'email_logs.db'))

def review_emails():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # THE SELECT QUERY: Using IS NULL
    select_query = """
        SELECT * FROM email_logs 
        WHERE human_label IS NULL
    """
    
    cursor.execute(select_query)
    unreviewed_emails = cursor.fetchall()

    if not unreviewed_emails:
        print("All caught up! No emails left to review.")
        return

    print(f"Found {len(unreviewed_emails)} emails to review.\n")

    for email in unreviewed_emails:
        # Unpacking the tuple returned by SQLite
        email_id, sender, subject, body, prediction, _, _ = email

        print("\n" + "=" * 50)
        print(f"From: {sender}")
        print(f"Subject: {subject}")
        print(f"AI Guessed: {prediction}")
        print("-" * 50)
        
        # Terminal UI
        choice = input("Is this Important? (y = Yes, n = No, q = Quit): ").strip().lower()

        if choice == 'q':
            print("Saving progress and exiting...")
            break
        elif choice == 'y':
            human_label = "IMPORTANT"
        elif choice == 'n':
            human_label = "IGNORE"
        else:
            print("Invalid input, skipping this email.")
            continue

        # THE UPDATE QUERY: Naming the table and using ? placeholders
        update_query = """
            UPDATE email_logs 
            SET human_label = ? 
            WHERE email_id = ?
        """
        
        # Executing the update with our safe parameterized tuple
        cursor.execute(update_query, (human_label, email_id))
        conn.commit()
        print("-> Label saved!")

    conn.close()
    print("\nReview session complete.")

if __name__ == "__main__":
    review_emails()