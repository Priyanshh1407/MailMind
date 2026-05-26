import pandas as pd
import email
from email.policy import default

def extract_email_parts(raw_email_string):
    # This function converts the raw text into a parsed Email object
    try:
        msg = email.message_from_string(raw_email_string, policy=default)
        
        # 1. Safely grab the subject
        subject = msg.get('Subject', '')
        if subject is None:
            subject = ''
            
        # 2. Safely extract the body (ignoring HTML tags or attachments)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == 'text/plain':
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='ignore')
                    break # Stop after finding the primary text
        else:
            body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='ignore')
            
        return pd.Series([subject, body])
    except Exception as e:
        # If an email is too corrupted to parse, return blanks
        return pd.Series(["", ""])

def align_dataset():
    print("1. Loading raw Kaggle dataset...")
    # Make sure your file path matches where you put the CSV!
    df = pd.read_csv('data/raw/Spamdataset.csv')
    
    print(f"Loaded {len(df)} raw emails. Parsing subjects and bodies (this might take a minute)...")
    # Apply our parsing function to split the 'emails' column into two new columns
    df[['subject', 'body']] = df['emails'].apply(extract_email_parts)
    
    # Drop rows that failed to parse or are empty
    df = df.dropna(subset=['body'])
    df = df[df['body'].str.strip() != '']
    
    print("2. Mapping labels to MailMind Schema...")
    # Kaggle usually uses 1 for Spam and 0 for Ham. 
    # We need to flip this for MailMind: Spam = 0 (IGNORE), Ham = 1 (IMPORTANT)
    # Let's map it explicitly to the text labels your train_pipeline.py expects
    mapping = {1: "IGNORE", 0: "IMPORTANT"} 
    df['human_label'] = df['label'].map(mapping)
    
    print("3. Saving aligned dataset...")
    # Keep only the columns our pipeline needs
    final_df = df[['subject', 'body', 'human_label']]
    
    # Save it to the processed folder
    final_df.to_csv('data/processed/kaggle_aligned.csv', index=False)
    print(f"Success! Aligned {len(final_df)} emails and saved to kaggle_aligned.csv")

if __name__ == "__main__":
    align_dataset()