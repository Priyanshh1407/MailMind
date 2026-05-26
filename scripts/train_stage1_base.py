import pandas as pd
import numpy as np
from datasets import Dataset
from transformers import AutoTokenizer, AutoModelForSequenceClassification, TrainingArguments, Trainer
from sklearn.metrics import accuracy_score

def prepare_data():
    print("1. Loading Kaggle dataset...")
    # CHANGED: Now pointing to the Kaggle dataset
    df = pd.read_csv("data/processed/kaggle_aligned.csv")
    
    df['subject'] = df['subject'].fillna("")
    df['body'] = df['body'].fillna("")
    df['combined_text'] = "Subject: " + df['subject'] + " | Body: " + df['body']

    # Keep only labels that are 0 (IGNORE) or 1 (IMPORTANT)
    mapping = {"IGNORE": 0, "IMPORTANT": 1}
    df['label'] = df['human_label'].map(mapping)
    df = df.dropna(subset=['label'])
    df['label'] = df['label'].astype(int)

    df = df[['combined_text', 'label']]
    hf_dataset = Dataset.from_pandas(df)

    print("2. Tokenizing 5,600+ emails (This will take a moment)...")
    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-uncased")

    def tokenize_function(examples):
        return tokenizer(examples["combined_text"], padding="max_length", truncation=True, max_length=512)

    tokenized_dataset = hf_dataset.map(tokenize_function, batched=True)
    return tokenized_dataset

def load_model():
    print("3. Loading raw DistilBERT architecture...")
    model = AutoModelForSequenceClassification.from_pretrained(
        "distilbert-base-uncased", 
        num_labels=2
    )
    return model

def compute_metrics(eval_pred):
    predictions, labels = eval_pred
    predictions = np.argmax(predictions, axis=1)
    return {"accuracy": accuracy_score(labels, predictions)}

if __name__ == "__main__":
    dataset = prepare_data()
    model = load_model()
    
    print("\n4. Configuring Stage 1 Training Loop...")
    training_args = TrainingArguments(
        output_dir="models/MailMind-Base",     # CHANGED: Save location
        num_train_epochs=1,                    # CHANGED: 1 Epoch to save time
        per_device_train_batch_size=8,
        save_strategy="no",
        logging_steps=50,                      # Print updates less frequently
    )
    
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        compute_metrics=compute_metrics,
    )
    
    print("\n🚀 STARTING STAGE 1 TRAINING (This may take 10-30 minutes) 🚀")
    trainer.train()
    
    print("\n💾 SAVING MAILMIND-BASE 💾")
    trainer.save_model("models/MailMind-Base")
    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-uncased")
    tokenizer.save_pretrained("models/MailMind-Base")
    print("Stage 1 Complete! The Generalist model is saved.")