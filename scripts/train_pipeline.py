import pandas as pd
from datasets import Dataset
from transformers import AutoTokenizer

def prepare_data():
    print("1. Loading cleaned data...")
    df = pd.read_csv("data/processed/cleaned_emails.csv")
    
    # Check if 'body' has any empty rows and drop them (data cleaning!)
    df = df.dropna(subset=['body', 'human_label'])

    print("2. Encoding labels...")
    # TODO: Create a dictionary mapping your exact text labels to 0 and 1
    # For example: If your labels are "IGNORE" and "IMPORTANT"
    label_mapping = {
        "IGNORE": 0,
        "IMPORTANT": 1
    }
    
    # Create a new column called 'label' (Hugging Face expects this exact name)
    df['label'] = df['human_label'].map(label_mapping)
    df['label'] = df['label'].astype(int)

    print("3. Combining Subject and Body...")
    # Fill any blank subjects or bodies with empty strings to prevent errors
    df['subject'] = df['subject'].fillna("")
    df['body'] = df['body'].fillna("")
    
    # Create the Super String
    df['combined_text'] = "Subject: " + df['subject'] + " | Body: " + df['body']

    # Keep the new combined text and the label
    df = df[['combined_text', 'label']]
    hf_dataset = Dataset.from_pandas(df)

    print("4. Tokenizing...")
    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-uncased")

    def tokenize_function(examples):
        # We truncate to 512 tokens (DistilBERT's max brain size)
        # We pad to max_length so all arrays are exactly the same size
        return tokenizer(examples["combined_text"], padding="max_length", truncation=True, max_length=512)

    # Apply the tokenizer to all rows simultaneously (batched=True makes it lightning fast)
    tokenized_dataset = hf_dataset.map(tokenize_function, batched=True)
    
    print("\n--- DATASET READY ---")
    print(tokenized_dataset)
    return tokenized_dataset

if __name__ == "__main__":
    dataset = prepare_data()