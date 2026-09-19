import pandas as pd

def extract_email_parts(raw_email_string):
    from src.mime_parser import parse_raw_email, MessageParseError
    from src.logging_utils import log_event
    try:
        parsed = parse_raw_email(raw_email_string)
        return pd.Series([parsed['subject'], parsed['body']])
    except MessageParseError:
        log_event('training_message_parse_failed')
        return pd.Series(['', ''])


def align_dataset(ham_category=None):
    if ham_category not in ('IMPORTANT','UPDATES'):
        raise ValueError('Spam/ham cannot determine urgency. Explicitly choose --ham-category IMPORTANT or UPDATES for this proxy dataset')
    print("1. Loading raw Kaggle dataset...")
    # Make sure your file path matches where you put the CSV!
    df = pd.read_csv('data/raw/Spamdataset.csv')
    
    print(f"Loaded {len(df)} raw emails. Parsing subjects and bodies (this might take a minute)...")
    # Apply our parsing function to split the 'emails' column into two new columns
    df[['subject', 'body']] = df['emails'].apply(extract_email_parts)
    
    if not df['label'].isin([0,1]).all():
        raise ValueError('Expected source labels 0=ham and 1=spam')
    
    print("2. Mapping labels to MailMind Schema...")
    # The caller must verify this dataset uses 1=spam and 0=ham.
    # Ham's chosen category is a proxy, not evidence of urgency.
    mapping = {1: 'SPAM', 0: ham_category}
    df['human_label'] = df['label'].map(mapping)
    
    print("3. Saving aligned dataset...")
    # Keep only the columns our pipeline needs
    final_df = df[['subject', 'body', 'human_label']]
    from src.training_data import prepare_training_frame
    prepare_training_frame(final_df)  # Reject bad rows; never silently discard them.
    
    # Save it to the processed folder
    from pathlib import Path
    Path('data/processed').mkdir(parents=True, exist_ok=True)
    final_df.to_csv('data/processed/kaggle_aligned.csv', index=False)
    print(f"Success! Aligned {len(final_df)} emails and saved to kaggle_aligned.csv")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Prepare a spam/ham proxy, not a validated urgency dataset')
    parser.add_argument('--ham-category', required=True, choices=['IMPORTANT','UPDATES'])
    align_dataset(parser.parse_args().ham_category)
