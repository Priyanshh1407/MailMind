import re
import sqlite3
import pandas as pd

def mask_financial_pii(text):
    # 1. PAN Card (Indian Tax ID)
    pan_pattern = r'[A-Z]{5}[0-9]{4}[A-Z]{1}'
    
    # 2. UPI IDs / VPA (e.g., user@okicici)
    upi_pattern = r'[\w\.\-_]+@[\w\-_]+'
    
    # 3. Folio / Account Numbers (Generic 8-16 digits)
    # Hint: Use \b to denote word boundaries so it doesn't catch years (2026)
    acct_pattern = r'\b\d{8,16}\b'

    currency_pattern = r'([\$|₹|Rs\.?]\s?\d+(?:,\d+)*(?:\.\d+)?)'
    text = re.sub(currency_pattern, "[AMOUNT]", text)

    # Apply substitutions
    text = re.sub(pan_pattern, "[PAN_ID]", text)
    text = re.sub(upi_pattern, "[UPI_ID]", text)
    text = re.sub(acct_pattern, "[ACCOUNT_NUM]", text)
    
    # Add one for 'Amount' here using the currency logic from the last step
    return text

def process_and_save():
    # TODO: 
    # 1. Connect to data/email_logs.db
    # 2. Load rows where 'label' is not null into a DataFrame
    # 3. Apply mask_financial_pii to the 'body' column
    # 4. Save to 'data/processed/cleaned_emails.csv'

    conn = sqlite3.connect('data/email_logs.db')
    df = pd.read_sql_query("SELECT * FROM email_logs WHERE human_label IS NOT NULL",con=conn)

    df['body']=df['body'].apply(mask_financial_pii)

    df.to_csv('data/processed/cleaned_emails.csv',index=False)\
    

    conn.close()
    print(f"Success! Cleaned {len(df)} emails and saved to CSV.")
    pass

if __name__ == "__main__":
    # Test your function here with a string containing a fake PAN or UPI
    test_str = "Your Folio 1234567890 for PAN ABCDE1234F has a balance of Rs. 5000"
    print("Testing Masker:")
    print(mask_financial_pii(test_str))
    print("-" * 30)
    
    # 2. ACTUALLY execute the database extraction and saving!
    print("Starting database extraction...")
    process_and_save()