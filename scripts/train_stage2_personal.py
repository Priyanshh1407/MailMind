from src.email_text import MODEL_MAX_TOKENS
from src.training_data import prepare_training_frame
from src.priority_training import load_training_frame
from src.prediction import LABEL2ID, ID2LABEL, checkpoint_labels
from uuid import uuid4
import pandas as pd
import numpy as np
from datasets import Dataset
from transformers import AutoConfig, AutoTokenizer, AutoModelForSequenceClassification, TrainingArguments, Trainer
from sklearn.metrics import accuracy_score

def prepare_personal_data():
    print("1. Loading verified training-only personalization groups...")
    df = load_training_frame(stage="personal")
    df = prepare_training_frame(df)
    hf_dataset = Dataset.from_pandas(df)

    print("2. Tokenizing personalized dataset...")
    tokenizer = AutoTokenizer.from_pretrained("models/MailMind-Base", local_files_only=True)

    def tokenize_function(examples):
        return tokenizer(examples["combined_text"], padding="max_length", truncation=True, max_length=MODEL_MAX_TOKENS)

    return hf_dataset.map(tokenize_function, batched=True)

def load_local_base_model():
    print("3. Loading local MailMind-Base checkpoint...")
    checkpoint_labels(AutoConfig.from_pretrained('models/MailMind-Base', local_files_only=True))
    model = AutoModelForSequenceClassification.from_pretrained(
        "models/MailMind-Base",
        num_labels=3, id2label=ID2LABEL, label2id=LABEL2ID, local_files_only=True
    )
    model.config.mailmind_model_version = 'personal-' + uuid4().hex
    return model

def compute_metrics(eval_pred):
    predictions, labels = eval_pred
    predictions = np.argmax(predictions, axis=1)
    return {"accuracy": accuracy_score(labels, predictions)}

if __name__ == "__main__":
    from scripts.train_priority_stage import main
    main("personal")
