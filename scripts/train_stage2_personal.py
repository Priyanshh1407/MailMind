import pandas as pd
import numpy as np
from datasets import Dataset
from transformers import AutoTokenizer, AutoModelForSequenceClassification, TrainingArguments, Trainer
from sklearn.metrics import accuracy_score

def prepare_personal_data():
    print("1. Loading your personalized clean data...")
    # TODO: Load your 'data/processed/cleaned_emails.csv' file using pandas
    df = pd.read_csv("data/processed/cleaned_emails.csv")
    
    # Standard clean up and combine step
    df['subject'] = df['subject'].fillna("")
    df['body'] = df['body'].fillna("")
    df['combined_text'] = "Subject: " + df['subject'] + " | Body: " + df['body']

    # Map IGNORE to 0 and IMPORTANT to 1
    mapping = {"IGNORE": 0, "IMPORTANT": 1}
    df['label'] = df['human_label'].map(mapping)
    df = df.dropna(subset=['label'])
    df['label'] = df['label'].astype(int)

    df = df[['combined_text', 'label']]
    hf_dataset = Dataset.from_pandas(df)

    print("2. Tokenizing personalized dataset...")
    # TODO: Load the tokenizer from your LOCAL model directory "models/MailMind-Base"
    tokenizer = AutoTokenizer.from_pretrained("models/MailMind-Base")

    def tokenize_function(examples):
        return tokenizer(examples["combined_text"], padding="max_length", truncation=True, max_length=512)

    return hf_dataset.map(tokenize_function, batched=True)

def load_local_base_model():
    print("3. Loading local MailMind-Base checkpoint...")
    # TODO: Load the sequence classification model from your LOCAL "models/MailMind-Base" folder
    model = AutoModelForSequenceClassification.from_pretrained(
        "models/MailMind-Base",
        num_labels=2
    )
    return model

# (Keep your standard compute_metrics function here)
def compute_metrics(eval_pred):
    predictions, labels = eval_pred
    predictions = np.argmax(predictions, axis=1)
    return {"accuracy": accuracy_score(labels, predictions)}

if __name__ == "__main__":
    dataset = prepare_personal_data()
    model = load_local_base_model()
    
    print("\n4. Configuring Stage 2 Personalization Loop...")
    training_args = TrainingArguments(
        output_dir="models/MailMind-Final",    # Save the specialized brain here
        num_train_epochs=5,                    # 5 epochs to firmly adapt to your inbox rules
        per_device_train_batch_size=8,
        save_strategy="no",
        logging_steps=10,                      # Log frequently since dataset is small
    )
    
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        compute_metrics=compute_metrics,
    )
    
    print("\n STARTING PERSONALIZATION LOOP ")
    
    trainer.train()
    
    print("\n SAVING FINAL CUSTOM PRODUCTION MODEL ")
    trainer.save_model("models/MailMind-Final")
    tokenizer = AutoTokenizer.from_pretrained("models/MailMind-Base")
    tokenizer.save_pretrained("models/MailMind-Final")
    print("Stage 2 Complete! Your independent local model is finalized.")