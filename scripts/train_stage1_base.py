from src.email_text import MODEL_MAX_TOKENS
from src.training_data import prepare_training_frame
from src.priority_training import load_training_frame
from src.prediction import LABEL2ID, ID2LABEL
from uuid import uuid4
import pandas as pd
import numpy as np
from datasets import Dataset
from transformers import AutoTokenizer, AutoModelForSequenceClassification, TrainingArguments, Trainer
from sklearn.metrics import accuracy_score

def prepare_data():
    print("1. Loading verified priority training groups...")
    df = load_training_frame()
    df = prepare_training_frame(df)
    hf_dataset = Dataset.from_pandas(df)

    print("2. Tokenizing the supplied dataset...")
    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-uncased", token=False)

    def tokenize_function(examples):
        return tokenizer(examples["combined_text"], padding="max_length", truncation=True, max_length=MODEL_MAX_TOKENS)

    tokenized_dataset = hf_dataset.map(tokenize_function, batched=True)
    return tokenized_dataset

def load_model():
    print("3. Loading raw DistilBERT architecture...")
    model = AutoModelForSequenceClassification.from_pretrained(
        "distilbert-base-uncased", 
        num_labels=3, id2label=ID2LABEL, label2id=LABEL2ID
    )
    model.config.mailmind_model_version = 'base-' + uuid4().hex
    return model

def compute_metrics(eval_pred):
    predictions, labels = eval_pred
    predictions = np.argmax(predictions, axis=1)
    return {"accuracy": accuracy_score(labels, predictions)}

if __name__ == "__main__":
    from scripts.train_priority_stage import main
    main("base")
