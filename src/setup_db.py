import sqlite3
import os

# 1. Start at the script's location (src/)
current_dir = os.path.dirname(__file__)

# 2. Go UP one level ('..'), then into 'data', then to the file
# 3. Use abspath to resolve it into a clean, full system path
DB_PATH = os.path.abspath(os.path.join(current_dir, '..', 'data', 'email_logs.db'))

def create_database():
    if os.path.exists(DB_PATH):
        print(f"Database already exists at: {DB_PATH}. Skipping initialization.")
        return

    print(f"Targeting database at: {DB_PATH}")
    
    # SAFEGUARD: Create the 'data' directory if it somehow doesn't exist
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    # 1. Open the connection (this creates the valid binary file)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 2. Write our SQL schema
    schema = """
    CREATE TABLE IF NOT EXISTS email_logs (
        email_id TEXT PRIMARY KEY,
        sender TEXT,
        subject TEXT,
        body TEXT,
        prediction TEXT,
        local_prediction TEXT,
        human_label TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """
    
    # 3. Execute and commit
    print("Creating table 'email_logs'...")
    cursor.execute(schema)
    conn.commit()
    conn.close()
    print("Database setup complete! A valid binary .db file has been created.")

if __name__ == "__main__":
    create_database()